"""Actual fund NAV only. Manual import until automated access/publication is authorized."""

import csv
import hashlib
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal
from pydantic import Field, field_validator, model_validator

from trading_bot.persistence.sqlite_repository import PortfolioRepository
from trading_bot.portfolio.models import Account, Contract, utc

FUND_ID = "253425"
JST = ZoneInfo("Asia/Tokyo")
JPX_CALENDAR = mcal.get_calendar("JPX")
# A fund's NAV calendar is not an exchange trading calendar. On 2020-10-01
# JPX's equity system outage stopped trading, but MUFG published a fund NAV.
# Retain domestic holidays (including 2019 enthronement), not that trading outage.
FUND_BUSINESS_DAY = pd.offsets.CustomBusinessDay(
    holidays=[d for d in JPX_CALENDAR.holidays().holidays if str(d) != "2020-10-01"]
)


def fund_business_days(start: date, end: date) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq=FUND_BUSINESS_DAY)


class NAV(Contract):
    fund_id: str = FUND_ID
    nav_date: date
    nav_jpy: Decimal = Field(gt=0)
    published_at: datetime
    fetched_at: datetime
    source_url: str
    distribution_jpy: Decimal = Field(default=Decimal(0), ge=0)
    derivative_publication_allowed: bool = False
    publication_time_basis: Literal["reported", "observed_by"] = "reported"
    source_kind: Literal["manual_declared", "official_csv"] = "manual_declared"
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    units_per_nav: Literal[10000] = 10000
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
            or host.port not in (None, 443)
            or host.query
            or host.fragment
        ):
            raise ValueError("official All Country fund source required")
        if self.published_at.astimezone(JST).date() < self.nav_date:
            raise ValueError("NAV cannot be published before its valuation date")
        if self.nav_date < date(2018, 10, 31):
            raise ValueError("NAV date predates fund inception")
        if self.published_at > self.fetched_at:
            raise ValueError("NAV publication is future to observation")
        if self.publication_time_basis == "observed_by" and self.published_at != self.fetched_at:
            raise ValueError("unknown publication time must use observation as availability bound")
        if self.source_kind == "official_csv" and (
            self.source_sha256 is None
            or self.source_url != "https://www.am.mufg.jp/fund_file/setteirai/253425.csv"
        ):
            raise ValueError("official CSV provenance requires its exact source and content digest")
        if not fund_business_days(self.nav_date, self.nav_date).size:
            raise ValueError("NAV date must be a Japanese business day")
        return self

    def key(self) -> str:
        return str(self.nav_date)


class BenchmarkProvider(Protocol):
    def observations(self) -> list[NAV]: ...


class ApplicationCalendar(Contract):
    """Owner-reviewed fund/distributor calendar; never just equity sessions."""

    valid_from: date
    valid_to: date
    closed_dates: list[date]
    shortened_dates: list[date] = Field(default_factory=list)  # Official starred dates: 11:30.
    cutoff_jst: time = time(15, 30)
    source_url: str
    observed_at: datetime
    _observed = field_validator("observed_at")(utc)

    @model_validator(mode="after")
    def valid(self) -> "ApplicationCalendar":
        source = urlparse(self.source_url)
        if (
            source.scheme != "https"
            or source.hostname not in {"www.am.mufg.jp", "emaxis.am.mufg.jp"}
            or source.username
            or source.port not in (None, 443)
        ):
            raise ValueError("official fund calendar source required")
        if self.valid_from > self.valid_to or self.cutoff_jst.tzinfo is not None:
            raise ValueError("invalid application calendar range/cutoff")
        if not time(0) < self.cutoff_jst <= time(15, 30):
            raise ValueError("distributor cutoff must not exceed fund cutoff")
        dates = self.closed_dates + self.shortened_dates
        if len(set(dates)) != len(dates) or any(
            not self.valid_from <= d <= self.valid_to for d in dates
        ):
            raise ValueError("invalid or duplicate application closure dates")
        return self


class ManualCSVProvider:
    def __init__(self, path: Path, observed_at: datetime) -> None:
        self.path, self.observed_at = path, utc(observed_at)

    def observations(self) -> list[NAV]:
        result = []
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("derivative_publication_allowed") not in ("true", "false"):
                    raise ValueError("explicit derivative_publication_allowed true/false required")
                if "units_per_nav" in row:
                    if row["units_per_nav"] != "10000":
                        raise ValueError("NAV must be quoted per 10000 units")
                    row.pop("units_per_nav")  # Exact textual literal checked; use typed default.
                if row.get("source_sha256") == "":
                    row.pop("source_sha256")  # An empty optional CSV cell represents no digest.
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
    accepted_on: date | None = None
    application_calendar: ApplicationCalendar | None = None


def import_nav(repository: PortfolioRepository, provider: BenchmarkProvider) -> int:
    values = provider.observations()  # Validate the whole file before changing anything.
    if not values or len({n.key() for n in values}) != len(values):
        raise ValueError("nonempty observations with unique NAV dates required")
    with repository.transaction() as tx:
        for nav in values:
            old = tx.get("benchmark_prices", nav.key())
            if old:
                previous = NAV.model_validate_json(old)
                # Re-import does not change the original observation/publication metadata.
                excluded = {"fetched_at", "source_sha256"}
                if previous.publication_time_basis == nav.publication_time_basis == "observed_by":
                    excluded.add("published_at")
                if previous.model_dump(exclude=excluded) != nav.model_dump(exclude=excluded):
                    raise ValueError("NAV revision requires a separately audited correction")
                continue
            tx.put("benchmark_prices", nav.key(), nav)
    return len(values)


def initialize_benchmark(
    repository: PortfolioRepository,
    purchase_date: date,
    at: datetime,
    *,
    accepted_on: date | None = None,
    application_calendar: ApplicationCalendar | None = None,
) -> BenchmarkInvestment:
    at = utc(at)
    with repository.transaction() as tx:
        account = tx.account()
        previous = tx.get("benchmark_investments", "all-country")
        if previous:
            investment = BenchmarkInvestment.model_validate_json(previous)
            if (
                investment.purchase_date != purchase_date
                or (accepted_on is not None and investment.accepted_on != accepted_on)
                or (
                    application_calendar is not None
                    and investment.application_calendar != application_calendar
                )
            ):
                raise ValueError("benchmark is already initialized for a different purchase date")
            return investment
        validate_application(account.created_at, purchase_date, accepted_on, application_calendar)
        raw = tx.get("benchmark_prices", str(purchase_date))
        if raw is None:
            raise ValueError("exact applicable purchase NAV is missing; no previous-price purchase")
        nav = NAV.model_validate_json(raw)
        if nav.source_kind == "official_csv" and (
            accepted_on is None or application_calendar is None
        ):
            raise ValueError(
                "official NAV requires an explicitly reviewed application calendar/date"
            )
        if application_calendar is not None and application_calendar.observed_at > at:
            raise ValueError("application calendar was not available at initialization")
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
            accepted_on=accepted_on,
            application_calendar=application_calendar,
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
    if at < max(investment.initialized_at, investment.nav.published_at, investment.nav.fetched_at):
        return None, None, "benchmark_investment_not_available_at_evaluation"
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


def validate_application(
    started_at: datetime,
    purchase_date: date,
    accepted_on: date | None,
    application_calendar: ApplicationCalendar | None = None,
) -> None:
    """Domestic lower bound; caller must review foreign/bank and distributor closure dates.

    Acceptance is explicit for actual CSV data. JPX alone is NOT a fund application calendar.
    No automatic foreign holiday inference or choice of an advantageous historical NAV.
    """
    local = utc(started_at).astimezone(JST)
    if application_calendar is not None:
        review = application_calendar
        if not review.valid_from <= local.date() <= purchase_date <= review.valid_to:
            raise ValueError("application calendar does not cover the comparison period")
        candidates = fund_business_days(local.date(), purchase_date)
        eligible = [
            d.date()
            for d in candidates
            if d.date() not in review.closed_dates
            and (
                d.date() > local.date()
                or local.time()
                < min(
                    review.cutoff_jst,
                    time(11, 30) if d.date() in review.shortened_dates else time(15, 30),
                )
            )
        ]
        if not eligible or accepted_on is None or accepted_on != eligible[0]:
            raise ValueError(
                "application date must be first eligible reviewed fund/distributor day"
            )
        following = fund_business_days(
            accepted_on + timedelta(days=1), accepted_on + timedelta(days=31)
        )
        if not following.size or purchase_date != following[0].date():
            raise ValueError("purchase date must follow reviewed application date")
        return
    first = local.date() + timedelta(days=int(local.time() >= time(15, 30)))
    acceptance = fund_business_days(first, first + timedelta(days=31))
    if not acceptance.size:
        raise ValueError("application calendar unavailable")
    accepted = accepted_on or acceptance[0].date()
    if accepted < acceptance[0].date() or not fund_business_days(accepted, accepted).size:
        raise ValueError("application date precedes account start/cutoff or is a domestic holiday")
    following = fund_business_days(accepted + timedelta(days=1), accepted + timedelta(days=31))
    if not following.size or purchase_date != following[0].date():
        raise ValueError(
            "purchase date must follow reviewed application date by one JP business day"
        )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
