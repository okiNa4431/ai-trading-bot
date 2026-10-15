"""Point-in-time JPY valuation; missing/stale/future inputs never become zero marks."""

import logging
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from trading_bot.portfolio.models import (
    ZERO,
    Account,
    FXRate,
    Mark,
    PositionValue,
    TradingRules,
    Valuation,
    utc,
)

logger = logging.getLogger(__name__)


class ValuationUnavailable(ValueError):
    """More/fresher price or FX observations may make the operation possible later."""


def validate_fx(fx: FXRate | None, as_of: datetime, rules: TradingRules) -> FXRate:
    if fx is None:
        raise ValuationUnavailable("USD/JPY rate is required")
    fx = FXRate.model_validate(fx.model_dump())
    age = (utc(as_of) - fx.timestamp).total_seconds()
    if age < 0 or age > rules.max_fx_age_seconds:
        raise ValuationUnavailable("USD/JPY rate is future or stale")
    return fx


def value_portfolio(
    account: Account,
    marks: dict[str, Mark],
    fx: FXRate | None,
    as_of: datetime,
    rules: TradingRules,
) -> Valuation:
    as_of = utc(as_of)
    needs_fx = account.balances["USD"] != 0 or any(
        p.instrument.currency == "USD" and p.quantity for p in account.positions.values()
    )
    rate = validate_fx(fx, as_of, rules).usd_jpy if needs_fx else Decimal(1)
    cash = account.balances["JPY"] + account.balances["USD"] * rate
    values: list[PositionValue] = []
    used_marks: list[Mark] = []
    for symbol, position in account.positions.items():
        if not position.quantity:
            continue
        mark = marks.get(symbol)
        if mark is None:
            raise ValuationUnavailable(f"missing price for {symbol}")
        mark = Mark.model_validate(mark.model_dump())
        if mark.symbol != symbol or mark.currency != position.instrument.currency:
            raise ValueError(f"price metadata mismatch for {symbol}")
        age = (as_of - mark.available_at).total_seconds()
        if age < 0 or age > rules.max_price_age_seconds:
            raise ValuationUnavailable(f"future or stale price for {symbol}")
        used_marks.append(mark)
        native = mark.price * position.quantity
        values.append(
            PositionValue(
                symbol=symbol,
                currency=mark.currency,
                quantity=position.quantity,
                price=mark.price,
                market_value=native,
                market_value_jpy=native * (rate if mark.currency == "USD" else 1),
                unrealized_pnl=native - position.cost_basis,
                ratio=ZERO,
            )
        )
    stocks = sum((p.market_value_jpy for p in values), ZERO)
    total = cash + stocks
    if total <= 0:
        raise ValueError("portfolio total must be positive")
    values = [p.model_copy(update={"ratio": p.market_value_jpy / total}) for p in values]
    breaches = [
        f"symbol ratio exceeded: {p.symbol}"
        for p in values
        if p.market_value_jpy > total * rules.max_symbol_ratio
    ]
    if stocks > total * rules.max_stock_ratio:
        breaches.append("stock ratio exceeded")
    if cash < total * rules.min_cash_ratio:
        breaches.append("cash ratio below minimum")
    for breach in breaches:
        logger.warning("valuation breach as_of=%s reason=%s", as_of, breach)
    return Valuation(
        valuation_id=str(uuid4()),
        as_of=as_of,
        total_jpy=total,
        cash_jpy=cash,
        stocks_jpy=stocks,
        stock_ratio=stocks / total,
        cash_ratio=cash / total,
        positions=values,
        realized_pnl=account.realized_pnl,
        breaches=breaches,
        marks=used_marks,
        fx=fx if needs_fx else None,
    )
