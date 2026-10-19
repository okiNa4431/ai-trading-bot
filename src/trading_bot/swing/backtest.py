"""Forward replay in NEW offline test directories only. No Supabase fallback."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from trading_bot.broker.execution import eligible_bar, known_marks
from trading_bot.broker.paper import PaperBroker
from trading_bot.operations import fingerprint
from trading_bot.orchestration.order_planner import OrderPlanner
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import Mark, TradingRules
from trading_bot.screening.models import ScreeningResult
from trading_bot.strategy.base import TradingStrategy
from trading_bot.strategy.models import DecisionContext, DecisionRun, validate_result
from trading_bot.swing.data import SwingData
from trading_bot.swing.metrics import metrics
from trading_bot.swing.models import Mode, ScreenSummary, SwingSettings
from trading_bot.swing.screening import screen_daily
from trading_bot.swing.strategies import RuleSwingStrategy


@dataclass
class BacktestResult:
    mode: str
    strategy: str
    status: str
    summary: dict[str, object]
    daily_equity: list[float]
    decision_times: list[datetime]
    alerts: list[dict[str, object]]
    account_path: str


def risk_alerts(
    context: DecisionContext,
    previous: dict[str, float],
    settings: SwingSettings,
    market_return: float,
) -> list[str]:
    alerts = []
    if market_return <= -settings.market_trigger:
        alerts.append("market_crash")
    for candidate in context.screening.candidates:
        f = candidate.features
        if not candidate.held or f is None:
            continue
        symbol = candidate.instrument.symbol
        price = float(f.mark.price)
        reference = previous.get(symbol, f.daily_close)
        if price / reference - 1 <= -settings.drawdown_trigger:
            alerts.append(f"{symbol}:rapid_drop")
        if abs(price - reference) >= settings.atr_multiple * f.atr14:
            alerts.append(f"{symbol}:ATR_move")
        if f.volatility20 > settings.max_volatility:
            alerts.append(f"{symbol}:volatility")
        if abs(price / reference - 1) > max(0.02, f.volatility20 / (252**0.5) * 3):
            alerts.append(f"{symbol}:intraday_volatility_spike")
    if context.portfolio.breaches:
        alerts.append("allocation_breach")
    for order in context.pending_orders:
        if order.status == "PENDING" and order.symbol in previous:
            pending_candidate = next(
                (c for c in context.screening.candidates if c.instrument.symbol == order.symbol),
                None,
            )
            if (
                pending_candidate
                and pending_candidate.features
                and float(pending_candidate.features.mark.price) / previous[order.symbol] - 1
                <= -settings.drawdown_trigger
            ):
                alerts.append(f"{order.symbol}:pending_risk")
    return alerts


def context_at(
    broker: PaperBroker, screening: ScreeningResult, data: SwingData, at: datetime
) -> tuple[DecisionContext, dict[str, Mark]]:
    account = broker.get_balance()
    pending = [o for o in broker.get_orders() if o.status == "PENDING"]
    symbols = {c.instrument.symbol for c in screening.candidates} | set(account.positions)
    bars = data.bars(symbols, at)
    required = {p.instrument.symbol for p in account.positions.values() if p.quantity}
    required |= {o.symbol for o in pending}
    for symbol in required:
        rows = bars.get(symbol, [])
        if rows and max(rows, key=lambda b: b.fetched_at).volume == 0:
            raise ValueError(f"possible halt/zero-volume protected security: {symbol}")
    instruments = [s.instrument for s in data.securities if s.instrument.symbol in symbols]
    marks = known_marks(instruments, bars, at, "paper")
    if data.hourly_available and data.rights.intraday:
        day = max((i for i, opening in enumerate(data.opens_at) if opening <= at), default=-1)
        if day >= 0:
            hours = int((at - data.latency - data.opens_at[day]).total_seconds() // 3600)
            if hours > 0 and at <= data.closes_at[day] + timedelta(hours=1):
                expected = data.opens_at[day] + timedelta(hours=hours)
                for symbol in symbols:
                    mark = marks.get(symbol)
                    if mark is None or (mark.price_at or mark.available_at) < expected:
                        if (
                            data.closes_at[day] + data.latency <= at
                            and mark is not None
                            and (mark.price_at or mark.available_at) >= data.closes_at[day]
                        ):
                            continue
                        raise ValueError(f"missing/delayed intraday coverage: {symbol}")
    value = broker.value(marks, data.fx(at), at)
    opened_positions: dict[str, datetime] = {}
    quantities: dict[str, int] = {}
    for fill in broker.get_executions():
        if fill.side == "BUY":
            if quantities.get(fill.symbol, 0) == 0:
                opened_positions[fill.symbol] = fill.price_at
            quantities[fill.symbol] = quantities.get(fill.symbol, 0) + fill.quantity
        else:
            quantities[fill.symbol] = quantities.get(fill.symbol, 0) - fill.quantity
    ages = {
        symbol: sum(start < t <= at for t in data.opens_at)
        for symbol, start in opened_positions.items()
        if quantities.get(symbol, 0)
    }
    updated = []
    for c in screening.candidates:
        f = c.features
        mark = marks.get(c.instrument.symbol)
        if f is not None and mark is not None:
            f = f.model_copy(update={"mark": mark})
        updated.append(
            c.model_copy(
                update={
                    "held": c.instrument.symbol in account.positions
                    and account.positions[c.instrument.symbol].quantity > 0,
                    "features": f,
                }
            )
        )
    return DecisionContext(
        as_of=at,
        market="US",
        screening=screening.model_copy(update={"as_of": at, "candidates": updated}),
        account=account,
        portfolio=value,
        fx=data.fx(at),
        rules=broker.rules,
        pending_orders=pending,
        holding_sessions=ages,
        data_origin="synthetic" if data.rights.kind == "synthetic" else "market",
    ), marks


def run_backtest(
    data: SwingData,
    root: Path,
    mode: Mode,
    settings: SwingSettings,
    strategy_kind: Literal["rule", "momentum", "cash", "gemini_mock"] = "rule",
    *,
    start: int = 201,
    end: int | None = None,
    strategy: TradingStrategy | None = None,
    rules: TradingRules | None = None,
) -> BacktestResult:
    if not root.name.startswith("test-swing-"):
        raise ValueError("new isolated test-swing- directory required")
    if root.exists():
        raise ValueError("refuse to overwrite/replay an existing research account")
    if data.rights.kind != "synthetic":
        raise ValueError("licensed replay requires reviewed availability/FX adapter; disabled")
    if mode in ("B", "C") and (
        settings.daily_only or not data.hourly_available or not data.rights.intraday
    ):
        return BacktestResult(
            mode, strategy_kind, "UNAVAILABLE_INTRADAY", {}, [], [], [], str(root)
        )
    end = len(data.opens_at) if end is None else end
    if not 201 <= start < end <= len(data.opens_at):
        raise ValueError("separate >=201 warmup sessions and forward evaluation required")
    if settings.daily_only:
        data = replace(data, hourly_available=False)
    root.mkdir(parents=True)
    repository = SQLiteRepository(root / "account.sqlite3")
    broker = PaperBroker(repository, rules)
    initial = data.opens_at[start]
    broker.initialize(initial)
    if strategy_kind != "cash":
        # Explicit, identical initial funding for A/B/C; never implicit OrderPlanner FX.
        broker.exchange("JPY", Decimal("800000"), data.fx(initial), initial, "initial-funding")
    chooser = strategy or RuleSwingStrategy(strategy_kind, settings)
    daily_equity, path = [1000000.0], [1000000.0]
    decisions: list[datetime] = []
    alerts: list[dict[str, object]] = []
    closed_ages: list[int] = []
    previous: dict[str, float] = {}
    summary_screen: ScreenSummary | None = None
    screening: ScreeningResult | None = None
    cached_index = -1
    freeze_day = False
    decision_count: dict[str, int] = {}
    month_count: dict[str, int] = {}
    status = "COMPLETE"
    reason = None
    crash_returns: list[float] = []
    for day in range(start, end):
        freeze_day = False
        events = {data.closes_at[day] + data.latency}
        if data.hourly_available and data.rights.intraday and not settings.daily_only:
            hour = data.opens_at[day] + timedelta(hours=1)
            while hour <= data.closes_at[day] + timedelta(minutes=30):
                events.add(hour + data.latency)
                hour += timedelta(hours=1)
        for at in sorted(events):
            try:
                account = broker.get_balance()
                pending = [o for o in broker.get_orders() if o.status == "PENDING"]
                protected = {p.instrument.symbol for p in account.positions.values() if p.quantity}
                protected |= {o.symbol for o in pending}
                for event in data.events:
                    if (
                        event.observed_at <= at
                        and event.effective_at <= at
                        and (event.symbol in protected or event.kind != "halt")
                    ):
                        raise ValueError(f"unsupported {event.kind}: {event.symbol}; manual review")
                active_day = at.date()
                if any(
                    s.instrument.symbol in protected
                    and s.delisted is not None
                    and s.delisted <= active_day
                    for s in data.securities
                ):
                    raise ValueError(
                        "delisted held/pending security; no invented liquidation price"
                    )
                inputs = data.bars(protected, at)
                eligible = []
                for order in pending:
                    instrument = next(
                        s.instrument for s in data.securities if s.instrument.symbol == order.symbol
                    )
                    candidate = eligible_bar(
                        order,
                        instrument,
                        inputs.get(order.symbol, []),
                        at,
                        "historical",
                        broker.rules,
                    )
                    if candidate:
                        bar, opened, observed = candidate
                        processed = (
                            observed
                            if order.stop_price and Decimal(str(bar.open)) > order.stop_price
                            else opened
                        )
                        eligible.append((processed, order.order_id))
                for price_at, order_id in sorted(eligible):
                    broker.execute(order_id, inputs, data.fx(price_at), at)
                account = broker.get_balance()
                pending = [o for o in broker.get_orders() if o.status == "PENDING"]
                protected = {p.instrument.symbol for p in account.positions.values() if p.quantity}
                protected |= {o.symbol for o in pending}
                index = data.daily_index(at)
                if (
                    screening is None
                    or index != cached_index
                    or not protected.issubset({c.instrument.symbol for c in screening.candidates})
                ):
                    screening, summary_screen = screen_daily(data, at, settings, protected)
                    cached_index = index
                assert summary_screen is not None
                context, marks = context_at(broker, screening, data, at)
                path.append(float(context.portfolio.total_jpy))
                is_daily = at == data.closes_at[day] + data.latency
                if at == max(events):
                    daily_equity.append(float(context.portfolio.total_jpy))
                    market_return = data.market[day] / data.market[day - 1] - 1
                    if market_return <= -settings.market_trigger:
                        crash_returns.append(daily_equity[-1] / daily_equity[-2] - 1)
                monitor = risk_alerts(context, previous, settings, data.market_return_at(day, at))
                full_hour = at - data.latency <= data.closes_at[day] and (
                    (at - data.latency - data.opens_at[day]).total_seconds() % 3600 == 0
                )
                should_decide = (mode in ("A", "B") and is_daily) or (mode == "C" and full_hour)
                if mode == "B" and not is_daily and monitor:
                    alerts.append(
                        {
                            "at": at.isoformat(),
                            "reasons": monitor,
                            "response": settings.monitor_response,
                        }
                    )
                    freeze_day = settings.monitor_response == "freeze" or freeze_day
                    if settings.monitor_response in ("freeze", "sell"):
                        for pending_order in context.pending_orders:
                            if pending_order.status == "PENDING" and pending_order.side == "BUY":
                                broker.cancel_order(pending_order.order_id)
                        context = context.model_copy(update={"pending_orders": broker.get_orders()})
                    if settings.monitor_response == "sell":
                        should_decide = True
                if should_decide:
                    # Offline proposal budget only; never reserve live API usage here.
                    quota_day = at.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
                    quota_month = at.strftime("%Y-%m")
                    if (
                        decision_count.get(quota_day, 0) >= 8
                        or month_count.get(quota_month, 0) >= 200
                    ):
                        raise ValueError("offline decision budget exhausted; no retries")
                    decision_key = f"swing:{mode}:{strategy_kind}:{at.isoformat()}"
                    journal = DecisionRepository(repository)
                    record = DecisionRun(
                        decision_id=decision_key,
                        started_at=at,
                        as_of=at,
                        market="US",
                        provider="mock",
                        model=strategy_kind,
                        prompt_version="swing-research-v1",
                        input_hash=fingerprint(context.model_dump()),
                        account_hash=fingerprint(context.account.model_dump()),
                        context=context,
                        status="RUNNING",
                    )
                    if not journal.claim(record):
                        raise ValueError("duplicate offline decision; manual review")
                    result = validate_result(chooser.decide(context), context)
                    if (
                        mode == "B"
                        and monitor
                        and settings.monitor_response == "sell"
                        and not is_daily
                    ):
                        result = result.model_copy(
                            update={
                                "decisions": [
                                    d.model_copy(update={"action": "SELL", "target_weight": 0.0})
                                    if c.held and c.features
                                    else d.model_copy(update={"action": "HOLD"})
                                    for d, c in zip(
                                        result.decisions, context.screening.candidates, strict=True
                                    )
                                ]
                            }
                        )
                    if freeze_day or not summary_screen.new_buys_allowed:
                        result = result.model_copy(
                            update={
                                "decisions": [
                                    d.model_copy(update={"action": "HOLD"})
                                    if d.action == "BUY"
                                    else d
                                    for d in result.decisions
                                ]
                            }
                        )
                    plan = OrderPlanner().plan(
                        f"swing:{mode}:{strategy_kind}:{at.isoformat()}", result, context, marks
                    )
                    registered = []
                    for item in plan.orders:
                        instrument = next(
                            c.instrument
                            for c in context.screening.candidates
                            if c.instrument.symbol == item.symbol
                        )
                        registered.append(
                            broker.place_order(
                                instrument, item.side, item.quantity, at, item.order_id
                            )
                        )
                    journal.save(
                        record.model_copy(
                            update={
                                "status": "REGISTERED",
                                "result": result,
                                "plan": plan,
                                "registered_orders": registered,
                            }
                        )
                    )
                    decision_count[quota_day] = decision_count.get(quota_day, 0) + 1
                    month_count[quota_month] = month_count.get(quota_month, 0) + 1
                    decisions.append(at)
                previous = {s: float(mark.price) for s, mark in marks.items()}
            except Exception as exc:
                from trading_bot.strategy.security import redact

                status = "SAFE_STOP"
                reason = (
                    redact(str(exc))
                    if isinstance(exc, ValueError)
                    else (f"{type(exc).__name__}: uncertain research step; no automatic replay")
                )
                alerts.append({"at": at.isoformat(), "reason": reason})
                break
        if status != "COMPLETE":
            break
    executions = broker.get_executions()
    acquired: dict[str, list[tuple[datetime, int]]] = {}
    costs = 0.0
    realized = []
    for fill in executions:
        costs += float(
            (fill.commission + fill.slippage_cost + fill.spread_cost + fill.rounding_cost)
            * data.fx(fill.price_at).usd_jpy
        )
        if fill.side == "BUY":
            acquired.setdefault(fill.symbol, []).append((fill.price_at, fill.quantity))
        else:
            realized.append(float(fill.realized_pnl * data.fx(fill.price_at).usd_jpy))
            remaining = fill.quantity
            while remaining:
                acquired_at, quantity = acquired[fill.symbol].pop(0)
                matched = min(remaining, quantity)
                age = sum(acquired_at < t <= fill.price_at for t in data.opens_at)
                closed_ages.extend([age] * matched)
                remaining -= matched
                if quantity > matched:
                    acquired[fill.symbol].insert(0, (acquired_at, quantity - matched))
    with repository.transaction() as tx:
        from trading_bot.portfolio.models import FXExchange

        for payload in tx.records("fx_exchanges"):
            fx_event = FXExchange.model_validate_json(payload)
            costs += float(
                fx_event.cost_in_source
                if fx_event.source_currency == "JPY"
                else fx_event.cost_in_source * fx_event.usd_jpy
            )
    measured = (
        metrics(daily_equity, path, realized, closed_ages, costs, len(executions), crash_returns)
        if len(daily_equity) > 1
        else {}
    )
    if status != "COMPLETE":
        measured["cumulative_return"] = None
    measured.update(
        {
            "stop_reason": reason,
            "decisions": len(decisions),
            "real_api_requests": 0,
            "pending_orders": sum(o.status == "PENDING" for o in broker.get_orders()),
            "rejected_orders": sum(o.status == "REJECTED" for o in broker.get_orders()),
            "final_balances": {k: str(v) for k, v in broker.get_balance().balances.items()},
            "data_kind": data.rights.kind,
            "source": data.rights.source,
            "universe_bias": "point-in-time synthetic"
            if data.rights.point_in_time_universe
            else "current universe; survivorship bias",
            "held_positions": len(broker.get_positions()),
            "public_performance_allowed": False,
            "same_engine": "OrderPlanner / PaperBroker",
            "daily_sampling": "session-end after conservative final partial-bar confirmation",
            "gemini_evidence": "mock HOLD only; no actual Gemini backtest",
            "start": initial.isoformat(),
            "requested_end": data.closes_at[end - 1].isoformat(),
            "funding": "JPY cash baseline"
            if strategy_kind == "cash"
            else "explicit initial 800000 JPY FX; costs included",
        }
    )
    return BacktestResult(
        mode, strategy_kind, status, measured, daily_equity, decisions, alerts, str(root)
    )
