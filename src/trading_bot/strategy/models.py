from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from trading_bot.config import Market
from trading_bot.portfolio.models import Account, Contract, FXRate, Order, TradingRules, Valuation
from trading_bot.screening.models import ScreeningResult


class AISettings(Contract):
    model: str = Field(min_length=1)
    approved_free_models: list[str] = Field(min_length=1)
    thinking_level: Literal["minimal", "low", "medium", "high"] = "minimal"
    max_input_tokens_equivalent: int = Field(default=16000, gt=0, le=1000000)
    max_output_tokens: int = Field(default=2048, ge=128, le=32768)
    timeout_seconds: float = Field(default=45, gt=0, le=120)
    retries: int = Field(default=0, ge=0, le=3)
    daily_request_limit: int = Field(default=5, ge=0)
    monthly_request_limit: int = Field(default=100, ge=0)
    max_requests_per_run: int = Field(default=1, ge=1, le=5)
    max_response_bytes: int = Field(default=32768, ge=1024, le=1048576)
    input_usd_per_million: Decimal | None = Field(default=None, ge=0)
    output_usd_per_million: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def approved(self) -> "AISettings":
        if (self.input_usd_per_million is None) != (self.output_usd_per_million is None):
            raise ValueError("both standard paid-equivalent price rates must be set together")
        if self.model not in self.approved_free_models:
            raise ValueError(
                "model must be explicitly approved after checking free-tier availability"
            )
        return self


class DecisionContext(Contract):
    as_of: datetime
    market: Market
    screening: ScreeningResult
    account: Account
    portfolio: Valuation
    fx: FXRate | None
    rules: TradingRules
    pending_orders: list[Order]


class ActionDecision(Contract):
    symbol: str = Field(min_length=1, max_length=32)
    action: Literal["BUY", "SELL", "HOLD"]
    target_weight: float = Field(ge=0, le=0.10, strict=True)
    reason: str = Field(min_length=1, max_length=1000)


class DecisionResult(Contract):
    market: Market
    decisions: list[ActionDecision] = Field(max_length=300)
    market_summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique(self) -> "DecisionResult":
        if len({item.symbol for item in self.decisions}) != len(self.decisions):
            raise ValueError("duplicate decision symbol")
        return self


def validate_result(result: DecisionResult, context: DecisionContext) -> DecisionResult:
    result = DecisionResult.model_validate(result.model_dump())
    if result.market != context.market:
        raise ValueError("AI market mismatch")
    expected = {c.instrument.symbol: c for c in context.screening.candidates}
    if {d.symbol for d in result.decisions} != set(expected):
        raise ValueError("AI symbols must exactly cover the evaluated candidates")
    ratios = {p.symbol: p.ratio for p in context.portfolio.positions}
    for item in result.decisions:
        if expected[item.symbol].features is None and item.action != "HOLD":
            raise ValueError("unavailable features require HOLD")
        current = ratios.get(item.symbol, Decimal(0))
        target = Decimal(str(item.target_weight))
        if item.action == "BUY" and target <= current:
            raise ValueError("BUY target must increase current weight")
        if item.action == "SELL" and (
            (current and target >= current) or (not current and target != 0)
        ):
            raise ValueError("SELL target contradicts current holdings")
    return result


class PlanItem(Contract):
    order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    target_weight: float
    reference_price: Decimal
    estimated_cash_change: Decimal
    estimated_commission: Decimal
    currency: Literal["JPY", "USD"]


class OrderPlan(Contract):
    orders: list[PlanItem] = Field(default_factory=list)
    skipped: dict[str, str] = Field(default_factory=dict)


class APIUsage(Contract):
    request_id: str
    decision_id: str
    at: datetime
    model: str
    status: Literal["RESERVED", "SUCCESS", "ERROR"] = "RESERVED"
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    thought_tokens: int | None = None
    estimated_cost_usd: Decimal | None = None
    error: str | None = None
    http_status: int | None = None


class DecisionRun(Contract):
    decision_id: str
    started_at: datetime
    as_of: datetime
    market: Market
    provider: Literal["mock", "gemini"]
    model: str
    prompt_version: str
    input_hash: str
    account_hash: str
    context: DecisionContext | None = None
    status: Literal["RUNNING", "SUCCESS", "REGISTERED", "FAILED"] = "RUNNING"
    raw_response: str | None = None
    result: DecisionResult | None = None
    plan: OrderPlan | None = None
    registered_orders: list[Order] = Field(default_factory=list)
    usage: list[APIUsage] = Field(default_factory=list)
    error: str | None = None
    registration_error: str | None = None
