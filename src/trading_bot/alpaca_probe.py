"""Offline integration probe using the production parser and existing Parquet store."""

from datetime import UTC, datetime, time
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

import httpx
import pandas_market_calendars as mcal

from trading_bot.alpaca import AlpacaDailyProvider, RequestPacer
from trading_bot.config import Instrument
from trading_bot.storage import ParquetStore


def mock_probe(size: int, *, sessions: int = 221, root: Path | None = None) -> dict[str, object]:
    """Never reads credentials; no real observations are represented by these prices."""
    if size not in (20, 100, 500, 2000) or not 1 <= sessions <= 221:
        raise ValueError("unsupported offline probe size")
    days = mcal.get_calendar("NYSE").schedule("2024-01-02", "2024-12-31").index[:sessions]
    dates = [datetime.combine(d.date(), time(), ZoneInfo("America/New_York")) for d in days]
    instruments = [
        Instrument(symbol=f"S{i:04d}", market="US", currency="USD", display_name="mock only")
        for i in range(size)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        assert p["feed"] == "iex" and p["adjustment"] == "raw" and p["timeframe"] == "1Day"
        symbols = p["symbols"].split(",")
        offset = int(p.get("page_token", "0"))
        end = min(offset + 10000, len(symbols) * sessions)
        bars: dict[str, list[dict[str, object]]] = {}
        for index in range(offset, end):
            symbol, day = symbols[index // sessions], index % sessions
            price = 50 + int(symbol[1:]) % 100 + day * 0.05
            bars.setdefault(symbol, []).append(
                dict(
                    t=dates[day].isoformat(),
                    o=price,
                    h=price + 1,
                    l=price - 1,
                    c=price + 0.2,
                    v=1000000,
                )
            )
        return httpx.Response(
            200,
            json={
                "bars": bars,
                "next_page_token": (str(end) if end < len(symbols) * sessions else None),
            },
        )

    start = perf_counter()
    provider = AlpacaDailyProvider(
        mock_transport=httpx.MockTransport(handler),
        now=lambda: datetime(2025, 1, 2, tzinfo=UTC),
        pacer=RequestPacer(sleep=lambda _: None),
    )
    try:
        result = provider.fetch_many(instruments, dates[0], dates[-1]).require_complete()
        persisted = 0
        if root is not None:
            if root.exists():
                raise ValueError("offline probe refuses existing storage")
            store = ParquetStore(root)
            for instrument in instruments:
                bars = result.bars[instrument.symbol]
                store.save(instrument, "1d", bars)
                store.save(instrument, "1d", bars)
                assert store.read(instrument, "1d") == bars
                persisted += len(bars)
        return dict(
            data_kind="mock",
            symbols=size,
            sessions=sessions,
            requests=result.requests,
            rows=sum(map(len, result.bars.values())),
            missing=len(result.missing),
            persisted_rows=persisted,
            seconds=round(perf_counter() - start, 3),
        )
    finally:
        provider.close()
