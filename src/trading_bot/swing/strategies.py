"""All proposals go through the existing Python OrderPlanner and PaperBroker."""

import json
from typing import Literal

from trading_bot.strategy.gemini import GeminiTradingStrategy
from trading_bot.strategy.models import (
    ActionDecision,
    DecisionContext,
    DecisionResult,
    validate_result,
)
from trading_bot.swing.models import DataRights, SwingSettings

SWING_SYSTEM = """You advise a simulated cash-only swing portfolio, usually held for 5-20
exchange sessions, without mandatory liquidation at a fixed age. Use ONLY supplied causal
features, not outside knowledge or remembered future events. HOLD/cash are valid; never
force turnover. Return BUY/SELL/HOLD, post-trade target_weight <=0.10, concise reason and
risk_factors for EVERY candidate including holdings and pending symbols. Missing features
require HOLD. No shorts, leverage, order sizing or execution authority. Respect total stocks
<=80% and cash >=20%. Python validates risk, quantities and execution separately.
"""


def compact_payload(context: DecisionContext) -> str:
    # This is compact, NOT proof of a license or of non-reconstructability.
    return json.dumps(
        {
            "as_of": context.as_of.isoformat(),
            "market": context.market,
            "stock_ratio": str(context.portfolio.stock_ratio),
            "candidates": [
                {
                    "symbol": c.instrument.symbol,
                    "held": c.held,
                    "features": None
                    if c.features is None
                    else {
                        "return20": c.features.momentum20,
                        "return60": c.features.momentum60,
                        "relative_strength": c.features.relative_strength,
                        "rsi14": c.features.rsi14,
                        "volatility": c.features.volatility20,
                        "distance_sma20": c.features.daily_close / c.features.sma20 - 1,
                        "sector": c.features.sector,
                    },
                    "weight": next(
                        (
                            float(p.ratio)
                            for p in context.portfolio.positions
                            if p.symbol == c.instrument.symbol
                        ),
                        0,
                    ),
                }
                for c in context.screening.candidates
            ],
            "pending": [
                {"symbol": o.symbol, "side": o.side}
                for o in context.pending_orders
                if o.status == "PENDING"
            ],
        },
        separators=(",", ":"),
        allow_nan=False,
    )


class SwingGeminiStrategy:
    def __init__(
        self, delegate: GeminiTradingStrategy, rights: DataRights, network_enabled: bool = False
    ) -> None:
        self.delegate, self.rights, self.network_enabled = delegate, rights, network_enabled

    def decide(self, context: DecisionContext) -> DecisionResult:
        if not self.network_enabled or not self.rights.external_ai:
            raise ValueError("swing Gemini disabled: explicit activation and data rights required")
        self.delegate.system_instruction = SWING_SYSTEM
        self.delegate.payload_builder = compact_payload
        return self.delegate.decide(context)


class RuleSwingStrategy:
    def __init__(
        self,
        kind: Literal["rule", "momentum", "cash", "gemini_mock"] = "rule",
        settings: SwingSettings | None = None,
    ):
        self.kind = kind
        self.settings = settings or SwingSettings()

    def decide(self, context: DecisionContext) -> DecisionResult:
        ratios = {p.symbol: float(p.ratio) for p in context.portfolio.positions}
        sector_counts: dict[str, int] = {}
        for candidate in context.screening.candidates:
            if candidate.held and candidate.features and candidate.features.sector:
                sector = candidate.features.sector
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
        decisions = []
        for candidate in context.screening.candidates:
            f, symbol = candidate.features, candidate.instrument.symbol
            weight = ratios.get(symbol, 0)
            action: Literal["BUY", "SELL", "HOLD"] = "HOLD"
            target = weight
            if f is not None and self.kind not in ("cash", "gemini_mock"):
                momentum = f.momentum20 or 0
                entry = (
                    momentum > 0
                    if self.kind == "momentum"
                    else (
                        f.daily_close > (f.sma50 or f.sma20)
                        and 40 <= f.rsi14 <= 75
                        and momentum > 0
                        and (f.relative_strength or 0) >= -0.01
                    )
                )
                exit_signal = (
                    momentum < 0
                    if self.kind == "momentum"
                    else (f.daily_close < f.sma20 * 0.97 or f.rsi14 < 35)
                )
                if self.kind == "rule":
                    age = context.holding_sessions.get(symbol, 0)
                    exit_signal = (
                        exit_signal
                        or (
                            age >= self.settings.min_holding_sessions
                            and momentum < 0
                            and f.daily_close < (f.sma50 or f.sma20)
                        )
                        or (age >= self.settings.expected_holding_sessions and momentum < 0)
                    )
                if weight > 0 and exit_signal:
                    action, target = "SELL", 0
                elif entry and weight < 0.075 and candidate.issue is None:
                    sector = f.sector or "unknown"
                    if weight or sector_counts.get(sector, 0) < self.settings.sector_limit:
                        action, target = "BUY", 0.08
                        if not weight:
                            sector_counts[sector] = sector_counts.get(sector, 0) + 1
                if weight > 0.10:
                    action, target = "SELL", 0.08
            decisions.append(
                ActionDecision(
                    symbol=symbol,
                    action=action,
                    target_weight=min(target, 0.10),
                    reason=f"{self.kind}: causal swing proposal; no mandatory age exit",
                    risk_factors=["gap", "corporate_action", "synthetic research"],
                )
            )
        return validate_result(
            DecisionResult(
                market="US",
                decisions=decisions,
                market_summary=f"{self.kind}; gemini_mock is HOLD, not real Gemini inference",
            ),
            context,
        )
