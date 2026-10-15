"""Data -> features -> AI -> plan -> optional atomic PAPER registration. Never fills."""

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError

from trading_bot.broker.execution import known_marks
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Instrument, Interval, Market
from trading_bot.orchestration.order_planner import OrderPlanner
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.sqlite_repository import Session
from trading_bot.portfolio.models import Account, FXRate, Mark, Order, TradingRules, utc
from trading_bot.portfolio.valuation import value_portfolio
from trading_bot.screening.models import ScreeningSettings
from trading_bot.screening.screener import screen
from trading_bot.storage import ParquetStore
from trading_bot.strategy.base import TradingStrategy
from trading_bot.strategy.gemini import GeminiTradingStrategy
from trading_bot.strategy.mock import MockTradingStrategy
from trading_bot.strategy.models import AISettings, DecisionContext, DecisionRun, validate_result
from trading_bot.strategy.prompt import PROMPT_VERSION, payload
from trading_bot.strategy.security import redact


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def account_hash(account: Account, pending: list[Order], rules: TradingRules) -> str:
    text = json.dumps(
        {
            "account": account.model_dump(mode="json"),
            "pending": [o.model_dump(mode="json") for o in pending],
            "rules": rules.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    return digest(text)


class BoundRepository:
    """Reuse PaperBroker's validation while the enclosing transaction owns the commit."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        yield self.session


class DecisionEngine:
    def __init__(
        self,
        store: ParquetStore,
        broker: PaperBroker,
        decisions: DecisionRepository,
        universe: list[Instrument],
        screening: ScreeningSettings,
        ai: AISettings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.broker = broker
        self.decisions = decisions
        self.universe = universe
        self.screening = screening
        self.ai = ai
        self.clock = clock

    def context(
        self, market: Market, as_of: datetime, fx: FXRate | None
    ) -> tuple[DecisionContext, dict[str, Mark]]:
        as_of = utc(as_of)
        account = self.broker.get_balance()
        if as_of > utc(self.clock()) or as_of < (account.last_event_at or account.created_at):
            raise ValueError("decision time is future or predates current account state")
        orders = self.broker.get_orders()
        pending = [o for o in orders if o.status == "PENDING"]
        if any(o.requested_at > as_of for o in pending):
            raise ValueError("pending orders contain future information")
        instruments = {i.symbol: i for i in self.universe}
        for position in account.positions.values():
            if position.quantity:
                instruments[position.instrument.symbol] = position.instrument
        for order in pending:
            instruments.setdefault(
                order.symbol,
                Instrument(
                    symbol=order.symbol,
                    market=order.market,
                    currency=order.currency,
                    display_name=order.symbol,
                ),
            )
        intervals: list[Interval] = ["1d", "1h"]
        data = {
            symbol: {str(interval): self.store.read(item, interval) for interval in intervals}
            for symbol, item in instruments.items()
        }
        held = {s for s, p in account.positions.items() if p.quantity}
        screening = screen(list(instruments.values()), data, held, market, as_of, self.screening)
        # Cross-market holdings and pending BUYs also need honest valuation/reservations.
        marks = known_marks(
            list(instruments.values()),
            {s: intervals["1h"] for s, intervals in data.items()},
            as_of,
            "paper",
        )
        for candidate in screening.candidates:
            if candidate.held and candidate.issue and "discontinuity" in candidate.issue:
                raise ValueError("held raw-price discontinuity prevents reliable valuation")
        portfolio = value_portfolio(account, marks, fx, as_of, self.broker.rules)
        context = DecisionContext(
            as_of=as_of,
            market=market,
            screening=screening,
            account=account,
            portfolio=portfolio,
            fx=fx,
            rules=self.broker.rules,
            pending_orders=pending,
        )
        return context, marks

    def run(
        self,
        market: Market,
        as_of: datetime,
        fx: FXRate | None = None,
        provider: Literal["mock", "gemini"] = "mock",
        submit: bool = False,
        decision_id: str | None = None,
        strategy: TradingStrategy | None = None,
    ) -> DecisionRun:
        as_of = utc(as_of)
        model = self.ai.model if provider == "gemini" else "mock-hold-v1"
        key = decision_id or digest(
            f"{market}:{as_of.isoformat()}:{provider}:{model}:{PROMPT_VERSION}"
        )
        previous = self.decisions.get(key)
        if previous:
            if (previous.market, previous.as_of, previous.provider, previous.model) != (
                market,
                as_of,
                provider,
                model,
            ):
                raise ValueError("decision_id reused with different request")
            if previous.status == "RUNNING":
                raise ValueError("decision already running; inspect history before using a new ID")
            return self.register(previous) if submit and previous.status == "SUCCESS" else previous
        record = DecisionRun(
            decision_id=key,
            started_at=utc(self.clock()),
            as_of=as_of,
            market=market,
            provider=provider,
            model=model,
            prompt_version=PROMPT_VERSION,
            input_hash="",
            account_hash="",
        )
        if not self.decisions.claim(record):
            raise ValueError("decision concurrently claimed; retry after it finishes")
        selected: TradingStrategy = strategy or (
            GeminiTradingStrategy(self.ai, self.decisions, key)
            if provider == "gemini"
            else MockTradingStrategy()
        )
        try:
            context, marks = self.context(market, as_of, fx)
            record = record.model_copy(
                update={
                    "context": context,
                    "input_hash": digest(payload(context)),
                    "account_hash": account_hash(
                        context.account, context.pending_orders, context.rules
                    ),
                }
            )
            self.decisions.save(record)
            if not context.screening.candidates:
                raise ValueError("no usable candidates; API not called")
            result = validate_result(selected.decide(context), context)
            # Guard providers other than Gemini too against echoed credentials.
            result = type(result).model_validate_json(redact(result.model_dump_json()))
            plan = OrderPlanner().plan(key, result, context, marks)
            record = record.model_copy(update={"status": "SUCCESS", "result": result, "plan": plan})
        except Exception as exc:
            if isinstance(exc, ValidationError):
                reason = "validation failed: " + "; ".join(
                    str(e["type"]) for e in exc.errors(include_input=False)
                )
            elif isinstance(exc, ValueError):
                reason = redact(str(exc))[:500]
            else:
                reason = f"{type(exc).__name__}: decision aborted safely"
            record = record.model_copy(update={"status": "FAILED", "error": reason})
        raw = (
            selected.last_response
            if isinstance(selected, GeminiTradingStrategy)
            else (record.result.model_dump_json() if record.result else None)
        )
        record = record.model_copy(
            update={
                "raw_response": redact(raw) if raw else None,
                "usage": self.decisions.usage(key),
            }
        )
        self.decisions.save(record)
        if submit and record.status == "SUCCESS":
            return self.register(record)
        return record

    def register(self, record: DecisionRun) -> DecisionRun:
        try:
            return self._register(record)
        except Exception as exc:
            reason = redact(str(exc))[:500] if isinstance(exc, ValueError) else type(exc).__name__
            self.decisions.registration_error(record.decision_id, reason)
            raise

    def _register(self, record: DecisionRun) -> DecisionRun:
        at = utc(self.clock())
        with self.decisions.repository.transaction() as tx:
            raw = tx.get("decisions", record.decision_id)
            if raw is None:
                raise ValueError("decision must be persisted before registration")
            saved = DecisionRun.model_validate_json(raw)
            if saved.status == "REGISTERED":
                return saved
            if (
                saved.status != "SUCCESS"
                or saved.context is None
                or saved.plan is None
                or saved.result is None
            ):
                raise ValueError("only a validated successful decision may register orders")
            # Only trusted persisted values are authoritative, never the caller's model_copy.
            context = saved.context
            marks = {mark.symbol: mark for mark in context.portfolio.marks}
            marks.update(
                {
                    c.instrument.symbol: c.features.mark
                    for c in context.screening.candidates
                    if c.features
                }
            )
            account = tx.account()
            pending = [Order.model_validate_json(s) for s in tx.records("orders")]
            pending = [order for order in pending if order.status == "PENDING"]
            if account_hash(account, pending, self.broker.rules) != saved.account_hash:
                raise ValueError("account/pending orders/rules changed; create a new decision")
            if (
                not 0
                <= (at - saved.as_of).total_seconds()
                <= self.broker.rules.max_price_age_seconds
            ):
                raise ValueError("saved decision is future or stale; no order registered")
            value_portfolio(account, marks, context.fx, at, self.broker.rules)
            if OrderPlanner().plan(saved.decision_id, saved.result, context, marks) != saved.plan:
                raise ValueError("persisted order plan does not match validated decision")
            candidates = {c.instrument.symbol: c.instrument for c in context.screening.candidates}
            orders = []
            bound = PaperBroker(BoundRepository(tx), self.broker.rules)
            for item in saved.plan.orders:
                mark = marks[item.symbol]
                age = (at - (mark.price_at or mark.available_at)).total_seconds()
                if (
                    mark.available_at > at
                    or not 0 <= age <= self.broker.rules.max_price_age_seconds
                ):
                    raise ValueError("candidate price became future or stale before registration")
                if item.currency == "USD":
                    from trading_bot.portfolio.valuation import validate_fx

                    validate_fx(context.fx, at, self.broker.rules)
                order = bound.place_order(
                    candidates[item.symbol], item.side, item.quantity, at, item.order_id
                )
                if order.status != "PENDING":
                    raise ValueError("paper order validation rejected the batch")
                orders.append(order)
            saved = saved.model_copy(
                update={
                    "status": "REGISTERED",
                    "registered_orders": orders,
                    "registration_error": None,
                }
            )
            tx.put("decisions", saved.decision_id, saved)
            return saved
