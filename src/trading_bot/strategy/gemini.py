"""Google's official SDK. Explicit opt-in, bounded calls, no tools or paid fallback."""

import json
import logging
import os
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import httpx
from dotenv import load_dotenv
from google import genai  # noqa: F401 -- retained as the synthetic SDK test patch namespace
from google.genai import types

from trading_bot.data_access import reject_diagnostic
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.strategy.models import AISettings, DecisionContext, DecisionResult, validate_result
from trading_bot.strategy.prompt import SYSTEM, payload, response_schema
from trading_bot.strategy.security import redact

logger = logging.getLogger(__name__)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class GeminiTradingStrategy:
    def __init__(
        self,
        settings: AISettings,
        repository: DecisionRepository,
        decision_id: str,
        env_file: Path = Path(".env"),
        *,
        system_instruction: str = SYSTEM,
        payload_builder: Callable[[DecisionContext], str] = payload,
        offline_sdk_factory: Mock | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.decision_id = decision_id
        self.env_file = env_file
        self.last_response: str | None = None
        self.system_instruction = system_instruction
        self.payload_builder = payload_builder
        self.offline_sdk_factory = offline_sdk_factory

    def decide(self, context: DecisionContext) -> DecisionResult:
        reject_diagnostic(context)
        if context.data_origin != "synthetic" or any(
            c.features is not None and c.features.source == "alpaca"
            for c in context.screening.candidates
        ):
            raise ValueError("external AI blocked for market-derived or unknown inputs")
        if not isinstance(self.offline_sdk_factory, Mock):
            raise ValueError("real Gemini transport disabled; synthetic offline SDK mock only")
        schema = response_schema(context)
        text = self.payload_builder(context)
        # UTF-8 bytes + schema/system + headroom: deliberately conservative token equivalent.
        estimated = (
            len((self.system_instruction + text + json.dumps(schema)).encode("utf-8")) + 1024
        )
        if estimated > self.settings.max_input_tokens_equivalent:
            raise ValueError("AI input exceeds conservative token-equivalent byte budget")
        load_dotenv(self.env_file, override=False)
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY is unavailable; no request made")
        for name in ("google_genai", "google.genai", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        # Pin the public Developer API; do not inherit Vertex/endpoint env overrides.
        client = self.offline_sdk_factory(
            api_key=key,
            vertexai=False,
            http_options=types.HttpOptions(
                base_url="https://generativelanguage.googleapis.com",
                timeout=int(self.settings.timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        if not isinstance(client, Mock):
            raise ValueError("offline SDK factory must return a mock client")
        try:
            for attempt in range(self.settings.retries + 1):
                usage = self.repository.reserve(self.decision_id, self.settings)
                try:
                    response = client.models.generate_content(
                        model=self.settings.model,
                        contents=text,
                        config=types.GenerateContentConfig(
                            system_instruction=self.system_instruction,
                            response_mime_type="application/json",
                            response_json_schema=schema,
                            max_output_tokens=self.settings.max_output_tokens,
                            thinking_config=types.ThinkingConfig(
                                thinking_level=types.ThinkingLevel(self.settings.thinking_level)
                            ),
                            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                                disable=True
                            ),
                            temperature=0.2,
                        ),
                    )
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    timeout = isinstance(exc, (TimeoutError, httpx.TimeoutException))
                    category = (
                        "rate_limit" if code == 429 else "timeout" if timeout else "api_error"
                    )
                    self.repository.save_usage(
                        usage.model_copy(
                            update={
                                "status": "ERROR",
                                "error": category,
                                "http_status": code if isinstance(code, int) else None,
                            }
                        )
                    )
                    logger.warning(
                        "Gemini request failed category=%s http_status=%s attempt=%d",
                        category,
                        code if isinstance(code, int) else None,
                        attempt + 1,
                    )
                    transient = (
                        timeout
                        or isinstance(exc, httpx.TransportError)
                        or code in (500, 502, 503, 504)
                    )
                    if not transient or attempt == self.settings.retries:
                        raise ValueError(
                            f"Gemini request failed: {category}; no fallback"
                        ) from None
                    time.sleep(min(2**attempt, 8))
                    continue
                metadata = response.usage_metadata
                updates: dict[str, object] = {"status": "SUCCESS"}
                if metadata is not None:
                    updates.update(
                        input_tokens=metadata.prompt_token_count,
                        output_tokens=metadata.candidates_token_count,
                        total_tokens=metadata.total_token_count,
                        thought_tokens=metadata.thoughts_token_count,
                    )
                    input_tokens = metadata.prompt_token_count
                    total_tokens = metadata.total_token_count
                    if (
                        input_tokens is not None
                        and total_tokens is not None
                        and total_tokens >= input_tokens
                        and self.settings.input_usd_per_million is not None
                        and self.settings.output_usd_per_million is not None
                    ):
                        updates["estimated_cost_usd"] = (
                            Decimal(input_tokens) * self.settings.input_usd_per_million
                            + Decimal(total_tokens - input_tokens)
                            * self.settings.output_usd_per_million
                        ) / Decimal(1000000)
                self.repository.save_usage(usage.model_copy(update=updates))
                self.last_response = redact(response.text or "")
                if len(self.last_response.encode("utf-8")) > self.settings.max_response_bytes:
                    self.last_response = "[response exceeded configured size limit]"
                    raise ValueError("Gemini response exceeds byte budget")
                try:
                    result = DecisionResult.model_validate(
                        json.loads(
                            self.last_response,
                            object_pairs_hook=unique_object,
                        )
                    )
                    return validate_result(result, context)
                except ValueError:
                    raise ValueError(
                        "invalid Gemini structured output; no orders planned"
                    ) from None
            raise AssertionError("unreachable")
        finally:
            client.close()
