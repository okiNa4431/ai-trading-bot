"""Decimal-based paper account contracts. All money is tagged by currency."""

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_bot.config import Instrument

Currency = Literal["JPY", "USD"]
Side = Literal["BUY", "SELL"]
Status = Literal["PENDING", "FILLED", "REJECTED", "CANCELLED"]
ZERO = Decimal("0")


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware UTC-compatible time required")
    return value.astimezone(UTC)


def money(amount: Decimal, currency: Currency) -> Decimal:
    return amount.quantize(
        Decimal("1") if currency == "JPY" else Decimal("0.01"), rounding=ROUND_DOWN
    )


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TradingRules(Contract):
    slippage: Decimal = Field(default=Decimal("0.001"), ge=0, lt=1)
    commission_rate: Decimal = Field(default=ZERO, ge=0, lt=1)
    commission_jpy: Decimal = Field(default=ZERO, ge=0)
    commission_usd: Decimal = Field(default=ZERO, ge=0)
    fx_cost: Decimal = Field(default=Decimal("0.0025"), ge=0, lt=1)
    max_symbol_ratio: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("0.10"))
    max_stock_ratio: Decimal = Field(default=Decimal("0.80"), gt=0, le=Decimal("0.80"))
    min_cash_ratio: Decimal = Field(default=Decimal("0.20"), ge=Decimal("0.20"), le=1)
    max_fx_age_seconds: int = Field(default=3600, gt=0)
    max_price_age_seconds: int = Field(default=86400, gt=0)
    lot_sizes: dict[str, int] = Field(default_factory=dict)

    @field_validator("lot_sizes")
    @classmethod
    def lots(cls, values: dict[str, int]) -> dict[str, int]:
        if any(value <= 0 for value in values.values()):
            raise ValueError("lot sizes must be positive integers")
        return values

    def lot(self, instrument: Instrument) -> int:
        return self.lot_sizes.get(instrument.symbol, 100 if instrument.market == "JP" else 1)


class FXRate(Contract):
    usd_jpy: Decimal = Field(gt=0)
    timestamp: datetime
    source: Literal["fixed-simulation", "observed"]
    _timestamp = field_validator("timestamp")(utc)


class Mark(Contract):
    symbol: str
    currency: Currency
    price: Decimal = Field(gt=0)
    available_at: datetime
    price_at: datetime | None = None
    _timestamp = field_validator("available_at")(utc)

    @field_validator("price_at")
    @classmethod
    def price_time(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value is not None else None


class Position(Contract):
    instrument: Instrument
    quantity: int = Field(ge=0, strict=True)
    average_cost: Decimal = Field(ge=0)
    cost_basis: Decimal = Field(default=ZERO, ge=0)


class Account(Contract):
    account_id: Literal["paper"] = "paper"
    created_at: datetime
    last_event_at: datetime | None = None
    base_currency: Literal["JPY"] = "JPY"
    initial_jpy: Decimal = Decimal("1000000")
    balances: dict[Currency, Decimal] = Field(default={"JPY": Decimal("1000000"), "USD": ZERO})
    positions: dict[str, Position] = Field(default_factory=dict)
    realized_pnl: dict[Currency, Decimal] = Field(default={"JPY": ZERO, "USD": ZERO})
    _timestamp = field_validator("created_at")(utc)

    @field_validator("last_event_at")
    @classmethod
    def last_time(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value is not None else None

    @model_validator(mode="after")
    def valid_balances(self) -> "Account":
        if set(self.balances) != {"JPY", "USD"} or any(v < 0 for v in self.balances.values()):
            raise ValueError("JPY/USD cash balances must be present and nonnegative")
        if set(self.realized_pnl) != {"JPY", "USD"}:
            raise ValueError("native P&L currencies must be explicit")
        return self


class Order(Contract):
    order_id: str = Field(min_length=1, max_length=128)
    symbol: str
    market: Literal["JP", "US"]
    currency: Currency
    side: Side
    quantity: int = Field(gt=0, strict=True)
    order_type: Literal["MARKET"] = "MARKET"
    requested_at: datetime
    status: Status = "PENDING"
    reason: str | None = None
    costs: TradingRules
    _timestamp = field_validator("requested_at")(utc)


class Execution(Contract):
    execution_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: int
    currency: Currency
    reference_price: Decimal
    price: Decimal
    commission: Decimal
    slippage_cost: Decimal
    rounding_cost: Decimal
    notional: Decimal
    realized_pnl: Decimal
    price_at: datetime
    observed_at: datetime
    price_fetched_at: datetime
    processed_at: datetime
    mode: Literal["historical", "paper"]
    _times = field_validator("price_at", "observed_at", "price_fetched_at", "processed_at")(utc)


class FXExchange(Contract):
    exchange_id: str
    source_currency: Currency
    destination_currency: Currency
    source_amount: Decimal
    received_amount: Decimal
    cost_in_source: Decimal
    usd_jpy: Decimal
    rate_timestamp: datetime
    rate_source: Literal["fixed-simulation", "observed"]
    requested_at: datetime
    _times = field_validator("rate_timestamp", "requested_at")(utc)


class CashEntry(Contract):
    entry_id: str
    event_id: str
    currency: Currency
    amount: Decimal
    balance_after: Decimal
    kind: Literal["INITIAL", "BUY", "SELL", "FX"]
    timestamp: datetime
    _timestamp = field_validator("timestamp")(utc)


class PositionValue(Contract):
    symbol: str
    currency: Currency
    quantity: int
    price: Decimal
    market_value: Decimal
    market_value_jpy: Decimal
    unrealized_pnl: Decimal
    ratio: Decimal


class Valuation(Contract):
    valuation_id: str
    as_of: datetime
    total_jpy: Decimal
    cash_jpy: Decimal
    stocks_jpy: Decimal
    stock_ratio: Decimal
    cash_ratio: Decimal
    positions: list[PositionValue]
    realized_pnl: dict[Currency, Decimal]
    breaches: list[str]
    marks: list[Mark]
    fx: FXRate | None
    _timestamp = field_validator("as_of")(utc)
