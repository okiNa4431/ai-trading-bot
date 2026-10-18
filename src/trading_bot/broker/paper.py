"""Transactional cash-only broker. Placement never executes an order."""

import logging
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Literal
from uuid import uuid4

from trading_bot.broker.execution import eligible_bar, known_marks, window
from trading_bot.config import Instrument
from trading_bot.models import PriceBar
from trading_bot.persistence.sqlite_repository import PortfolioRepository, Session
from trading_bot.portfolio.ledger import apply_execution
from trading_bot.portfolio.models import (
    ZERO,
    Account,
    CashEntry,
    Currency,
    Execution,
    FXExchange,
    FXRate,
    Mark,
    Order,
    Position,
    Side,
    TradingRules,
    Valuation,
    money,
    utc,
)
from trading_bot.portfolio.valuation import ValuationUnavailable, validate_fx, value_portfolio
from trading_bot.risk.manager import check_buy

logger = logging.getLogger(__name__)


def ceiling(value: Decimal, currency: Currency) -> Decimal:
    return value.quantize(
        Decimal("1") if currency == "JPY" else Decimal("0.01"), rounding=ROUND_CEILING
    )


class PaperBroker:
    def __init__(self, repository: PortfolioRepository, rules: TradingRules | None = None) -> None:
        self.repository = repository
        self.rules = TradingRules.model_validate(rules.model_dump()) if rules else TradingRules()

    def initialize(self, at: datetime) -> Account:
        at = utc(at)
        with self.repository.transaction() as tx:
            if tx.initialized():
                return tx.account()
            account = Account(created_at=at)
            tx.save_account(account)
            self.cash_entry(tx, "initial", "JPY", account.initial_jpy, account, "INITIAL", at)
            return account

    def get_balance(self) -> Account:
        with self.repository.transaction() as tx:
            return tx.account()

    def get_positions(self) -> list[Position]:
        return [p for p in self.get_balance().positions.values() if p.quantity]

    def get_order(self, order_id: str) -> Order:
        with self.repository.transaction() as tx:
            return self.read_order(tx, order_id)

    @staticmethod
    def read_order(tx: Session, order_id: str) -> Order:
        payload = tx.get("orders", order_id)
        if payload is None:
            raise ValueError("unknown order_id")
        return Order.model_validate_json(payload)

    def get_orders(self) -> list[Order]:
        with self.repository.transaction() as tx:
            return [Order.model_validate_json(row) for row in tx.records("orders")]

    def get_executions(self) -> list[Execution]:
        with self.repository.transaction() as tx:
            return [Execution.model_validate_json(row) for row in tx.records("executions")]

    def history(self) -> dict[str, list[str]]:
        with self.repository.transaction() as tx:
            tx.account()
            return {
                name: tx.records(name)
                for name in ("executions", "fx_exchanges", "cash_ledger", "valuations")
            }

    def place_order(
        self,
        instrument: Instrument,
        side: Side,
        quantity: int,
        requested_at: datetime,
        order_id: str | None = None,
        *,
        stop_price: Decimal | None = None,
    ) -> Order:
        order = Order(
            order_id=order_id or str(uuid4()),
            symbol=instrument.symbol,
            market=instrument.market,
            currency=instrument.currency,
            side=side,
            quantity=quantity,
            requested_at=requested_at,
            costs=self.rules,
            order_type="STOP" if stop_price is not None else "MARKET",
            stop_price=stop_price,
        )
        with self.repository.transaction() as tx:
            account = tx.account()
            existing = tx.get("orders", order.order_id)
            if existing:
                previous = Order.model_validate_json(existing)
                fields = {"costs", "status", "reason"}
                if previous.model_dump(exclude=fields) != order.model_dump(exclude=fields):
                    raise ValueError("order_id reused with different request")
                return previous
            if order.requested_at < (account.last_event_at or account.created_at):
                raise ValueError("order predates latest account event")
            reason = None
            if quantity % self.rules.lot(instrument):
                reason = "quantity violates configured lot size"
            position = account.positions.get(instrument.symbol)
            if side == "SELL" and (position is None or position.quantity < quantity):
                reason = "short selling is forbidden"
            if reason:
                order = order.model_copy(update={"status": "REJECTED", "reason": reason})
            tx.put("orders", order.order_id, order)
            return order

    def cancel_order(self, order_id: str) -> Order:
        with self.repository.transaction() as tx:
            order = self.read_order(tx, order_id)
            if order.status == "CANCELLED":
                return order
            if order.status != "PENDING":
                raise ValueError("only pending orders can be cancelled")
            order = order.model_copy(update={"status": "CANCELLED", "reason": "user cancelled"})
            tx.put("orders", order_id, order)
            return order

    def exchange(
        self,
        source: Currency,
        amount: Decimal,
        fx: FXRate,
        at: datetime,
        exchange_id: str | None = None,
    ) -> FXExchange:
        at = utc(at)
        fx = validate_fx(fx, at, self.rules)
        if source not in ("JPY", "USD"):
            raise ValueError("unsupported FX source currency")
        if not amount.is_finite() or amount <= 0 or money(amount, source) != amount:
            raise ValueError("FX amount must be positive and respect currency precision")
        key = exchange_id or str(uuid4())
        with self.repository.transaction() as tx:
            account = tx.account()
            previous = tx.get("fx_exchanges", key)
            if previous:
                event = FXExchange.model_validate_json(previous)
                if (
                    event.source_currency,
                    event.source_amount,
                    event.requested_at,
                    event.usd_jpy,
                    event.rate_timestamp,
                    event.rate_source,
                ) != (
                    source,
                    amount,
                    at,
                    fx.usd_jpy,
                    fx.timestamp,
                    fx.source,
                ):
                    raise ValueError("exchange_id reused with different request")
                return event
            if at < (account.last_event_at or account.created_at):
                raise ValueError("FX exchange predates latest account event")
            if account.balances[source] < amount:
                raise ValueError("insufficient source currency for FX exchange")
            destination: Currency = "USD" if source == "JPY" else "JPY"
            factor = 1 / fx.usd_jpy if source == "JPY" else fx.usd_jpy
            received = money(amount * factor * (1 - self.rules.fx_cost), destination)
            if received <= 0:
                raise ValueError("FX amount is too small after costs")
            event = FXExchange(
                exchange_id=key,
                source_currency=source,
                destination_currency=destination,
                source_amount=amount,
                received_amount=received,
                cost_in_source=amount - received / factor,
                usd_jpy=fx.usd_jpy,
                rate_timestamp=fx.timestamp,
                rate_source=fx.source,
                requested_at=at,
            )
            balances = dict(account.balances)
            balances[source] -= amount
            balances[destination] += received
            account = account.model_copy(update={"balances": balances, "last_event_at": at})
            tx.put("fx_exchanges", key, event)
            self.cash_entry(tx, key, source, -amount, account, "FX", at)
            self.cash_entry(tx, key, destination, received, account, "FX", at)
            tx.save_account(account)
            return event

    @staticmethod
    def cash_entry(
        tx: Session,
        event: str,
        currency: Currency,
        amount: Decimal,
        account: Account,
        kind: Literal["INITIAL", "BUY", "SELL", "FX"],
        at: datetime,
    ) -> None:
        key = f"{kind}:{event}:{currency}"
        tx.put(
            "cash_ledger",
            key,
            CashEntry(
                entry_id=key,
                event_id=event,
                currency=currency,
                amount=amount,
                balance_after=account.balances[currency],
                kind=kind,
                timestamp=at,
            ),
        )

    def execute(
        self,
        order_id: str,
        data: dict[str, list[PriceBar]],
        fx: FXRate | None,
        as_of: datetime,
        mode: Literal["historical", "paper"] = "historical",
    ) -> Order:
        as_of = utc(as_of)
        if mode not in ("historical", "paper"):
            raise ValueError("unknown execution mode")
        with self.repository.transaction() as tx:
            order = self.read_order(tx, order_id)
            if order.status != "PENDING":
                return order
            account = tx.account()
            instrument = Instrument(
                symbol=order.symbol,
                market=order.market,
                currency=order.currency,
                display_name=order.symbol,
            )
            candidate = eligible_bar(
                order, instrument, data.get(order.symbol, []), as_of, mode, order.costs
            )
            if candidate is None:
                order = order.model_copy(update={"reason": "no eligible confirmed future bar"})
                tx.put("orders", order_id, order)
                logger.info("order remains PENDING order_id=%s reason=%s", order_id, order.reason)
                return order
            bar, opened, observed = candidate
            intrabar_stop = (
                order.stop_price is not None and Decimal(str(bar.open)) > order.stop_price
            )
            at = as_of if mode == "paper" else observed if intrabar_stop else opened
            if at < (account.last_event_at or account.created_at):
                raise ValueError("execution predates latest account event")
            reference = Decimal(str(bar.open))
            if order.stop_price is not None:
                # A gap below a SELL stop fills at the worse open, never at an invented stop price.
                reference = min(reference, order.stop_price)
            friction = order.costs.slippage + order.costs.spread / 2
            price = reference * (1 + friction if order.side == "BUY" else 1 - friction)
            raw_notional = price * order.quantity
            notional = (
                ceiling(raw_notional, order.currency)
                if order.side == "BUY"
                else money(raw_notional, order.currency)
            )
            fixed = (
                order.costs.commission_jpy
                if order.currency == "JPY"
                else order.costs.commission_usd
            )
            commission = ceiling(notional * order.costs.commission_rate + fixed, order.currency)
            position = account.positions.get(order.symbol) or Position(
                instrument=instrument,
                quantity=0,
                average_cost=ZERO,
            )
            execution = Execution(
                execution_id=order_id,
                order_id=order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                currency=order.currency,
                reference_price=reference,
                price=price,
                commission=commission,
                slippage_cost=reference * order.costs.slippage * order.quantity,
                spread_cost=reference * order.costs.spread / 2 * order.quantity,
                rounding_cost=abs(notional - raw_notional),
                notional=notional,
                realized_pnl=(
                    notional
                    - commission
                    - (
                        position.cost_basis
                        if order.quantity == position.quantity
                        else position.average_cost * order.quantity
                    )
                    if order.side == "SELL"
                    else ZERO
                ),
                price_at=observed if intrabar_stop else opened,
                observed_at=observed,
                price_fetched_at=bar.fetched_at,
                processed_at=as_of,
                mode=mode,
            )
            instruments = [p.instrument for p in account.positions.values() if p.quantity]
            marks = known_marks(instruments, data, at, mode)
            if mode == "historical":
                # At a historical open, contemporaneous opens also mark OTHER holdings.
                # This avoids reusing stale Friday closes for Monday risk checks.
                for held_instrument in instruments:
                    for held_bar in data.get(held_instrument.symbol, []):
                        span = window(held_bar)
                        if (
                            span
                            and span[0] == at
                            and span[1] <= as_of
                            and span[1] <= held_bar.fetched_at <= as_of
                        ):
                            marks[held_instrument.symbol] = Mark(
                                symbol=held_instrument.symbol,
                                currency=held_instrument.currency,
                                price=Decimal(str(held_bar.open)),
                                available_at=at,
                                price_at=at,
                            )
            marks[order.symbol] = Mark(
                symbol=order.symbol,
                currency=order.currency,
                price=reference,
                available_at=opened if mode == "historical" else observed,
                price_at=opened,
            )
            try:
                updated = apply_execution(account, execution, position)
                valuation = value_portfolio(updated, marks, fx, at, order.costs)
                check_buy(order, valuation)
            except ValueError as exc:
                # Missing marks/rates are recoverable: keep the order pending.
                recoverable = isinstance(exc, ValuationUnavailable)
                order = order.model_copy(
                    update={
                        "status": "PENDING" if recoverable else "REJECTED",
                        "reason": str(exc),
                    }
                )
                tx.put("orders", order_id, order)
                logger.warning(
                    "order %s order_id=%s reason=%s", order.status, order_id, order.reason
                )
                return order
            order = order.model_copy(update={"status": "FILLED", "reason": None})
            tx.put("orders", order_id, order)
            tx.put("executions", execution.execution_id, execution)
            delta = -notional - commission if order.side == "BUY" else notional - commission
            self.cash_entry(tx, order_id, order.currency, delta, updated, order.side, at)
            tx.put("valuations", valuation.valuation_id, valuation)
            tx.save_account(updated)
            logger.info(
                "order FILLED order_id=%s currency=%s price=%s", order_id, order.currency, price
            )
            return order

    def value(self, marks: dict[str, Mark], fx: FXRate | None, as_of: datetime) -> Valuation:
        with self.repository.transaction() as tx:
            account = tx.account()
            if utc(as_of) < (account.last_event_at or account.created_at):
                raise ValueError("valuation predates latest account event")
            valuation = value_portfolio(account, marks, fx, as_of, self.rules)
            tx.put("valuations", valuation.valuation_id, valuation)
            return valuation
