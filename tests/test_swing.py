from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
import yaml

from trading_bot.broker.paper import PaperBroker
from trading_bot.data_access import MarketAccessDisabled, require_live_market_access
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import Order, TradingRules
from trading_bot.swing.backtest import run_backtest
from trading_bot.swing.data import alpaca_request_plan, synthetic
from trading_bot.swing.metrics import chronological_split, metrics
from trading_bot.swing.models import DataRights, MarketEvent, SwingSettings
from trading_bot.swing.screening import screen_daily
from trading_bot.swing.strategies import SwingGeminiStrategy


def test_daily_and_hourly_synthetic_bars_are_one_price_series():
    data = synthetic(20, 205)
    for day in (201, 204):
        bars = [data.hourly_bar(day, 0, hour) for hour in range(7)]
        assert bars[0].open == data.opening[day, 0]
        assert bars[-1].close == data.closing[day, 0]
        assert max(b.high for b in bars) == pytest.approx(data.high[day, 0])
        assert min(b.low for b in bars) == pytest.approx(data.low[day, 0])
        assert sum(b.volume for b in bars) == pytest.approx(data.volume[day, 0])


def test_missing_intraday_bar_stops_instead_of_reusing_yesterday(tmp_path):
    data = synthetic(20, 203)
    data.missing_hourly = {(201, column, 0) for column in range(20)}
    result = run_backtest(data, tmp_path / "test-swing-missing-hour", "C", SwingSettings())
    assert result.status == "SAFE_STOP"
    assert "coverage" in result.summary["stop_reason"]


def test_unknown_strategy_response_keeps_running_claim_and_never_retries(tmp_path):
    from trading_bot.persistence.decision_repository import DecisionRepository

    strategy = Mock()
    strategy.decide.side_effect = TimeoutError("do not expose response")
    root = tmp_path / "test-swing-unknown"
    result = run_backtest(synthetic(20, 203), root, "A", SwingSettings(), strategy=strategy)
    assert result.status == "SAFE_STOP" and strategy.decide.call_count == 1
    journal = DecisionRepository(SQLiteRepository(root / "account.sqlite3"))
    assert journal.history()[0].status == "RUNNING"
    assert not journal.history()[0].registered_orders


@pytest.mark.parametrize("response", ["hold", "sell", "freeze"])
def test_conditional_monitor_policies_are_explicit(tmp_path, response):
    settings = SwingSettings(monitor_response=response)
    result = run_backtest(synthetic(20, 206), tmp_path / f"test-swing-{response}", "B", settings)
    assert result.status == "COMPLETE"
    assert any(a.get("response") == response for a in result.alerts)


def test_walk_forward_windows_do_not_overlap_evaluation():
    from trading_bot.swing.metrics import walk_forward_windows

    assert walk_forward_windows(246) == ((0, 201, 221), (0, 221, 241), (0, 241, 246))
    with pytest.raises(ValueError):
        walk_forward_windows(200)


def test_all_country_rights_and_no_fabricated_comparison():
    from trading_bot.portfolio.models import Account
    from trading_bot.swing.benchmarks import compare_all_country

    data = synthetic(20, 203)
    provider = Mock()
    account = Account(created_at=data.opens_at[201])
    with pytest.raises(ValueError, match="rights"):
        compare_all_country(provider, None, account, [])
    provider.observations.assert_not_called()
    result = compare_all_country(provider, None, account, [], internal_use_allowed=True)
    assert result["status"] == "NOT_INITIALIZED" and result["excess_return"] is None


def test_delisted_and_unknown_metadata_do_not_leak_membership():
    data = synthetic(100, 205)
    at = data.closes_at[-1] + data.latency
    s = data.securities[0].model_copy(update={"delisted": at.date()})
    data.securities = (s, *data.securities[1:])
    ordinary, _ = screen_daily(data, at, SwingSettings())
    assert "S0000" not in {c.instrument.symbol for c in ordinary.candidates}
    protected, _ = screen_daily(data, at, SwingSettings(), {"S0000"})
    assert next(c for c in protected.candidates if c.instrument.symbol == "S0000").features is None
    data.securities = (
        s.model_copy(update={"delisted": None, "metadata_at": at + timedelta(days=1)}),
        *data.securities[1:],
    )
    unavailable, _ = screen_daily(data, at, SwingSettings())
    assert "S0000" not in {c.instrument.symbol for c in unavailable.candidates}


@pytest.mark.parametrize("days,expected_calls", [(205, 4), (211, 8)])
def test_sdk_mock_gemini_and_rules_use_existing_broker(tmp_path, monkeypatch, days, expected_calls):
    import json
    from types import SimpleNamespace

    from tests.ai_helpers import ai_settings
    from trading_bot.persistence.decision_repository import DecisionRepository
    from trading_bot.strategy.gemini import GeminiTradingStrategy

    client = Mock()

    def generated(**kwargs):
        inputs = json.loads(kwargs["contents"])
        decisions = []
        traded = False
        for c in inputs["candidates"]:
            action, weight = "HOLD", c["weight"]
            if c["weight"] > 0:
                action, weight = "SELL", 0.0
            elif not traded and c["features"] is not None:
                action, weight, traded = "BUY", 0.08, True
            decisions.append(
                {
                    "symbol": c["symbol"],
                    "action": action,
                    "target_weight": weight,
                    "reason": "synthetic SDK fixture",
                }
            )
        return SimpleNamespace(
            text=json.dumps(
                {
                    "market": "US",
                    "decisions": decisions,
                    "market_summary": "mock SDK, not real Gemini",
                }
            ),
            usage_metadata=None,
        )

    client.models.generate_content.side_effect = generated
    monkeypatch.setenv("GEMINI_API_KEY", "mock-not-a-real-key")
    monkeypatch.setattr("trading_bot.strategy.gemini.genai.Client", lambda **kwargs: client)
    ledger = DecisionRepository(SQLiteRepository(tmp_path / "gemini-mock.sqlite3"))

    class PerDecision:
        def decide(self, context):
            delegate = GeminiTradingStrategy(
                ai_settings(), ledger, context.as_of.isoformat(), env_file=tmp_path / "absent.env"
            )
            return SwingGeminiStrategy(delegate, DataRights(external_ai=True), True).decide(context)

    root = tmp_path / "test-swing-sdk"
    result = run_backtest(
        synthetic(20, days), root, "A", SwingSettings(), "gemini_mock", strategy=PerDecision()
    )
    assert result.status == ("COMPLETE" if days == 205 else "SAFE_STOP")
    fills = PaperBroker(SQLiteRepository(root / "account.sqlite3")).get_executions()
    assert {f.side for f in fills} == {"BUY", "SELL"}
    assert all(
        f.price_at
        > PaperBroker(SQLiteRepository(root / "account.sqlite3")).get_order(f.order_id).requested_at
        for f in fills
    )
    assert client.models.generate_content.call_count == expected_calls


def test_historical_monday_open_marks_other_holdings_without_stale_friday_close(tmp_path):
    data = synthetic(20, 205)
    broker = PaperBroker(SQLiteRepository(tmp_path / "weekend.sqlite3"))
    friday, monday = 201, 202
    assert data.opens_at[friday].weekday() == 4 and data.opens_at[monday].weekday() == 0
    initial = data.opens_at[friday]
    broker.initialize(initial)
    broker.exchange("JPY", Decimal("800000"), data.fx(initial), initial)
    first = data.securities[0].instrument
    bar = data.hourly_bar(friday, 0, 1)
    broker.place_order(first, "BUY", 1, initial, "first")
    assert (
        broker.execute(
            "first", {first.symbol: [bar]}, data.fx(bar.timestamp), bar.fetched_at
        ).status
        == "FILLED"
    )
    second = data.securities[1].instrument
    broker.place_order(second, "BUY", 1, data.closes_at[friday] + data.latency, "second")
    opening = data.hourly_bar(monday, 1, 0)
    inputs = data.bars({first.symbol, second.symbol}, opening.fetched_at)
    assert (
        broker.execute("second", inputs, data.fx(opening.timestamp), opening.fetched_at).status
        == "FILLED"
    )


def test_untriggered_stop_remains_pending():
    from trading_bot.broker.execution import eligible_bar

    data = synthetic(20, 203)
    instrument = data.securities[0].instrument
    order = Order(
        order_id="waiting",
        symbol=instrument.symbol,
        market="US",
        currency="USD",
        side="SELL",
        quantity=1,
        requested_at=data.opens_at[201],
        costs=TradingRules(),
        order_type="STOP",
        stop_price=Decimal(1),
    )
    bar = data.hourly_bar(201, 0, 1)
    assert (
        eligible_bar(order, instrument, [bar], bar.fetched_at, "historical", TradingRules()) is None
    )


def test_explicit_spread_is_also_reserved_by_order_planner(tmp_path):
    data = synthetic(20, 205)
    rules = TradingRules(spread=Decimal("0.002"), commission_rate=Decimal("0.001"))
    result = run_backtest(data, tmp_path / "test-swing-spread", "A", SwingSettings(), rules=rules)
    assert result.status == "COMPLETE" and result.summary["costs_jpy"] > 2000
    fills = PaperBroker(
        SQLiteRepository(Path(result.account_path) / "account.sqlite3")
    ).get_executions()
    assert fills and all(f.spread_cost > 0 for f in fills)


@pytest.mark.parametrize("size", [20, 100, 500, 2000])
def test_scale_and_sector_diversity(size):
    data = synthetic(size, 205)
    screening, summary = screen_daily(data, data.closes_at[-1] + data.latency, SwingSettings())
    assert screening.analyzed == size
    assert len(summary.first_stage) == min(size, 50)
    assert 10 <= len(screening.candidates) <= 20
    sectors = [c.features.sector for c in screening.candidates]
    assert all(sectors.count(s) <= 3 for s in sectors)
    assert all(c.features.source == "synthetic" for c in screening.candidates)


def test_daily_indicators_have_no_fabricated_hourly_observation():
    data = synthetic(20, 205)
    prices = np.arange(205, dtype=float) + 40
    data.closing[:, 0] = prices
    data.opening[:, 0] = prices - 0.5
    data.high[:, 0] = prices + 1
    data.low[:, 0] = prices - 1
    result, _ = screen_daily(data, data.closes_at[-1] + data.latency, SwingSettings(), {"S0000"})
    f = next(c.features for c in result.candidates if c.instrument.symbol == "S0000")
    assert f.sma20 == pytest.approx(prices[-20:].mean())
    assert f.sma50 == pytest.approx(prices[-50:].mean())
    assert f.sma200 == pytest.approx(prices[-200:].mean())
    assert f.rsi14 == 100 and f.atr14 == pytest.approx(2)
    assert f.momentum20 == pytest.approx(prices[-1] / prices[-21] - 1)
    assert f.hourly_return is None and f.hourly_observed_at is None


def test_future_daily_rows_do_not_change_ranking_or_exclusions():
    data = synthetic(100, 220)
    at = data.closes_at[205] + data.latency
    before = screen_daily(data, at, SwingSettings())
    data.closing[206:] = np.nan
    data.volume[206:] = -1
    data.market[206:] = np.nan
    assert screen_daily(data, at, SwingSettings()) == before


@pytest.mark.parametrize("issue", ["missing", "infinity", "negative", "jump", "volume"])
def test_bad_symbol_blocks_new_buys_without_favoring_remaining_symbols(issue):
    data = synthetic(100, 205)
    if issue == "missing":
        data.closing[-1, 0] = np.nan
    elif issue == "infinity":
        data.high[-1, 0] = np.inf
    elif issue == "negative":
        data.low[-1, 0] = -1
    elif issue == "jump":
        data.closing[-1, 0] *= 2
    else:
        data.volume[-1, 0] = -1
    result, summary = screen_daily(
        data, data.closes_at[-1] + data.latency, SwingSettings(), {"S0000"}
    )
    assert not summary.new_buys_allowed
    assert next(c for c in result.candidates if c.instrument.symbol == "S0000").features is None
    assert "S0000" in result.excluded


@pytest.mark.parametrize("kind", ["ETF", "ADR", "preferred", "special"])
def test_non_common_excluded_but_protected_still_evaluated(kind):
    data = synthetic(100, 205)
    first = data.securities[0].model_copy(update={"kind": kind})
    data.securities = (first, *data.securities[1:])
    result, summary = screen_daily(
        data, data.closes_at[-1] + data.latency, SwingSettings(), {"S0000"}
    )
    assert "S0000" not in summary.first_stage
    assert next(c for c in result.candidates if c.instrument.symbol == "S0000").features is not None


@pytest.mark.parametrize("mode", ["B", "C"])
def test_daily_only_never_claims_intraday_success(tmp_path, mode):
    data = synthetic(20, 203)
    data.hourly_available = False
    result = run_backtest(data, tmp_path / f"test-swing-{mode}", mode, SwingSettings())
    assert result.status == "UNAVAILABLE_INTRADAY" and not result.decision_times


@pytest.mark.parametrize("mode", ["A", "B", "C"])
def test_forward_simulation_and_cash_baseline(tmp_path, mode):
    data = synthetic(20, 205)
    result = run_backtest(data, tmp_path / f"test-swing-{mode}", mode, SwingSettings(), "cash")
    assert result.status == "COMPLETE"
    assert result.summary["cumulative_return"] == 0 and result.summary["fills"] == 0
    assert result.decision_times == sorted(set(result.decision_times))
    assert len(result.decision_times) == (4 if mode in ("A", "B") else 24)


@pytest.mark.parametrize("kind", ["split", "dividend", "delisting"])
def test_unsupported_events_stop_and_do_not_manufacture_prices(tmp_path, kind):
    data = synthetic(20, 205)
    at = data.opens_at[202]
    data.events = (MarketEvent(symbol="S0000", kind=kind, effective_at=at, observed_at=at),)
    result = run_backtest(data, tmp_path / f"test-swing-{kind}", "A", SwingSettings())
    assert result.status == "SAFE_STOP" and result.summary["cumulative_return"] is None
    assert kind in result.summary["stop_reason"]


def test_future_event_not_used_before_observation(tmp_path):
    data = synthetic(20, 205)
    future = data.closes_at[-1] + timedelta(days=1)
    data.events = (
        MarketEvent(
            symbol="S0000", kind="split", effective_at=data.opens_at[201], observed_at=future
        ),
    )
    result = run_backtest(data, tmp_path / "test-swing-future", "A", SwingSettings(), "cash")
    assert result.status == "COMPLETE"


def test_unknown_outcome_not_automatically_replayed(tmp_path):
    directory = tmp_path / "test-swing-existing"
    directory.mkdir()
    with pytest.raises(ValueError, match="overwrite/replay"):
        run_backtest(synthetic(20, 203), directory, "A", SwingSettings())
    with pytest.raises(ValueError, match="isolated"):
        run_backtest(synthetic(20, 203), tmp_path / "paper-live", "A", SwingSettings())


@pytest.mark.parametrize("rights,enabled", [(False, False), (False, True), (True, False)])
def test_external_gemini_gate_precedes_sdk_and_quota(rights, enabled):
    delegate = Mock()
    strategy = SwingGeminiStrategy(delegate, DataRights(external_ai=rights), enabled)
    with pytest.raises(ValueError, match="disabled"):
        strategy.decide(Mock())
    delegate.decide.assert_not_called()


def test_global_market_stop_and_default_configuration():
    settings = SwingSettings.model_validate(yaml.safe_load(Path("config/swing.yaml").read_text()))
    assert not settings.enabled and not settings.legacy_hourly_enabled
    with pytest.raises(MarketAccessDisabled):
        require_live_market_access()


def test_metrics_known_drawdown_and_undefined_ratios():
    m = metrics([100, 120, 90, 110], [100, 120, 80, 90, 110], [10, -5], [5, 10], 2, 4, [-0.25])
    assert m["cumulative_return"] == pytest.approx(0.1)
    assert m["max_drawdown"] == pytest.approx(-1 / 3)
    assert m["profit_factor"] == 2 and m["average_holding_sessions"] == 7.5
    assert m["annualized_return"] is None
    assert metrics([100, 100], [100, 100], [], [], 0, 0, [])["sharpe_zero_risk_free"] is None


def test_split_and_api_pagination():
    data = synthetic(20, 205)
    train, evaluation = chronological_split(data.opens_at, data.closes_at[200], data.opens_at[201])
    assert max(train) < min(evaluation)
    with pytest.raises(ValueError):
        chronological_split(data.opens_at, data.opens_at[201], data.opens_at[200])
    plan = alpaca_request_plan([f"S{i:04d}" for i in range(2000)], 221)
    assert plan == {"symbol_batches": 20, "bars": 442000, "minimum_pages": 60}
    assert alpaca_request_plan([f"S{i:04d}" for i in range(2000)], 2)["minimum_pages"] == 20


@pytest.mark.parametrize("gap", [True, False])
def test_protective_stop_gap_slippage_and_restart(tmp_path, gap):
    data = synthetic(20, 203)
    day = 201
    instrument = data.securities[0].instrument
    broker = PaperBroker(SQLiteRepository(tmp_path / "stop.sqlite3"))
    initial = data.opens_at[day]
    broker.initialize(initial)
    broker.exchange("JPY", Decimal("800000"), data.fx(initial), initial)
    buy_bar = data.hourly_bar(day, 0, 1)
    broker.place_order(instrument, "BUY", 1, initial, "buy")
    assert (
        broker.execute(
            "buy", {instrument.symbol: [buy_bar]}, data.fx(buy_bar.timestamp), buy_bar.fetched_at
        ).status
        == "FILLED"
    )
    stop = Decimal(str(buy_bar.open)) * Decimal("0.95")
    broker.place_order(instrument, "SELL", 1, buy_bar.fetched_at, "stop", stop_price=stop)
    bar = data.hourly_bar(day, 0, 3)
    opening = float(stop) * (0.8 if gap else 1.1)
    close = float(stop) * (0.85 if gap else 1.05)
    bar = bar.model_copy(
        update={
            "open": opening,
            "close": close,
            "high": max(opening, close) * 1.01,
            "low": float(stop) * 0.7,
        }
    )
    broker = PaperBroker(SQLiteRepository(tmp_path / "stop.sqlite3"))
    assert broker.get_order("stop").stop_price == stop
    quote_at = bar.timestamp if gap else bar.fetched_at - data.latency
    assert (
        broker.execute("stop", {instrument.symbol: [bar]}, data.fx(quote_at), bar.fetched_at).status
        == "FILLED"
    )
    execution = next(e for e in broker.get_executions() if e.order_id == "stop")
    expected = min(Decimal(str(opening)), stop)
    assert execution.price == expected * (1 - TradingRules().slippage)
    assert execution.price_at == quote_at
    assert broker.execute("stop", {}, None, bar.fetched_at).status == "FILLED"
    assert len(broker.get_executions()) == 2


def test_stop_order_contract_rejects_buy():
    data = synthetic(20, 203)
    with pytest.raises(ValueError, match="STOP"):
        Order(
            order_id="bad",
            symbol="S0000",
            market="US",
            currency="USD",
            side="BUY",
            quantity=1,
            requested_at=data.opens_at[201],
            costs=TradingRules(),
            order_type="STOP",
            stop_price=Decimal(10),
        )
