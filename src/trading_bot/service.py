"""Fetch orchestration; one failed instrument does not stop other instruments."""

import logging
import time
from datetime import UTC, datetime, timedelta

from trading_bot.config import Instrument, Interval, Settings
from trading_bot.provider import DataError, MarketDataProvider
from trading_bot.storage import ParquetStore

logger = logging.getLogger(__name__)


def collect(
    settings: Settings,
    instruments: list[Instrument],
    intervals: list[Interval],
    provider: MarketDataProvider,
    store: ParquetStore,
    update: bool = False,
) -> list[str]:
    failures: list[str] = []
    for instrument in instruments:
        for interval in intervals:
            began = time.monotonic()
            key = f"{instrument.market}/{instrument.symbol}/{interval}"
            logger.info("fetch start %s", key)
            try:
                end = datetime.now(UTC)
                days = settings.daily_days if interval == "1d" else settings.hourly_days
                start = end - timedelta(days=days)
                if update:
                    existing = store.read(instrument, interval)
                    if existing:
                        start = max(start, existing[-settings.overlap_bars :][0].timestamp)
                        if existing[-1].timestamp < end - timedelta(days=days):
                            logger.warning("history gap outside configured lookback %s", key)
                bars = provider.fetch(instrument, start, end, interval)
                inserted, updated = store.save(instrument, interval, bars)
                logger.info(
                    "fetch end %s fetched=%d new=%d updated=%d elapsed=%.2fs",
                    key,
                    len(bars),
                    inserted,
                    updated,
                    time.monotonic() - began,
                )
            except Exception as exc:
                logger.error(
                    "fetch failed %s reason=%s elapsed=%.2fs",
                    key,
                    str(exc) if isinstance(exc, DataError) else type(exc).__name__,
                    time.monotonic() - began,
                )
                failures.append(key)
    return failures
