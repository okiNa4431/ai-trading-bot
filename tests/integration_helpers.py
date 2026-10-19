"""Counterfactual fixtures with Alpaca-shaped metadata; NOT market observations."""

from datetime import datetime, timedelta
from decimal import Decimal

from trading_bot.alpaca import NY, DailyBatch, confirmed_at
from trading_bot.models import PriceBar
from trading_bot.portfolio.models import FXRate
from trading_bot.swing.data import synthetic
from trading_bot.swing.observed import ActionCoverage, ObservedDailyData


def observed_fixture(day=201):
    raw = synthetic(20, 205)
    dates = tuple(o.astimezone(NY).date() for o in raw.opens_at)
    bars = {}
    for col, s in enumerate(raw.securities):
        bars[s.instrument.symbol] = [
            PriceBar(
                symbol=s.instrument.symbol,
                market="US",
                currency="USD",
                interval="1d",
                source="alpaca",
                timestamp=datetime.combine(dates[i], datetime.min.time(), NY),
                open=float(raw.opening[i, col]),
                close=float(raw.closing[i, col]),
                high=float(raw.high[i, col]),
                low=float(raw.low[i, col]),
                volume=float(raw.volume[i, col]),
                fetched_at=confirmed_at(dates[i]) + timedelta(minutes=1),
            )
            for i in range(205)
        ]
    reference = [
        bars[raw.securities[0].instrument.symbol][i].model_copy(
            update={
                "symbol": "SPY",
                "open": 100 + i,
                "close": 100 + i,
                "high": 101 + i,
                "low": 99 + i,
            }
        )
        for i in range(205)
    ]
    at = confirmed_at(dates[day]) + timedelta(minutes=2)
    fx = [
        FXRate(
            usd_jpy=Decimal(150),
            source="observed",
            timestamp=confirmed_at(d) + timedelta(minutes=1),
        )
        for d in dates
    ]
    data = ObservedDailyData(
        raw.securities,
        DailyBatch(bars, dates, complete_response=True),
        reference,
        fx,
        ActionCoverage(since=raw.opens_at[0], through=at, observed_at=at, events=[]),
    )
    return data, at
