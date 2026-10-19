"""Reviewed security identities and temporal aliases; never rewrite raw observations."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from trading_bot.alpaca import NY, DailyBatch
from trading_bot.models import PriceBar
from trading_bot.provider import DataError
from trading_bot.research_universe import validate_research_symbols

SOURCE = (
    "https://www.marsh.com/en/corp/about/news/marsh-mclennan-to-change-nyse-symbol-to-mrsh.html"
)


class TickerAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    security_id: str
    ticker: str
    market: Literal["US"] = "US"
    exchange: Literal["NYSE"] = "NYSE"
    currency: Literal["USD"] = "USD"
    provider: Literal["alpaca"] = "alpaca"
    valid_from: date
    valid_to: date | None = None  # Exclusive.
    evidence: str
    identity_confirmed: bool


class CorporateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    security_id: str
    kind: Literal["ticker_change", "split", "dividend", "spinoff"]
    effective_date: date
    evidence: str
    old_ticker: str | None = None
    new_ticker: str | None = None


class SecurityRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    security_ids: tuple[str, ...]
    assignments: tuple[TickerAssignment, ...]
    events: tuple[CorporateEvent, ...]

    @model_validator(mode="after")
    def consistent(self) -> "SecurityRegistry":
        if (
            not self.security_ids
            or any(not sid.strip() for sid in self.security_ids)
            or len(set(self.security_ids)) != len(self.security_ids)
        ):
            raise ValueError("invalid security identities")
        for item in self.assignments:
            validate_research_symbols([item.ticker], 1)
            if (
                item.security_id not in self.security_ids
                or not item.identity_confirmed
                or not item.evidence
                or (item.valid_to and item.valid_from >= item.valid_to)
            ):
                raise ValueError("unconfirmed or invalid ticker assignment")
        for sid in self.security_ids:
            items = sorted(
                (a for a in self.assignments if a.security_id == sid), key=lambda a: a.valid_from
            )
            if not items:
                raise ValueError("missing ticker assignment")
            for left, right in zip(items, items[1:], strict=False):
                if left.valid_to != right.valid_from:
                    raise ValueError("ticker interval overlap or gap")
                if not any(
                    e.kind == "ticker_change"
                    and e.security_id == sid
                    and e.effective_date == right.valid_from
                    and e.old_ticker == left.ticker
                    and e.new_ticker == right.ticker
                    and e.evidence
                    for e in self.events
                ):
                    raise ValueError("unreviewed ticker boundary")
        for index, a in enumerate(self.assignments):
            for b in self.assignments[index + 1 :]:
                if (a.ticker, a.market, a.provider) == (b.ticker, b.market, b.provider) and (
                    a.valid_from < (b.valid_to or date.max)
                    and b.valid_from < (a.valid_to or date.max)
                ):
                    raise ValueError("ambiguous provider ticker")
        for event in self.events:
            if event.security_id not in self.security_ids or not event.evidence:
                raise ValueError("unreviewed corporate event")
            if event.kind == "ticker_change" and not any(
                a.security_id == event.security_id
                and a.ticker == event.old_ticker
                and a.valid_to == event.effective_date
                for a in self.assignments
            ):
                raise ValueError("unmatched ticker change")
            if event.kind == "ticker_change" and not any(
                a.security_id == event.security_id
                and a.ticker == event.new_ticker
                and a.valid_from == event.effective_date
                for a in self.assignments
            ):
                raise ValueError("unmatched new ticker")
        return self


def marsh_registry() -> SecurityRegistry:
    # Old alias coverage reviewed for this research window, NOT an IPO date claim.
    return SecurityRegistry(
        security_ids=("sec-000001",),
        assignments=(
            TickerAssignment(
                security_id="sec-000001",
                ticker="MMC",
                valid_from=date(2025, 12, 1),
                valid_to=date(2026, 1, 14),
                evidence=SOURCE,
                identity_confirmed=True,
            ),
            TickerAssignment(
                security_id="sec-000001",
                ticker="MRSH",
                valid_from=date(2026, 1, 14),
                evidence=SOURCE,
                identity_confirmed=True,
            ),
        ),
        events=(
            CorporateEvent(
                security_id="sec-000001",
                kind="ticker_change",
                effective_date=date(2026, 1, 14),
                evidence=SOURCE,
                old_ticker="MMC",
                new_ticker="MRSH",
            ),
        ),
    )


@dataclass(frozen=True)
class IdentifiedObservation:
    security_id: str
    raw: PriceBar  # Preserve provider, observed ticker, timestamps and raw OHLCV unchanged.


def validate_window(registry: SecurityRegistry, days: set[date]) -> None:
    SecurityRegistry.model_validate(registry.model_dump())
    if not days:
        raise DataError("empty identity window")
    for sid in registry.security_ids:
        for day in days:
            if (
                sum(
                    a.security_id == sid
                    and a.valid_from <= day
                    and (a.valid_to is None or day < a.valid_to)
                    for a in registry.assignments
                )
                != 1
            ):
                raise DataError("uncovered or ambiguous security interval")


def identify_batch(registry: SecurityRegistry, batch: DailyBatch) -> list[IdentifiedObservation]:
    registry = SecurityRegistry.model_validate(registry.model_dump())
    if not batch.complete_response or batch.excluded_unconfirmed or not batch.expected_sessions:
        raise DataError("identity validation requires completed confirmed response")
    if set(batch.bars) != {a.ticker for a in registry.assignments}:
        raise DataError("unexpected identity response scope")
    expected = set(batch.expected_sessions)
    validate_window(registry, expected)
    for event in registry.events:
        if event.kind != "ticker_change" and min(expected) <= event.effective_date <= max(expected):
            raise DataError("unsupported corporate action; stop")
    seen = set()
    result = []
    for ticker, bars in batch.bars.items():
        for bar in bars:
            if ticker != bar.symbol:
                raise DataError("response ticker provenance mismatch")
            day = bar.timestamp.astimezone(NY).date()
            matches = [
                a
                for a in registry.assignments
                if a.ticker == bar.symbol
                and a.provider == bar.source
                and a.market == bar.market
                and a.currency == bar.currency
                and a.valid_from <= day
                and (a.valid_to is None or day < a.valid_to)
            ]
            if len(matches) != 1 or day not in expected:
                raise DataError("unmapped or out-of-period observation")
            key = (matches[0].security_id, day)
            if key in seen:
                raise DataError("duplicate security observation")
            seen.add(key)
            result.append(IdentifiedObservation(matches[0].security_id, bar))
    if seen != {(sid, day) for sid in registry.security_ids for day in expected}:
        raise DataError("incomplete security coverage; stop")
    return sorted(result, key=lambda o: (o.security_id, o.raw.timestamp))
