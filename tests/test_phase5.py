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
