import importlib.util
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from tests.ai_helpers import JP, NOW, BuyStrategy, environment
from trading_bot.cli import main
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.sqlite_repository import SQLiteSession


def test_pipeline_default_mock_is_dry_run_and_history_is_saved(tmp_path):
    engine = environment(tmp_path)
    account = engine.broker.get_balance()
    history = engine.broker.history()
    run = engine.run("JP", NOW, decision_id="mock")
    assert run.status == "SUCCESS" and run.result.decisions[0].action == "HOLD"
    assert run.input_hash and run.context.screening.candidates
    assert not run.plan.orders and not engine.broker.get_orders()
    assert engine.broker.get_balance() == account and engine.broker.history() == history
    assert not run.usage
    assert engine.decisions.get("mock") == run


def test_buy_plan_does_not_register_until_separate_authorization(tmp_path):
    engine = environment(tmp_path)
    strategy = BuyStrategy()
    run = engine.run("JP", NOW, decision_id="buy", strategy=strategy)
    assert run.status == "SUCCESS" and run.plan.orders
    assert not engine.broker.get_orders()
    replay = engine.run("JP", NOW, decision_id="buy", submit=True, strategy=strategy)
    assert replay.status == "REGISTERED" and strategy.calls == 1
    assert len(replay.registered_orders) == 1
    assert replay.registered_orders[0].status == "PENDING"
    assert not engine.broker.get_executions()
    assert engine.broker.get_balance().balances["JPY"] == 1000000
    again = engine.run("JP", NOW, decision_id="buy", submit=True, strategy=strategy)
    assert again == replay and len(engine.broker.get_orders()) == 1


def test_default_id_is_deterministic_and_same_id_cannot_change_request(tmp_path):
    engine = environment(tmp_path)
    strategy = BuyStrategy()
    first = engine.run("JP", NOW, strategy=strategy)
    second = engine.run("JP", NOW, strategy=strategy)
    assert first == second and strategy.calls == 1
    with pytest.raises(ValueError):
        engine.run("US", NOW, decision_id=first.decision_id)


def test_ai_failure_saves_error_without_orders_or_secret(tmp_path, monkeypatch):
    engine = environment(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-private-value")
    strategy = Mock()
    strategy.decide.side_effect = RuntimeError("fake-private-value")
    result = engine.run("JP", NOW, submit=True, strategy=strategy)
    assert result.status == "FAILED" and result.error
    assert not engine.broker.get_orders()
    assert "fake-private-value" not in result.model_dump_json()
    assert engine.decisions.history()[0].status == "FAILED"


def test_account_change_blocks_saved_plan_submission(tmp_path):
    engine = environment(tmp_path)
    run = engine.run("JP", NOW, decision_id="original", strategy=BuyStrategy())
    engine.broker.place_order(JP, "BUY", 100, NOW)
    with pytest.raises(ValueError, match="changed"):
        engine.register(run)
    assert len(engine.broker.get_orders()) == 1


def test_batch_registration_rollback_keeps_saved_decision_and_no_orders(tmp_path):
    engine = environment(tmp_path)
    run = engine.run("JP", NOW, strategy=BuyStrategy())
    original = SQLiteSession.put

    def fail(session, table, key, value):
        if table == "decisions" and value.status == "REGISTERED":
            raise RuntimeError("injected final write failure")
        return original(session, table, key, value)

    with patch.object(SQLiteSession, "put", fail), pytest.raises(RuntimeError):
        engine.register(run)
    assert not engine.broker.get_orders()
    assert engine.decisions.get(run.decision_id).status == "SUCCESS"


def test_future_and_missing_data_fail_before_strategy_and_stale_replay(tmp_path):
    engine = environment(tmp_path)
    strategy = BuyStrategy()
    run = engine.run("JP", NOW + timedelta(hours=1), strategy=strategy)
    assert run.status == "FAILED" and strategy.calls == 0
    run = engine.run("JP", NOW, strategy=strategy)
    engine.clock = lambda: NOW + timedelta(days=2)
    with pytest.raises(ValueError, match="stale"):
        engine.register(run)
    assert not engine.broker.get_orders()


def test_restart_history_and_usage_are_retained(tmp_path):
    engine = environment(tmp_path)
    run = engine.run("JP", NOW)
    restored = DecisionRepository(engine.decisions.repository)
    assert restored.get(run.decision_id) == run
    assert restored.usage() == []


def test_cli_defaults_mock_and_no_submission(tmp_path, capsys):
    engine = environment(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text("storage_dir: market\n")
    prefix = ["--settings", str(config / "settings.yaml")]
    # Limit universe to the one symbol with synthetic history.
    universe = tmp_path / "universe.yaml"
    universe.write_text(yaml.safe_dump([JP.model_dump()]))
    prefix += ["--universe", str(universe)]
    assert (
        main(
            prefix
            + [
                "screen",
                "--market",
                "JP",
                "--at",
                NOW.isoformat(),
                "--db",
                str(engine.decisions.repository.path),
            ]
        )
        == 0
    )
    assert (
        main(
            prefix
            + [
                "ai",
                "decide",
                "--market",
                "JP",
                "--dry-run",
                "--at",
                NOW.isoformat(),
                "--db",
                str(engine.decisions.repository.path),
                "--decision-id",
                "cli",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"provider": "mock"' in output and '"action": "HOLD"' in output
    assert not engine.broker.get_orders()
    for command in ["history", "usage"]:
        assert main(["ai", command, "--db", str(engine.decisions.repository.path)]) == 0
    with pytest.raises(SystemExit):
        main(["ai", "decide", "--market", "JP", "--dry-run", "--submit-orders"])


def test_workflow_is_main_only_secret_scoped_and_manual():
    raw = Path(".github/workflows/test-gemini.yml").read_text()
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["default"] == "mock"
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["smoke"]
    assert "refs/heads/main" in job["if"] and "ref_protected" in job["if"]
    secret_steps = [step for step in job["steps"] if "GEMINI_API_KEY" in step.get("env", {})]
    assert len(secret_steps) == 1 and "inputs.mode == 'gemini'" in secret_steps[0]["if"]
    assert "upload-artifact" not in raw and "schedule" not in workflow["on"]


def test_smoke_mock_end_to_end_uses_disposable_account(monkeypatch, tmp_path, capsys):
    from tests.ai_helpers import histories

    spec = importlib.util.spec_from_file_location("gemini_smoke", "scripts/test_gemini.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_fetch(settings, instruments, intervals, provider, store):
        for instrument in instruments:
            data = histories(instrument)
            for interval in intervals:
                store.save(instrument, interval, data[interval])
        return []

    # Use current-time metadata while keeping the synthetic bars fresh for the smoke clock.
    class Clock:
        @staticmethod
        def now(zone):
            return NOW

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "collect", fake_fetch)
    monkeypatch.setattr(
        module,
        "DecisionEngine",
        lambda *args: type(environment(tmp_path))(*args, clock=lambda: NOW),
    )
    monkeypatch.setattr("sys.argv", ["test_gemini.py", "--market", "JP", "--mode", "mock"])
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    assert module.main() == 0
    assert "SUCCESS" in summary_file.read_text(encoding="utf-8")
    assert '"api_requests": 0' in capsys.readouterr().out


def test_registration_revalidates_candidate_age_not_just_decision_age(tmp_path):
    engine = environment(tmp_path)
    record = engine.run("JP", NOW, strategy=BuyStrategy())
    engine.clock = lambda: NOW + timedelta(hours=24, minutes=-1)
    with pytest.raises(ValueError, match="stale"):
        engine.register(record)
    assert not engine.broker.get_orders()


def test_registration_uses_persisted_plan_not_modified_caller_copy(tmp_path):
    engine = environment(tmp_path)
    record = engine.run("JP", NOW, strategy=BuyStrategy())
    altered = record.model_copy(
        update={
            "plan": record.plan.model_copy(
                update={"orders": [record.plan.orders[0].model_copy(update={"quantity": 10000})]}
            )
        }
    )
    registered = engine.register(altered)
    assert registered.registered_orders[0].quantity == record.plan.orders[0].quantity


def test_concurrent_saved_decision_registration_is_exactly_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    engine = environment(tmp_path)
    record = engine.run("JP", NOW, strategy=BuyStrategy())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: engine.register(record), range(2)))
    assert results[0] == results[1]
    assert len(engine.broker.get_orders()) == 1
    assert not engine.broker.get_executions()


def test_order_time_is_actual_registration_after_ai_latency(tmp_path):
    engine = environment(tmp_path)
    record = engine.run("JP", NOW, strategy=BuyStrategy())
    engine.clock = lambda: NOW + timedelta(minutes=5)
    registered = engine.register(record)
    assert registered.registered_orders[0].requested_at == NOW + timedelta(minutes=5)


def test_engine_gemini_tokens_and_invalid_response_are_persisted(tmp_path, monkeypatch):
    from tests.test_gemini_strategy import response
    from trading_bot.strategy.gemini import GeminiTradingStrategy

    class OfflineSDK(GeminiTradingStrategy):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, offline_sdk_factory=Mock(return_value=client))

        def decide(self, context):
            return super().decide(context.model_copy(update={"data_origin": "synthetic"}))

    monkeypatch.setattr("trading_bot.orchestration.engine.GeminiTradingStrategy", OfflineSDK)

    engine = environment(tmp_path)
    context, _ = engine.context("JP", NOW, None)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-integration-secret")
    client = Mock()
    client.models.generate_content.return_value = response(context)
    with patch("trading_bot.strategy.gemini.genai.Client", return_value=client):
        run = engine.run("JP", NOW, provider="gemini")
    assert run.status == "SUCCESS" and run.usage[0].total_tokens == 168
    assert engine.decisions.get(run.decision_id).usage == run.usage
    client.models.generate_content.return_value = response(
        context, "invalid JSON fake-integration-secret"
    )
    with patch("trading_bot.strategy.gemini.genai.Client", return_value=client):
        failed = engine.run("JP", NOW, provider="gemini", decision_id="invalid-output", submit=True)
    assert failed.status == "FAILED" and failed.usage[0].total_tokens == 168
    assert "fake-integration-secret" not in failed.model_dump_json()
    assert not engine.broker.get_orders()


def test_failed_registration_is_recorded_without_losing_ai_result(tmp_path):
    engine = environment(tmp_path)
    record = engine.run("JP", NOW, strategy=BuyStrategy())
    engine.broker.place_order(JP, "BUY", 100, NOW)
    with pytest.raises(ValueError):
        engine.register(record)
    saved = engine.decisions.get(record.decision_id)
    assert saved.status == "SUCCESS" and saved.result == record.result
    assert saved.registration_error and "changed" in saved.registration_error
