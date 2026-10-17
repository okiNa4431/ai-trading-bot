"""Replaceable FX/actions sources and validated private incremental market inputs."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import yfinance as yf
from pydantic import Field, field_validator, model_validator

from trading_bot.broker.execution import window
from trading_bot.config import Instrument, Interval
from trading_bot.data_access import require_live_market_access
from trading_bot.models import PriceBar
from trading_bot.operations import Operation, fingerprint
from trading_bot.portfolio.models import Contract, FXRate, utc
from trading_bot.provider import MarketDataProvider
from trading_bot.storage import ParquetStore


class FXObservation(Contract):
    rate: FXRate
    observed_at: datetime
    symbol: str = "JPY=X"
    provider: str = "yahoo"
    base_currency: Literal["USD"] = "USD"
    quote_currency: Literal["JPY"] = "JPY"
    _time = field_validator("observed_at")(utc)

    @model_validator(mode="after")
    def consistent(self) -> "FXObservation":
        if self.rate.timestamp > self.observed_at or self.rate.source != "observed":
            raise ValueError("real FX requires a quote observed after its own timestamp")
        return self


class CorporateAction(Contract):
    symbol: str
    effective_date: date
    split: Decimal = Field(ge=0)
    dividend: Decimal = Field(ge=0)
    currency: Literal["JPY", "USD"]
    observed_at: datetime
    _time = field_validator("observed_at")(utc)


class FXProvider(Protocol):
    def fetch_fx(self, at: datetime) -> FXObservation: ...


class CorporateActionProvider(Protocol):
    def fetch_actions(self, instrument: Instrument, at: datetime) -> list[CorporateAction]: ...


class YahooInputs:
    """Normal Yahoo/yfinance requests, bounded by timeout; no synthetic fallback."""

    def fetch_fx(self, at: datetime) -> FXObservation:
        at = utc(at)
        require_live_market_access()
        frame = yf.Ticker("JPY=X").history(
            period="5d",
            interval="5m",
            auto_adjust=False,
            back_adjust=False,
            repair=False,
            actions=False,
            keepna=True,
            timeout=20,
            raise_errors=True,
        )
        if frame.empty or "Close" not in frame or frame.index.tz is None:
            raise ValueError("FX history unavailable")
        rows = [
            (index.to_pydatetime().astimezone(UTC) + timedelta(minutes=5), row)
            for index, row in frame.iterrows()
            if index.to_pydatetime().astimezone(UTC) + timedelta(minutes=5) <= at
        ]
        if not rows:
            raise ValueError("no confirmed FX observation")
        stamp, row = max(rows, key=lambda r: r[0])
        rate = FXRate(usd_jpy=Decimal(str(row["Close"])), timestamp=stamp, source="observed")
        return FXObservation(rate=rate, observed_at=datetime.now(UTC))

    def fetch_actions(self, instrument: Instrument, at: datetime) -> list[CorporateAction]:
        require_live_market_access()
        frame = yf.Ticker(instrument.symbol).history(
            start=utc(at) - timedelta(days=60),
            end=utc(at),
            interval="1d",
            auto_adjust=False,
            back_adjust=False,
            repair=False,
            actions=True,
            keepna=True,
            timeout=20,
            raise_errors=True,
        )
        if frame.empty or not {"Dividends", "Stock Splits"}.issubset(frame.columns):
            raise ValueError("corporate action coverage unavailable")
        if frame.index.tz is None:
            raise ValueError("corporate action timezone missing")
        result = []
        zone = ZoneInfo("Asia/Tokyo" if instrument.market == "JP" else "America/New_York")
        observed = datetime.now(UTC)
        for index, row in frame.iterrows():
            item = CorporateAction(
                symbol=instrument.symbol,
                effective_date=index.to_pydatetime().astimezone(zone).date(),
                split=Decimal(str(row["Stock Splits"])),
                dividend=Decimal(str(row["Dividends"])),
                currency=instrument.currency,
                observed_at=observed,
            )
            if item.split or item.dividend:
                result.append(item)
        return result


def update_inputs(
    operation: Operation,
    store: ParquetStore,
    instruments: list[Instrument],
    provider: MarketDataProvider,
    actions: CorporateActionProvider,
    at: datetime,
    account_started: datetime,
) -> None:
    """Validate every partition before publishing its immutable DB generation.

    Preserve successful symbols when another symbol fails; caller must not trade
    from an incomplete set. Failed action coverage is NOT an empty event list.
    """
    at = utc(at)
    saved = operation.read()["data"]
    failures = []
    for instrument in instruments:
        try:
            prior_actions = saved.get("actions:" + instrument.symbol)
            covered_from = (
                datetime.fromisoformat(prior_actions["observed_at"])
                if prior_actions
                else account_started
            )
            if at - utc(covered_from) > timedelta(days=60):
                raise ValueError("corporate action history gap requires explicit backfill")
            events = actions.fetch_actions(instrument, at)
            # An unresolved split/dividend must not disappear as the source window rolls.
            if prior_actions:
                previous = [CorporateAction.model_validate(r) for r in prior_actions["events"]]
                unique: dict[tuple[str, date, Decimal, Decimal], CorporateAction] = {}
                for event in previous + events:
                    unique.setdefault(
                        (event.symbol, event.effective_date, event.split, event.dividend), event
                    )
                events = list(unique.values())
            for event in events:
                CorporateAction.model_validate(event.model_dump())
                if (event.symbol, event.currency) != (
                    instrument.symbol,
                    instrument.currency,
                ) or event.observed_at > datetime.now(UTC):
                    raise ValueError("corporate action provenance mismatch")
            operation.data(
                "actions:" + instrument.symbol,
                {
                    "events": [e.model_dump(mode="json") for e in events],
                    "observed_at": utc(at).isoformat(),
                },
            )
            zone = ZoneInfo("Asia/Tokyo" if instrument.market == "JP" else "America/New_York")
            if any(e.effective_date >= account_started.astimezone(zone).date() for e in events):
                raise ValueError("corporate action requires reviewed accounting")
            for interval in ("1d", "1h"):
                typed: Interval = interval
                key = f"bars:{instrument.market}:{instrument.symbol}:{interval}:yahoo:raw"
                old = saved.get(key)
                existing: list[PriceBar] = []
                if old:
                    existing = [PriceBar.model_validate(r) for r in old["bars"]]
                    if fingerprint([b.model_dump(mode="json") for b in existing]) != old["sha256"]:
                        raise ValueError("stored data digest mismatch")
                    store.save(instrument, typed, existing)
                lookback = timedelta(days=90 if interval == "1d" else 30)
                start = (
                    max(at - lookback, existing[-5:][0].timestamp) if existing else at - lookback
                )
                if (
                    existing
                    and interval == "1h"
                    and at - existing[-1].timestamp > timedelta(days=29)
                ):
                    raise ValueError("hourly history gap requires explicit backfill")
                incoming = provider.fetch(instrument, start, at, typed)
                if not incoming:
                    raise ValueError("empty incremental price response")
                store.save(instrument, typed, incoming)
                merged = store.read(instrument, typed)
                validated_at = datetime.now(UTC)
                for bar in merged:
                    span = window(bar)
                    if span is None or span[1] > validated_at or bar.fetched_at > validated_at:
                        raise ValueError("unconfirmed or future price")
                if interval == "1d" and any(
                    abs(b.close / a.close - 1) > 0.35
                    for a, b in zip(merged, merged[1:], strict=False)
                ):
                    raise ValueError("raw price discontinuity")
                rows = [b.model_dump(mode="json") for b in merged[-2500:]]
                operation.data(
                    key,
                    {
                        "bars": rows,
                        "sha256": fingerprint(rows),
                        "watermark": merged[-1].timestamp.isoformat(),
                    },
                )
        except Exception:
            failures.append(instrument.symbol)
    if failures:
        raise ValueError("required price/action input unavailable: " + ",".join(failures))
