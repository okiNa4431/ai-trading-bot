"""Real PostgreSQL tests: CI's disposable local service ONLY, never production Supabase."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from tests.ai_helpers import ai_settings
from tests.test_paper import JP, T, bar, rate
from trading_bot.broker.paper import PaperBroker
from trading_bot.orchestration.engine import BoundRepository
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.supabase_repository import (
    RepositoryConflict,
    SupabaseAPI,
    SupabaseRepository,
)
from trading_bot.phase4_cli import verify
from trading_bot.strategy.models import DecisionRun


class PGAPI(SupabaseAPI):
    def __init__(self, uri):
        self.uri = uri

    def rpc(self, name, body):
        assert name.startswith("bot_")
        fields = list(body)
        query = sql.SQL("select public.{}({})").format(
            sql.Identifier(name),
            sql.SQL(",").join(sql.SQL("{} := %s").format(sql.Identifier(k)) for k in fields),
        )
        values = [Jsonb(body[k]) if isinstance(body[k], (dict, list)) else body[k] for k in fields]
        try:
            with psycopg.connect(self.uri) as c:
                c.execute("set role service_role")
                return c.execute(query, values).fetchone()[0]
        except psycopg.errors.SerializationFailure:
            raise RepositoryConflict("version conflict") from None


@pytest.fixture(scope="module")
def uri():
    uri = os.environ.get("TEST_DATABASE_URL")
    if not uri:
        pytest.skip(
            "requires isolated PostgreSQL service; local SQL is also tested with verify_sql.mjs"
        )
    parsed = urlparse(uri)
    assert parsed.hostname in ("localhost", "127.0.0.1") and parsed.path == "/phase4_test"
    with psycopg.connect(uri) as c:
        for role in ["anon", "authenticated", "service_role"]:
            if not c.execute("select 1 from pg_roles where rolname=%s", (role,)).fetchone():
                c.execute(sql.SQL("create role {}").format(sql.Identifier(role)))
        c.execute("grant usage on schema public to anon,authenticated,service_role")
        for p in sorted(Path("supabase/migrations").glob("*.sql")):
            c.execute(p.read_text(encoding="utf-8"), prepare=False)
    return uri


@pytest.fixture
def repo(uri):
    with psycopg.connect(uri) as c:
        c.execute("truncate bot_private.accounts cascade")
        c.execute("delete from public.bot_dashboard")
    r = SupabaseRepository(PGAPI(uri), "test-postgres", test=True)
    PaperBroker(r).initialize(T - timedelta(days=1))
    return r


def test_actual_pg_account_fx_order_execution_reconnect(repo):
    b = PaperBroker(repo)
    original = b.get_balance()
    assert b.initialize(T) == original
    b.place_order(JP, "BUY", 100, T - timedelta(seconds=1), "buy")
    assert b.execute("buy", {JP.symbol: [bar()]}, None, T + timedelta(hours=1)).status == "FILLED"
    assert b.execute("buy", {JP.symbol: [bar()]}, None, T + timedelta(hours=1)).status == "FILLED"
    b.exchange(
        "JPY", original.initial_jpy / 10, rate(T + timedelta(hours=2)), T + timedelta(hours=2), "fx"
    )
    assert len(PaperBroker(repo).get_executions()) == 1
    assert verify(repo)["verified"]
    assert len(b.history()["fx_exchanges"]) == 1


def test_actual_pg_competing_buy_snapshots_have_one_atomic_winner(repo):
    b = PaperBroker(repo)
    for key in ["a", "b"]:
        b.place_order(JP, "BUY", 100, T - timedelta(seconds=1), key)
    barrier = threading.Barrier(2)

    def buyer(key):
        try:
            with repo.transaction() as tx:
                barrier.wait(timeout=10)
                # Both executions use the same original cash; the stale one must roll back.
                bound = PaperBroker(BoundRepository(tx))
                assert (
                    bound.execute(key, {JP.symbol: [bar()]}, None, T + timedelta(hours=1)).status
                    == "FILLED"
                )
            return True
        except RepositoryConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(buyer, ["a", "b"]))
    assert sum(results) == 1
    assert len(b.get_executions()) == 1
    assert sum(o.status == "FILLED" for o in b.get_orders()) == 1
    assert b.get_balance().balances["JPY"] == 949950
    assert verify(repo)["verified"]


def test_actual_pg_bad_batch_rolls_back_all_writes(repo):
    b = PaperBroker(repo)
    original = b.get_balance()
    b.place_order(JP, "BUY", 100, T, "before")
    with pytest.raises(psycopg.Error):
        with repo.transaction() as tx:
            order = b.get_order("before")
            tx.put("orders", "before", order.model_copy(update={"status": "CANCELLED"}))
            tx.save_account(original.model_copy(update={"balances": {"JPY": 1, "USD": 0}}))
    assert b.get_order("before").status == "PENDING"
    assert b.get_balance() == original


def claim(repo, key):
    d = DecisionRepository(repo)
    d.claim(
        DecisionRun(
            decision_id=key,
            started_at=T,
            as_of=T,
            market="JP",
            provider="mock",
            model="test",
            prompt_version="test",
            input_hash="hash",
            account_hash="hash",
        )
    )
    return d


def test_actual_pg_global_quota_simultaneous_reservation_and_restart(repo):
    d = claim(repo, "quota")
    settings = ai_settings(daily_request_limit=1, monthly_request_limit=1)

    def reserve(_):
        try:
            return d.reserve("quota", settings)
        except psycopg.Error:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(reserve, range(2)))
    assert sum(r is not None for r in rows) == 1
    assert len(DecisionRepository(repo).usage()) == 1
    reservation = next(r for r in rows if r)
    assert reservation.input_tokens is None
    d.save_usage(reservation.model_copy(update={"status": "ERROR", "error": "timeout"}))
    assert DecisionRepository(repo).usage()[0].status == "ERROR"
    with pytest.raises(psycopg.Error):
        d.reserve("quota", settings)


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize(
    "operation",
    [
        "select * from bot_private.accounts",
        "select * from bot_private.records",
        "select public.bot_read('test-postgres')",
        "select public.bot_delete_test('test-postgres')",
        "insert into public.bot_dashboard values('paper-live','{}',now())",
        "update public.bot_dashboard set document='{}'",
        "delete from public.bot_dashboard",
    ],
)
def test_actual_pg_public_roles_cannot_read_or_write_internal_state(repo, uri, role, operation):
    with psycopg.connect(uri) as c:
        c.execute(sql.SQL("set role {}").format(sql.Identifier(role)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(operation)


def test_actual_pg_public_projection_is_readable_but_tests_cannot_publish(repo, uri):
    state = repo.api.rpc("bot_read", {"p_account": repo.account_id})
    # The repository adapter normalizes SQLSTATE 40001 to its public conflict contract.
    with pytest.raises(RepositoryConflict, match="version conflict"):
        repo.api.rpc(
            "bot_publish",
            {
                "p_account": repo.account_id,
                "p_version": state["version"],
                "p_document": {"schema_version": 1, "state": "PREPARING"},
            },
        )
    with psycopg.connect(uri) as c:
        c.execute(
            "insert into public.bot_dashboard values('paper-live','{\"schema_version\":1}',now())"
        )
        c.execute("set role anon")
        assert len(c.execute("select * from public.bot_dashboard").fetchall()) == 1


def test_actual_pg_terminal_order_cannot_reopen(repo):
    b = PaperBroker(repo)
    b.place_order(JP, "BUY", 100, T, "cancel")
    closed = b.cancel_order("cancel")
    with pytest.raises(psycopg.Error):
        with repo.transaction() as tx:
            tx.put("orders", "cancel", closed.model_copy(update={"status": "PENDING"}))
    assert b.get_order("cancel").status == "CANCELLED"
