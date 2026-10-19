from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest

from tests.test_postgres import PGAPI
from tests.test_postgres import uri as base_uri
from trading_bot.persistence.research import PrivateResearchStore
from trading_bot.persistence.supabase_repository import RepositoryConflict

uri = base_uri  # Register the existing disposable-DB fixture without redefining its implementation.


@pytest.fixture
def store(uri):
    return PrivateResearchStore(PGAPI(uri), "test-integration-" + uuid4().hex, approved=True)


def test_cas_atomic_replay_and_different_identity(store):
    assert store.read()["version"] == 0
    assert store.commit(
        uuid := uuid4().hex, 0, {"a": 1}, [], {"account": None, "records": []}, {"ok": True}
    ) == {"ok": True}
    assert store.commit(uuid, 0, {"a": 1}, [], {}, {"ok": False}) == {"ok": True}
    assert store.read()["version"] == 1 and store.reconcile(uuid) == {"ok": True}
    with pytest.raises(RepositoryConflict):
        store.commit(uuid, 1, {"a": 2}, [], {}, {})
    with pytest.raises(RepositoryConflict):
        store.commit(uuid4().hex, 0, {}, [], {}, {})


def test_concurrent_cas_winner_only(store):
    def writer(_):
        try:
            store.commit(uuid4().hex, 0, {}, [], {}, {})
            return True
        except RepositoryConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(writer, range(2))) == 1
    assert store.read()["version"] == 1


def test_rls_and_rpc_access(uri, store):
    store.commit(uuid4().hex, 0, {}, [], {}, {})
    for role in ("anon", "authenticated"):
        with psycopg.connect(uri) as c:
            c.execute("set role " + role)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select public.bot_research_read(%s)", (store.account_id,))
        # Temporarily grant table access to test RLS itself independently of the ACL.
        with psycopg.connect(uri) as c:
            c.execute("grant usage on schema bot_research_private to " + role)
            c.execute("grant select on bot_research_private.accounts to " + role)
            c.execute("set role " + role)
            assert (
                c.execute("select count(*) from bot_research_private.accounts").fetchone()[0] == 0
            )
            c.rollback()


def test_invalid_observation_rolls_back_all(uri, store):
    with pytest.raises(psycopg.Error):
        store.api.rpc(
            "bot_research_commit",
            dict(
                p_account=store.account_id,
                p_run=uuid4().hex,
                p_version=0,
                p_request_hash="a" * 64,
                p_bars=[{"id": "bad", "payload": {}}],
                p_state={},
                p_result={},
            ),
        )
    assert store.read()["version"] == 0


def test_live_account_and_unapproved_store_rejected(uri):
    with pytest.raises(ValueError):
        PrivateResearchStore(PGAPI(uri), "paper-live", approved=True)
    with pytest.raises(ValueError, match="approval"):
        PrivateResearchStore(PGAPI(uri), "test-integration-no-approval").read()
