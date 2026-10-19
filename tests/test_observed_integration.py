import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest

from tests.ai_helpers import ai_settings
from tests.integration_helpers import observed_fixture
from trading_bot.persistence.research import ResearchMemory
from trading_bot.portfolio.models import TradingRules
from trading_bot.strategy.gemini import GeminiTradingStrategy
from trading_bot.strategy.models import DecisionContext
from trading_bot.swing.cycle import stage_cycle
from trading_bot.swing.internal_input import DraftInput, parse_offline_response
from trading_bot.swing.models import MarketEvent, SwingSettings

RULES = TradingRules(spread=Decimal("0.0004"), commission_rate=Decimal("0.0001"))
SETTINGS = SwingSettings(daily_only=True)


def stage(state, data, at, run, kind="decision", **kwargs):
    return stage_cycle(
        state,
        data,
        at,
        run,
        kind,
        ai_settings(max_input_tokens_equivalent=100000),
        SETTINGS,
        RULES,
        **kwargs,
    )


def proposed(result, action):
    context = DecisionContext.model_validate(result["input"]["context"])
    decisions = []
    first = True
    held = {p.symbol for p in context.portfolio.positions}
    for c in context.screening.candidates:
        selected = (first and action == "BUY") or (c.instrument.symbol in held and action == "SELL")
        decisions.append(
            dict(
                symbol=c.instrument.symbol,
                action=action if selected else "HOLD",
                target_weight=0.08 if selected and action == "BUY" else 0.0,
                reason="preapproved counterfactual mock fixture",
            )
        )
        first = False
    return json.dumps(dict(market="US", market_summary="fixture only", decisions=decisions))


def test_pending_buy_fill_sell_and_costs_across_separate_cycles(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="trading_bot.broker.paper")
    data, at = observed_fixture()
    state = {"account": None, "records": []}
    _, draft = stage(state, data, at, "preview")
    first, result = stage(
        state, data, at, "buy", funding_jpy=Decimal(800000), mock_response=proposed(draft, "BUY")
    )
    assert len(result["orders"]) == 1 and result["orders"][0]["status"] == "PENDING"
    assert result["executions"] == []
    # At the same available history, no future qualifying bar exists.
    later = at + timedelta(minutes=1)
    data.actions = data.actions.model_copy(update={"through": later, "observed_at": later})
    second, pending = stage(first, data, later, "no-future", "execute")
    assert pending["orders"][0]["status"] == "PENDING"
    data2, at2 = observed_fixture(202)
    third, filled = stage(second, data2, at2, "fill", "execute")
    assert len(filled["executions"]) == 1 and filled["orders"][0]["status"] == "FILLED"
    assert Decimal(filled["executions"][0]["commission"]) > 0
    assert filled["executions"][0]["price_at"] > result["orders"][0]["requested_at"]
    decision_at = at2 + timedelta(minutes=1)
    data2.actions = data2.actions.model_copy(
        update={"through": decision_at, "observed_at": decision_at}
    )
    fourth, sell = stage(third, data2, decision_at, "sell", mock_response=proposed(filled, "SELL"))
    assert any(o["side"] == "SELL" and o["status"] == "PENDING" for o in sell["orders"])
    data3, at3 = observed_fixture(203)
    fifth, closed = stage(fourth, data3, at3, "sell-fill", "execute")
    assert {f["side"] for f in closed["executions"]} == {"BUY", "SELL"}
    assert all(o["status"] == "FILLED" for o in closed["orders"])
    assert closed["external_ai_sent"] is False and not closed["public_projection_allowed"]
    assert closed["all_country"]["excess_return"] is None
    assert fifth["account"]["balances"]["JPY"] == "200000"
    assert "price=" not in caplog.text and '"close"' not in caplog.text
    with pytest.raises(ValueError, match="advance"):
        stage(fifth, data3, at3, "duplicate")


def test_schema_draft_and_no_sdk_even_with_market_relabel(tmp_path):
    from trading_bot.persistence.decision_repository import DecisionRepository
    from trading_bot.persistence.sqlite_repository import SQLiteRepository

    data, at = observed_fixture()
    _, result = stage({}, data, at, "draft")
    draft = DraftInput.model_validate(result["input"])
    assert draft.input_token_equivalent_upper > 0 and draft.paid_equivalent_upper_usd is not None
    sdk = Mock(side_effect=AssertionError("market data sent"))
    strategy = GeminiTradingStrategy(
        ai_settings(),
        DecisionRepository(SQLiteRepository(tmp_path / "mock.db")),
        "blocked",
        offline_sdk_factory=sdk,
    )
    with pytest.raises(ValueError, match="blocked"):
        strategy.decide(draft.context)
    sdk.assert_not_called()
    for bad in [
        '{"market":"US","market":"US"}',
        "oops",
        json.dumps(dict(market="US", market_summary="x", decisions=[])),
    ]:
        with pytest.raises(ValueError, match="invalid"):
            parse_offline_response(bad, draft.context, ai_settings())


@pytest.mark.parametrize(
    "failure",
    [
        "fx",
        "stale-fx",
        "ca",
        "split",
        "future-history",
        "missing",
        "unverified-ca",
        "latest-session",
    ],
)
def test_quality_failure_does_not_mutate_staged_account(failure):
    data, at = observed_fixture()
    state = {"account": None, "records": []}
    before = deepcopy(state)
    if failure == "fx":
        data.fx_observations = []
    elif failure == "stale-fx":
        data.fx_observations = [
            f for f in data.fx_observations if f.timestamp < at - timedelta(days=1)
        ]
    elif failure == "ca":
        data.actions = data.actions.model_copy(update={"observed_at": at - timedelta(hours=2)})
    elif failure == "unverified-ca":
        data.actions = data.actions.model_copy(update={"verified": False})
    elif failure == "latest-session":
        known = list(data.available)
        known[201] = at + timedelta(days=1)
        data.available = tuple(known)
    elif failure == "split":
        data.actions = data.actions.model_copy(
            update={
                "events": [
                    MarketEvent(
                        symbol="S0000",
                        kind="split",
                        observed_at=at,
                        effective_at=at - timedelta(days=1),
                    )
                ]
            }
        )
    elif failure == "future-history":
        known = list(data.available)
        known[100] = at + timedelta(days=1)
        data.available = tuple(known)
    else:
        data.closing[200, 0] = float("nan")
    with pytest.raises(ValueError):
        stage(state, data, at, "bad")
    assert state == before


def test_future_prices_do_not_change_observable_draft_and_holdings_are_protected():
    data, at = observed_fixture()
    _, before = stage({}, data, at, "before")
    data.closing[203:] *= 10
    _, after = stage({}, data, at, "after")
    # Random valuation IDs are unrelated to the input's compact features.
    assert before["input"]["compact_json"] == after["input"]["compact_json"]
    result, summary, _ = data.screen(at, SETTINGS, {"S0019"}, at)
    assert "S0019" in {c.instrument.symbol for c in result.candidates}
    assert summary.first_stage


def test_memory_transaction_rolls_back_on_error():
    memory = ResearchMemory()
    with pytest.raises(RuntimeError):
        with memory.transaction() as tx:
            tx.rows["orders"]["bad"] = "bad"
            raise RuntimeError("simulated crash")
    assert memory.state == {"account": None, "records": []}


@pytest.mark.parametrize("strategy", ["rule", "momentum", "mock"])
def test_offline_strategies_share_risk_and_broker(strategy):
    data, at = observed_fixture()
    state, result = stage({}, data, at, strategy, strategy=strategy, funding_jpy=Decimal(800000))
    if strategy == "mock":
        assert result["orders"] == []
    else:
        assert result["orders"]
        data2, at2 = observed_fixture(202)
        _, filled = stage(state, data2, at2, strategy + "-fill", "execute")
        assert Decimal(filled["valuation"]["stock_ratio"]) <= Decimal("0.80")
        assert Decimal(filled["valuation"]["cash_ratio"]) >= Decimal("0.20")
        assert all(Decimal(p["ratio"]) <= Decimal("0.10") for p in filled["valuation"]["positions"])


def test_database_outage_never_falls_back_to_another_account():
    from trading_bot.persistence.supabase_repository import SupabaseError
    from trading_bot.swing.cycle import commit_cycle

    data, at = observed_fixture()
    store = Mock()
    store.read.side_effect = SupabaseError("offline")
    with pytest.raises(SupabaseError):
        commit_cycle(store, data, at, "outage", "decision", ai_settings(), SETTINGS, RULES)
    store.commit.assert_not_called()


def test_same_session_cannot_place_second_decision_under_different_run_id():
    data, at = observed_fixture()
    state, _ = stage({}, data, at, "one")
    later = at + timedelta(minutes=1)
    data.actions = data.actions.model_copy(update={"through": later, "observed_at": later})
    with pytest.raises(ValueError, match="slot already"):
        stage(state, data, later, "different-id")


def test_nav_comparison_is_private_and_does_not_invent_application_nav():
    data, at = observed_fixture()
    provider = Mock()
    with pytest.raises(ValueError, match="rights"):
        stage({}, data, at, "blocked-nav", nav_provider=provider)
    provider.observations.assert_not_called()
    _, result = stage({}, data, at, "private-nav", nav_provider=provider, nav_allowed=True)
    assert result["all_country"]["status"] == "NOT_INITIALIZED"
    assert result["all_country"]["excess_return"] is None


def test_private_comparison_requires_equal_window_capital_and_inputs():
    from trading_bot.swing.comparison import compare_private

    data, at = observed_fixture()
    _, one = stage({}, data, at, "mock")
    _, two = stage({}, data, at, "rule", strategy="rule")
    comparison = compare_private({"mock": one, "rule": two})
    assert comparison["publication_reviewed"] is False
    assert "input" not in comparison["strategies"]["mock"]
    assert one["performance"]["annualized_return"] is None
    altered = deepcopy(two)
    altered["data_history"] = {}
    with pytest.raises(ValueError):
        compare_private({"mock": one, "altered": altered})


def test_missed_execution_slot_does_not_use_a_later_convenient_price():
    data, at = observed_fixture()
    _, preview = stage({}, data, at, "preview")
    state, _ = stage(
        {}, data, at, "buy", funding_jpy=Decimal(800000), mock_response=proposed(preview, "BUY")
    )
    delayed, delayed_at = observed_fixture(203)
    with pytest.raises(ValueError, match="missed execution"):
        stage(state, delayed, delayed_at, "late", "execute")
    assert not any(r["kind"] == "executions" for r in state["records"])
