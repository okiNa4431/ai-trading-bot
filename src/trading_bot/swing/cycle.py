"""Private daily research cycle. Explicit staging then one atomic store commit."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

import pandas_market_calendars as mcal

from trading_bot.alpaca import NY
from trading_bot.benchmark import BenchmarkInvestment, BenchmarkProvider
from trading_bot.broker.execution import window
from trading_bot.broker.paper import PaperBroker
from trading_bot.orchestration.order_planner import OrderPlanner
from trading_bot.persistence.research import PrivateResearchStore, ResearchMemory, digest
from trading_bot.portfolio.models import FXExchange, TradingRules, utc
from trading_bot.strategy.mock import MockTradingStrategy
from trading_bot.strategy.models import AISettings, DecisionContext, validate_result
from trading_bot.swing.benchmarks import compare_all_country
from trading_bot.swing.internal_input import build_draft, parse_offline_response
from trading_bot.swing.metrics import metrics
from trading_bot.swing.models import SwingSettings
from trading_bot.swing.observed import ObservedDailyData
from trading_bot.swing.strategies import RuleSwingStrategy


def stage_cycle(
    state: dict[str, Any],
    data: ObservedDailyData,
    at: datetime,
    run_id: str,
    kind: Literal["decision", "execute"],
    ai: AISettings,
    settings: SwingSettings,
    rules: TradingRules,
    *,
    strategy: Literal["rule", "momentum", "mock"] = "mock",
    funding_jpy: Decimal = Decimal(0),
    mock_response: str | None = None,
    nav_provider: BenchmarkProvider | None = None,
    nav_investment: BenchmarkInvestment | None = None,
    nav_allowed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if kind not in ("decision", "execute") or strategy not in ("rule", "momentum", "mock"):
        raise ValueError("offline strategy and explicit cycle kind required")
    at = utc(at)
    memory = ResearchMemory(state)
    broker = PaperBroker(memory, rules)
    prior = memory.state.get("integration", {})
    if prior and at <= datetime.fromisoformat(prior["at"]):
        raise ValueError("cycle must advance; reconcile repeated run IDs before staging")
    if prior and prior["rules"] != rules.model_dump(mode="json"):
        raise ValueError("cannot change risk/cost rules on an existing research account")
    initialized = bool(state.get("account"))
    account = broker.get_balance() if initialized else broker.initialize(at)
    pending = [o for o in broker.get_orders() if o.status == "PENDING"]
    protected = {s for s, p in account.positions.items() if p.quantity} | {
        o.symbol for o in pending
    }
    screening, summary, marks = data.screen(at, settings, protected, account.created_at)
    slot = f"US:{data.closes_at[data.daily_index(at)].astimezone(NY).date()}:{kind}"
    if slot in prior.get("completed_slots", []):
        raise ValueError("research session/processing slot already committed")
    fx = data.checked_fx(at, rules)
    if funding_jpy:
        if initialized or kind != "decision" or not Decimal(0) < funding_jpy <= Decimal(800000):
            raise ValueError("funding is explicit, bounded, first-decision only")
        broker.exchange("JPY", funding_jpy, fx, at, "initial-research-fx")
    if kind == "execute":
        bars = data.bars(protected, at)
        for order in pending:
            later = [
                span
                for b in bars.get(order.symbol, [])
                if (span := window(b)) is not None
                and span[0] > order.requested_at
                and span[1] <= at
                and b.volume > 0
            ]
            if later and (at - min(later)[1]).total_seconds() > rules.max_price_age_seconds:
                raise ValueError(
                    "missed execution observation slot; manual reconciliation required"
                )
            broker.execute(order.order_id, bars, fx, at, mode="paper")
    account = broker.get_balance()
    value = broker.value(marks, fx, at)
    opened: dict[str, datetime] = {}
    quantities: dict[str, int] = {}
    for fill in broker.get_executions():
        if fill.side == "BUY" and quantities.get(fill.symbol, 0) == 0:
            opened[fill.symbol] = fill.price_at
        quantities[fill.symbol] = quantities.get(fill.symbol, 0) + (
            fill.quantity if fill.side == "BUY" else -fill.quantity
        )
    ages = {}
    for symbol, start in opened.items():
        if quantities.get(symbol, 0):
            days = mcal.get_calendar("NYSE").schedule(start.date(), at.date())
            ages[symbol] = sum(start < t.to_pydatetime() <= at for t in days.market_open)
    context = DecisionContext(
        data_origin="market",
        as_of=at,
        market="US",
        screening=screening,
        account=account,
        portfolio=value,
        fx=fx,
        rules=rules,
        pending_orders=[o for o in broker.get_orders() if o.status == "PENDING"],
        holding_sessions=ages,
    )
    draft = build_draft(context, ai)
    proposals = None
    if kind == "decision":
        selected = (
            MockTradingStrategy() if strategy == "mock" else RuleSwingStrategy(strategy, settings)
        )
        if mock_response is not None and strategy != "mock":
            raise ValueError("explicit mock response requires mock strategy")
        proposals = (
            parse_offline_response(mock_response, context, ai)
            if mock_response is not None
            else validate_result(selected.decide(context), context)
        )
        proposals = parse_offline_response(proposals.model_dump_json(), context, ai)
        if not summary.new_buys_allowed and any(d.action == "BUY" for d in proposals.decisions):
            raise ValueError("new buys frozen by screening regime/quality")
        plan = OrderPlanner().plan(run_id, proposals, context, marks)
        instruments = {s.instrument.symbol: s.instrument for s in data.securities}
        for planned in plan.orders:
            broker.place_order(
                instruments[planned.symbol],
                planned.side,
                planned.quantity,
                at,
                planned.order_id,
            )
    points = dict(prior.get("equity_points", {}))
    points[slot.split(":")[1]] = float(value.total_jpy)
    inputs = dict(prior.get("data_points", {}))
    inputs[slot.split(":")[1]] = digest(
        dict(
            bars=[
                b.model_dump(mode="json", exclude={"fetched_at"})
                for rows in data.original.values()
                for b in rows
                if b.fetched_at <= at
            ],
            fx=[f.model_dump(mode="json") for f in data.fx_observations if f.timestamp <= at],
            universe=[s.model_dump(mode="json") for s in data.securities],
        )
    )
    path = [float(account.initial_jpy), *[points[d] for d in sorted(points)]]
    fills = broker.get_executions()
    performance = metrics(
        path,
        path,
        [float(f.realized_pnl) for f in fills if f.side == "SELL"],
        [],
        0,
        len(fills),
        [],
    )
    cost_usd = sum(
        (f.commission + f.slippage_cost + f.spread_cost + f.rounding_cost for f in fills),
        Decimal(0),
    )
    fx_costs = {"JPY": Decimal(0), "USD": cost_usd}
    for raw in broker.history()["fx_exchanges"]:
        exchange = FXExchange.model_validate_json(raw)
        fx_costs[exchange.source_currency] += exchange.cost_in_source
    performance.update(
        costs_jpy=None,
        costs_native={k: str(v) for k, v in fx_costs.items()},
        realized_currency="USD",
        sampling="daily last cycle per observed session; no intraday path",
        average_holding_sessions=None,
    )
    memory.state["integration"] = dict(
        at=at.isoformat(),
        rules=rules.model_dump(mode="json"),
        completed_slots=[*prior.get("completed_slots", []), slot],
        equity_points=points,
        data_points=inputs,
    )
    result = dict(
        private_only=True,
        external_ai_sent=False,
        public_projection_allowed=False,
        input=draft.model_dump(mode="json"),
        proposals=proposals.model_dump(mode="json") if proposals else None,
        valuation=value.model_dump(mode="json"),
        orders=[o.model_dump(mode="json") for o in broker.get_orders()],
        executions=[e.model_dump(mode="json") for e in broker.get_executions()],
        return_since_initial=float(value.total_jpy / account.initial_jpy - 1),
        performance=performance,
        evaluation_window=[min(points), max(points)],
        initial_jpy=str(account.initial_jpy),
        data_history=inputs,
        all_country=(
            compare_all_country(
                nav_provider,
                nav_investment,
                account,
                [(at, float(value.total_jpy))],
                internal_use_allowed=nav_allowed,
                publish=False,
            )
            if nav_provider is not None
            else {"status": "UNAVAILABLE_MATCHED_NAV", "excess_return": None}
        ),
        data_basis="raw IEX daily price returns, not consolidated volume or dividend total return",
        universe_basis="observed metadata; historical membership not certified",
    )
    return memory.state, result


def commit_cycle(
    store: PrivateResearchStore,
    data: ObservedDailyData,
    at: datetime,
    run_id: str,
    kind: Literal["decision", "execute"],
    ai: AISettings,
    settings: SwingSettings,
    rules: TradingRules,
    *,
    strategy: Literal["rule", "momentum", "mock"] = "mock",
    funding_jpy: Decimal = Decimal(0),
    mock_response: str | None = None,
    nav_provider: BenchmarkProvider | None = None,
    nav_investment: BenchmarkInvestment | None = None,
    nav_allowed: bool = False,
) -> dict[str, Any]:
    # No fallback and no automatic resend/reconcile: a lost response must be inspected explicitly.
    snapshot = store.read()
    new_state, result = stage_cycle(
        snapshot["state"],
        data,
        at,
        run_id,
        kind,
        ai,
        settings,
        rules,
        strategy=strategy,
        funding_jpy=funding_jpy,
        mock_response=mock_response,
        nav_provider=nav_provider,
        nav_investment=nav_investment,
        nav_allowed=nav_allowed,
    )
    bars = [b for rows in data.original.values() for b in rows if b.fetched_at <= utc(at)]
    request = dict(
        kind=kind,
        at=utc(at).isoformat(),
        strategy=strategy,
        settings=settings.model_dump(mode="json"),
        rules=rules.model_dump(mode="json"),
        input_hash=digest(result["input"]),
        proposals_hash=digest(result["proposals"]),
        funding=str(funding_jpy),
        comparison_hash=digest(result["all_country"]),
    )
    return store.commit(run_id, snapshot["version"], request, bars, new_state, result)
