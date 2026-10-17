"""Prepared, inactive FX adapters. No quote is ever fetched by default.

Twelve Data: internal non-display access/storage, subject to an active Basic account
and source restrictions. ECB: source-attributed daily reference statistics only.
Neither adapter is wired into the live trading runner.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from xml.etree import ElementTree

import httpx
from pydantic import Field, field_validator

from trading_bot.data_access import require_live_market_access
from trading_bot.market_inputs import FXObservation
from trading_bot.portfolio.models import Contract, FXRate, utc


class FXSourceError(ValueError):
    """Safe diagnostic without an upstream body, URL or credentials."""


def get_document(
    url: str,
    *,
    client: httpx.Client | None,
    headers: dict[str, str] | None = None,
    params: dict[str, str | int] | None = None,
) -> bytes:
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        if client is None:
            with httpx.Client(trust_env=False, follow_redirects=False) as owned:
                return get_document(url, client=owned, headers=headers, params=params)
        # Streaming limit also bounds an unexpected error document.
        with client.stream(
            "GET", url, headers=headers, params=params, timeout=20, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise FXSourceError(f"FX provider rejected request (HTTP {response.status_code})")
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 1_000_000:
                    raise FXSourceError("FX provider response too large")
            return bytes(chunks)
    except httpx.HTTPError:
        raise FXSourceError("FX provider transport unavailable; no retry") from None


class TwelveDataFXProvider:
    """One USD/JPY 5-minute request. Header authentication; no retry/fallback."""

    def __init__(self, key: str, client: httpx.Client | None = None) -> None:
        self.key, self.client = key, client

    def fetch_fx(self, at: datetime) -> FXObservation:
        require_live_market_access()
        return self.parse(self.document(), at, datetime.now(UTC))

    def document(self) -> bytes:
        require_live_market_access()
        if not self.key or self.key.strip() != self.key or "\n" in self.key or "\r" in self.key:
            raise FXSourceError("FX provider key missing or invalid")
        return get_document(
            "https://api.twelvedata.com/time_series",
            client=self.client,
            headers={"Authorization": "apikey " + self.key},
            params={
                "symbol": "USD/JPY",
                "interval": "5min",
                "timezone": "UTC",
                "outputsize": 24,
                "order": "desc",
                "format": "JSON",
            },
        )

    @staticmethod
    def parse(raw: bytes, at: datetime, observed: datetime) -> FXObservation:
        import json

        at, observed = utc(at), utc(observed)
        if at > observed:
            raise FXSourceError("FX selection time is future")
        try:
            data: Any = json.loads(raw)
            if data.get("status") == "error":
                raise FXSourceError("FX provider returned an error; no retry")
            meta = data["meta"]
            if (
                meta["symbol"],
                meta["interval"],
                meta["currency_base"],
                meta["currency_quote"],
            ) != ("USD/JPY", "5min", "US Dollar", "Japanese Yen"):
                raise FXSourceError("FX pair/interval metadata mismatch")
            if meta.get("exchange_timezone", meta.get("timezone")) != "UTC":
                raise FXSourceError("FX timezone metadata missing or unsupported")
            rows: list[tuple[datetime, Decimal]] = []
            seen: set[datetime] = set()
            for row in data["values"]:
                # Naive provider strings are accepted ONLY with the explicit UTC contract above.
                stamp = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                confirmed = stamp + timedelta(minutes=5)
                if stamp in seen:
                    raise FXSourceError("duplicate FX timestamp")
                seen.add(stamp)
                rate = Decimal(row["close"])
                if not rate.is_finite() or rate <= 0:
                    raise FXSourceError("invalid FX number")
                if confirmed <= at:
                    rows.append((confirmed, rate))
            if not rows:
                raise FXSourceError("no confirmed FX bar")
            stamp, rate = max(rows)
            return FXObservation(
                rate=FXRate(usd_jpy=rate, timestamp=stamp, source="observed"),
                observed_at=observed,
                symbol="USD/JPY",
                provider="twelve_data",
            )
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
            raise FXSourceError("invalid or unavailable FX response") from None


class DailyReferenceFX(Contract):
    reference_date: date
    observed_at: datetime
    eur_usd: Decimal = Field(gt=0)
    eur_jpy: Decimal = Field(gt=0)
    provider: Literal["ecb"] = "ecb"
    attribution: str = (
        "Source: ECB statistics. USD/JPY is calculated as EUR/JPY divided by EUR/USD."
    )
    _time = field_validator("observed_at")(utc)

    @property
    def usd_jpy(self) -> Decimal:
        return self.eur_jpy / self.eur_usd


class ECBDailyFXProvider:
    """Date precision retained. This is NOT an FXProvider for trading/freshness checks."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client

    def fetch_daily(self) -> DailyReferenceFX:
        require_live_market_access()
        raw = get_document(
            "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", client=self.client
        )
        return self.parse(raw, datetime.now(UTC))

    @staticmethod
    def parse(raw: bytes, observed: datetime) -> DailyReferenceFX:
        if len(raw) > 1_000_000 or b"<!" in raw or b"\x00" in raw:
            raise FXSourceError("unsupported XML declarations or size")
        try:
            root = ElementTree.fromstring(raw)
            days = [node for node in root.iter() if "time" in node.attrib]
            if len(days) != 1:
                raise FXSourceError("ambiguous ECB reference date")
            day = date.fromisoformat(days[0].attrib["time"])
            if day > utc(observed).date():
                raise FXSourceError("future ECB reference date")
            rates = {}
            for node in days[0]:
                symbol = node.attrib["currency"]
                if symbol in rates:
                    raise FXSourceError("duplicate ECB currency")
                rates[symbol] = node.attrib["rate"]
            return DailyReferenceFX(
                reference_date=day,
                observed_at=observed,
                eur_usd=Decimal(rates["USD"]),
                eur_jpy=Decimal(rates["JPY"]),
            )
        except (KeyError, ValueError, ElementTree.ParseError):
            raise FXSourceError("invalid ECB reference data") from None


def prepared_fx_provider(name: str, key: str = "") -> TwelveDataFXProvider | ECBDailyFXProvider:
    if name == "twelve_data":
        return TwelveDataFXProvider(key)
    if name == "ecb_daily":
        return ECBDailyFXProvider()
    raise FXSourceError("FX provider has not passed the rights review")
