"""Validated YAML configuration; paths are relative to the settings file's parent project."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Market = Literal["JP", "US"]
Interval = Literal["1d", "1h"]


class Instrument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.\-^=]*$")
    market: Market
    currency: Literal["JPY", "USD"]
    display_name: str = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self) -> "Instrument":
        if self.market == "JP":
            if not self.symbol.endswith(".T") or self.currency != "JPY":
                raise ValueError("JP requires a .T symbol and JPY")
        elif self.symbol.endswith(".T") or self.currency != "USD":
            raise ValueError("US requires a US symbol and USD")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    provider: Literal["yahoo"] = "yahoo"
    intervals: list[Interval] = Field(default=["1d", "1h"], min_length=1)
    daily_days: int = Field(default=730, ge=1, le=730)
    hourly_days: int = Field(default=30, ge=1, le=60)
    storage_dir: Path = Path("data/market")
    http_timeout: float = Field(default=20, gt=0, le=120)
    retries: int = Field(default=2, ge=0, le=5)
    request_interval: float = Field(default=1, ge=0)
    overlap_bars: int = Field(default=5, ge=1, le=100)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def unique_intervals(self) -> "Settings":
        if len(set(self.intervals)) != len(self.intervals):
            raise ValueError("duplicate intervals")
        return self


def load_settings(path: Path) -> Settings:
    settings = Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if not settings.storage_dir.is_absolute():
        settings.storage_dir = path.resolve().parent.parent / settings.storage_dir
    return settings


def load_universe(path: Path) -> list[Instrument]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("universe must be a nonempty list")
    instruments = [Instrument.model_validate(item) for item in raw]
    if len({i.symbol for i in instruments}) != len(instruments):
        raise ValueError("duplicate universe symbol")
    return instruments
