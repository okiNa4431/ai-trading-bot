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
    from tests.test_paper import bar

    store.commit(uuid4().hex, 0, {}, [bar()], {}, {})
    for role in ("anon", "authenticated"):
        with psycopg.connect(uri) as c:
            c.execute("set role " + role)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select public.bot_research_read(%s)", (store.account_id,))
        # Temporarily grant table access to test RLS itself independently of the ACL.
        with psycopg.connect(uri) as c:
            c.execute("grant usage on schema bot_research_private to " + role)
            c.execute("grant select on all tables in schema bot_research_private to " + role)
            c.execute("set role " + role)
            for table in ("accounts", "observations", "runs"):
                assert (
                    c.execute("select count(*) from bot_research_private." + table).fetchone()[0]
                    == 0
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


def test_observed_cycle_persists_and_restores_with_observation_dedup(uri, store):
    from decimal import Decimal

    from tests.ai_helpers import ai_settings
    from tests.integration_helpers import observed_fixture
    from tests.test_observed_integration import RULES, SETTINGS, proposed, stage
    from trading_bot.swing.cycle import commit_cycle

    data, at = observed_fixture()
    _, draft = stage({}, data, at, "preview")
    first = commit_cycle(
        store,
        data,
        at,
        uuid4().hex,
        "decision",
        ai_settings(),
        SETTINGS,
        RULES,
        funding_jpy=Decimal(800000),
        mock_response=proposed(draft, "BUY"),
    )
    assert first["orders"][0]["status"] == "PENDING"
    with psycopg.connect(uri) as c:
        count = c.execute(
            "select count(*) from bot_research_private.observations where account_id=%s",
            (store.account_id,),
        ).fetchone()[0]
    assert count == 202 * 21
    data2, at2 = observed_fixture(202)
    restored = PrivateResearchStore(PGAPI(uri), store.account_id, approved=True)
    second = commit_cycle(
        restored, data2, at2, uuid4().hex, "execute", ai_settings(), SETTINGS, RULES
    )
    assert second["orders"][0]["status"] == "FILLED" and len(second["executions"]) == 1
    with psycopg.connect(uri) as c:
        assert (
            c.execute(
                "select count(*) from bot_research_private.observations where account_id=%s",
                (store.account_id,),
            ).fetchone()[0]
            == count + 21
        )
    assert restored.read()["version"] == 2


def test_successful_commit_response_loss_is_reconciled_not_resent(store):
    from trading_bot.persistence.supabase_repository import SupabaseError

    original = store.api

    class LoseResponse:
        calls = 0

        def rpc(self, name, body):
            result = original.rpc(name, body)
            if name == "bot_research_commit":
                self.calls += 1
                raise SupabaseError("response lost; inspect run")
            return result

    lost = LoseResponse()
    store.api = lost
    key = uuid4().hex
    with pytest.raises(SupabaseError):
        store.commit(key, 0, {}, [], {}, {"private": True})
    assert lost.calls == 1
    assert store.reconcile(key) == {"private": True} and store.read()["version"] == 1


def test_retrieval_time_change_deduplicates_but_preserves_first_observation(uri, store):
    from datetime import timedelta

    from tests.test_paper import bar

    first = bar()
    second = first.model_copy(update={"fetched_at": first.fetched_at + timedelta(minutes=1)})
    original = store.commit(uuid4().hex, 0, {}, [first], {}, {})
    repeated = store.commit(uuid4().hex, 1, {}, [second], {}, {})
    assert original["observation_ids"] == repeated["observation_ids"]
    with psycopg.connect(uri) as c:
        rows = c.execute(
            "select payload from bot_research_private.observations where account_id=%s",
            (store.account_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0]["fetched_at"] == first.model_dump(mode="json")["fetched_at"]
    changed = second.model_copy(update={"volume": second.volume + 1})
    revised = store.commit(uuid4().hex, 2, {}, [changed], {}, {})
    reverted = store.commit(uuid4().hex, 3, {}, [second], {}, {})
    assert revised["observation_ids"] != original["observation_ids"]
    assert reverted["observation_ids"] == original["observation_ids"]
