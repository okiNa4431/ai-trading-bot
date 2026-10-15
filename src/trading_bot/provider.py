"""Yahoo adapter; no resampling or price adjustment is performed."""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from pydantic import ValidationError

from trading_bot.config import Instrument, Interval, Settings
from trading_bot.models import PriceBar

logger = logging.getLogger(__name__)


class DataError(ValueError):
    """Missing, malformed, or unavailable market data."""


class MarketDataProvider(Protocol):
    def fetch(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        interval: Interval,
    ) -> list[PriceBar]: ...


def convert_frame(
    frame: pd.DataFrame,
    instrument: Instrument,
    interval: Interval,
    now: datetime,
) -> list[PriceBar]:
    if frame.empty:
        raise DataError("provider returned empty data")
    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(frame.columns):
        raise DataError(f"missing columns: {sorted(required - set(frame.columns))}")
    bars: list[PriceBar] = []
    seen: set[datetime] = set()
    zone = ZoneInfo("Asia/Tokyo" if instrument.market == "JP" else "America/New_York")
    try:
        for index, row in frame.iterrows():
            stamp = pd.Timestamp(index)  # type: ignore[arg-type]
            if pd.isna(stamp) or stamp.tzinfo is None:
                raise DataError("invalid or naive provider timestamp")
            timestamp = stamp.to_pydatetime().astimezone(UTC)
            if timestamp in seen:
                raise DataError("duplicate symbol/interval/timestamp")
            seen.add(timestamp)
            # Conservative completion rule: hourly bars need a full hour;
            # daily bars wait until the following local calendar day.
            local = timestamp.astimezone(zone)
            complete_at = (
                timestamp + timedelta(hours=1)
                if interval == "1h"
                else (local + timedelta(days=1)).replace(hour=0, minute=0, second=0)
            )
            if complete_at > now:
                logger.info(
                    "exclude unconfirmed bar symbol=%s time=%s", instrument.symbol, timestamp
                )
                continue
            bars.append(
                PriceBar(
                    symbol=instrument.symbol,
                    market=instrument.market,
                    currency=instrument.currency,
                    timestamp=timestamp,
                    interval=interval,
                    fetched_at=now,
                    open=row["Open"],
                    high=row["High"],
                    low=row["Low"],
                    close=row["Close"],
                    volume=row["Volume"],
                )
            )
    except (ValueError, TypeError, OverflowError) as exc:
        if isinstance(exc, ValidationError):
            reason = "; ".join(
                f"{'.'.join(map(str, error['loc'])) or 'OHLC'}: {error['type']}"
                for error in exc.errors(include_input=False)
            )
        else:
            reason = str(exc) if isinstance(exc, DataError) else type(exc).__name__
        logger.error("validation error symbol=%s reason=%s", instrument.symbol, reason)
        raise DataError(f"invalid OHLCV for {instrument.symbol}: {reason}") from exc
    if not bars:
        raise DataError("no confirmed bars returned")
    bars.sort(key=lambda bar: bar.timestamp)
    for previous, current in zip(bars, bars[1:], strict=False):
        if current.timestamp - previous.timestamp > timedelta(days=7):
            logger.warning(
                "possible data gap symbol=%s from=%s to=%s",
                current.symbol,
                previous.timestamp,
                current.timestamp,
            )
        if abs(current.close / previous.close - 1) > 0.5:
            logger.warning(
                "large raw price change symbol=%s time=%s", current.symbol, current.timestamp
            )
    return bars


class YahooFinanceProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_request: float | None = None

    def fetch(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        interval: Interval,
    ) -> list[PriceBar]:
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise DataError("invalid aware request range")
        if interval == "1h" and (
            end - start > timedelta(days=60)
            or start < datetime.now(UTC) - timedelta(days=60, minutes=1)
        ):
            raise DataError("hourly request exceeds conservative 60-day limit")
        for attempt in range(self.settings.retries + 1):
            if self.last_request is not None:
                time.sleep(
                    max(0, self.settings.request_interval - (time.monotonic() - self.last_request))
                )
            self.last_request = time.monotonic()
            try:
                frame = yf.Ticker(instrument.symbol).history(
                    start=start,
                    end=end,
                    interval=interval,
                    auto_adjust=False,
                    back_adjust=False,
                    repair=False,
                    actions=False,
                    keepna=True,
                    prepost=False,
                    timeout=self.settings.http_timeout,
                    raise_errors=True,
                )
            except Exception as exc:
                # Do not log upstream exception messages: they may contain URLs or credentials.
                logger.warning(
                    "fetch failed symbol=%s error=%s attempt=%d",
                    instrument.symbol,
                    type(exc).__name__,
                    attempt + 1,
                )
                if attempt == self.settings.retries:
                    raise DataError(f"provider request failed ({type(exc).__name__})") from exc
                logger.info("retry symbol=%s", instrument.symbol)
                time.sleep(min(2**attempt, 8))
                continue
            return convert_frame(frame, instrument, interval, datetime.now(UTC))
        raise AssertionError("unreachable")
