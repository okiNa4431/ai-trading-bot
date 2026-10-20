"""Explicit synthetic-only replay. No provider, AI, environment secrets or remote DB."""

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_bot.alpaca import DailyBatch
from trading_bot.broker.execution import eligible_bar, known_marks
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Instrument
from trading_bot.data_access import reject_diagnostic
from trading_bot.models import PriceBar
from trading_bot.orchestration.order_planner import estimate
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import (
    Execution,
    FXExchange,
    FXRate,
    Order,
    Side,
    TradingRules,
    Valuation,
)
from trading_bot.portfolio.valuation import value_portfolio
from trading_bot.security_identity import TickerAssignment
from trading_bot.security_quality import (
    BatchQualityStop,
    QualityAssessment,
    QualityHistory,
    QualityUniverse,
    ReviewFact,
    SecurityEvidence,
    SecurityQualityStop,
    evaluate_quality,
)
from trading_bot.swing.data import SwingData, synthetic
from trading_bot.swing.metrics import metrics


class MVPSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbols: int = Field(default=100, ge=20, le=100)
    sessions: int = Field(default=221, ge=202, le=500)  # Generator's minimum includes warmup.
    seed: int = 551
    fast_sma: int = Field(default=20, ge=2)
    slow_sma: int = Field(default=60, ge=3)
    breakout_sessions: int = Field(default=20, ge=2)
    stop_loss: float = Field(default=0.08, gt=0, lt=1)
    trailing_stop: float = Field(default=0.12, gt=0, lt=1)
    max_holding_sessions: int = Field(default=30, ge=1)
    target_weight: Decimal = Field(default=Decimal(".09"), gt=0, le=Decimal(".10"))
    max_positions: int = Field(default=8, ge=1, le=8)
    review_fixtures: bool = True
    rules: TradingRules = TradingRules(commission_usd=Decimal(1))

    @model_validator(mode="after")
    def periods(self) -> "MVPSettings":
        if (
            not self.fast_sma < self.slow_sma < self.sessions
            or self.breakout_sessions >= self.slow_sma
        ):
            raise ValueError("invalid warmup/signal periods")
        return self


def signal(
    closes: list[float],
    highs: list[float],
    settings: MVPSettings,
    *,
    average_cost: float | None = None,
    peak: float = 0,
    age: int = 0,
) -> tuple[Literal["BUY", "SELL", "HOLD"], str]:
    if len(closes) < settings.slow_sma or len(closes) != len(highs):
        return "HOLD", "warmup"
    price = closes[-1]
    if average_cost is not None:
        if price <= average_cost * (1 - settings.stop_loss):
            return "SELL", "stop_loss"
        if price <= peak * (1 - settings.trailing_stop):
            return "SELL", "trailing_stop"
        # The entry session is session one. Decide at the 30th close so that
        # the next eligible OPEN exits after 30 completed holding sessions.
        if age + 1 >= settings.max_holding_sessions:
            return "SELL", "max_holding_sessions"
        return "HOLD", "held"
    fast = sum(closes[-settings.fast_sma :]) / settings.fast_sma
    slow = sum(closes[-settings.slow_sma :]) / settings.slow_sma
    if fast > slow and price > max(highs[-settings.breakout_sessions - 1 : -1]):
        return "BUY", "trend_breakout"
    return "HOLD", "no_entry"


class SyntheticQualityBroker:
    """A local wrapper; never installs an alternative path in the production broker."""

    def __init__(self, broker: PaperBroker, quality: QualityAssessment):
        if type(broker.repository) is not SQLiteRepository or quality.origin != "synthetic":
            raise SecurityQualityStop("isolated synthetic SQLite broker required")
        self.broker, self.quality = broker, quality

    def update(self, quality: QualityAssessment) -> None:
        if quality.origin != "synthetic" or quality.as_of < self.quality.as_of:
            raise SecurityQualityStop("future research cannot be replayed backwards")
        self.quality = quality

    def protected(self) -> None:
        for p in self.broker.get_positions():
            self.quality.require(p.instrument.symbol)

    def place(
        self, instrument: Instrument, side: Side, quantity: int, at: datetime, key: str
    ) -> Order:
        reject_diagnostic(instrument)
        self.protected()
        self.quality.require(instrument.symbol)
        if at != self.quality.as_of:
            raise SecurityQualityStop("order must use current point-in-time approval")
        if side not in ("BUY", "SELL"):
            raise ValueError("unknown synthetic side")
        return self.broker.place_order(instrument, side, quantity, at, key)

    def execute(self, key: str, data: dict[str, list[PriceBar]], fx: FXRate, at: datetime) -> Order:
        reject_diagnostic(data)
        self.protected()
        order = self.broker.get_order(key)
        self.quality.require(order.symbol)
        if (
            fx.source != "fixed-simulation"
            or self.quality.as_of > at
            or any(
                b.source != "synthetic" or b.fetched_at > at for rows in data.values() for b in rows
            )
        ):
            raise SecurityQualityStop("market or future observations forbidden")
        allowed = {
            s.ticker for s in self.quality.population if s.strategy_eligibility == "ELIGIBLE"
        }
        selected = {s: bars for s, bars in data.items() if s in allowed}
        if set(data) - {s.ticker for s in self.quality.population} or any(
            b.symbol != symbol for symbol, bars in data.items() for b in bars
        ):
            raise SecurityQualityStop("unexpected synthetic scope")
        if order.status == "PENDING":
            instrument = Instrument(
                symbol=order.symbol,
                market=order.market,
                currency=order.currency,
                display_name=order.symbol,
            )
            candidate = eligible_bar(
                order, instrument, selected.get(order.symbol, []), at, "historical", order.costs
            )
            if candidate and self.quality.as_of > candidate[1]:
                raise SecurityQualityStop("later quality cannot authorize a historical fill")
        return self.broker.execute(key, selected, fx, at)

    def value(self, data: dict[str, list[PriceBar]], fx: FXRate, at: datetime) -> Valuation:
        reject_diagnostic(data)
        self.protected()
        if at != self.quality.as_of or fx.source != "fixed-simulation":
            raise SecurityQualityStop("valuation requires current synthetic quality/FX")
        if any(
            b.source != "synthetic" or b.fetched_at > at for rows in data.values() for b in rows
        ):
            raise SecurityQualityStop("market or future valuation inputs forbidden")
        instruments = [p.instrument for p in self.broker.get_positions()]
        if set(data) - {s.ticker for s in self.quality.population} or any(
            b.symbol != symbol for symbol, bars in data.items() for b in bars
        ):
            raise SecurityQualityStop("unexpected synthetic scope")
        allowed = {
            s.ticker for s in self.quality.population if s.strategy_eligibility == "ELIGIBLE"
        }
        marks = known_marks(
            instruments, {s: rows for s, rows in data.items() if s in allowed}, at, "historical"
        )
        return self.broker.value(marks, fx, at)


@dataclass
class SyntheticMVPResult:
    summary: dict[str, object]
    history: QualityHistory
    daily: list[dict[str, object]]
    executions: list[Execution]


def synthetic_inputs(
    data: SwingData, settings: MVPSettings
) -> tuple[QualityUniverse, dict[str, SecurityEvidence], dict[str, list[PriceBar]]]:
    if (
        type(data) is not SwingData
        or data.rights.kind != "synthetic"
        or data.rights.source != "deterministic synthetic generator"
        or [s.instrument.symbol for s in data.securities]
        != [f"S{i:04d}" for i in range(len(data.securities))]
    ):
        raise SecurityQualityStop("real data and diagnostic inputs are forbidden")
    if any(e.symbol not in {s.instrument.symbol for s in data.securities} for e in data.events):
        raise BatchQualityStop("unknown corporate-event security")
    assignments = tuple(
        TickerAssignment(
            security_id=f"synthetic:{s.instrument.symbol}",
            ticker=s.instrument.symbol,
            exchange="NYSE",
            valid_from=data.opens_at[0].date(),
            evidence="synthetic://generator-contract",
            identity_confirmed=True,
        )
        for s in data.securities
    )
    observed = data.opens_at[0] - timedelta(days=1)
    passed = ReviewFact(
        status="PASSED",
        observed_at=observed,
        coverage_start=data.opens_at[0].date(),
        coverage_end=data.closes_at[-1].date(),
        sources=("synthetic://predetermined-generator",),
    )
    evidence = {
        a.security_id: SecurityEvidence(
            identity=passed,
            corporate_actions=passed,
            permission="SYNTHETIC_ONLY",
            permission_observed_at=observed,
        )
        for a in assignments
    }
    if settings.review_fixtures:
        for index, axis, status in (
            (-1, "identity", "UNKNOWN"),
            (-2, "corporate_actions", "UNKNOWN"),
            (-3, "corporate_actions", "UNSUPPORTED"),
        ):
            sid = assignments[index].security_id
            evidence[sid] = evidence[sid].model_copy(
                update={axis: passed.model_copy(update={"status": status})}
            )
    rows = {}
    for col, s in enumerate(data.securities):
        rows[s.instrument.symbol] = [
            PriceBar(
                symbol=s.instrument.symbol,
                market="US",
                currency="USD",
                source="synthetic",
                interval="1d",
                timestamp=data.opens_at[i],
                fetched_at=data.closes_at[i] + data.latency,
                open=float(data.opening[i, col]),
                high=float(data.high[i, col]),
                low=float(data.low[i, col]),
                close=float(data.closing[i, col]),
                volume=float(data.volume[i, col]),
            )
            for i in range(len(data.opens_at))
        ]
    return QualityUniverse(assignments), evidence, rows


def replay(
    data: SwingData, settings: MVPSettings, repository: SQLiteRepository
) -> SyntheticMVPResult:
    reject_diagnostic(data)
    if len(data.opens_at) != settings.sessions or len(data.securities) != settings.symbols:
        raise BatchQualityStop("synthetic configuration and population mismatch")
    universe, evidence, rows = synthetic_inputs(data, settings)
    broker = PaperBroker(repository, settings.rules)
    if repository.path.exists():
        with broker.repository.transaction() as tx:
            if tx.initialized():
                raise SecurityQualityStop("existing account replay forbidden")
    start = settings.slow_sma
    initial = data.opens_at[start]
    broker.initialize(initial)
    broker.exchange("JPY", Decimal(800000), data.fx(initial), initial, "synthetic-funding")
    history = QualityHistory()
    daily: list[dict[str, object]] = []
    equity = [1000000.0]
    benchmark = [1000000.0]
    benchmark_units = (
        (float(broker.get_balance().balances["USD"]) - float(settings.rules.commission_usd))
        / (1 + float(settings.rules.commission_rate))
    ) / (
        float(data.opening[start].mean())
        * (1 + float(settings.rules.slippage + settings.rules.spread / 2))
    )
    guard = None
    entry_days: dict[str, int] = {}
    peaks: dict[str, float] = {}
    decisions = []
    status, reason = "COMPLETE", None
    batch_status = "PASSED"

    def assess(last: int, at: datetime) -> QualityAssessment:
        first = max(0, last - settings.slow_sma + 1)
        batch = DailyBatch(
            {s: bars[first : last + 1] for s, bars in rows.items()},
            tuple(t.date() for t in data.opens_at[first : last + 1]),
            complete_response=True,
        )
        known = dict(evidence)
        for security in data.securities:
            sid = f"synthetic:{security.instrument.symbol}"
            if security.metadata_at > at:
                known[sid] = known[sid].model_copy(
                    update={
                        "identity": ReviewFact(observed_at=security.metadata_at, status="UNKNOWN")
                    }
                )
            if security.metadata_at <= at and (
                security.listed > at.date()
                or (security.delisted is not None and security.delisted <= at.date())
            ):
                known[sid] = known[sid].model_copy(
                    update={
                        "corporate_actions": ReviewFact(
                            observed_at=security.metadata_at, status="UNSUPPORTED"
                        )
                    }
                )
        for event in data.events:
            if event.observed_at <= at and event.effective_at <= at:
                sid = f"synthetic:{event.symbol}"
                known[sid] = known[sid].model_copy(
                    update={
                        "corporate_actions": ReviewFact(
                            observed_at=event.observed_at,
                            status="UNSUPPORTED",
                            sources=("synthetic://event-observation",),
                        )
                    }
                )
        return history.observe(
            evaluate_quality(
                universe,
                batch,
                known,
                at,
                origin="synthetic",
                minimum_sessions=settings.slow_sma,
            )
        )

    try:
        for day in range(start, len(data.opens_at)):
            opening, at = data.opens_at[day], data.closes_at[day] + data.latency
            quality_open = assess(day - 1, opening)
            if guard is None:
                guard = SyntheticQualityBroker(broker, quality_open)
            else:
                guard.update(quality_open)
            guard.protected()
            pending = [o for o in broker.get_orders() if o.status == "PENDING"]
            for order in pending:
                try:
                    quality_open.require(order.symbol)
                except SecurityQualityStop:
                    broker.cancel_order(order.order_id)
                    continue
                # The historical engine sees this bar after confirmation, but approval
                # is frozen at the OPEN, before today's close or later diagnosis exists.
                inputs = {s: bars[day : day + 1] for s, bars in rows.items()}
                result = guard.execute(order.order_id, inputs, data.fx(opening), at)
                if result.status == "FILLED" and order.side == "BUY":
                    entry_days[order.symbol] = day
                    peaks[order.symbol] = float(
                        data.opening[
                            day,
                            next(
                                i
                                for i, s in enumerate(data.securities)
                                if s.instrument.symbol == order.symbol
                            ),
                        ]
                    )
                elif result.status == "FILLED" and order.side == "SELL":
                    entry_days.pop(order.symbol, None)
                    peaks.pop(order.symbol, None)
            quality = assess(day, at)
            guard.update(quality)
            guard.protected()  # No invented valuation or forced sale of quarantined holdings.
            eligible = {
                s.ticker for s in quality.population if s.strategy_eligibility == "ELIGIBLE"
            }
            current = {s: bars[day : day + 1] for s, bars in rows.items() if s in eligible}
            instruments = [s.instrument for s in data.securities if s.instrument.symbol in eligible]
            marks = known_marks(instruments, current, at, "historical")
            value = guard.value(current, data.fx(at), at)
            equity.append(float(value.total_jpy))
            benchmark.append(
                200000 + benchmark_units * float(data.market[day]) * float(data.fx(at).usd_jpy)
            )
            daily.append(
                dict(
                    data_kind="synthetic",
                    at=at.isoformat(),
                    equity_jpy=str(value.total_jpy),
                    cash_ratio=str(value.cash_ratio),
                    held_positions=len(broker.get_positions()),
                    quality=quality.summary(),
                    risk_breaches=len(value.breaches),
                )
            )
            planned = broker.get_balance()
            pending_orders = [o for o in broker.get_orders() if o.status == "PENDING"]
            for order in pending_orders:
                if order.symbol not in eligible:
                    broker.cancel_order(order.order_id)
            pending_orders = [o for o in broker.get_orders() if o.status == "PENDING"]
            pending_symbols = {o.symbol for o in pending_orders}
            budget_available = True
            for order in pending_orders:
                if order.side == "BUY":
                    try:
                        instrument = next(
                            s.instrument
                            for s in data.securities
                            if s.instrument.symbol == order.symbol
                        )
                        planned, _, _ = estimate(
                            planned,
                            instrument,
                            "BUY",
                            order.quantity,
                            marks[order.symbol],
                            order.costs,
                            at,
                        )
                        if value_portfolio(planned, marks, data.fx(at), at, broker.rules).breaches:
                            budget_available = False
                    except (ValueError, KeyError):
                        budget_available = False
            slots = len(broker.get_positions()) + len(pending_symbols)
            for col, security in enumerate(data.securities):
                instrument, symbol = security.instrument, security.instrument.symbol
                if symbol not in eligible or symbol in pending_symbols:
                    continue
                position = broker.get_balance().positions.get(symbol)
                held = position is not None and position.quantity > 0
                close = float(data.closing[day, col])
                if held:
                    peaks[symbol] = max(peaks[symbol], close)
                action, why = signal(
                    list(data.closing[: day + 1, col]),
                    list(data.high[: day + 1, col]),
                    settings,
                    average_cost=float(position.average_cost) if held and position else None,
                    peak=peaks.get(symbol, 0),
                    age=day - entry_days.get(symbol, day),
                )
                quantity = position.quantity if held and position else 0
                if (
                    action == "BUY"
                    and slots < settings.max_positions
                    and not value.breaches
                    and budget_available
                ):
                    quantity = int(
                        value.total_jpy
                        * settings.target_weight
                        / (marks[symbol].price * data.fx(at).usd_jpy)
                    )
                    while quantity:
                        try:
                            updated, _, _ = estimate(
                                planned,
                                instrument,
                                "BUY",
                                quantity,
                                marks[symbol],
                                broker.rules,
                                at,
                            )
                            if not value_portfolio(
                                updated, marks, data.fx(at), at, broker.rules
                            ).breaches:
                                planned = updated
                                break
                        except ValueError:
                            pass
                        quantity -= 1
                elif action == "BUY":
                    quantity = 0
                if (action == "BUY" or action == "SELL") and quantity:
                    guard.place(
                        instrument, action, quantity, at, f"synthetic:{day}:{symbol}:{action}"
                    )
                    slots += int(action == "BUY")
                    decisions.append(dict(data_kind="synthetic", at=at.isoformat(), reason=why))
    except BatchQualityStop:
        status, reason, batch_status = "SAFE_STOP", "batch_contract_failure", "STOPPED"
    except SecurityQualityStop:
        status, reason = "SAFE_STOP", "security_eligibility_failure"
    except (ValueError, KeyError):
        status, reason = "SAFE_STOP", "quality_or_replay_invariant_failure"
        batch_status = "UNCERTAIN"
    fills = broker.get_executions()
    realized = [
        float(f.realized_pnl * data.fx(f.price_at).usd_jpy) for f in fills if f.side == "SELL"
    ]
    costs = sum(
        float(FXExchange.model_validate_json(p).cost_in_source)
        for p in broker.history()["fx_exchanges"]
    ) + sum(
        float(
            (f.commission + f.slippage_cost + f.spread_cost + f.rounding_cost)
            * data.fx(f.price_at).usd_jpy
        )
        for f in fills
    )
    measured = (
        metrics(equity, equity, realized, [], costs, len(fills), []) if len(equity) > 1 else {}
    )
    benchmark_return = benchmark[-1] / 1000000 - 1 if status == "COMPLETE" else None
    measured.update(
        dict(
            data_kind="synthetic",
            purpose="synthetic_mvp_only",
            status=status,
            batch_status=batch_status,
            stop_reason=reason,
            initial_jpy=1000000,
            requested_population=settings.symbols,
            requested_evaluation_sessions=len(data.opens_at) - start,
            completed_valuation_sessions=len(daily),
            last_valid_valuation_at=daily[-1]["at"] if daily else None,
            current_assets_jpy=equity[-1] if status == "COMPLETE" else None,
            total_pnl_jpy=equity[-1] - 1000000 if status == "COMPLETE" else None,
            cumulative_return=equity[-1] / 1000000 - 1 if status == "COMPLETE" else None,
            cash_ratio=daily[-1]["cash_ratio"] if daily and status == "COMPLETE" else None,
            held_positions=len(broker.get_positions()),
            trade_count=len(fills),
            pending_orders=sum(o.status == "PENDING" for o in broker.get_orders()),
            rejected_orders=sum(o.status == "REJECTED" for o in broker.get_orders()),
            benchmark_name="SYNTHETIC full-population 80% buy-and-hold index / 20% JPY cash",
            benchmark_return=benchmark_return,
            excess_return=equity[-1] / 1000000 - 1 - benchmark_return
            if benchmark_return is not None
            else None,
            quality=history.snapshots[-1].summary() if history.snapshots else None,
            real_api_requests=0,
            market_strategy_approved=False,
            public_dashboard_updated=False,
            final_balances={k: str(v) for k, v in broker.get_balance().balances.items()},
            realized_pnl={k: str(v) for k, v in broker.get_balance().realized_pnl.items()},
            risk_exit_counts=dict(Counter(d["reason"] for d in decisions)),
            sampling="SYNTHETIC session-close equity; no intraday max-drawdown claim",
            selection_bias=(
                "fixed synthetic population; excluded members remain in audit and benchmark"
            ),
        )
    )
    return SyntheticMVPResult(measured, history, daily, list(fills))


def run_mvp(settings: MVPSettings | None = None) -> SyntheticMVPResult:
    settings = settings or MVPSettings()
    data = synthetic(settings.symbols, settings.sessions, settings.seed)
    data.hourly_available = False
    with TemporaryDirectory(prefix="test-synthetic-mvp-") as root:
        return replay(data, settings, SQLiteRepository(Path(root) / "synthetic.sqlite3"))


def main() -> int:
    settings = MVPSettings.model_validate(
        yaml.safe_load(Path("config/synthetic-mvp.yaml").read_text())
    )
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        result = run_mvp(settings)
        print(json.dumps(result.summary, sort_keys=True))
        return 0 if result.summary["status"] == "COMPLETE" else 1
    finally:
        logging.disable(previous)
