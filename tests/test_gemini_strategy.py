import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from google.genai import errors

from tests.ai_helpers import NOW, ai_settings, environment
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.strategy.gemini import GeminiTradingStrategy
from trading_bot.strategy.mock import MockTradingStrategy


@pytest.fixture
def setup_ai(tmp_path, monkeypatch):
    engine = environment(tmp_path)
    context, _ = engine.context("JP", NOW, None)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-secret")
    strategy = GeminiTradingStrategy(
        ai_settings(), engine.decisions, "sdk-test", tmp_path / "absent.env"
    )
    return strategy, context, engine


def response(context, text=None):
    return SimpleNamespace(
        text=text or MockTradingStrategy().decide(context).model_dump_json(),
        usage_metadata=SimpleNamespace(
            prompt_token_count=123,
            candidates_token_count=45,
            total_token_count=168,
            thoughts_token_count=0,
        ),
    )


def test_structured_response_tokens_and_sdk_options(setup_ai):
    strategy, context, engine = setup_ai
    client = Mock()
    client.models.generate_content.return_value = response(context)
    with patch("trading_bot.strategy.gemini.genai.Client", return_value=client) as constructor:
        result = strategy.decide(context)
    assert result.decisions[0].action == "HOLD"
    assert client.models.generate_content.call_count == 1
    options = constructor.call_args.kwargs["http_options"]
    assert options.retry_options.attempts == 1
    assert options.timeout == 45000
    assert constructor.call_args.kwargs["vertexai"] is False
    config = client.models.generate_content.call_args.kwargs["config"]
    assert config.max_output_tokens == 2048
    assert config.thinking_config.thinking_level.value == "MINIMAL"
    assert config.automatic_function_calling.disable
    assert client.close.call_count == 1
    usage = engine.decisions.usage()[0]
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (123, 45, 168)
    from decimal import Decimal

    assert usage.estimated_cost_usd == Decimal("0.0001494")


@pytest.mark.parametrize(
    "mutation",
    [
        "invalid-json",
        "unknown",
        "duplicate",
        "weight",
        "action",
        "market",
        "missing",
        "extra",
        "contradiction",
    ],
)
def test_invalid_outputs_fail_closed(setup_ai, mutation):
    strategy, context, engine = setup_ai
    data = MockTradingStrategy().decide(context).model_dump()
    if mutation == "invalid-json":
        text = "this is not JSON"
    else:
        if mutation == "unknown":
            data["decisions"][0]["symbol"] = "UNKNOWN"
        if mutation == "duplicate":
            data["decisions"] *= 2
        if mutation == "weight":
            data["decisions"][0]["target_weight"] = 0.11
        if mutation == "action":
            data["decisions"][0]["action"] = "BORROW"
        if mutation == "market":
            data["market"] = "US"
        if mutation == "missing":
            data["decisions"] = []
        if mutation == "extra":
            data["command"] = "execute now"
        if mutation == "contradiction":
            data["decisions"][0]["action"] = "BUY"
        text = json.dumps(data)
    client = Mock()
    client.models.generate_content.return_value = response(context, text)
    with patch("trading_bot.strategy.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError, match="invalid Gemini"):
            strategy.decide(context)
    assert len(engine.decisions.usage()) == 1
    assert not engine.broker.get_orders()


@pytest.mark.parametrize(
    "failure,category",
    [
        (TimeoutError("fake-test-secret"), "timeout"),
        (errors.ClientError(429, {"error": {"message": "fake-test-secret"}}), "rate_limit"),
    ],
)
def test_timeout_and_rate_limit_never_leak_or_fallback(setup_ai, failure, category, caplog):
    strategy, context, engine = setup_ai
    client = Mock()
    client.models.generate_content.side_effect = failure
    with patch("trading_bot.strategy.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError, match=category) as error:
            strategy.decide(context)
    assert "fake-test-secret" not in str(error.value) + caplog.text
    assert client.models.generate_content.call_count == 1
    usage = engine.decisions.usage()[0]
    assert usage.status == "ERROR" and usage.input_tokens is None


def test_missing_key_and_input_budget_make_no_request(setup_ai, monkeypatch):
    strategy, context, engine = setup_ai
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(ValueError, match="unavailable"):
        strategy.decide(context)
    strategy.settings = ai_settings(max_input_tokens_equivalent=100)
    with pytest.raises(ValueError, match="budget"):
        strategy.decide(context)
    assert not engine.decisions.usage()


@pytest.mark.parametrize("setting", ["daily_request_limit", "monthly_request_limit"])
def test_local_quota_is_checked_before_network(setup_ai, setting):
    strategy, context, _ = setup_ai
    strategy.settings = ai_settings(**{setting: 0})
    client = Mock()
    with patch("trading_bot.strategy.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError, match="quota exhausted"):
            strategy.decide(context)
    assert client.models.generate_content.call_count == 0


def test_bounded_retry_usage_is_persistent(setup_ai):
    strategy, context, engine = setup_ai
    strategy.settings = ai_settings(retries=1, max_requests_per_run=2)
    client = Mock()
    client.models.generate_content.side_effect = [TimeoutError(), response(context)]
    with (
        patch("trading_bot.strategy.gemini.genai.Client", return_value=client),
        patch("trading_bot.strategy.gemini.time.sleep"),
    ):
        strategy.decide(context)
    assert client.models.generate_content.call_count == 2
    assert [u.status for u in DecisionRepository(engine.decisions.repository).usage()] == [
        "ERROR",
        "SUCCESS",
    ]


def test_env_file_loading_and_echo_redaction(setup_ai, tmp_path, monkeypatch):
    strategy, context, _ = setup_ai
    monkeypatch.delenv("GEMINI_API_KEY")
    strategy.env_file = tmp_path / ".env"
    strategy.env_file.write_text("GEMINI_API_KEY=fake-env-secret\n")
    data = MockTradingStrategy().decide(context).model_dump()
    data["decisions"][0]["reason"] = "fake-env-secret"
    client = Mock()
    client.models.generate_content.return_value = response(context, json.dumps(data))
    with patch("trading_bot.strategy.gemini.genai.Client", return_value=client):
        result = strategy.decide(context)
    assert "fake-env-secret" not in result.model_dump_json() + strategy.last_response
    monkeypatch.delenv("GEMINI_API_KEY")


def test_approved_free_model_only():
    with pytest.raises(ValueError):
        ai_settings(model="unapproved-paid-model")


def test_concurrent_daily_reservations_are_atomic(setup_ai):
    from concurrent.futures import ThreadPoolExecutor

    _, _, engine = setup_ai
    config = ai_settings(daily_request_limit=1)

    def reserve(index):
        try:
            engine.decisions.reserve(str(index), config)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, range(2))) == 1


def test_old_quota_windows_do_not_block_new_day(setup_ai):
    from trading_bot.strategy.models import APIUsage

    _, _, engine = setup_ai
    engine.decisions.save_usage(
        APIUsage(
            request_id="old", decision_id="old", model="old", at=datetime(2020, 1, 1, tzinfo=UTC)
        )
    )
    assert engine.decisions.reserve("new", ai_settings(daily_request_limit=1)).status == "RESERVED"


def test_duplicate_json_fields_and_oversized_response_rejected(setup_ai):
    strategy, context, _ = setup_ai
    client = Mock()
    client.models.generate_content.return_value = response(
        context, '{"market":"US","market":"JP","decisions":[],"market_summary":"x"}'
    )
    with patch("trading_bot.strategy.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError, match="invalid Gemini"):
            strategy.decide(context)
    strategy.decision_id = "oversize"
    client.models.generate_content.return_value = response(context, "x" * 40000)
    with patch("trading_bot.strategy.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError, match="byte budget"):
            strategy.decide(context)
    assert strategy.last_response == "[response exceeded configured size limit]"


def test_one_request_budget_prevents_custom_retry_from_making_second_call(setup_ai):
    strategy, context, engine = setup_ai
    strategy.settings = ai_settings(retries=3, max_requests_per_run=1)
    client = Mock()
    client.models.generate_content.side_effect = TimeoutError("fake-test-secret")
    with (
        patch("trading_bot.strategy.gemini.genai.Client", return_value=client),
        patch("trading_bot.strategy.gemini.time.sleep"),
    ):
        with pytest.raises(ValueError, match="quota exhausted"):
            strategy.decide(context)
    assert client.models.generate_content.call_count == 1
    assert len(engine.decisions.usage()) == 1
