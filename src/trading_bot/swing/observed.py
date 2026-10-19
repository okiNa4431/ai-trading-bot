"""Causal raw IEX bridge. No synthetic FX, intraday interpolation, or inferred clearance."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas_market_calendars as mcal
from pydantic import Field

from trading_bot.alpaca import NY, DailyBatch, confirmed_at
from trading_bot.alpaca_research import ResearchProvider
from trading_bot.broker.execution import session
from trading_bot.config import Instrument
from trading_bot.models import PriceBar
from trading_bot.portfolio.models import Contract, FXRate, Mark, TradingRules, utc
from trading_bot.portfolio.valuation import validate_fx
from trading_bot.screening.models import ScreeningResult
from trading_bot.swing.data import Array, SwingData
from trading_bot.swing.models import DataRights, MarketEvent, ScreenSummary, Security, SwingSettings
from trading_bot.swing.screening import screen_daily


class ActionCoverage(Contract):
    source: str = Field(default="unverified", min_length=1)
    verified: bool = False
    since: datetime
    through: datetime
    observed_at: datetime
    events: list[MarketEvent]


class ObservedDailyData(SwingData):
    @classmethod
    def collect(
        cls,
        provider: ResearchProvider,
        securities: Sequence[Security],
        start: datetime,
        end: datetime,
        reference: list[PriceBar],
        fx: list[FXRate],
        actions: ActionCoverage,
    ) -> "ObservedDailyData":
        # Provider retains Phase 6's exact configuration, contract and approval restrictions.
        instruments: list[Instrument] = [s.instrument for s in securities]
        return cls(
            securities,
            provider.fetch_many(instruments, start, end).require_complete(),
            reference,
            fx,
            actions,
        )

    def __init__(
        self,
        securities: Sequence[Security],
        prices: DailyBatch,
        reference: list[PriceBar],
        fx: list[FXRate],
        actions: ActionCoverage,
    ) -> None:
        prices.require_complete()
        if len(securities) < 20 or len(securities) > 2000:
            raise ValueError("observed universe requires 20-2000 reviewed securities")
        if set(prices.bars) != {s.instrument.symbol for s in securities}:
            raise ValueError("observed universe coverage mismatch")
        dates = prices.expected_sessions
        if list(dates) != sorted(set(dates)):
            raise ValueError("unordered/duplicate observed sessions")
        rows = dict(prices.bars)
        if not reference or reference[0].symbol in rows:
            raise ValueError("independent market reference required")
        rows[reference[0].symbol] = reference
        matrix: dict[str, list[PriceBar]] = {}
        for symbol, bars in rows.items():
            indexed = {}
            for raw in bars:
                try:
                    b = PriceBar.model_validate(raw.model_dump())
                except ValueError:
                    raise ValueError("invalid observed OHLCV contract") from None
                day = b.timestamp.astimezone(NY).date()
                if b.source != "alpaca" or b.interval != "1d" or b.market != "US":
                    raise ValueError("raw US Alpaca daily observations required")
                if b.symbol != symbol or day in indexed or day not in dates:
                    raise ValueError("invalid or duplicate observed coverage")
                if b.fetched_at < confirmed_at(day):
                    raise ValueError("unconfirmed observed daily bar")
                indexed[day] = b
            if set(indexed) != set(dates):
                raise ValueError("missing observed/reference daily session")
            matrix[symbol] = [indexed[d] for d in dates]
        self.original = matrix
        self.available = tuple(
            max(matrix[s][i].fetched_at for s in matrix) for i in range(len(dates))
        )
        self.fx_observations = sorted(
            (FXRate.model_validate(f.model_dump()) for f in fx), key=lambda f: f.timestamp
        )
        if len({f.timestamp for f in self.fx_observations}) != len(self.fx_observations):
            raise ValueError("duplicate observed FX timestamps")
        if any(f.source != "observed" for f in self.fx_observations):
            raise ValueError("observed FX required; fixed simulation forbidden")
        self.actions = ActionCoverage.model_validate(actions.model_dump())
        times = [session("US", d.isoformat()) for d in dates]
        if any(not t for t in times):
            raise ValueError("non-session observed bar")

        def array(key: str) -> Array:
            return np.array(
                [
                    [getattr(matrix[s.instrument.symbol][i], key) for s in securities]
                    for i in range(len(dates))
                ],
                dtype=float,
            )

        super().__init__(
            securities=tuple(securities),
            opens_at=tuple(t["market_open"] for t in times),
            closes_at=tuple(t["market_close"] for t in times),
            opening=array("open"),
            closing=array("close"),
            high=array("high"),
            low=array("low"),
            volume=array("volume"),
            market=np.array([b.close for b in matrix[reference[0].symbol]], dtype=float),
            fx_values=np.full(len(dates), np.nan),
            rights=DataRights(
                kind="licensed",
                external_ai=False,
                public_performance=False,
                intraday=False,
                point_in_time_universe=False,
                source="Alpaca IEX raw",
            ),
            latency=timedelta(0),
            hourly_available=False,
        )

    def daily_index(self, at: datetime) -> int:
        return max((i for i, known in enumerate(self.available) if known <= utc(at)), default=-1)

    def fx(self, at: datetime) -> FXRate:
        known = [f for f in self.fx_observations if f.timestamp <= utc(at)]
        if not known:
            raise ValueError("missing observed FX")
        return known[-1]

    def checked_fx(self, at: datetime, rules: TradingRules) -> FXRate:
        return validate_fx(self.fx(at), utc(at), rules)

    def bars(self, symbols: set[str], at: datetime) -> dict[str, list[PriceBar]]:
        return {
            s: [b for b in self.original.get(s, []) if b.fetched_at <= utc(at)] for s in symbols
        }

    def screen(
        self, at: datetime, settings: SwingSettings, protected: set[str], since: datetime
    ) -> tuple[ScreeningResult, ScreenSummary, dict[str, Mark]]:
        at, since = utc(at), utc(since)
        if any(s.metadata_at > at for s in self.securities):
            raise ValueError("future universe metadata cannot be used")
        end = self.daily_index(at)
        recent = mcal.get_calendar("NYSE").schedule(at.date() - timedelta(days=10), at.date())
        expected_latest = max(
            (d.date() for d in recent.index if confirmed_at(d.date()) <= at), default=None
        )
        if end < 0 or self.closes_at[end].astimezone(NY).date() != expected_latest:
            raise ValueError("latest completed session is missing or delayed")
        if end < 200 or any(k > at for k in self.available[end - 200 : end + 1]):
            raise ValueError("insufficient point-in-time observed history")
        coverage = self.actions
        if not (
            coverage.verified
            and coverage.source != "unverified"
            and utc(coverage.since) <= self.opens_at[end - 200]
            and utc(coverage.through) >= at
            and utc(coverage.observed_at) <= at
            and at - utc(coverage.observed_at) <= timedelta(hours=1)
        ):
            raise ValueError("corporate action coverage missing, stale, or future")
        for e in coverage.events:
            if e.symbol in self.original and utc(e.observed_at) <= at and utc(e.effective_at) <= at:
                if e.kind == "split" or utc(e.effective_at) >= since:
                    raise ValueError("unsupported corporate action; trading stopped")
        result, summary = screen_daily(self, at, settings, protected)
        if any("missing/invalid" in r for r in summary.excluded.values()):
            raise ValueError("invalid observed universe; no trading decisions")
        marks = {}
        candidates = []
        for c in result.candidates:
            if c.instrument.symbol in protected and c.features is None:
                raise ValueError("protected observed security unavailable")
            f = c.features
            if f is not None:
                mark = Mark(
                    symbol=c.instrument.symbol,
                    currency="USD",
                    price=Decimal(str(f.daily_close)),
                    price_at=self.closes_at[end],
                    available_at=self.available[end],
                )
                f = f.model_copy(update={"fetched_at": self.available[end], "mark": mark})
                marks[c.instrument.symbol] = mark
            candidates.append(c.model_copy(update={"features": f}))
        return result.model_copy(update={"candidates": candidates}), summary, marks
