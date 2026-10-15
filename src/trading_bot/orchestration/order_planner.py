"""Sizing is Python-owned. Planned sells never fund unfilled buys; no automatic FX."""

import hashlib
from datetime import datetime
from decimal import Decimal

from trading_bot.broker.paper import ceiling
from trading_bot.config import Instrument
from trading_bot.portfolio.ledger import apply_execution
from trading_bot.portfolio.models import (
    ZERO,
    Account,
    Execution,
    Mark,
    Position,
    Side,
    TradingRules,
    money,
)
from trading_bot.portfolio.valuation import validate_fx, value_portfolio
from trading_bot.strategy.models import (
    DecisionContext,
    DecisionResult,
    OrderPlan,
    PlanItem,
    validate_result,
)


def estimate(
    account: Account,
    instrument: Instrument,
    side: Side,
    quantity: int,
    mark: Mark,
    rules: TradingRules,
    at: datetime,
) -> tuple[Account, Decimal, Decimal]:
    price = mark.price * (1 + rules.slippage if side == "BUY" else 1 - rules.slippage)
    raw = price * quantity
    notional = (
        ceiling(raw, instrument.currency) if side == "BUY" else money(raw, instrument.currency)
    )
    fixed = rules.commission_jpy if instrument.currency == "JPY" else rules.commission_usd
    fee = ceiling(notional * rules.commission_rate + fixed, instrument.currency)
    position = account.positions.get(instrument.symbol) or Position(
        instrument=instrument,
        quantity=0,
        average_cost=ZERO,
    )
    basis = (
        position.cost_basis if quantity == position.quantity else position.average_cost * quantity
    )
    execution = Execution(
        execution_id="estimate",
        order_id="estimate",
        symbol=instrument.symbol,
        side=side,
        quantity=quantity,
        currency=instrument.currency,
        reference_price=mark.price,
        price=price,
        commission=fee,
        slippage_cost=abs(price - mark.price) * quantity,
        rounding_cost=abs(notional - raw),
        notional=notional,
        realized_pnl=notional - fee - basis if side == "SELL" else ZERO,
        price_at=at,
        observed_at=at,
        price_fetched_at=mark.available_at,
        processed_at=at,
        mode="historical",
    )
    delta = -notional - fee if side == "BUY" else notional - fee
    return apply_execution(account, execution, position), delta, fee


class OrderPlanner:
    def plan(
        self,
        decision_id: str,
        result: DecisionResult,
        context: DecisionContext,
        marks: dict[str, Mark],
    ) -> OrderPlan:
        validate_result(result, context)
        rules = context.rules
        account = context.account
        total = context.portfolio.total_jpy
        candidates = {c.instrument.symbol: c for c in context.screening.candidates}
        pending = [o for o in context.pending_orders if o.status == "PENDING"]
        skips: dict[str, str] = {}
        plans = []
        reservation_error = None
        # Reserve ALL existing pending BUYs at current estimated costs, across markets.
        # They can still reject at execution if prices change; proceeds of SELLs are never credited.
        try:
            for order in pending:
                if order.side != "BUY":
                    continue
                mark = marks.get(order.symbol)
                if mark is None:
                    raise ValueError("cannot reserve pending BUY without a current price")
                instrument = Instrument(
                    symbol=order.symbol,
                    market=order.market,
                    currency=order.currency,
                    display_name=order.symbol,
                )
                account, _, _ = estimate(
                    account, instrument, "BUY", order.quantity, mark, order.costs, context.as_of
                )
            reserved_value = value_portfolio(account, marks, context.fx, context.as_of, rules)
            if reserved_value.breaches:
                reservation_error = "pending BUY reservations already exceed risk limits"
        except ValueError:
            reservation_error = "pending BUY budget cannot be safely valued/reserved"
        for item in result.decisions:
            symbol = item.symbol
            instrument = candidates[symbol].instrument
            mark = marks.get(symbol)
            if item.action == "HOLD":
                skips[symbol] = "HOLD: no order"
                continue
            if any(order.symbol == symbol for order in pending):
                skips[symbol] = "existing PENDING order for this symbol"
                continue
            if mark is None:
                skips[symbol] = "current price unavailable"
                continue
            try:
                # Validate price freshness even for a currently unheld candidate.
                age = (context.as_of - (mark.price_at or mark.available_at)).total_seconds()
                if mark.available_at > context.as_of or not 0 <= age <= rules.max_price_age_seconds:
                    raise ValueError("candidate price is future or stale")
                rate = (
                    validate_fx(context.fx, context.as_of, rules).usd_jpy
                    if instrument.currency == "USD"
                    else Decimal(1)
                )
            except ValueError:
                skips[symbol] = "fresh price/FX rate required; no implicit conversion"
                continue
            actual = context.account.positions.get(symbol)
            current = actual.quantity if actual else 0
            lot = rules.lot(instrument)
            per_share_jpy = mark.price * rate
            target = Decimal(str(item.target_weight)) * total
            if item.action == "SELL":
                excess = max(ZERO, current * per_share_jpy - target)
                quantity = min(current, int(excess / per_share_jpy / lot) * lot)
                if quantity <= 0:
                    skips[symbol] = "no holdings/excess meeting minimum sell lot"
                    continue
                try:
                    _, delta, fee = estimate(
                        context.account, instrument, "SELL", quantity, mark, rules, context.as_of
                    )
                except ValueError:
                    skips[symbol] = "sell costs exceed available cash/holdings"
                    continue
            else:
                if reservation_error:
                    skips[symbol] = reservation_error
                    continue
                desired = max(ZERO, target - current * per_share_jpy)
                hi = int(desired / per_share_jpy / lot)
                lo = 0
                # Monotone post-cost cash/exposure constraints: binary-search whole lots.
                while lo < hi:
                    middle = (lo + hi + 1) // 2
                    try:
                        updated, _, _ = estimate(
                            account, instrument, "BUY", middle * lot, mark, rules, context.as_of
                        )
                        value = value_portfolio(updated, marks, context.fx, context.as_of, rules)
                        weight_value = next(
                            p.market_value_jpy for p in value.positions if p.symbol == symbol
                        )
                        allowed = (
                            not value.breaches
                            and weight_value <= Decimal(str(item.target_weight)) * value.total_jpy
                        )
                    except ValueError:
                        allowed = False
                    if allowed:
                        lo = middle
                    else:
                        hi = middle - 1
                quantity = lo * lot
                if not quantity:
                    skips[symbol] = (
                        "minimum lot exceeds target, native-currency cash or risk budget"
                    )
                    continue
                account, delta, fee = estimate(
                    account, instrument, "BUY", quantity, mark, rules, context.as_of
                )
            identity = hashlib.sha256(f"{decision_id}:{symbol}:{item.action}".encode()).hexdigest()
            plans.append(
                PlanItem(
                    order_id=f"ai-{identity}",
                    symbol=symbol,
                    side=item.action,
                    quantity=quantity,
                    target_weight=item.target_weight,
                    reference_price=mark.price,
                    estimated_cash_change=delta,
                    estimated_commission=fee,
                    currency=instrument.currency,
                )
            )
        return OrderPlan(orders=plans, skipped=skips)
