"""Explicit bar-start semantics and exchange calendars; no fabricated weekend fills."""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from trading_bot.config import Instrument, Market
from trading_bot.models import PriceBar
from trading_bot.portfolio.models import Mark, Order, TradingRules, utc


@lru_cache(maxsize=2048)
def session(market: Market, day: str) -> dict[str, datetime]:
    schedule = mcal.get_calendar("JPX" if market == "JP" else "NYSE").schedule(day, day)
    if schedule.empty:
        return {}
    return {
        str(key): value.to_pydatetime().astimezone(UTC)
        for key, value in schedule.iloc[0].items()
        if hasattr(value, "to_pydatetime")
    }


def window(bar: PriceBar) -> tuple[datetime, datetime] | None:
    zone = ZoneInfo("Asia/Tokyo" if bar.market == "JP" else "America/New_York")
    times = session(bar.market, bar.timestamp.astimezone(zone).date().isoformat())
    if not times:
        return None
    opening, closing = times["market_open"], times["market_close"]
    if bar.interval == "1d":
        return opening, closing
    start = bar.timestamp
    if not opening <= start < closing:
        return None
    if "break_start" in times and times["break_start"] <= start < times["break_end"]:
        return None
    end = min(start + timedelta(hours=1), closing)
    if "break_start" in times and start < times["break_start"]:
        end = min(end, times["break_start"])
    # Wait the full nominal hour too, matching Phase 1's conservative confirmation.
    return start, max(end, start + timedelta(hours=1))


def eligible_bar(
    order: Order,
    instrument: Instrument,
    bars: list[PriceBar],
    as_of: datetime,
    mode: Literal["historical", "paper"],
    rules: TradingRules,
) -> tuple[PriceBar, datetime, datetime] | None:
    as_of = utc(as_of)
    if as_of < order.requested_at:
        raise ValueError("execution time precedes order")
    candidates = []
    seen = set()
    for original in bars:
        bar = PriceBar.model_validate(original.model_dump())
        if (bar.symbol, bar.market, bar.currency) != (
            instrument.symbol,
            instrument.market,
            instrument.currency,
        ):
            raise ValueError("execution bar metadata mismatch")
        key = (bar.interval, bar.timestamp)
        if key in seen:
            raise ValueError("duplicate execution bar")
        seen.add(key)
        span = window(bar)
        if span is None or bar.volume == 0:
            continue
        opened, confirmed = span
        if opened <= order.requested_at or confirmed > as_of or bar.fetched_at < confirmed:
            continue
        if mode == "paper" and (
            not order.requested_at < bar.fetched_at <= as_of
            or bar.fetched_at < confirmed
            or (as_of - confirmed).total_seconds() > rules.max_price_age_seconds
        ):
            continue
        candidates.append((bar, opened, confirmed))
    return min(candidates, key=lambda item: (item[1], item[2])) if candidates else None


def known_marks(
    instruments: list[Instrument],
    data: dict[str, list[PriceBar]],
    as_of: datetime,
    mode: Literal["historical", "paper"],
) -> dict[str, Mark]:
    """Only closes of completed bars observable at as_of; future closes never escape."""
    from decimal import Decimal

    marks: dict[str, Mark] = {}
    for instrument in instruments:
        for original in data.get(instrument.symbol, []):
            bar = PriceBar.model_validate(original.model_dump())
            if (bar.symbol, bar.market, bar.currency) != (
                instrument.symbol,
                instrument.market,
                instrument.currency,
            ):
                raise ValueError("valuation bar metadata mismatch")
            span = window(bar)
            if span is None or span[1] > as_of or bar.fetched_at < span[1]:
                continue
            available = max(span[1], bar.fetched_at) if mode == "paper" else span[1]
            if available > as_of:
                continue
            current = marks.get(bar.symbol)
            if current is None or current.available_at < available:
                marks[bar.symbol] = Mark(
                    symbol=bar.symbol,
                    currency=bar.currency,
                    price=Decimal(str(bar.close)),
                    available_at=available,
                )
    return marks
