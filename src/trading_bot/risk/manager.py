"""Risk limits are checked against the post-cost, post-trade JPY valuation."""

from trading_bot.portfolio.models import Order, Valuation


def check_buy(order: Order, valuation: Valuation) -> None:
    if order.side == "BUY" and valuation.breaches:
        raise ValueError("risk limit: " + "; ".join(valuation.breaches))
