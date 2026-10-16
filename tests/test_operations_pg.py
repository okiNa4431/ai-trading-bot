"""Real isolated PostgreSQL concurrency/fencing tests, never Supabase production."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from tests.test_postgres import PGAPI
from tests.test_postgres import uri as postgres_uri
from trading_bot.broker.paper import PaperBroker
from trading_bot.operations import Operation
from trading_bot.persistence.supabase_repository import SupabaseRepository


@pytest.fixture
def phase5_uri():
    return postgres_uri.__wrapped__()


@pytest.fixture
def operation(phase5_uri):
    account = "test-phase5-" + uuid4().hex[:12]
    return Operation(PGAPI(phase5_uri), account, "JP:2026-10-15:decision:close")


def expire(op):
    with psycopg.connect(op.api.uri) as c:
        c.execute(
            "update bot_private.operation_runs "
            "set lease_until=clock_timestamp()-interval '1 second' where account=%s",
            (op.account,),
        )


def test_fenced_initialization_legacy_bypass_and_expired_writes(operation):
    op = operation
    assert op.claim("runner-1", {})
    broker = PaperBroker(op.repository)
    broker.initialize(datetime(2026, 10, 14, tzinfo=UTC))
    assert broker.get_balance().initial_jpy == 1000000
    bare = SupabaseRepository(op.api, op.account, test=True)
    with pytest.raises(psycopg.Error):
        with bare.transaction() as tx:
            tx.save_account(tx.account())
    expire(op)
    with pytest.raises(psycopg.Error):
        with op.repository.transaction() as tx:
            tx.save_account(tx.account())
    with pytest.raises(psycopg.Error):
        op.step(1)


def test_parallel_claim_has_single_winner(operation):
    def claim(i):
        op = Operation(operation.api, operation.account, "slot-" + str(i))
        try:
            return op.claim(str(i), {})
        except psycopg.Error:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, [1, 2])) == 1


def test_expired_runner_cannot_write_after_review_and_new_token(operation):
    op = operation
    op.claim("old", {})
    PaperBroker(op.repository).initialize(datetime(2026, 10, 14, tzinfo=UTC))
    expire(op)
    with pytest.raises(psycopg.Error):
        Operation(op.api, op.account, "next").claim("new", {})
    op.api.rpc(
        "bot_operation_review",
        {
            "p_account": op.account,
            "p_run": op.run_id,
            "p_reason": "Isolated ledger inspected; no external API was sent",
            "p_stop": False,
        },
    )
    new = Operation(op.api, op.account, "next")
    assert new.claim("new", {}) and new.token > op.token
    with pytest.raises(psycopg.Error):
        op.data("late", {"invalid": True})
    new.data("valid", {"ok": True})
    assert new.read()["data"]["valid"] == {"ok": True}


def test_stage_order_no_replay_and_append_only_generations(operation):
    op = operation
    op.claim("runner", {})
    with pytest.raises(psycopg.Error):
        op.step(3)
    for stage in range(1, 9):
        op.step(stage, "SUCCEEDED" if stage == 8 else "RUNNING")
    assert not Operation(op.api, op.account, op.run_id).claim("retry", {})
    with pytest.raises(psycopg.Error):
        op.data("late", {})
    with pytest.raises(psycopg.Error):
        Operation(op.api, op.account, op.run_id).claim("retry", {"changed": True})


def test_emergency_stop_fences_active_runner(operation):
    op = operation
    op.claim("runner", {})
    op.api.rpc(
        "bot_operation_review",
        {
            "p_account": op.account,
            "p_run": op.run_id,
            "p_reason": "Emergency stop test; retained immutable events",
            "p_stop": True,
        },
    )
    with pytest.raises(psycopg.Error):
        op.data("late", {})
    with pytest.raises(psycopg.Error):
        Operation(op.api, op.account, "next").claim("new", {})


def test_production_account_rejected_by_operation_rpcs(operation):
    with pytest.raises(ValueError):
        Operation(operation.api, "paper-live", "bad")
    with pytest.raises(psycopg.Error):
        operation.api.rpc(
            "bot_operation_claim",
            {"p_account": "paper-live", "p_run": "bad", "p_owner": "bad", "p_metadata": {}},
        )


def test_deadline_checked_using_db_clock(operation):
    operation.claim("runner", {})
    expire(operation)
    with pytest.raises(psycopg.Error):
        operation.api.rpc(
            "bot_operation_commit",
            {
                "p_account": operation.account,
                "p_run": operation.run_id,
                "p_token": operation.token,
                "p_test": True,
                "p_version": 0,
                "p_value": {},
                "p_writes": [],
            },
        )


def test_old_api_reservation_path_is_fenced(operation):
    from tests.ai_helpers import ai_settings
    from trading_bot.persistence.decision_repository import DecisionRepository
    from trading_bot.strategy.models import DecisionRun

    op = operation
    op.claim("runner", {})
    PaperBroker(op.repository).initialize(datetime.now(UTC) - timedelta(days=1))
    record = DecisionRun(
        decision_id="quota",
        market="JP",
        provider="gemini",
        model="model",
        prompt_version="test",
        input_hash="",
        account_hash="",
        as_of=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    DecisionRepository(op.repository).claim(record)
    with pytest.raises(psycopg.Error):
        SupabaseRepository(op.api, op.account, test=True).reserve_api("quota", ai_settings())
    usage = op.repository.reserve_api("quota", ai_settings())
    assert usage.status == "RESERVED"


@pytest.mark.parametrize("market", ["JP", "US"])
def test_actual_pg_separate_test_cycle_operations(phase5_uri, tmp_path, market):
    from scripts.phase5_fixture import fixture

    api = PGAPI(phase5_uri)
    # Disposable CI database only: unique fixture account per market, no live service.
    with psycopg.connect(phase5_uri) as c:
        c.execute(
            "delete from bot_private.operation_runs where account=%s",
            ("test-phase5-fixture-" + market.lower(),),
        )
    outcomes = [fixture(api, market, step, tmp_path / str(step)) for step in range(1, 6)]
    assert outcomes[0]["pending"] == 1 and outcomes[0]["executions"] == 0
    assert outcomes[1]["executions"] == 1
    assert outcomes[2]["orders"] == 1
    assert outcomes[3]["pending"] == 1
    assert outcomes[4]["executions"] == 2 and outcomes[4]["pending"] == 0
    assert fixture(api, market, 5, tmp_path / "retry")["state"] == "ALREADY_COMPLETED"
