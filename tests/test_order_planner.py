from decimal import Decimal

import pytest

from tests.ai_helpers import (
    JP,
    NOW,
    US,
    BuyStrategy,
    context_with_holding,
    environment,
)
from trading_bot.config import Instrument
from trading_bot.orchestration.order_planner import OrderPlanner, estimate
from trading_bot.portfolio.models import FXRate, Mark, Position
from trading_bot.portfolio.valuation import value_portfolio
from trading_bot.strategy.models import ActionDecision, DecisionResult, validate_result


def inputs(engine, market="JP", fx=None):
    context, marks = engine.context(market, NOW, fx)
    return context, marks


def test_buy_rounds_down_to_jp_lot_and_includes_costs(tmp_path):
    engine = environment(tmp_path)
    context, marks = inputs(engine)
    result = BuyStrategy().decide(context)
    plan = OrderPlanner().plan("id", result, context, marks)
    order = plan.orders[0]
    assert order.quantity == 400 and order.quantity % 100 == 0
    assert order.estimated_cash_change == Decimal("-48449")
    assert OrderPlanner().plan("id", result, context, marks) == plan
    assert not engine.broker.get_orders()


def test_sell_and_hold_meanings_and_unheld_sell(tmp_path):
    engine = environment(tmp_path)
    context, marks = inputs(engine)
    context = context_with_holding(context)
    result = DecisionResult(
        market="JP",
        market_summary="Synthetic",
        decisions=[
            ActionDecision(symbol=JP.symbol, action="SELL", target_weight=0.02, reason="Synthetic"),
        ],
    )
    plan = OrderPlanner().plan("sell", result, context, marks)
    assert plan.orders[0].quantity == 200
    hold = result.model_copy(
        update={
            "decisions": [
                result.decisions[0].model_copy(update={"action": "HOLD", "target_weight": 0.0})
            ]
        }
    )
    assert not OrderPlanner().plan("hold", hold, context, marks).orders
    empty, marks = inputs(engine)
    result = result.model_copy(
        update={"decisions": [result.decisions[0].model_copy(update={"target_weight": 0.0})]}
    )
    assert not OrderPlanner().plan("empty", result, empty, marks).orders


def test_sell_does_not_round_small_excess_up_to_a_lot(tmp_path):
    engine = environment(tmp_path)
    context, marks = inputs(engine)
    context = context_with_holding(context, quantity=200)
    result = DecisionResult(
        market="JP",
        market_summary="Synthetic",
        decisions=[
            ActionDecision(symbol=JP.symbol, action="SELL", target_weight=0.02, reason="Synthetic"),
        ],
    )
    assert not OrderPlanner().plan("small", result, context, marks).orders


def test_minimum_lot_and_zero_cash_prevent_buy(tmp_path):
    engine = environment(tmp_path)
    context, marks = inputs(engine)
    result = BuyStrategy(weight=0.001).decide(context)
    plan = OrderPlanner().plan("tiny", result, context, marks)
    assert not plan.orders and "minimum lot" in plan.skipped[JP.symbol]


def test_us_integer_shares_explicit_fx_and_native_cash(tmp_path):
    engine = environment(tmp_path, [US])
    context, marks = inputs(engine, "US")
    result = BuyStrategy(weight=0.08).decide(context)
    assert not OrderPlanner().plan("no-fx", result, context, marks).orders
    rate = FXRate(usd_jpy=Decimal(150), timestamp=NOW, source="fixed-simulation")
    context = context.model_copy(update={"fx": rate})
    assert not OrderPlanner().plan("no-usd", result, context, marks).orders
    account = context.account.model_copy(
        update={"balances": {"JPY": Decimal(900000), "USD": Decimal(665)}}
    )
    context = context.model_copy(
        update={
            "account": account,
            "portfolio": value_portfolio(account, marks, rate, NOW, context.rules),
        }
    )
    plan = OrderPlanner().plan("funded", result, context, marks)
    assert plan.orders[0].quantity == 4
    assert plan.orders[0].currency == "USD"
    assert -plan.orders[0].estimated_cash_change <= 665


def test_multiple_buys_stay_within_ten_eighty_and_cash_limits(tmp_path):
    instruments = [
        Instrument(symbol=f"{1000 + i}.T", market="JP", currency="JPY", display_name="Synthetic")
        for i in range(10)
    ]
    engine = environment(tmp_path, instruments)
    engine.screening = engine.screening.model_copy(update={"top_k": 10})
    context, marks = inputs(engine)
    plan = OrderPlanner().plan("batch", BuyStrategy(weight=0.10).decide(context), context, marks)
    assert plan.orders
    account = context.account
    for item in plan.orders:
        instrument = next(i for i in instruments if i.symbol == item.symbol)
        account, _, _ = estimate(
            account, instrument, item.side, item.quantity, marks[item.symbol], context.rules, NOW
        )
    value = value_portfolio(account, marks, None, NOW, context.rules)
    assert value.stock_ratio <= Decimal("0.8") and value.cash_ratio >= Decimal("0.2")
    assert all(p.ratio <= Decimal("0.1") for p in value.positions)
    assert len(plan.orders) < 10 or plan.orders[-1].quantity < plan.orders[0].quantity


def test_pending_orders_reserved_and_duplicate_symbol_skipped(tmp_path):
    other = Instrument(symbol="6758.T", market="JP", currency="JPY", display_name="Synthetic")
    engine = environment(tmp_path, [JP, other])
    engine.broker.place_order(JP, "BUY", 5000, NOW)
    context, marks = inputs(engine)
    result = BuyStrategy().decide(context)
    plan = OrderPlanner().plan("pending", result, context, marks)
    assert not plan.orders
    assert "PENDING" in plan.skipped[JP.symbol]
    assert "pending BUY" in plan.skipped[other.symbol]


def test_pending_sells_do_not_fund_buys(tmp_path):
    instruments = [
        Instrument(symbol=f"{1000 + i}.T", market="JP", currency="JPY", display_name="Synthetic")
        for i in range(9)
    ]
    engine = environment(tmp_path, instruments)
    engine.screening = engine.screening.model_copy(update={"top_k": 10})
    context, marks = inputs(engine)
    positions = {
        i.symbol: Position(
            instrument=i, quantity=1000, average_cost=Decimal(100), cost_basis=Decimal(100000)
        )
        for i in instruments[:8]
    }
    marks = {
        i.symbol: Mark(symbol=i.symbol, currency="JPY", price=Decimal(100), available_at=NOW)
        for i in instruments
    }
    account = context.account.model_copy(
        update={"positions": positions, "balances": {"JPY": Decimal(200000), "USD": Decimal(0)}}
    )
    context = context.model_copy(
        update={
            "account": account,
            "portfolio": value_portfolio(account, marks, None, NOW, context.rules),
        }
    )
    result = DecisionResult(
        market="JP",
        market_summary="Synthetic",
        decisions=[
            ActionDecision(
                symbol=c.instrument.symbol,
                action="SELL"
                if c.instrument.symbol == instruments[0].symbol
                else "BUY"
                if c.instrument.symbol == instruments[-1].symbol
                else "HOLD",
                target_weight=0.0 if c.instrument.symbol == instruments[0].symbol else 0.10,
                reason="Synthetic",
            )
            for c in context.screening.candidates
        ],
    )
    plan = OrderPlanner().plan("no-funding", result, context, marks)
    assert [p.side for p in plan.orders] == ["SELL"]


def test_candidate_stale_mark_and_direction_contradiction(tmp_path):
    from datetime import timedelta

    engine = environment(tmp_path)
    context, marks = inputs(engine)
    marks[JP.symbol] = marks[JP.symbol].model_copy(update={"price_at": NOW - timedelta(days=2)})
    assert not OrderPlanner().plan("old", BuyStrategy().decide(context), context, marks).orders
    held = context_with_holding(context)
    with pytest.raises(ValueError, match="increase"):
        validate_result(BuyStrategy(weight=0.01).decide(held), held)
