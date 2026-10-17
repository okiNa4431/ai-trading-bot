"""Offline US scheduling and budget proposal; never connected to trading or API clients."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trading_bot.broker.execution import session


class USHourlyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["mock"] = "mock"
    market: Literal["US"] = "US"
    symbols: tuple[str, ...] = ("AAPL", "MSFT", "AMZN", "GOOGL", "META")
    poll_minutes: Literal[5, 15, 60] = 15
    decision_minutes: Literal[60] = 60
    latency_minutes: int = Field(default=15, ge=0, le=60)

    @field_validator("symbols")
    @classmethod
    def valid_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if not 5 <= len(value) <= 10 or len(set(value)) != len(value):
            raise ValueError("Require 5-10 distinct US symbols")
        if any(not re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", s) for s in value):
            raise ValueError("Invalid US symbol syntax (listing still requires verification)")
        return value


@dataclass(frozen=True)
class PlannedSlot:
    run_id: str
    bar_end: datetime
    due_at: datetime
    kind: Literal["collect", "decision"]


def slots(plan: USHourlyPlan, day: str) -> tuple[PlannedSlot, ...]:
    """Session-anchored full intervals only; no partial close bar or catch-up execution."""
    times = session("US", day)
    if not times:
        return ()
    opening, closing = times["market_open"], times["market_close"]
    result = []
    intervals: tuple[tuple[Literal["collect", "decision"], int], ...] = (
        ("collect", plan.poll_minutes),
        ("decision", 60),
    )
    for kind, minutes in intervals:
        end = opening + timedelta(minutes=minutes)
        while end <= closing:
            result.append(
                PlannedSlot(
                    f"US:{day}:{kind}:{end:%H%MZ}",
                    end,
                    end + timedelta(minutes=plan.latency_minutes),
                    kind,
                )
            )
            end += timedelta(minutes=minutes)
    return tuple(sorted(result, key=lambda s: (s.due_at, s.kind)))


@dataclass(frozen=True)
class Budget:
    collection_slots: int
    decision_requests: int
    price_fx_credits: int
    warmup_credits: int
    reserve_credits: int
    total_credits: int
    minute_batches: tuple[int, ...]

    @property
    def fits_basic_daily(self) -> bool:
        return self.total_credits <= 800


def budget(plan: USHourlyPlan, day: str, reserve_credits: int = 100) -> Budget:
    """One time_series credit/symbol; daily history once, two extra polls, no CA entitlement.

    Reserve is a planning allowance, NOT an endpoint weight or permission. Cold-start
    pagination, corrections, other consumers and CA requests need their own accounting.
    """
    if reserve_credits < 0:
        raise ValueError("Negative reserve")
    schedule = slots(plan, day)
    polls = sum(s.kind == "collect" for s in schedule)
    decisions = sum(s.kind == "decision" for s in schedule)
    if not schedule:
        return Budget(0, 0, 0, 0, 0, 0, ())
    count = len(plan.symbols)
    price_fx = (polls + 2) * (count + 1)
    batches = []
    remaining = count + 1
    while remaining:
        batch = min(7, remaining)  # Leave one credit/minute headroom under Basic's eight.
        batches.append(batch)
        remaining -= batch
    return Budget(
        polls,
        decisions,
        price_fx,
        count,
        reserve_credits,
        price_fx + count + reserve_credits,
        tuple(batches),
    )


class FeatureProposal(BaseModel):
    """Mock-only minimized external DTO; this schema is NOT proof of data-use rights."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    symbol: str
    return_1h: float = Field(ge=-1, le=10)
    distance_sma20: float = Field(ge=-1, le=10)
    volatility_ratio: float = Field(ge=0, le=10)
    portfolio_weight: float = Field(ge=0, le=1)


def hourly_features(
    ends: tuple[datetime, ...],
    closes: tuple[float, ...],
    hour_end: datetime,
    observed_at: datetime,
) -> float:
    """Synthetic 15m grid completeness check, not a production PriceBar adapter."""
    import math

    times = (*ends, hour_end, observed_at)
    if any(t.utcoffset() != timedelta(0) for t in times):
        raise ValueError("UTC required")
    expected = tuple(hour_end - timedelta(minutes=n) for n in (45, 30, 15, 0))
    if ends != expected or len(closes) != 4 or observed_at < hour_end:
        raise ValueError("Incomplete, duplicate, unordered or future hourly input")
    if any(not math.isfinite(p) or p <= 0 for p in closes):
        raise ValueError("Invalid synthetic close")
    return closes[-1]
