"""Raw, validated price bars. Invalid observations are never silently repaired."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_bot.config import Instrument, Interval, Market


class PriceBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    symbol: str
    market: Market
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    currency: Literal["JPY", "USD"]
    interval: Interval
    source: Literal["yahoo", "synthetic", "alpaca"] = "yahoo"
    fetched_at: datetime
    adjusted: Literal[False] = False

    @field_validator("timestamp", "fetched_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent(self) -> "PriceBar":
        Instrument(
            symbol=self.symbol,
            market=self.market,
            currency=self.currency,
            display_name=self.symbol,
        )
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below OHLC")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above OHLC")
        return self
