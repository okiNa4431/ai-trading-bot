"""Manual test-account cycle. Registration and execution are distinct operations."""

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from trading_bot.broker.execution import known_marks, session
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Instrument, Market
from trading_bot.models import PriceBar
from trading_bot.operations import Operation
from trading_bot.orchestration.engine import DecisionEngine
from trading_bot.performance import snapshot
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.phase4_cli import verify
from trading_bot.portfolio.models import FXRate, Order
from trading_bot.portfolio.valuation import validate_fx, value_portfolio
from trading_bot.screening.models import ScreeningSettings
from trading_bot.storage import ParquetStore
from trading_bot.strategy.base import TradingStrategy
from trading_bot.strategy.models import ActionDecision, AISettings, DecisionContext, DecisionResult


class TestProposal:
    """Deliberate mock actions for integration tests; never described as AI output."""

    def __init__(self, action: Literal["BUY", "SELL", "HOLD"] = "HOLD") -> None:
        self.action = action

    def decide(self, context: DecisionContext) -> DecisionResult:
        fx = context.fx.usd_jpy if context.fx else Decimal(1)
        affordable = next(
            (
                c.instrument.symbol
                for c in context.screening.candidates
                if c.features
                and Decimal(str(c.features.mark.price))
                * context.rules.lot(c.instrument)
                * (fx if c.instrument.currency == "USD" else Decimal(1))
                <= context.account.initial_jpy * Decimal("0.05")
            ),
            None,
        )
        return DecisionResult(
            market=context.market,
            market_summary="Isolated controlled mock proposal",
            decisions=[
                ActionDecision(
                    symbol=c.instrument.symbol,
                    action=self.action
                    if (self.action != "BUY" or c.instrument.symbol == affordable) and c.features
                    else "HOLD",
                    target_weight=0.05
                    if self.action == "BUY" and c.instrument.symbol == affordable
                    else 0,
                    reason="Explicit isolated integration mock",
                )
                for c in context.screening.candidates
            ],
        )


def require_execution_coverage(order: Order, bars: list[PriceBar], at: datetime) -> None:
    """Missing first future bars must not turn into a more convenient later fill."""
    zone = ZoneInfo("Asia/Tokyo" if order.market == "JP" else "America/New_York")
    day = order.requested_at.astimezone(zone).date()
    last = at.astimezone(zone).date()
    starts = {b.timestamp for b in bars}
    while day <= last:
        times = session(order.market, day.isoformat())
        if times:
            segments = [(times["market_open"], times["market_close"])]
            if "break_start" in times:
                segments = [
                    (times["market_open"], times["break_start"]),
                    (times["break_end"], times["market_close"]),
                ]
            for opening, closing in segments:
                stamp = opening
                while stamp < closing:
                    if (
                        order.requested_at < stamp
                        and stamp + timedelta(hours=1) <= at
                        and stamp not in starts
                    ):
                        raise ValueError("missing required future execution bar")
                    stamp += timedelta(hours=1)
        day += timedelta(days=1)


def run_cycle(
    operation: Operation,
    store: ParquetStore,
    instruments: list[Instrument],
    market: Market,
    settings: AISettings,
    *,
    kind: Literal["decision", "execute", "observe"],
    owner: str,
    submit: bool = False,
    provider: Literal["mock", "gemini"] = "mock",
    strategy: TradingStrategy | None = None,
    prepare: Callable[[PaperBroker], FXRate | None],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    initialization_at: datetime | None = None,
    historical_replay: bool = False,
    fund_jpy: Decimal = Decimal(0),
) -> dict[str, object]:
    if historical_replay and provider != "mock":
        raise ValueError("synthetic fixture replay must never call Gemini")
    if provider == "gemini" and (settings.retries != 0 or settings.max_requests_per_run != 1):
        raise ValueError("Phase 5 Gemini must use one request and zero retries")
    if fund_jpy and (
        kind != "decision" or not submit or market != "US" or fund_jpy != Decimal("100000")
    ):
        raise ValueError("explicit test funding requires a submitted US decision")
    metadata: dict[str, object] = {
        "market": market,
        "kind": kind,
        "submit": submit,
        "provider": provider,
        "settings": settings.model_dump(mode="json"),
        "historical_replay": historical_replay,
        "fund_jpy": str(fund_jpy),
        "mock_action": getattr(strategy, "action", None),
        "implementation_sha": os.environ.get("GITHUB_SHA", "local"),
        "instruments": [i.model_dump(mode="json") for i in instruments],
    }
    if not operation.claim(owner, metadata):
        return {"state": "ALREADY_COMPLETED", "run_id": operation.run_id}
    broker = PaperBroker(operation.repository)
    before_executions = 0
    before_account = None
    before_orders = 0
    try:
        # Test-only creation, idempotent. Never run against paper-live.
        broker.initialize(initialization_at or clock())
        before_executions = len(broker.get_executions())
        before_account = broker.get_balance()
        before_orders = len(broker.get_orders())
        operation.step(1)
        fx = prepare(broker)
        operation.step(2)
        at = clock()
        account = broker.get_balance()
        required_fx = (
            market == "US"
            or account.balances["USD"] != 0
            or any(
                p.quantity and p.instrument.currency == "USD" for p in account.positions.values()
            )
        )
        if required_fx:
            validate_fx(fx, at, broker.rules)
        data = {i.symbol: store.read(i, "1h") for i in instruments}
        if any(not rows for rows in data.values()):
            raise ValueError("required confirmed history missing")
        operation.step(3)
        if kind == "execute" and submit:
            for order in broker.get_orders():
                if order.status != "PENDING" or order.market != market:
                    continue
                if at - order.requested_at > timedelta(days=7):
                    raise ValueError("pending order expired; reviewed cancellation required")
                require_execution_coverage(order, data.get(order.symbol, []), at)
                broker.execute(
                    order.order_id,
                    data,
                    fx,
                    at,
                    mode="paper",
                )
        operation.step(4)
        record = None
        if kind == "decision":
            # USD funding is an explicit test setting: fixed upper bound, real rate and ledger.
            if fund_jpy and (not submit or market != "US" or fund_jpy != Decimal("100000")):
                raise ValueError("only explicit 100000 JPY test funding is supported")
            if fund_jpy:
                if broker.get_balance().balances["USD"] != 0:
                    raise ValueError("explicit initial test funding already exists")
                if fx is None:
                    raise ValueError("fresh FX required for explicit test funding")
                broker.exchange("JPY", fund_jpy, fx, at, operation.run_id + ":fund")
            engine = DecisionEngine(
                store,
                broker,
                DecisionRepository(operation.repository),
                instruments,
                ScreeningSettings(),
                settings,
                clock=clock,
            )
            record = engine.run(
                market,
                at,
                fx,
                provider,
                submit=False,
                decision_id=operation.run_id + ":decision",
                strategy=strategy,
            )
            if record.status != "SUCCESS":
                raise ValueError("decision failed; inspect saved usage and decision")
        operation.step(5)
        if record is not None and submit:
            record = engine.register(record)
        operation.step(6)
        account = broker.get_balance()
        marks = known_marks(instruments, data, at, "paper")
        valuation = value_portfolio(account, marks, fx, at, broker.rules)
        value = snapshot(operation.repository, valuation, at)
        operation.step(7)
        result: dict[str, object] = {
            "state": "SUCCEEDED",
            "run_id": operation.run_id,
            "test_only": True,
            "historical_replay": historical_replay,
            "orders": len(broker.get_orders()),
            "executions": len(broker.get_executions()),
            "pending": sum(o.status == "PENDING" for o in broker.get_orders()),
            "valuation_available": value.bot_jpy is not None,
            "verified": verify(operation.repository)["verified"],
            "decision": record.status if record else None,
        }
        # Test-account data is NEVER promoted to the production public dashboard.
        operation.data("public-test-summary", result)
        operation.step(8, "SUCCEEDED", summary=result)
        return result
    except Exception as exc:
        try:
            changed = (
                len(broker.get_executions()) != before_executions
                or len(broker.get_orders()) != before_orders
                or (before_account is not None and broker.get_balance() != before_account)
            )
            operation.step(
                operation.stage,
                "PARTIAL" if changed else "NEEDS_REVIEW",
                error_code=type(exc).__name__,
            )
        except Exception:
            pass  # DB may be unavailable. Retained RUNNING attempt blocks automatic replay.
        raise
