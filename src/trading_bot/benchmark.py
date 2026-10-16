"""Actual fund NAV only. Manual import until automated access/publication is authorized."""

import csv
import hashlib
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
from pydantic import Field, field_validator, model_validator

from trading_bot.persistence.sqlite_repository import PortfolioRepository
from trading_bot.portfolio.models import Account, Contract, utc

FUND_ID = "253425"
JST = ZoneInfo("Asia/Tokyo")


class NAV(Contract):
    fund_id: str = FUND_ID
    nav_date: date
    nav_jpy: Decimal = Field(gt=0)
    published_at: datetime
    fetched_at: datetime
    source_url: str
    distribution_jpy: Decimal = Field(default=Decimal(0), ge=0)
    derivative_publication_allowed: bool = False
    _times = field_validator("published_at", "fetched_at")(utc)

    @model_validator(mode="after")
    def valid(self) -> "NAV":
        host = urlparse(self.source_url)
        if (
            self.fund_id != FUND_ID
            or host.scheme != "https"
            or host.hostname
            not in {
                "www.am.mufg.jp",
                "emaxis.am.mufg.jp",
            }
            or host.username
            or host.query
            or host.fragment
        ):
            raise ValueError("official All Country fund source required")
        if self.published_at.astimezone(JST).date() < self.nav_date:
            raise ValueError("NAV cannot be published before its valuation date")
        if self.published_at > self.fetched_at:
            raise ValueError("NAV publication is future to observation")
        if not mcal.get_calendar("JPX").valid_days(self.nav_date, self.nav_date).size:
            raise ValueError("NAV date must be a Japanese business day")
        return self

    def key(self) -> str:
        return str(self.nav_date)


class BenchmarkProvider(Protocol):
    def observations(self) -> list[NAV]: ...


class ManualCSVProvider:
    def __init__(self, path: Path, observed_at: datetime) -> None:
        self.path, self.observed_at = path, utc(observed_at)

    def observations(self) -> list[NAV]:
        result = []
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("derivative_publication_allowed") not in ("true", "false"):
                    raise ValueError("explicit derivative_publication_allowed true/false required")
                result.append(
                    NAV.model_validate(
                        {
                            **row,
                            "fetched_at": self.observed_at,
                            "derivative_publication_allowed": row["derivative_publication_allowed"]
                            == "true",
                        }
                    )
                )
        if not result or len({r.nav_date for r in result}) != len(result):
            raise ValueError("nonempty CSV with unique NAV dates required")
        return result


class BenchmarkInvestment(Contract):
    account_started_at: datetime
    purchase_date: date
    invested_jpy: Decimal
    units: int = Field(gt=0)
    residual_jpy: Decimal = Field(ge=0)
    nav: NAV
    initialized_at: datetime


def import_nav(repository: PortfolioRepository, provider: BenchmarkProvider) -> int:
    values = provider.observations()  # Validate the whole file before changing anything.
    with repository.transaction() as tx:
        for nav in values:
            old = tx.get("benchmark_prices", nav.key())
            if old:
                previous = NAV.model_validate_json(old)
                # Re-import does not change the original observation/publication metadata.
                if previous.model_dump(exclude={"fetched_at"}) != nav.model_dump(
                    exclude={"fetched_at"}
                ):
                    raise ValueError("NAV revision requires a separately audited correction")
                continue
            tx.put("benchmark_prices", nav.key(), nav)
    return len(values)


def initialize_benchmark(
    repository: PortfolioRepository,
    purchase_date: date,
    at: datetime,
) -> BenchmarkInvestment:
    at = utc(at)
    with repository.transaction() as tx:
        account = tx.account()
        previous = tx.get("benchmark_investments", "all-country")
        if previous:
            investment = BenchmarkInvestment.model_validate_json(previous)
            if investment.purchase_date != purchase_date:
                raise ValueError("benchmark is already initialized for a different purchase date")
            return investment
        # Explicit applicable NAV date, at least the following JP business day.
        # No claim to reproduce a particular distributor's order cutoff or foreign holidays.
        earliest = mcal.get_calendar("JPX").valid_days(
            account.created_at.astimezone(JST).date() + timedelta(days=1), purchase_date
        )
        if not earliest.size or purchase_date < earliest[0].date():
            raise ValueError("purchase date must follow account start on a JP business day")
        raw = tx.get("benchmark_prices", str(purchase_date))
        if raw is None:
            raise ValueError("exact applicable purchase NAV is missing; no previous-price purchase")
        nav = NAV.model_validate_json(raw)
        if nav.published_at > at or nav.fetched_at > at:
            raise ValueError("purchase NAV was not available at initialization time")
        if nav.distribution_jpy:
            raise ValueError("distribution requires an explicit distribution ledger")
        units = int(
            (account.initial_jpy * 10000 / nav.nav_jpy).to_integral_value(rounding=ROUND_DOWN)
        )
        cost = Decimal(units) * nav.nav_jpy / 10000
        investment = BenchmarkInvestment(
            account_started_at=account.created_at,
            purchase_date=purchase_date,
            invested_jpy=cost,
            units=units,
            residual_jpy=account.initial_jpy - cost,
            nav=nav,
            initialized_at=at,
        )
        tx.put("benchmark_investments", "all-country", investment)
        return investment


def benchmark_value(
    investment: BenchmarkInvestment | None,
    values: list[NAV],
    account: Account,
    at: datetime,
) -> tuple[Decimal | None, NAV | None, str | None]:
    at = utc(at)
    if investment is None:
        return None, None, "benchmark_not_initialized"
    if investment.account_started_at != account.created_at:
        raise ValueError("benchmark and account start must match")
    if at < account.created_at:
        raise ValueError("evaluation predates operation")
    if at.astimezone(JST).date() < investment.purchase_date:
        return account.initial_jpy, None, "benchmark_awaiting_applicable_nav_date"
    available = [
        n
        for n in values
        if n.nav_date == at.astimezone(JST).date() and n.published_at <= at and n.fetched_at <= at
    ]
    if len(available) != 1:
        return None, None, "benchmark_same_date_nav_unavailable"
    nav = available[0]
    if any(
        n.distribution_jpy for n in values if investment.purchase_date <= n.nav_date <= nav.nav_date
    ):
        return None, nav, "benchmark_distribution_requires_manual_accounting"
    return Decimal(investment.units) * nav.nav_jpy / 10000 + investment.residual_jpy, nav, None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
