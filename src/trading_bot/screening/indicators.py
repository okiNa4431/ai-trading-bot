"""Causal indicators only. No inferred/future bars or silent repairs."""

from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from trading_bot.broker.execution import window
from trading_bot.config import Instrument, Interval
from trading_bot.models import PriceBar
from trading_bot.portfolio.models import Mark, utc
from trading_bot.screening.models import Features, ScreeningSettings


class FeatureUnavailable(ValueError):
    pass


def confirmed(
    bars: list[PriceBar],
    instrument: Instrument,
    interval: Interval,
    as_of: datetime,
) -> list[tuple[PriceBar, datetime]]:
    as_of = utc(as_of)
    result = []
    seen = set()
    for original in bars:
        try:
            bar = PriceBar.model_validate(original.model_dump())
        except ValueError as exc:
            raise FeatureUnavailable("invalid OHLCV") from exc
        if (bar.symbol, bar.market, bar.currency, bar.interval) != (
            instrument.symbol,
            instrument.market,
            instrument.currency,
            interval,
        ):
            raise FeatureUnavailable("OHLCV metadata mismatch")
        if bar.timestamp in seen:
            raise FeatureUnavailable("duplicate OHLCV timestamp")
        seen.add(bar.timestamp)
        span = window(bar)
        if span is None:
            continue
        if span[1] > as_of or bar.fetched_at > as_of or bar.fetched_at < span[1]:
            continue
        result.append((bar, span[1]))
    return sorted(result, key=lambda pair: pair[0].timestamp)


def wilder(values: NDArray[np.float64], periods: int = 14) -> float:
    if len(values) < periods or not np.isfinite(values).all():
        raise FeatureUnavailable("insufficient or nonfinite Wilder observations")
    value = float(values[:periods].mean())
    for item in values[periods:]:
        value = (value * (periods - 1) + float(item)) / periods
    return value


def calculate(
    instrument: Instrument,
    daily: list[PriceBar],
    hourly: list[PriceBar],
    as_of: datetime,
    settings: ScreeningSettings,
) -> Features:
    as_of = utc(as_of)
    days = confirmed(daily, instrument, "1d", as_of)
    hours = confirmed(hourly, instrument, "1h", as_of)
    if len(days) < 21 or len(hours) < 2:
        raise FeatureUnavailable("need 21 confirmed daily and 2 hourly bars")
    if (as_of - days[-1][1]).total_seconds() > settings.max_daily_age_seconds:
        raise FeatureUnavailable("stale daily history")
    if (as_of - hours[-1][1]).total_seconds() > settings.max_hourly_age_seconds:
        raise FeatureUnavailable("stale hourly history")
    if hours[-1][0].timestamp - hours[-2][0].timestamp != timedelta(hours=1):
        raise FeatureUnavailable("last hourly observations are not one hour apart")
    frame = pd.DataFrame([bar.model_dump() for bar, _ in days])
    closes = frame.close.astype(float)
    returns = closes.pct_change().iloc[1:]
    hour_return = hours[-1][0].close / hours[-2][0].close - 1
    if (
        (returns.abs() > settings.max_price_jump).any()
        or abs(hour_return) > settings.max_price_jump
        or abs(hours[-1][0].close / days[-1][0].close - 1) > settings.max_price_jump
    ):
        raise FeatureUnavailable("raw price discontinuity; possible split/dividend or anomaly")
    deltas = closes.diff().iloc[1:].to_numpy()
    gains = wilder(np.maximum(deltas, 0))
    losses = wilder(np.maximum(-deltas, 0))
    rsi = (
        50.0 if gains == losses == 0 else 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
    )
    previous = closes.shift(1)
    ranges = (
        pd.concat(
            [frame.high - frame.low, (frame.high - previous).abs(), (frame.low - previous).abs()],
            axis=1,
        )
        .max(axis=1)
        .iloc[1:]
    )
    mean_volume = float(frame.volume.iloc[-21:-1].mean())
    if mean_volume <= 0:
        raise FeatureUnavailable("relative volume unavailable: zero historical volume")
    price_bar, observed = hours[-1]
    return Features(
        symbol=instrument.symbol,
        hourly_return=hour_return,
        daily_return=float(returns.iloc[-1]),
        sma5=float(closes.iloc[-5:].mean()),
        sma20=float(closes.iloc[-20:].mean()),
        rsi14=rsi,
        atr14=wilder(ranges.to_numpy()),
        relative_volume=float(frame.volume.iloc[-1]) / mean_volume,
        volatility20=float(returns.iloc[-20:].std(ddof=1) * np.sqrt(252)),
        average_turnover20=float((closes * frame.volume).iloc[-20:].mean()),
        daily_close=float(closes.iloc[-1]),
        daily_observed_at=days[-1][1],
        hourly_observed_at=observed,
        fetched_at=max(bar.fetched_at for bar, _ in [*days, *hours]),
        mark=Mark(
            symbol=instrument.symbol,
            currency=instrument.currency,
            price=Decimal(str(price_bar.close)),
            price_at=observed,
            available_at=max(observed, price_bar.fetched_at),
        ),
    )
