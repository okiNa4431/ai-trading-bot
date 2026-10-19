"""Validated internal draft only. Deliberately has no send/client method."""

import json
from decimal import Decimal
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import Field

from trading_bot.portfolio.models import Contract
from trading_bot.strategy.gemini import unique_object
from trading_bot.strategy.models import AISettings, DecisionContext, DecisionResult, validate_result
from trading_bot.strategy.prompt import response_schema
from trading_bot.swing.strategies import SWING_SYSTEM, compact_payload


class DraftInput(Contract):
    version: Literal["internal-swing-v1"] = "internal-swing-v1"
    external_send_allowed: Literal[False] = False
    origin: Literal["market", "synthetic", "unknown"]
    context: DecisionContext
    compact_json: str
    input_token_equivalent_upper: int = Field(gt=0)
    output_token_budget: int = Field(gt=0)
    paid_equivalent_upper_usd: Decimal | None
    price_basis: Literal["configured rates; not a billing quote"] = (
        "configured rates; not a billing quote"
    )


def build_draft(context: DecisionContext, settings: AISettings) -> DraftInput:
    text = compact_payload(context)
    bound = len((SWING_SYSTEM + text + json.dumps(response_schema(context))).encode()) + 1024
    if bound > settings.max_input_tokens_equivalent:
        raise ValueError("internal input exceeds conservative byte/token-equivalent budget")
    cost = None
    if settings.input_usd_per_million is not None and settings.output_usd_per_million is not None:
        cost = (
            bound * settings.input_usd_per_million
            + settings.max_output_tokens * settings.output_usd_per_million
        ) / 1000000
    draft = DraftInput(
        origin=context.data_origin,
        context=context,
        compact_json=text,
        input_token_equivalent_upper=bound,
        output_token_budget=settings.max_output_tokens,
        paid_equivalent_upper_usd=cost,
    )
    Draft202012Validator(DraftInput.model_json_schema()).validate(draft.model_dump(mode="json"))
    return draft


def parse_offline_response(
    text: str, context: DecisionContext, settings: AISettings
) -> DecisionResult:
    if len(text.encode()) > settings.max_response_bytes:
        raise ValueError("offline response byte budget exceeded")
    try:
        value = json.loads(text, object_pairs_hook=unique_object)
        Draft202012Validator(response_schema(context)).validate(value)
        return validate_result(DecisionResult.model_validate(value), context)
    except Exception:
        raise ValueError("invalid offline structured output; no orders") from None
