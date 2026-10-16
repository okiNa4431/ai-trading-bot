"""Owner-downloaded MUFG setting-inception CSV; no network or publication permission implied."""

import csv
import hashlib
import io
import unicodedata
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

from pydantic import Field

from trading_bot.benchmark import (
    JST,
    NAV,
    ApplicationCalendar,
    fund_business_days,
    validate_application,
)
from trading_bot.portfolio.models import Contract, utc

OFFICIAL_CSV_URL = "https://www.am.mufg.jp/fund_file/setteirai/253425.csv"
TITLE = "eMAXIS Slim 全世界株式(オール・カントリー)"
HEADER = [
    "基準日",
    "基準価額(円)",
    "基準価額（分配金再投資）(円)",
    "分配金（税引前）(円)",
    "純資産総額（億円）",
]


class OfficialCSVProvider:
    """Parse the official downloadable file without guessing its exact release time.

    The first row identifies this fund; prices are raw JPY per 10,000 units, after
    trust fees. Blank distribution cells mean no distribution on that date.
    A file has no publication timestamp: observed_at is a conservative availability
    bound, never an invented 18:30 timestamp. Only pass the actual download time.
    """

    def __init__(self, path: Path, observed_at: datetime) -> None:
        self.path, self.observed_at = path, utc(observed_at)

    def observations(self) -> list[NAV]:
        if self.path.stat().st_size > 4_000_000:
            raise ValueError("official CSV exceeds size limit")
        payload = self.path.read_bytes()
        # The official downloadable CSV is CP932, including full-width Latin title.
        rows = [row for row in csv.reader(io.StringIO(payload.decode("cp932"))) if row]
        if len(rows) < 3 or len(rows[0]) != 1:
            raise ValueError("official CSV title/header/data required")
        if unicodedata.normalize("NFKC", rows[0][0]) != TITLE or rows[1] != HEADER:
            raise ValueError("official All Country CSV identity/schema mismatch")
        digest = hashlib.sha256(payload).hexdigest()
        result: list[NAV] = []
        for row in rows[2:]:
            if len(row) != len(HEADER):
                raise ValueError("official CSV row width mismatch")
            nav_date = datetime.strptime(row[0], "%Y/%m/%d").date()
            if row[0] != nav_date.strftime("%Y/%m/%d"):
                raise ValueError("official CSV date format mismatch")
            if result and nav_date <= result[-1].nav_date:
                raise ValueError("official CSV dates must be unique and increasing")
            raw, reinvested, distribution, assets = (
                decimal(row[1], positive=True),
                decimal(row[2], positive=True),
                decimal(row[3] or "0"),
                decimal(row[4]),
            )
            if raw != raw.to_integral_value() or assets < 0 or reinvested <= 0:
                raise ValueError("invalid official NAV precision/assets")
            result.append(
                NAV(
                    nav_date=nav_date,
                    nav_jpy=raw,
                    distribution_jpy=distribution,
                    published_at=self.observed_at,
                    fetched_at=self.observed_at,
                    source_url=OFFICIAL_CSV_URL,
                    source_kind="official_csv",
                    source_sha256=digest,
                    publication_time_basis="observed_by",
                    derivative_publication_allowed=False,
                )
            )
        return result


def decimal(value: str, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid official CSV number") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError("invalid official CSV number")
    return number


class RetrospectiveComparison(Contract):
    mode: str = "retrospective_real_nav_vs_cash_scenario"
    # This is a separate arithmetic verification, never an actual bot performance record.
    account_started_at: datetime
    accepted_on: date
    purchase_date: date
    valuation_date: date
    observed_at: datetime
    initial_jpy: Decimal = Field(gt=0)
    units: int
    residual_jpy: Decimal
    benchmark_jpy: Decimal
    benchmark_return: Decimal
    cash_scenario_jpy: Decimal
    cash_scenario_return: Decimal = Decimal(0)
    excess_cash_return: Decimal
    source_sha256: str
    application_calendar: ApplicationCalendar
    derivative_publication_allowed: bool = False


def compare_historical_nav(
    values: list[NAV],
    *,
    started_at: datetime,
    accepted_on: date,
    purchase_date: date,
    valuation_date: date,
    application_calendar: ApplicationCalendar,
    initial_jpy: Decimal = Decimal("1000000"),
) -> RetrospectiveComparison:
    """Owner-only retrospective arithmetic. Does not backdate live snapshots or trades.

    Dates must be explicitly reviewed against the fund's application calendar.
    Availability today does not prove historical point-in-time availability.
    """
    validate_application(started_at, purchase_date, accepted_on, application_calendar)
    if not initial_jpy.is_finite() or initial_jpy <= 0 or valuation_date < purchase_date:
        raise ValueError("positive capital and non-reversed comparison dates required")
    if len({n.nav_date for n in values}) != len(values):
        raise ValueError("duplicate NAV dates")
    indexed = {n.nav_date: n for n in values}
    if purchase_date not in indexed or valuation_date not in indexed:
        raise ValueError("exact purchase and valuation NAV dates required")
    selected = [n for n in values if purchase_date <= n.nav_date <= valuation_date]
    if {d.date() for d in fund_business_days(purchase_date, valuation_date)} != {
        n.nav_date for n in selected
    }:
        raise ValueError("complete NAV history required; missing dates may hide distributions")
    if any(n.source_kind != "official_csv" or n.distribution_jpy for n in selected):
        raise ValueError("official real NAV without distributions required for this verification")
    hashes = {n.source_sha256 for n in selected}
    if len(hashes) != 1 or None in hashes:
        raise ValueError("one complete official CSV source required")
    purchase, latest = indexed[purchase_date], indexed[valuation_date]
    observed = max(n.fetched_at for n in selected)
    if observed.astimezone(JST).date() < valuation_date:
        raise ValueError("valuation date has not been observed")
    units = int((initial_jpy * 10000 / purchase.nav_jpy).to_integral_value(rounding=ROUND_DOWN))
    if units <= 0:
        raise ValueError("capital is insufficient for one unit")
    residual = initial_jpy - Decimal(units) * purchase.nav_jpy / 10000
    value = Decimal(units) * latest.nav_jpy / 10000 + residual
    ret = value / initial_jpy - 1
    return RetrospectiveComparison(
        account_started_at=utc(started_at),
        accepted_on=accepted_on,
        purchase_date=purchase_date,
        valuation_date=valuation_date,
        observed_at=observed,
        initial_jpy=initial_jpy,
        units=units,
        residual_jpy=residual,
        benchmark_jpy=value,
        benchmark_return=ret,
        cash_scenario_jpy=initial_jpy,
        excess_cash_return=-ret,
        source_sha256=str(purchase.source_sha256),
        application_calendar=application_calendar,
    )
