"""Serial cross-process writes, validated merges and atomic file replacement."""

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from trading_bot.config import Instrument, Interval
from trading_bot.models import PriceBar

logger = logging.getLogger(__name__)


class ParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, instrument: Instrument, interval: Interval) -> Path:
        return (
            self.root
            / f"market={instrument.market}"
            / f"symbol={instrument.symbol}"
            / f"interval={interval}"
            / "bars.parquet"
        )

    @contextmanager
    def writer(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".write.lock"
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
            yield
        finally:
            os.close(fd)
            lock.unlink()

    def read(self, instrument: Instrument, interval: Interval) -> list[PriceBar]:
        path = self.path(instrument, interval)
        if not path.exists():
            return []
        frame = pd.read_parquet(path)
        missing = set(PriceBar.model_fields) - set(frame.columns)
        if missing or not frame.columns.is_unique:
            raise ValueError(f"invalid stored schema; missing columns: {sorted(missing)}")
        if frame.empty:
            raise ValueError("stored parquet contains no bars")
        bars = [PriceBar.model_validate(row) for row in frame.to_dict("records")]
        self.check_partition(bars, instrument, interval)
        return sorted(bars, key=lambda b: b.timestamp)

    @staticmethod
    def check_partition(bars: list[PriceBar], instrument: Instrument, interval: Interval) -> None:
        keys = set()
        for bar in bars:
            # model_copy/update and model_construct bypass Pydantic's validation.
            PriceBar.model_validate(bar.model_dump())
            if (bar.symbol, bar.market, bar.currency, bar.interval) != (
                instrument.symbol,
                instrument.market,
                instrument.currency,
                interval,
            ):
                raise ValueError("bar does not match storage partition")
            if bar.timestamp in keys:
                raise ValueError("duplicate incoming timestamp")
            keys.add(bar.timestamp)

    def save(
        self,
        instrument: Instrument,
        interval: Interval,
        bars: list[PriceBar],
    ) -> tuple[int, int]:
        try:
            return self._save(instrument, interval, bars)
        except Exception as exc:
            logger.error(
                "save failed market=%s symbol=%s interval=%s error=%s",
                instrument.market,
                instrument.symbol,
                interval,
                type(exc).__name__,
            )
            raise

    def _save(
        self,
        instrument: Instrument,
        interval: Interval,
        bars: list[PriceBar],
    ) -> tuple[int, int]:
        if not bars:
            raise ValueError("cannot save empty bars")
        self.check_partition(bars, instrument, interval)
        with self.writer():
            old = {b.timestamp: b for b in self.read(instrument, interval)}
            inserted = sum(b.timestamp not in old for b in bars)
            updated = sum(
                b.timestamp in old
                and b.model_dump(exclude={"fetched_at"})
                != old[b.timestamp].model_dump(exclude={"fetched_at"})
                for b in bars
            )
            old.update({b.timestamp: b for b in bars})
            frame = pd.DataFrame(
                [b.model_dump() for b in sorted(old.values(), key=lambda b: b.timestamp)]
            )
            path = self.path(instrument, interval)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            os.close(fd)
            temporary = Path(name)
            try:
                frame.to_parquet(temporary, engine="pyarrow", index=False)
                with temporary.open("rb+") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return inserted, updated

    def status(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for path in sorted(self.root.glob("market=*/symbol=*/interval=*/bars.parquet")):
            market, symbol, interval = [p.split("=", 1)[1] for p in path.parts[-4:-1]]
            instrument = Instrument.model_validate(
                dict(
                    symbol=symbol,
                    market=market,
                    currency="JPY" if market == "JP" else "USD",
                    display_name=symbol,
                )
            )
            if interval not in ("1d", "1h"):
                raise ValueError("unknown stored interval")
            bars = self.read(instrument, interval)  # type: ignore[arg-type]
            results.append(
                dict(
                    symbol=symbol,
                    market=market,
                    interval=interval,
                    count=len(bars),
                    oldest=bars[0].timestamp.isoformat() if bars else "-",
                    latest=bars[-1].timestamp.isoformat() if bars else "-",
                )
            )
        return results
