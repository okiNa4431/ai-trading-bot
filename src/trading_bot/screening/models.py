from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from trading_bot.config import Instrument, Market
from trading_bot.portfolio.models import Contract, Mark


class Weights(Contract):
    trend: float = Field(default=0.25, ge=0)
    momentum: float = Field(default=0.20, ge=0)
    volume: float = Field(default=0.15, ge=0)
    liquidity: float = Field(default=0.15, ge=0)
    volatility: float = Field(default=0.15, ge=0)
    quality: float = Field(default=0.10, ge=0)

    @model_validator(mode="after")
    def nonzero(self) -> "Weights":
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("at least one screening weight must be positive")
        return self


class ScreeningSettings(Contract):
    top_k: int = Field(default=5, ge=5, le=10)
    max_hourly_age_seconds: int = Field(default=86400, gt=0)
    max_daily_age_seconds: int = Field(default=604800, gt=0)
    max_price_jump: float = Field(default=0.35, gt=0, lt=1)
    liquidity_jpy: float = Field(default=100000000, gt=0)
    liquidity_usd: float = Field(default=10000000, gt=0)
    weights: Weights = Weights()


class Features(Contract):
    symbol: str
    hourly_return: float
    daily_return: float
    sma5: float
    sma20: float
    rsi14: float
    atr14: float
    relative_volume: float
    volatility20: float
    average_turnover20: float
    daily_close: float
    daily_observed_at: datetime
    hourly_observed_at: datetime
    fetched_at: datetime
    source: Literal["yahoo"] = "yahoo"
    mark: Mark


class Candidate(Contract):
    instrument: Instrument
    held: bool
    features: Features | None
    score: float | None
    components: dict[str, float] = Field(default_factory=dict)
    issue: str | None = None


class ScreeningResult(Contract):
    market: Market
    as_of: datetime
    analyzed: int
    candidates: list[Candidate]
    excluded: dict[str, str]
