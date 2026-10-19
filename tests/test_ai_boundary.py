from unittest.mock import Mock

import pytest

from tests.ai_helpers import NOW, ai_settings, environment
from trading_bot.strategy.gemini import GeminiTradingStrategy


@pytest.mark.parametrize("origin", ["market", "unknown", "synthetic"])
def test_no_real_sdk_or_usage_or_payload_before_authorization(tmp_path, origin):
    engine = environment(tmp_path)
    context, _ = engine.context("JP", NOW, None)
    context = context.model_copy(update={"data_origin": origin})
    payload = Mock(side_effect=AssertionError("payload must not be built"))
    strategy = GeminiTradingStrategy(
        ai_settings(), engine.decisions, "blocked", payload_builder=payload
    )
    with pytest.raises(ValueError, match="blocked|disabled"):
        strategy.decide(context)
    payload.assert_not_called()
    assert engine.decisions.usage() == []


def test_alpaca_features_cannot_be_relabelled_as_synthetic(tmp_path):
    engine = environment(tmp_path)
    context, _ = engine.context("JP", NOW, None)
    c = context.screening.candidates[0]
    contaminated = c.model_copy(
        update={"features": c.features.model_copy(update={"source": "alpaca"})}
    )
    context = context.model_copy(
        update={
            "data_origin": "synthetic",
            "screening": context.screening.model_copy(update={"candidates": [contaminated]}),
        }
    )
    factory = Mock()
    strategy = GeminiTradingStrategy(
        ai_settings(), engine.decisions, "blocked", offline_sdk_factory=factory
    )
    with pytest.raises(ValueError, match="blocked"):
        strategy.decide(context)
    factory.assert_not_called()
    assert engine.decisions.usage() == []
