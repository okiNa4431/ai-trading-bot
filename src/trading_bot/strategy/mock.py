from trading_bot.strategy.models import (
    ActionDecision,
    DecisionContext,
    DecisionResult,
    validate_result,
)


class MockTradingStrategy:
    def decide(self, context: DecisionContext) -> DecisionResult:
        return validate_result(
            DecisionResult(
                market=context.market,
                decisions=[
                    ActionDecision(
                        symbol=c.instrument.symbol,
                        action="HOLD",
                        target_weight=0.0,
                        reason="モック判断：売買せず現金・現在保有を維持",
                    )
                    for c in context.screening.candidates
                ],
                market_summary="モックモード。実API通信なし。売買を強制しません。",
            ),
            context,
        )
