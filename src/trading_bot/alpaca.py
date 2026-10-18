"""Read-only raw IEX daily adapter. Live construction remains globally disabled."""

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from zoneinfo import ZoneInfo

import httpx
import pandas_market_calendars as mcal

from trading_bot.config import Instrument, Interval
from trading_bot.data_access import require_live_market_access
from trading_bot.models import PriceBar
from trading_bot.provider import DataError

logger = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataError("Alpaca requires timezone-aware times")
    return value.astimezone(UTC)


def confirmed_at(session: date) -> datetime:
    # Deliberately wait beyond the next local midnight, not merely exchange close.
    return datetime.combine(session + timedelta(days=1), day_time(0, 15), NY).astimezone(UTC)


class RequestPacer:
    """One shared process-wide 120/min ceiling; no unbounded retry sleeps."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.clock, self.sleep = clock, sleep
        self.last: float | None = None
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = self.clock()
            if self.last is not None:
                self.sleep(max(0, 0.5 - (now - self.last)))
            self.last = self.clock()


LIVE_PACER = RequestPacer()


@dataclass
class DailyBatch:
    bars: dict[str, list[PriceBar]]
    expected_sessions: tuple[date, ...]
    missing: dict[str, tuple[date, ...]] = field(default_factory=dict)
    requests: int = 0
    excluded_unconfirmed: int = 0
    feed: str = "iex"
    adjustment: str = "raw"
    calendar: str = "NYSE"
    data_kind: str = "mock"
    complete_response: bool = False

    def require_complete(self) -> "DailyBatch":
        if not self.complete_response or not self.expected_sessions or self.missing:
            raise DataError("Alpaca incomplete daily coverage; trading must remain stopped")
        return self


class AlpacaBatchError(DataError):
    """Validated earlier rows remain inspectable, never implicitly tradeable."""

    def __init__(self, reason: str, partial: DailyBatch) -> None:
        super().__init__(reason)
        self.partial = partial


class AlpacaDailyProvider:
    def __init__(
        self,
        *,
        mock_transport: httpx.MockTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        pacer: RequestPacer | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_requests: int = 500,
        max_pages: int = 100,
        retries: int = 2,
    ) -> None:
        if not 1 <= max_requests <= 1000 or not 1 <= max_pages <= 500 or not 0 <= retries <= 2:
            raise ValueError("invalid Alpaca request budget")
        if mock_transport is not None and not isinstance(mock_transport, httpx.MockTransport):
            raise ValueError("only MockTransport may bypass the live collection gate")
        if mock_transport is None:
            require_live_market_access()  # Before reading secrets or constructing an HTTP client.
            key, secret = (
                os.environ.get("ALPACA_API_KEY_ID"),
                os.environ.get("ALPACA_API_SECRET_KEY"),
            )
            if not key or not secret:
                raise DataError("missing Alpaca credentials")
        else:
            key, secret = "offline-mock-key", "offline-mock-secret"
        self._initialize(
            key,
            secret,
            mock_transport,
            now,
            pacer,
            sleep,
            max_requests,
            max_pages,
            retries,
            "mock" if mock_transport is not None else "real",
        )

    def _initialize(
        self,
        key: str,
        secret: str,
        transport: httpx.BaseTransport | None,
        now: Callable[[], datetime],
        pacer: RequestPacer | None,
        sleep: Callable[[float], None],
        max_requests: int,
        max_pages: int,
        retries: int,
        data_kind: str,
    ) -> None:
        """Internal setup shared with the separately authorized read-only research path."""
        self.client = httpx.Client(
            base_url="https://data.alpaca.markets",
            transport=transport,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        )
        self.now, self.sleep = now, sleep
        self.pacer = pacer or LIVE_PACER
        self.max_requests, self.max_pages, self.retries = max_requests, max_pages, retries
        self.data_kind = data_kind

    def close(self) -> None:
        self.client.close()

    def fetch(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        interval: Interval,
    ) -> list[PriceBar]:
        if interval != "1d":
            raise DataError("Alpaca adapter currently supports daily bars only")
        return self.fetch_many([instrument], start, end).require_complete().bars[instrument.symbol]

    def _request(self, params: dict[str, str], report: DailyBatch) -> dict[str, object]:
        for attempt in range(self.retries + 1):
            if report.requests >= self.max_requests:
                raise DataError("Alpaca request budget exhausted")
            self.pacer.wait()
            report.requests += 1
            try:
                response = self.client.get("/v2/stocks/bars", params=params)
            except httpx.TransportError:
                response = None
            if response is not None and response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    raise DataError("Alpaca invalid JSON response") from None
                if not isinstance(payload, dict):
                    raise DataError("Alpaca invalid response shape")
                return payload
            status = response.status_code if response is not None else 0
            if status not in (0, 429, 500, 502, 503, 504) or attempt == self.retries:
                raise DataError(f"Alpaca request stopped (HTTP {status or 'transport'})") from None
            delay = float(2**attempt)
            if response is not None and "Retry-After" in response.headers:
                try:
                    delay = float(response.headers["Retry-After"])
                except ValueError:
                    raise DataError("Alpaca unsupported Retry-After; stop") from None
                if not 0 <= delay <= 60:
                    raise DataError("Alpaca Retry-After exceeds safe wait budget")
            self.sleep(delay)
        raise AssertionError("unreachable")

    def fetch_many(
        self,
        instruments: Sequence[Instrument],
        start: datetime,
        end: datetime,
    ) -> DailyBatch:
        start, end, cutoff = aware(start), aware(end), aware(self.now())
        if start >= end or end > cutoff:
            raise DataError("invalid or future Alpaca request interval")
        symbols = [i.symbol for i in instruments]
        if not 1 <= len(symbols) <= 10000 or len(set(symbols)) != len(symbols):
            raise DataError("Alpaca requires a nonempty unique symbol list")
        if any(i.market != "US" or i.currency != "USD" for i in instruments):
            raise DataError("Alpaca daily adapter requires US/USD instruments")
        schedule = mcal.get_calendar("NYSE").schedule(
            start_date=start.astimezone(NY).date(),
            end_date=end.astimezone(NY).date(),
        )
        expected = tuple(
            d.date()
            for d in schedule.index
            if start <= datetime.combine(d.date(), day_time(), NY).astimezone(UTC) <= end
            and confirmed_at(d.date()) <= cutoff
        )
        report = DailyBatch({s: [] for s in symbols}, expected, data_kind=self.data_kind)
        seen: set[tuple[str, datetime]] = set()
        try:
            for offset in range(0, len(symbols), 100):
                batch = symbols[offset : offset + 100]
                params = dict(
                    symbols=",".join(batch),
                    timeframe="1Day",
                    feed="iex",
                    adjustment="raw",
                    currency="USD",
                    sort="asc",
                    asof="-",
                    limit="10000",
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
                tokens: set[str] = set()
                for _ in range(self.max_pages):
                    payload = self._request(params, report)
                    rows = payload.get("bars")
                    if not isinstance(rows, dict) or any(s not in batch for s in rows):
                        raise DataError("Alpaca invalid symbols/bars response")
                    for symbol, items in rows.items():
                        if not isinstance(items, list):
                            raise DataError("Alpaca invalid bar list")
                        for row in items:
                            if not isinstance(row, dict):
                                raise DataError("Alpaca invalid bar")
                            try:
                                stamp = aware(datetime.fromisoformat(row["t"]))
                                local = stamp.astimezone(NY)
                                if local.time() != day_time() or not start <= stamp <= end:
                                    raise ValueError("unexpected bar timestamp")
                                if (symbol, stamp) in seen:
                                    raise ValueError("duplicate timestamp")
                                seen.add((symbol, stamp))
                                values = {
                                    k: row[v]
                                    for k, v in dict(
                                        open="o",
                                        high="h",
                                        low="l",
                                        close="c",
                                        volume="v",
                                    ).items()
                                }
                                if any(type(v) not in (int, float) for v in values.values()):
                                    raise ValueError("nonnumeric OHLCV")
                                bar = PriceBar(
                                    symbol=symbol,
                                    market="US",
                                    currency="USD",
                                    interval="1d",
                                    source="alpaca",
                                    timestamp=stamp,
                                    fetched_at=aware(self.now()),
                                    **values,
                                )
                            except (KeyError, ValueError, TypeError, OverflowError):
                                raise DataError(
                                    "Alpaca invalid or duplicate OHLCV/timestamp"
                                ) from None
                            if confirmed_at(local.date()) > cutoff:
                                report.excluded_unconfirmed += 1
                            elif local.date() not in expected:
                                raise DataError("Alpaca bar outside expected exchange sessions")
                            else:
                                report.bars[symbol].append(bar)
                    token = payload.get("next_page_token")
                    if token is None:
                        break
                    if not isinstance(token, str) or not token or token in tokens:
                        raise DataError("Alpaca invalid or repeated page token")
                    tokens.add(token)
                    params["page_token"] = token
                else:
                    raise DataError("Alpaca page budget exhausted")
        except DataError as exc:
            self._coverage(report)
            logger.error("Alpaca batch stopped: %s; requests=%d", str(exc), report.requests)
            raise AlpacaBatchError(str(exc), report) from None
        self._coverage(report)
        report.complete_response = True
        logger.info(
            "Alpaca batch feed=iex requests=%d missing_symbols=%d",
            report.requests,
            len(report.missing),
        )
        return report

    @staticmethod
    def _coverage(report: DailyBatch) -> None:
        for symbol, bars in report.bars.items():
            bars.sort(key=lambda b: b.timestamp)
            found = {b.timestamp.astimezone(NY).date() for b in bars}
            missing = tuple(d for d in report.expected_sessions if d not in found)
            if missing:
                report.missing[symbol] = missing
