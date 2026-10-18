"""Compact synthetic history with explicit availability, not a live provider."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import ceil

import numpy as np
import pandas_market_calendars as mcal
from numpy.typing import NDArray

from trading_bot.config import Instrument
from trading_bot.models import PriceBar
from trading_bot.portfolio.models import FXRate, utc
from trading_bot.swing.models import DataRights, MarketEvent, Security

Array = NDArray[np.float64]


@dataclass
class SwingData:
    securities: tuple[Security, ...]
    opens_at: tuple[datetime, ...]
    closes_at: tuple[datetime, ...]
    opening: Array
    closing: Array
    high: Array
    low: Array
    volume: Array
    market: Array
    fx_values: Array
    rights: DataRights = field(default_factory=DataRights)
    events: tuple[MarketEvent, ...] = ()
    latency: timedelta = timedelta(minutes=1)
    hourly_available: bool = True
    missing_hourly: set[tuple[int, int, int]] = field(default_factory=set)

    def __post_init__(self) -> None:
        shape = (len(self.opens_at), len(self.securities))
        if any(
            a.shape != shape for a in (self.opening, self.closing, self.high, self.low, self.volume)
        ):
            raise ValueError("history shape mismatch")
        if len({s.instrument.symbol for s in self.securities}) != len(self.securities):
            raise ValueError("duplicate security")
        if len(self.closes_at) != shape[0] or self.market.shape != (shape[0],):
            raise ValueError("calendar/market shape mismatch")
        if self.fx_values.shape != (shape[0],):
            raise ValueError("FX shape mismatch")
        if self.latency < timedelta(0):
            raise ValueError("negative publication latency")
        if list(self.opens_at) != sorted(set(self.opens_at)):
            raise ValueError("unordered/duplicate sessions")
        for opening, closing in zip(self.opens_at, self.closes_at, strict=True):
            if utc(opening) != opening or opening >= closing:
                raise ValueError("invalid session times")

    def daily_index(self, at: datetime) -> int:
        at = utc(at)
        return next(
            (
                i
                for i in range(len(self.closes_at) - 1, -1, -1)
                if self.closes_at[i] + self.latency <= at
            ),
            -1,
        )

    def fx(self, at: datetime) -> FXRate:
        if self.rights.kind != "synthetic":
            raise ValueError("synthetic FX cannot be used for licensed history")
        at = utc(at)
        i = max((i for i, opening in enumerate(self.opens_at) if opening <= at), default=-1)
        if i < 0 or not np.isfinite(self.fx_values[i]) or self.fx_values[i] <= 0:
            raise ValueError("missing synthetic FX")
        # Synthetic generator declares this rate available at the session open.
        # Intermediate quotes are explicit synthetic observations, never real quotes.
        return FXRate(
            usd_jpy=Decimal(str(self.fx_values[i])), timestamp=at, source="fixed-simulation"
        )

    def market_return_at(self, day: int, at: datetime) -> float:
        if self.rights.kind != "synthetic":
            raise ValueError("synthetic market proxy only")
        if self.closes_at[day] + self.latency <= at:
            return float(self.market[day] / self.market[day - 1] - 1)
        hours = int((at - self.latency - self.opens_at[day]).total_seconds() // 3600)
        if hours <= 0:
            return 0
        duration = (self.closes_at[day] - self.opens_at[day]).total_seconds() / 3600
        value = float(
            self.opening[day].mean()
            + (self.closing[day].mean() - self.opening[day].mean()) * min(hours / duration, 1)
        )
        if day % 17 == 0 and hours == 2:
            value *= 0.91
        return value / float(self.market[day - 1]) - 1

    def hourly_bar(self, day: int, column: int, hour: int) -> PriceBar:
        if not self.hourly_available or not self.rights.intraday:
            raise ValueError("intraday data unavailable or unlicensed")
        opening, closing = self.opens_at[day], self.closes_at[day]
        start = opening + timedelta(hours=hour)
        if start >= closing:
            raise ValueError("outside session")
        fraction = min((closing - opening).total_seconds() / 3600, hour + 1)
        length = (closing - opening).total_seconds() / 3600
        a, b = self.opening[day, column], self.closing[day, column]
        before = a + (b - a) * hour / length
        after = a + (b - a) * fraction / length
        # Predetermined intraday crash/recovery, absent from close-to-close-only monitoring.
        if day % 17 == 0:
            before *= 0.91 if hour == 2 else 1
            after *= 0.91 if hour == 1 else 1
        return PriceBar(
            symbol=self.securities[column].instrument.symbol,
            market="US",
            currency="USD",
            timestamp=start,
            open=float(before),
            close=float(after),
            high=float(max(before, after) * 1.002),
            low=float(min(before, after) * 0.998),
            volume=float(self.volume[day, column] * (fraction - hour) / length),
            interval="1h",
            source="synthetic",
            fetched_at=start + timedelta(hours=1) + self.latency,
        )

    def bars(self, symbols: set[str], at: datetime) -> dict[str, list[PriceBar]]:
        if self.rights.kind != "synthetic":
            raise ValueError("synthetic interpolation cannot be applied to licensed prices")
        result: dict[str, list[PriceBar]] = {}
        for column, security in enumerate(self.securities):
            symbol = security.instrument.symbol
            if symbol not in symbols:
                continue
            rows = []
            for day in range(max(0, self.daily_index(at) - 2), len(self.opens_at)):
                opening = self.opens_at[day]
                if opening > at:
                    break
                if self.closes_at[day] + self.latency <= at:
                    rows.append(
                        PriceBar(
                            symbol=symbol,
                            market="US",
                            currency="USD",
                            timestamp=opening,
                            open=float(self.opening[day, column]),
                            close=float(self.closing[day, column]),
                            high=float(self.high[day, column]),
                            low=float(self.low[day, column]),
                            volume=float(self.volume[day, column]),
                            interval="1d",
                            source="synthetic",
                            fetched_at=self.closes_at[day] + self.latency,
                        )
                    )
                if self.hourly_available and self.rights.intraday:
                    for hour in range(ceil((self.closes_at[day] - opening).total_seconds() / 3600)):
                        if (day, column, hour) in self.missing_hourly:
                            continue
                        if opening + timedelta(hours=hour + 1) + self.latency <= at:
                            rows.append(self.hourly_bar(day, column, hour))
            result[symbol] = rows
        return result


def synthetic(size: int = 20, days: int = 245, seed: int = 551) -> SwingData:
    if not 20 <= size <= 10000 or days < 202:
        raise ValueError("need >=20 symbols and >=202 sessions")
    schedule = mcal.get_calendar("NYSE").schedule("2024-01-02", "2027-12-31").iloc[:days]
    if len(schedule) != days:
        raise ValueError("synthetic calendar exhausted")
    opens = tuple(t.to_pydatetime().astimezone(UTC) for t in schedule.market_open)
    closes = tuple(t.to_pydatetime().astimezone(UTC) for t in schedule.market_close)
    rng = np.random.default_rng(seed)
    moves = rng.normal(0.0008, 0.009, (days, size))
    moves += np.linspace(-0.0002, 0.001, size)
    for crash in (211, 229):
        if crash < days:
            moves[crash] -= 0.085
    closing = 40 * np.exp(np.cumsum(moves, axis=0))
    opening = np.vstack((np.full(size, 40.0), closing[:-1])) * np.exp(
        rng.normal(0, 0.002, (days, size))
    )
    high, low = np.maximum(opening, closing) * 1.002, np.minimum(opening, closing) * 0.998
    for day in range(days):
        if day % 17 == 0:
            length = (closes[day] - opens[day]).total_seconds() / 3600
            valley = (opening[day] + (closing[day] - opening[day]) * 2 / length) * 0.91
            low[day] = np.minimum(low[day], valley * 0.998)
    volume = rng.uniform(500000, 2000000, (days, size))
    securities = tuple(
        Security(
            instrument=Instrument(
                symbol=f"S{i:04d}",
                market="US",
                currency="USD",
                display_name=f"Synthetic common {i}",
            ),
            sector=f"sector-{i % 11}",
            listed=date(2020, 1, 1),
            metadata_at=opens[0] - timedelta(days=1),
        )
        for i in range(size)
    )
    return SwingData(
        securities,
        opens,
        closes,
        opening,
        closing,
        high,
        low,
        volume,
        np.asarray(closing.mean(axis=1), dtype=np.float64),
        np.asarray(145 + np.arange(days) * 0.015, dtype=np.float64),
    )


def alpaca_request_plan(symbols: list[str], sessions: int, batch: int = 100) -> dict[str, int]:
    """Offline lower-bound request estimate. No HTTP, credentials or permission grant."""
    if not symbols or len(set(symbols)) != len(symbols) or sessions < 1 or not 1 <= batch <= 100:
        raise ValueError("invalid batch plan")
    batches = [symbols[i : i + batch] for i in range(0, len(symbols), batch)]
    return {
        "symbol_batches": len(batches),
        "bars": len(symbols) * sessions,
        "minimum_pages": sum(ceil(len(b) * sessions / 10000) for b in batches),
    }
