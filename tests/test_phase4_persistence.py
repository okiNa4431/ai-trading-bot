import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from tests.phase4_helpers import MemoryRPC
from tests.test_paper import JP, T, bar, rate
from trading_bot.broker.paper import PaperBroker
from trading_bot.cli import main
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.persistence.supabase_repository import (
    RepositoryConflict,
    SupabaseAPI,
    SupabaseError,
    SupabaseRepository,
)
from trading_bot.phase4_cli import verify
from trading_bot.strategy.models import DecisionRun
from trading_bot.strategy.security import redact


@pytest.fixture(params=["sqlite", "rpc"])
def repo(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteRepository(tmp_path / "account.sqlite3")
    return SupabaseRepository(MemoryRPC().api, "test-contract", test=True)


@pytest.fixture
def broker(repo):
    b = PaperBroker(repo)
    b.initialize(T - timedelta(days=1))
    return b


def test_shared_contract_initialization_reconnection(broker, repo):
    before = broker.get_balance()
    assert PaperBroker(repo).initialize(T) == before
    assert len(broker.history()["cash_ledger"]) == 1
    assert before.initial_jpy == Decimal("1000000")


def test_shared_contract_execution_persistence_and_exactly_once(broker, repo):
    order = broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1), "one-buy")
    assert not broker.get_executions()
    assert (
        broker.execute(order.order_id, {JP.symbol: [bar()]}, None, T + timedelta(hours=1)).status
        == "FILLED"
    )
    after = broker.get_balance()
    restarted = PaperBroker(repo)
    assert restarted.get_balance() == after
    assert restarted.get_positions()[0].quantity == 100
    assert (
        restarted.execute(order.order_id, {JP.symbol: [bar()]}, None, T + timedelta(hours=1)).status
        == "FILLED"
    )
    assert len(restarted.get_executions()) == 1
    assert len(restarted.get_orders()) == 1
    assert verify(repo)["verified"]
    assert after.balances["JPY"] == Decimal("949950")


def test_shared_contract_decision_fx_and_cash_ledger(broker, repo):
    event = broker.exchange("JPY", Decimal("300000"), rate(T), T, "fx-once")
    assert broker.exchange("JPY", Decimal("300000"), rate(T), T, "fx-once") == event
    assert PaperBroker(repo).get_balance().balances["USD"] == Decimal("1995.00")
    r = DecisionRun(
        decision_id="decision-one",
        started_at=T,
        as_of=T,
        market="JP",
        provider="mock",
        model="mock",
        prompt_version="test",
        input_hash="hash",
        account_hash="hash",
    )
    d = DecisionRepository(repo)
    assert d.claim(r) and not d.claim(r)
    d.save(r.model_copy(update={"status": "SUCCESS"}))
    assert DecisionRepository(repo).history()[0].status == "SUCCESS"
    assert len(broker.history()["fx_exchanges"]) == 1
    assert verify(repo)["cash_entries"] == 3


def test_shared_contract_transaction_rollback(broker, repo):
    before = broker.get_balance()
    with pytest.raises(RuntimeError):
        with repo.transaction() as tx:
            tx.save_account(
                before.model_copy(update={"balances": {"JPY": Decimal(0), "USD": Decimal(0)}})
            )
            raise RuntimeError("abort")
    assert broker.get_balance() == before


def test_shared_contract_invalid_id_reuse_and_append_only(broker):
    broker.place_order(JP, "BUY", 100, T, "same")
    with pytest.raises(ValueError, match="different"):
        broker.place_order(JP, "BUY", 200, T, "same")
    assert len(broker.get_orders()) == 1


def test_rpc_stale_transaction_cannot_overwrite_concurrent_account():
    server = MemoryRPC()
    repo = SupabaseRepository(server.api, "test-concurrent", test=True)
    broker = PaperBroker(repo)
    broker.initialize(T)
    with pytest.raises(RepositoryConflict):
        with repo.transaction() as stale:
            broker.place_order(JP, "BUY", 100, T, "first")
            stale.save_account(stale.account())
    assert len(broker.get_orders()) == 1
    assert verify(repo)["verified"]


def test_rpc_read_only_never_commits():
    server = MemoryRPC()
    repo = SupabaseRepository(server.api, "test-read", test=True)
    PaperBroker(repo).initialize(T)
    server.requests.clear()
    PaperBroker(repo).get_balance()
    assert server.requests == ["bot_read"]


@pytest.mark.parametrize("name,test", [("paper-live", True), ("test-a", False), ("unsafe/?", True)])
def test_account_scope_cannot_confuse_test_and_live(name, test):
    with pytest.raises(ValueError):
        SupabaseRepository(MemoryRPC().api, name, test=test)


@pytest.mark.parametrize(
    "url",
    [
        "http://project.test",
        "https://user:pass@project.test",
        "https://project.test/rest",
        "https://project.test?key=secret",
    ],
)
def test_project_url_rejects_unsafe_configuration(url):
    with pytest.raises(ValueError):
        SupabaseAPI(url, "fake")


@pytest.mark.parametrize("status,code", [(401, "42501"), (500, "XX000"), (404, "PGRST202")])
def test_transport_never_reveals_remote_exception_body(status, code, caplog):
    api = SupabaseAPI(
        "https://isolated.invalid",
        "sb_secret_test",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    status, json={"code": code, "message": "sb_secret_should_never_appear"}
                )
            )
        ),
    )
    with pytest.raises(SupabaseError) as error:
        api.rpc("bot_read", {"p_account": "test-a"})
    assert "sb_secret_should_never_appear" not in str(error.value) + caplog.text


def test_secret_redaction_all_server_secrets(monkeypatch):
    for name in [
        "GEMINI_API_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_DB_URL",
        "SUPABASE_ACCESS_TOKEN",
    ]:
        monkeypatch.setenv(name, "hidden-" + name)
        assert "hidden-" not in redact("hidden-" + name)
    assert "sb_secret_foo" not in redact("echo sb_secret_foo")


def test_local_phase4_cli_init_status_verify_and_empty_performance(tmp_path, capsys):
    path = str(tmp_path / "test.sqlite3")
    for command in [
        ("db", "initialize"),
        ("db", "status"),
        ("db", "verify"),
        ("portfolio", "performance"),
        ("dashboard", "publish"),
    ]:
        assert main([*command, "--db", path]) == 0
        value = json.loads(capsys.readouterr().out)
        assert isinstance(value, dict)
    assert main(["db", "delete-test", "--db", path, "--confirm-account", "paper-live"]) == 2
