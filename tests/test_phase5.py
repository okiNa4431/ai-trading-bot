"""Offline market/FX/cycle regressions. Explicit synthetic data; no production fallback."""

from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from tests.ai_helpers import JP, NOW, ai_settings, histories
from trading_bot.broker.paper import PaperBroker
from trading_bot.cycle import TestProposal as Proposal
from trading_bot.cycle import run_cycle
from trading_bot.market_inputs import CorporateAction, YahooInputs, update_inputs
from trading_bot.operations import market_slot
from trading_bot.operations import test_scope as require_test
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import FXRate
from trading_bot.portfolio.valuation import validate_fx
from trading_bot.storage import ParquetStore


class MemoryOperation:
    """Offline test double only. Runtime Operation has no SQLite path."""

    def __init__(self, repo, key):
        self.repository, self.run_id = repo, key
        self.stage = 0
        self.saved = {}
        self.events = []

    def claim(self, owner, metadata):
        return True

    def step(self, stage, state="RUNNING", **outcome):
        self.stage = stage
        self.events.append((stage, state, outcome))

    def read(self):
        return {"data": self.saved}

    def data(self, key, payload):
        self.saved[key] = payload


def test_unresolved_action_is_retained_after_source_window_rolls(tmp_path):
    op = MemoryOperation(None, "retained-action")
    event = CorporateAction(
        symbol=JP.symbol,
        effective_date=(NOW - timedelta(days=61)).date(),
        split=2,
        dividend=0,
        currency="JPY",
        observed_at=NOW - timedelta(days=60),
    )
    op.saved["actions:" + JP.symbol] = {
        "events": [event.model_dump(mode="json")],
        "observed_at": (NOW - timedelta(days=1)).isoformat(),
    }

    class Actions:
        def fetch_actions(self, *args):
            return []

    with pytest.raises(ValueError):
        update_inputs(
            op, ParquetStore(tmp_path), [JP], None, Actions(), NOW, NOW - timedelta(days=90)
        )
    assert len(op.saved["actions:" + JP.symbol]["events"]) == 1


@pytest.mark.parametrize("account", ["paper-live", "test-other", "test-phase5-", "../paper-live"])
def test_production_scopes_never_enter_cycle(account):
    with pytest.raises(ValueError):
        require_test(account)


@pytest.mark.parametrize(
    "market,stamp,kind,valid",
    [
        ("JP", "2026-10-15T02:45:00Z", "execute", False),  # lunch
        ("JP", "2026-10-12T01:00:00Z", "execute", False),  # holiday
        ("JP", "2026-10-15T07:45:00Z", "decision", True),
        ("JP", "2026-10-15T06:35:00Z", "decision", False),
        ("US", "2026-03-06T14:35:00Z", "execute", True),
        ("US", "2026-03-09T13:35:00Z", "execute", True),  # DST
        ("US", "2026-11-02T14:35:00Z", "execute", True),
        ("US", "2026-11-27T19:15:00Z", "decision", True),  # 13 ET early close
        ("US", "2026-11-26T18:00:00Z", "execute", False),
        ("JP", "2026-10-17T01:00:00Z", "execute", False),
    ],
)
def test_market_calendars_and_slots(market, stamp, kind, valid):
    at = datetime.fromisoformat(stamp)
    if valid:
        assert market in market_slot(market, at, kind)
    else:
        with pytest.raises(ValueError, match="SKIPPED"):
            market_slot(market, at, kind)


def test_complete_buy_pending_hold_sell_roundtrip_across_executions(tmp_path):
    repo = SQLiteRepository(tmp_path / "only-test.sqlite3")
    store = ParquetStore(tmp_path / "data")
    for interval, bars in histories().items():
        store.save(JP, interval, bars)
    clock = NOW

    def prepare(broker):
        return None

    def cycle(key, kind, action="HOLD"):
        op = MemoryOperation(repo, key)
        result = run_cycle(
            op,
            store,
            [JP],
            "JP",
            ai_settings(),
            kind=kind,
            owner="offline-test",
            submit=True,
            strategy=Proposal(action),
            prepare=prepare,
            clock=lambda: clock,
            initialization_at=NOW - timedelta(days=30),
        )
        return result

    result = cycle("buy", "decision", "BUY")
    assert result["pending"] == 1 and result["executions"] == 0
    assert cycle("too-early", "execute")["pending"] == 1
    b = histories()["1h"][-1]
    future = b.model_copy(
        update={
            "timestamp": NOW.replace(hour=3, minute=30),
            "fetched_at": NOW.replace(hour=4, minute=31),
            "open": 125.0,
            "close": 125.0,
            "high": 126.0,
            "low": 124.0,
        }
    )
    store.save(JP, "1h", [future])
    store.save(
        JP,
        "1h",
        [
            future.model_copy(
                update={
                    "timestamp": NOW.replace(hour=4, minute=30),
                    "fetched_at": NOW.replace(hour=5, minute=31),
                }
            )
        ],
    )
    clock = NOW.replace(hour=5, minute=35)
    assert cycle("fill-buy", "execute")["executions"] == 1
    assert cycle("fill-buy-again", "execute")["executions"] == 1
    clock += timedelta(minutes=1)
    assert cycle("hold", "decision", "HOLD")["orders"] == 1
    clock += timedelta(minutes=1)
    assert cycle("sell", "decision", "SELL")["pending"] == 1
    nextbar = future.model_copy(
        update={
            "timestamp": NOW.replace(day=16, hour=0, minute=0),
            "fetched_at": NOW.replace(day=16, hour=1, minute=1),
            "open": 130.0,
            "close": 130.0,
            "high": 131.0,
            "low": 129.0,
        }
    )
    store.save(JP, "1h", [nextbar])
    clock = NOW.replace(day=16, hour=1, minute=5)
    result = cycle("fill-sell", "execute")
    broker = PaperBroker(repo)
    assert result["executions"] == 2 and result["pending"] == 0
    assert broker.get_balance().positions[JP.symbol].quantity == 0
    assert broker.get_balance().balances["JPY"] > Decimal("1000000")
    assert broker.get_balance().realized_pnl["JPY"] == sum(
        e.realized_pnl for e in broker.get_executions()
    )


@pytest.mark.parametrize("age", [-1, 3601])
def test_stale_future_fx_not_relabelled_as_fresh(age):
    rate = FXRate(usd_jpy=150, timestamp=NOW - timedelta(seconds=age), source="observed")
    with pytest.raises(ValueError):
        validate_fx(rate, NOW, PaperBroker(SQLiteRepository.__new__(SQLiteRepository)).rules)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -150])
def test_invalid_fx_rejected(value):
    with pytest.raises(ValueError):
        FXRate(usd_jpy=value, timestamp=NOW, source="observed")


def test_yahoo_fx_uses_confirmed_quote_time_and_no_synthetic_fallback(monkeypatch):
    class Ticker:
        def history(self, **kwargs):
            return pd.DataFrame(
                {"Close": [149.0, 150.0, 151.0]},
                index=pd.date_range(NOW - timedelta(minutes=10), periods=3, freq="5min"),
            )

    monkeypatch.setattr("trading_bot.market_inputs.yf.Ticker", lambda s: Ticker())
    monkeypatch.setattr("trading_bot.market_inputs.require_live_market_access", lambda: None)
    observed = YahooInputs().fetch_fx(NOW)
    assert observed.rate.usd_jpy == 150 and observed.rate.timestamp == NOW
    assert observed.observed_at != observed.rate.timestamp


@pytest.mark.parametrize("mode", ["split", "dividend", "unavailable", "discontinuity"])
def test_actions_and_bad_prices_stop_before_orders(tmp_path, mode):
    op = MemoryOperation(SQLiteRepository(tmp_path / "test.db"), "input")

    class Actions:
        def fetch_actions(self, instrument, at):
            if mode == "unavailable":
                raise TimeoutError()
            if mode == "discontinuity":
                return []
            return [
                CorporateAction(
                    symbol=JP.symbol,
                    effective_date=NOW.date(),
                    split=2 if mode == "split" else 0,
                    dividend=10 if mode == "dividend" else 0,
                    currency="JPY",
                    observed_at=NOW,
                )
            ]

    class Prices:
        def fetch(self, i, start, end, interval):
            rows = histories()[interval]
            return (
                rows
                if mode != "discontinuity" or interval != "1d"
                else [*rows[:-1], rows[-1].model_copy(update={"close": 300.0, "high": 301.0})]
            )

    with pytest.raises(ValueError):
        update_inputs(
            op,
            ParquetStore(tmp_path / "data"),
            [JP],
            Prices(),
            Actions(),
            NOW,
            NOW - timedelta(days=1),
        )


def test_unavailable_db_never_creates_fallback(tmp_path):
    from trading_bot.operations import Operation

    class Broken:
        def rpc(self, *args, **kwargs):
            raise ConnectionError("offline")

    op = Operation(Broken(), "test-phase5-offline", "slot")
    with pytest.raises(ConnectionError):
        op.claim("runner", {})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("market", ["JP", "US"])
def test_workflow_fixture_input_sequence_offline(tmp_path, monkeypatch, market):
    import scripts.phase5_fixture as module

    repo = SQLiteRepository(tmp_path / "fixture.db")
    runs = []

    class FixtureOperation(MemoryOperation):
        def __init__(self, api, account, key):
            super().__init__(repo, key)

        def read(self):
            return {"runs": runs}

        def step(self, stage, state="RUNNING", **outcome):
            super().step(stage, state, **outcome)
            if state == "SUCCEEDED":
                runs.append({"run_id": self.run_id, "state": state})

    monkeypatch.setattr(module, "Operation", FixtureOperation)
    results = [module.fixture(None, market, step, tmp_path / str(step)) for step in range(1, 6)]
    assert [r["executions"] for r in results] == [0, 1, 1, 1, 2]
    assert [r["pending"] for r in results] == [1, 0, 0, 1, 0]


@pytest.mark.parametrize(
    "age,expected",
    [(0, "FRESH"), (3600, "FRESH"), (3601, "STALE_OR_FUTURE"), (-1, "STALE_OR_FUTURE")],
)
def test_health_separates_fx_quote_age_from_download_time(age, expected):
    from trading_bot.operations import health_summary

    state = {
        "runs": [],
        "stopped": False,
        "data": {
            "fx:USDJPY": {
                "rate": {"timestamp": (NOW - timedelta(seconds=age)).isoformat()},
                "observed_at": NOW.isoformat(),
            }
        },
    }
    assert health_summary(state, NOW)["fx_state"] == expected


def test_health_detects_dead_runner_without_claiming_unknown_success():
    from trading_bot.operations import health_summary

    state = {
        "runs": [
            {
                "state": "RUNNING",
                "stage": 4,
                "outcome": None,
                "lease_until": (NOW - timedelta(seconds=1)).isoformat(),
            }
        ],
        "stopped": False,
        "data": {},
    }
    summary = health_summary(state, NOW)
    assert summary["unresolved"] == 1 and summary["active"] == 0
    assert state["runs"][0]["state"] == "RUNNING"


def test_fx_provenance_cannot_use_simulation_or_inverse_currency():
    from trading_bot.market_inputs import FXObservation

    rate = FXRate(usd_jpy=150, timestamp=NOW, source="fixed-simulation")
    with pytest.raises(ValueError):
        FXObservation(rate=rate, observed_at=NOW)
    rate = rate.model_copy(update={"source": "observed"})
    with pytest.raises(ValueError):
        FXObservation(rate=rate, observed_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError):
        FXObservation(rate=rate, observed_at=NOW, base_currency="JPY", quote_currency="USD")


def test_missing_first_future_bar_is_not_replaced_with_later_price():
    from trading_bot.cycle import require_execution_coverage

    # Use a real contract, but an intentionally empty synthetic history.
    from trading_bot.portfolio.models import Order, TradingRules

    o = Order(
        order_id="missing",
        symbol=JP.symbol,
        market="JP",
        currency="JPY",
        side="BUY",
        quantity=100,
        requested_at=NOW,
        costs=TradingRules(),
    )
    with pytest.raises(ValueError, match="missing required"):
        require_execution_coverage(o, [], NOW.replace(hour=5, minute=35))


def test_incremental_inputs_survive_a_fresh_runner_and_preserve_history(tmp_path):
    op = MemoryOperation(None, "fetch")

    class Actions:
        def fetch_actions(self, instrument, at):
            return []

    class Prices:
        def __init__(self):
            self.starts = []

        def fetch(self, instrument, start, end, interval):
            self.starts.append((interval, start))
            return histories()[interval]

    source = Prices()
    for name in ["first-runner", "second-runner"]:
        update_inputs(
            op,
            ParquetStore(tmp_path / name),
            [JP],
            source,
            Actions(),
            NOW,
            NOW - timedelta(days=30),
        )
    key = "bars:JP:7203.T:1d:yahoo:raw"
    assert len(op.saved[key]["bars"]) == 21
    assert source.starts[2][1] == histories()["1d"][-5].timestamp
    op.saved[key]["sha256"] = "tampered"
    with pytest.raises(ValueError):
        update_inputs(
            op,
            ParquetStore(tmp_path / "corrupt"),
            [JP],
            source,
            Actions(),
            NOW,
            NOW - timedelta(days=30),
        )


def test_one_bad_symbol_keeps_other_successful_data_but_blocks_cycle(tmp_path):
    from tests.ai_helpers import US

    op = MemoryOperation(None, "partial")

    class Actions:
        def fetch_actions(self, instrument, at):
            return []

    class Prices:
        def fetch(self, instrument, start, end, interval):
            if instrument.market == "US":
                raise TimeoutError()
            return histories()[interval]

    with pytest.raises(ValueError):
        update_inputs(
            op, ParquetStore(tmp_path), [JP, US], Prices(), Actions(), NOW, NOW - timedelta(days=30)
        )
    assert "bars:JP:7203.T:1d:yahoo:raw" in op.saved
    assert "bars:US:AAPL:1d:yahoo:raw" not in op.saved


@pytest.mark.parametrize(
    "filename", ["paper-cycle.yml", "paper-healthcheck.yml", "paper-reconcile.yml"]
)
def test_manual_workflows_cannot_enable_a_schedule(filename):
    from pathlib import Path

    import yaml

    document = yaml.safe_load((Path(".github/workflows") / filename).read_text(encoding="utf-8"))
    triggers = document.get("on", document.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert document["permissions"] == {"contents": "read"}
    defaults = triggers["workflow_dispatch"]["inputs"]
    assert defaults["account"]["default"].startswith("test-phase5-")
    if filename == "paper-cycle.yml":
        assert defaults["provider"]["default"] == "mock"
        assert defaults["submit"]["default"] is False
    if filename == "paper-reconcile.yml":
        assert defaults["apply_review"]["default"] is False
        assert document["jobs"]["reviewed_recovery"]["environment"] == "phase5-recovery"


def test_phase5_gemini_retry_override_is_rejected_before_database_or_network():
    with pytest.raises(ValueError, match="one request"):
        run_cycle(
            None,
            None,
            [],
            "JP",
            ai_settings(retries=1),
            kind="decision",
            owner="bad",
            provider="gemini",
            prepare=lambda b: None,
        )
