"""JP-1: generated inputs only; no provider, AI client, secrets or production storage."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import numpy as np
import pandas_market_calendars as mcal
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_bot.broker.execution import known_marks, window
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Instrument
from trading_bot.data_access import reject_diagnostic
from trading_bot.models import PriceBar
from trading_bot.orchestration.order_planner import estimate
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import TradingRules, utc
from trading_bot.portfolio.valuation import value_portfolio
from trading_bot.security_quality import (
    QualityAssessment,
    ReviewFact,
    SecurityQuality,
    SecurityQualityStop,
)
from trading_bot.synthetic_mvp import MVPSettings, signal

D = Decimal
INITIAL = D("1000000")


class JapanSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    data_kind: Literal["SYNTHETIC"] = "SYNTHETIC"
    symbols: int = Field(default=20, ge=20, le=2000)
    sessions: int = Field(default=221, ge=90, le=500)
    seed: int = 2101
    selection_count: int = Field(default=10, ge=1, le=30)
    rules: TradingRules = TradingRules(commission_jpy=D(55))
    timing: MVPSettings = MVPSettings()


class FinancialScore(BaseModel):
    """Mock of the future financial-evaluation response, never an order instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    data_kind: Literal["SYNTHETIC"] = "SYNTHETIC"
    provider: Literal["gemini-mock"] = "gemini-mock"
    security_id: str = Field(pattern=r"^synthetic:jp:\d{4}$")
    score: float = Field(ge=0, le=100)
    growth: float = Field(ge=0, le=100)
    profitability: float = Field(ge=0, le=100)
    financial_health: float = Field(ge=0, le=100)
    moat: float = Field(ge=0, le=100)
    risk: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: tuple[str, ...] = Field(min_length=1)
    published_at: datetime
    retrieved_at: datetime
    as_of: datetime
    _times = field_validator("published_at", "retrieved_at", "as_of")(utc)

    @model_validator(mode="after")
    def point_in_time(self) -> "FinancialScore":
        if not self.published_at <= self.retrieved_at <= self.as_of or any(
            not e.startswith("synthetic://") for e in self.evidence
        ):
            raise ValueError("synthetic point-in-time evidence required")
        return self


@dataclass(frozen=True)
class SyntheticNAV:
    """A simulated fund contract, deliberately incompatible with official NAV."""

    nav_date: date
    per_10000_jpy: Decimal
    published_at: datetime
    data_kind: Literal["SYNTHETIC"] = "SYNTHETIC"

    def __post_init__(self) -> None:
        if (
            self.data_kind != "SYNTHETIC"
            or not self.per_10000_jpy.is_finite()
            or self.per_10000_jpy <= 0
            or utc(self.published_at).date() < self.nav_date
        ):
            raise ValueError("invalid synthetic NAV")


@dataclass(frozen=True)
class JapanFixture:
    instruments: tuple[Instrument, ...]
    opens: tuple[datetime, ...]
    closes: tuple[datetime, ...]
    rows: dict[str, tuple[PriceBar, ...]]
    financials: tuple[FinancialScore, ...]
    navs: tuple[SyntheticNAV, ...]
    data_kind: Literal["SYNTHETIC"] = "SYNTHETIC"


def generate(settings: JapanSettings) -> JapanFixture:
    """Fictitious JP symbols and finances; no US market input or real company mapping."""
    schedule = mcal.get_calendar("JPX").schedule("2024-01-04", "2026-12-30")
    opens = tuple(
        t.to_pydatetime().astimezone(UTC) for t in schedule.market_open[: settings.sessions]
    )
    closes = tuple(
        t.to_pydatetime().astimezone(UTC) for t in schedule.market_close[: settings.sessions]
    )
    rng = np.random.default_rng(settings.seed)
    instruments, financials, rows = [], [], {}
    published = opens[0] - timedelta(days=1)
    for i in range(settings.symbols):
        symbol = f"JPTEST{i:04d}.T"
        instrument = Instrument(
            symbol=symbol, market="JP", currency="JPY", display_name=f"SYNTHETIC 日本株 {i:04d}"
        )
        instruments.append(instrument)
        factors = rng.uniform(20, 95, 4)
        financials.append(
            FinancialScore(
                security_id=f"synthetic:jp:{i:04d}",
                score=float(np.mean(factors)),
                growth=float(factors[0]),
                profitability=float(factors[1]),
                financial_health=float(factors[2]),
                moat=float(factors[3]),
                risk="架空の事業リスク。実企業の評価ではない。",
                reason="合成財務4因子の単純平均によるmock評価",
                evidence=(f"synthetic://financials/{i:04d}/v1",),
                published_at=published,
                retrieved_at=published,
                as_of=published,
            )
        )
        prices = (320 + i % 15 * 15) * np.exp(
            np.cumsum(rng.normal(0.001, 0.013, settings.sessions))
        )
        bars = []
        for day, closing in enumerate(prices):
            opening = float(prices[max(0, day - 1)] * (1 + rng.normal(0, 0.002)))
            bars.append(
                PriceBar(
                    symbol=symbol,
                    market="JP",
                    currency="JPY",
                    interval="1d",
                    source="synthetic",
                    timestamp=opens[day],
                    fetched_at=closes[day] + timedelta(minutes=1),
                    open=opening,
                    close=float(closing),
                    high=max(opening, closing) * 1.002,
                    low=min(opening, closing) * 0.998,
                    volume=100000,
                )
            )
        rows[symbol] = tuple(bars)
    navs = tuple(
        SyntheticNAV(
            opening.date(),
            D(str(10000 * np.exp(0.0003 * day + 0.012 * np.sin(day / 17)))),
            closes[day] + timedelta(minutes=1),
        )
        for day, opening in enumerate(opens)
    )
    return JapanFixture(tuple(instruments), opens, closes, rows, tuple(financials), navs)


def validate_scope(data: JapanFixture, settings: JapanSettings) -> None:
    reject_diagnostic(data)
    if type(data) is not JapanFixture or data.data_kind != "SYNTHETIC":
        raise ValueError("generated Japanese fixture required")
    expected = {f"JPTEST{i:04d}.T" for i in range(settings.symbols)}
    if (
        set(data.rows) != expected
        or {i.symbol for i in data.instruments} != expected
        or len(data.instruments) != settings.symbols
        or len(data.opens) != settings.sessions
        or len(data.closes) != settings.sessions
        or len(data.financials) != settings.symbols
        or len(data.navs) != settings.sessions
        or {f.security_id for f in data.financials}
        != {f"synthetic:jp:{i:04d}" for i in range(settings.symbols)}
    ):
        raise ValueError("fixture scope mismatch")
    for instrument in data.instruments:
        if instrument.market != "JP" or instrument.currency != "JPY":
            raise ValueError("JP/JPY fixture required")
        ordered = data.rows[instrument.symbol]
        if any(a.timestamp > b.timestamp for a, b in pairwise(ordered)):
            raise ValueError("out-of-order observations prohibited")
        for bar in data.rows[instrument.symbol]:
            if (
                bar.source != "synthetic"
                or bar.market != "JP"
                or bar.currency != "JPY"
                or bar.symbol != instrument.symbol
            ):
                raise ValueError("real or incorrectly attributed input prohibited")
    for i, opening in enumerate(data.opens):
        # Verify the supplied fixture calendar, not only monotonic timestamps.
        from trading_bot.broker.execution import session

        actual = session("JP", opening.date().isoformat())
        if (
            not actual
            or actual["market_open"] != opening
            or actual["market_close"] != data.closes[i]
        ):
            raise ValueError("JPX session mismatch")
        if i and opening <= data.opens[i - 1]:
            raise ValueError("duplicate or reversed session")
        nav = data.navs[i]
        SyntheticNAV(nav.nav_date, nav.per_10000_jpy, nav.published_at, nav.data_kind)
        if nav.nav_date != opening.date() or not (
            data.closes[i] <= nav.published_at <= data.closes[i] + timedelta(minutes=2)
        ):
            raise ValueError("missing or unavailable NAV")
    for index, score in enumerate(data.financials):
        FinancialScore.model_validate(score.model_dump())
        if score.security_id != f"synthetic:jp:{index:04d}":
            raise ValueError("ambiguous financial identity")


def quality(data: JapanFixture, day: int, at: datetime, warmup: int) -> QualityAssessment:
    population = []
    first = max(0, day - warmup + 1)
    sessions = dict(zip(data.opens, data.closes, strict=True))
    for i, instrument in enumerate(data.instruments):
        rows = data.rows[instrument.symbol]
        reasons = []
        prefix = [b for b in rows if b.timestamp <= data.opens[day]]
        if len(prefix) != day + 1 or len({b.timestamp for b in prefix}) != len(prefix):
            reasons.append("missing_or_duplicate")
        for n, bar in enumerate(prefix):
            PriceBar.model_validate(bar.model_dump())
            if (
                bar.source != "synthetic"
                or bar.symbol != instrument.symbol
                or bar.market != "JP"
                or bar.currency != "JPY"
                or bar.interval != "1d"
                or bar.timestamp not in sessions
                or window(bar) != (bar.timestamp, sessions[bar.timestamp])
                or not sessions[bar.timestamp] <= bar.fetched_at <= at
            ):
                raise ValueError("non-synthetic, unconfirmed or mismatched bar")
            if n and abs(bar.close / prefix[n - 1].close - 1) > 0.35:
                reasons.append("discontinuity")
        score = data.financials[i]
        fact = ReviewFact(
            status="PASSED",
            observed_at=score.retrieved_at,
            coverage_start=data.opens[0].date(),
            coverage_end=data.opens[-1].date(),
            sources=("synthetic://jp-generator/identity-and-no-corporate-actions",),
        )
        reviewed = fact.at(
            at, tuple(t.date() for t in data.opens[first : day + 1]), synthetic_contract=True
        )
        if reviewed != "PASSED" or score.as_of > at:
            reasons.append("unknown_evidence")
        if len(prefix) < warmup:
            reasons.append("warmup")
        eligible: Literal["ELIGIBLE", "QUARANTINED", "UNKNOWN"] = "ELIGIBLE"
        if reasons:
            eligible = (
                "UNKNOWN" if set(reasons) <= {"warmup", "unknown_evidence"} else "QUARANTINED"
            )
        population.append(
            SecurityQuality(
                score.security_id,
                instrument.symbol,
                at,
                "PASSED" if not reasons else "FAILED",
                reviewed,
                reviewed,
                "SYNTHETIC_ONLY",
                eligible,
                tuple(sorted(set(reasons))),
                (score.retrieved_at,),
            )
        )
    return QualityAssessment(at, "synthetic", tuple(population))


def benchmark_value(nav: SyntheticNAV, units: int, residual: Decimal, at: datetime) -> Decimal:
    if nav.published_at > at:
        raise ValueError("future NAV cannot be used")
    return residual + D(units) * nav.per_10000_jpy / 10000


def replay(
    data: JapanFixture, settings: JapanSettings, repository: SQLiteRepository
) -> dict[str, object]:
    validate_scope(data, settings)
    if type(repository) is not SQLiteRepository:
        raise ValueError("local synthetic repository only")
    with repository.transaction() as tx:
        if tx.initialized():
            raise ValueError("new isolated synthetic account required")
    broker = PaperBroker(repository, settings.rules)
    start = settings.timing.slow_sma
    if start + 1 >= settings.sessions:
        raise ValueError("insufficient simulation sessions")
    began = data.closes[start] + timedelta(minutes=2)
    broker.initialize(began)
    selected = {
        f.security_id
        for f in sorted(data.financials, key=lambda f: (-f.score, f.security_id))[
            : settings.selection_count
        ]
        if f.retrieved_at <= began and f.as_of <= began
    }
    by_symbol = {i.symbol: i for i in data.instruments}
    ages: dict[str, int] = {}
    peaks: dict[str, float] = {}
    daily, decisions = [], []
    units, residual = 0, INITIAL
    status, stop_reason = "COMPLETE", None
    maximum, drawdown = INITIAL, D(0)
    final_quality = quality(data, start, began, settings.timing.slow_sma)
    try:
        for day in range(start, settings.sessions):
            at = data.closes[day] + timedelta(minutes=2)
            current_rows = {}
            for symbol, observations in data.rows.items():
                current_bars = [b for b in observations if b.timestamp == data.opens[day]]
                if len(current_bars) != 1:
                    final_quality = quality(data, day, at, settings.timing.slow_sma)
                    raise SecurityQualityStop("missing or duplicated current session")
                bar = PriceBar.model_validate(current_bars[0].model_dump())
                if not data.closes[day] <= bar.fetched_at <= at:
                    raise SecurityQualityStop("unconfirmed current session")
                current_rows[symbol] = [bar]
            if day > start:
                # Approve using only yesterday's history before today's OPEN.
                q_open = quality(data, day - 1, data.opens[day], settings.timing.slow_sma)
                for p in broker.get_positions():
                    q_open.require(p.instrument.symbol)
                for order in broker.get_orders():
                    if order.status != "PENDING":
                        continue
                    q_open.require(order.symbol)
                    current = current_rows[order.symbol][0]
                    if current.source != "synthetic":
                        raise ValueError("real observation prohibited")
                    result = broker.execute(
                        order.order_id,
                        current_rows,
                        None,
                        at,
                    )
                    if result.status == "FILLED" and order.side == "BUY":
                        ages[order.symbol] = day
                        peaks[order.symbol] = current.open
                    elif result.status == "FILLED" and order.side == "SELL":
                        ages.pop(order.symbol, None)
                        peaks.pop(order.symbol, None)
            q = quality(data, day, at, settings.timing.slow_sma)
            final_quality = q
            # JP-1 fail closed: no forced sale or fabricated mark for unsafe holdings.
            for instrument in data.instruments:
                q.require(instrument.symbol)
            marks = known_marks(list(data.instruments), current_rows, at, "historical")
            valuation = broker.value(marks, None, at)
            maximum = max(maximum, valuation.total_jpy)
            drawdown = min(drawdown, valuation.total_jpy / maximum - 1)
            if day == start + 1:
                # Mock contract: application at start close, next JPX day's NAV.
                nav = data.navs[day]
                units = int(
                    (INITIAL * 10000 / nav.per_10000_jpy).to_integral_value(rounding=ROUND_DOWN)
                )
                residual = INITIAL - D(units) * nav.per_10000_jpy / 10000
            nav_value = (
                INITIAL if not units else benchmark_value(data.navs[day], units, residual, at)
            )
            daily.append(
                dict(
                    data_kind="SYNTHETIC",
                    at=at.isoformat(),
                    ai_jpy=str(valuation.total_jpy),
                    nav_jpy=str(nav_value),
                    cash_ratio=str(valuation.cash_ratio),
                    holdings=len(broker.get_positions()),
                )
            )
            planned = broker.get_balance()
            pending = [o for o in broker.get_orders() if o.status == "PENDING"]
            for order in pending:
                if order.side == "BUY":
                    planned, _, _ = estimate(
                        planned,
                        by_symbol[order.symbol],
                        "BUY",
                        order.quantity,
                        marks[order.symbol],
                        settings.rules,
                        at,
                    )
            for instrument in data.instruments:
                symbol = instrument.symbol
                if any(o.symbol == symbol for o in pending):
                    continue
                position = planned.positions.get(symbol)
                held = position is not None and position.quantity > 0
                index = next(i for i, ins in enumerate(data.instruments) if ins.symbol == symbol)
                if not held and data.financials[index].security_id not in selected:
                    continue
                history = [b for b in data.rows[symbol] if b.timestamp <= data.opens[day]]
                peaks[symbol] = max(peaks.get(symbol, 0), history[-1].close) if held else 0
                action, reason = signal(
                    [b.close for b in history],
                    [b.high for b in history],
                    settings.timing,
                    average_cost=float(position.average_cost) if held and position else None,
                    peak=peaks[symbol],
                    age=day - ages.get(symbol, day),
                )
                decisions.append(
                    dict(data_kind="SYNTHETIC", at=at.isoformat(), action=action, reason=reason)
                )
                if action == "HOLD":
                    continue
                qty = position.quantity if action == "SELL" and position else 0
                if action == "BUY":
                    if (
                        sum(p.quantity > 0 for p in planned.positions.values())
                        >= settings.timing.max_positions
                    ):
                        continue
                    budget = valuation.total_jpy * settings.timing.target_weight
                    qty = int(budget / (marks[symbol].price * 100)) * 100
                    if qty == 0:
                        continue
                    proposal, _, _ = estimate(
                        planned, instrument, "BUY", qty, marks[symbol], settings.rules, at
                    )
                    risk = value_portfolio(proposal, marks, None, at, settings.rules)
                    if risk.breaches:
                        continue
                    planned = proposal
                broker.place_order(
                    instrument, action, qty, at, f"jp-synthetic:{day}:{symbol}:{action}"
                )
    except (ValueError, SecurityQualityStop) as exc:
        status, stop_reason = "SAFE_STOP", type(exc).__name__
    fills = broker.get_executions()
    sells = [f for f in fills if f.side == "SELL"]
    last = daily[-1] if daily else None
    final_ai = D(str(last["ai_jpy"])) if last and status == "COMPLETE" else None
    final_nav = D(str(last["nav_jpy"])) if last and status == "COMPLETE" else None
    return dict(
        data_kind="SYNTHETIC",
        provider="gemini-mock",
        market="JP",
        currency="JPY",
        status=status,
        stop_reason=stop_reason,
        real_api_calls=0,
        real_market_approved=False,
        initial_jpy=str(INITIAL),
        security_count=settings.symbols,
        lot_size=100,
        started_at=began.isoformat(),
        nav_application_date=data.opens[start + 1].date().isoformat(),
        nav_units=units,
        nav_residual_jpy=str(residual),
        nav_contract="SYNTHETIC_NEXT_JPX_SESSION",
        current_assets_jpy=str(final_ai) if final_ai is not None else None,
        cumulative_return=str(final_ai / INITIAL - 1) if final_ai is not None else None,
        benchmark_jpy=str(final_nav) if final_nav is not None else None,
        benchmark_return=str(final_nav / INITIAL - 1) if final_nav is not None else None,
        excess_return=str((final_ai - final_nav) / INITIAL)
        if final_ai is not None and final_nav is not None
        else None,
        maximum_drawdown=str(drawdown),
        trade_count=len(fills),
        closed_trades=len(sells),
        win_rate=str(D(sum(f.realized_pnl > 0 for f in sells)) / len(sells)) if sells else None,
        cash_ratio=last["cash_ratio"] if last and status == "COMPLETE" else None,
        holdings=last["holdings"] if last and status == "COMPLETE" else None,
        commission_jpy=str(sum((f.commission for f in fills), D(0))),
        slippage_jpy=str(sum((f.slippage_cost for f in fills), D(0))),
        pending_orders=sum(o.status == "PENDING" for o in broker.get_orders()),
        quality=final_quality.summary(),
        daily=daily,
        decision_count=len(decisions),
        limitations=["合成データの動作検証。AI能力・実市場の運用実績ではない", "B/C比較は未実施"],
    )


def run(settings: JapanSettings | None = None) -> dict[str, object]:
    settings = settings or JapanSettings()
    with TemporaryDirectory(prefix="test-jp-synthetic-") as temporary:
        return replay(
            generate(settings), settings, SQLiteRepository(Path(temporary) / "synthetic.sqlite")
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="SYNTHETIC | %(message)s")
    settings = JapanSettings.model_validate(
        yaml.safe_load(Path("config/japan-synthetic.yaml").read_text(encoding="utf-8"))
    )
    result = run(settings)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "COMPLETE":
        raise SystemExit(1)
