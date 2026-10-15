from typing import Protocol

from trading_bot.strategy.models import DecisionContext, DecisionResult


class TradingStrategy(Protocol):
    def decide(self, context: DecisionContext) -> DecisionResult: ...
