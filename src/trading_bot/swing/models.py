from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from trading_bot.config import Instrument
from trading_bot.portfolio.models import Contract, utc

Mode = Literal["A", "B", "C"]


class SwingSettings(Contract):
    enabled: Literal[False] = False
    legacy_hourly_enabled: Literal[False] = False
    universe_size: int = Field(default=2000, ge=20, le=10000)
    stage_one: int = Field(default=50, ge=20)
    stage_two: int = Field(default=15, ge=10, le=20)
    sector_limit: int = Field(default=3, ge=1)
    min_price: float = Field(default=5, gt=0)
    min_turnover: float = Field(default=1000000, gt=0)
    max_volatility: float = Field(default=0.8, gt=0)
    drawdown_trigger: float = Field(default=0.05, gt=0, lt=1)
    market_trigger: float = Field(default=0.03, gt=0, lt=1)
    atr_multiple: float = Field(default=2, gt=0)
    min_holding_sessions: int = Field(default=5, ge=0)
    expected_holding_sessions: int = Field(default=20, ge=1)
    monitor_response: Literal["hold", "sell", "freeze"] = "freeze"
    daily_only: bool = False

    @model_validator(mode="after")
    def consistent(self) -> "SwingSettings":
        if self.stage_one < self.stage_two:
            raise ValueError("stage one must contain stage two")
        return self


class Security(Contract):
    instrument: Instrument
    sector: str
    kind: Literal["common", "ETF", "ADR", "preferred", "special"] = "common"
    listed: date
    metadata_at: datetime
    delisted: date | None = None
    _timestamp = field_validator("metadata_at")(utc)


class MarketEvent(Contract):
    symbol: str
    kind: Literal["split", "dividend", "delisting", "halt"]
    effective_at: datetime
    observed_at: datetime
    _times = field_validator("effective_at", "observed_at")(utc)


class DataRights(Contract):
    kind: Literal["synthetic", "licensed"] = "synthetic"
    external_ai: bool = False
    public_performance: bool = False
    intraday: bool = True
    point_in_time_universe: bool = True
    source: str = "deterministic synthetic generator"


class ScreenSummary(Contract):
    first_stage: list[str]
    excluded: dict[str, str]
    new_buys_allowed: bool
    issues: list[str]
