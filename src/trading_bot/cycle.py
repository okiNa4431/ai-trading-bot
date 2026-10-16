"""Manual test-account cycle. Registration and execution are distinct operations."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from trading_bot.broker.execution import known_marks
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Instrument, Market
from trading_bot.operations import Operation
from trading_bot.orchestration.engine import DecisionEngine
from trading_bot.performance import snapshot
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.phase4_cli import verify
from trading_bot.portfolio.models import FXRate
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
        return DecisionResult(
            market=context.market,
            market_summary="Isolated controlled mock proposal",
            decisions=[
                ActionDecision(
                    symbol=c.instrument.symbol,
                    action=self.action
                    if (self.action != "BUY" or i == 0) and c.features
                    else "HOLD",
                    target_weight=0.05 if self.action == "BUY" and i == 0 else 0,
                    reason="Explicit isolated integration mock",
                )
                for i, c in enumerate(context.screening.candidates)
            ],
        )


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
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "market": market,
        "kind": kind,
        "submit": submit,
        "provider": provider,
        "settings": settings.model_dump(mode="json"),
        "historical_replay": historical_replay,
        "mock_action": getattr(strategy, "action", None),
    }
    if not operation.claim(owner, metadata):
        return {"state": "ALREADY_COMPLETED", "run_id": operation.run_id}
    broker = PaperBroker(operation.repository)
    before_executions = 0
    try:
        # Test-only creation, idempotent. Never run against paper-live.
        broker.initialize(initialization_at or clock())
        before_executions = len(broker.get_executions())
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
                broker.execute(
                    order.order_id,
                    data,
                    fx,
                    at,
                    mode="historical" if historical_replay else "paper",
                )
        operation.step(4)
        record = None
        if kind == "decision":
            # USD funding is an explicit test setting: fixed upper bound, real rate and ledger.
            if submit and market == "US" and broker.get_balance().balances["USD"] == 0:
                if fx is None:
                    raise ValueError("fresh FX required for explicit test funding")
                broker.exchange("JPY", Decimal("100000"), fx, at, operation.run_id + ":fund")
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
        marks = known_marks(instruments, data, at, "historical" if historical_replay else "paper")
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
            changed = len(broker.get_executions()) != before_executions
            operation.step(
                operation.stage,
                "PARTIAL" if changed else "NEEDS_REVIEW",
                error_code=type(exc).__name__,
            )
        except Exception:
            pass  # DB may be unavailable. Retained RUNNING attempt blocks automatic replay.
        raise
