"""Synthetic next-session execution and downstream safety boundaries."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest

from tests.test_security_quality import assessment
from tests.test_security_quality import inputs as inputs
from trading_bot.broker.paper import PaperBroker
from trading_bot.data_access import ReadOnlyQualityReport
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import FXRate, TradingRules
from trading_bot.security_quality import SecurityQualityStop
from trading_bot.synthetic_mvp import (
    MVPSettings,
    SyntheticQualityBroker,
    run_mvp,
    signal,
    synthetic_inputs,
)


@pytest.fixture
def engine(inputs, tmp_path):
    data, u, _, _ = inputs
    q = assessment(inputs)
    b = PaperBroker(
        SQLiteRepository(tmp_path / "synthetic.sqlite3"), TradingRules(commission_usd=Decimal(1))
    )
    b.initialize(q.as_of - timedelta(seconds=1))
    fx = FXRate(usd_jpy=Decimal(150), timestamp=q.as_of, source="fixed-simulation")
    b.exchange("JPY", Decimal(800000), fx, q.as_of, "synthetic-funding")
    gate = SyntheticQualityBroker(b, q)
    instrument = next(
        s.instrument for s in data.securities if s.instrument.symbol == u.assignments[0].ticker
    )
    _, _, rows = synthetic_inputs(
        data, MVPSettings(symbols=20, sessions=202, review_fixtures=False)
    )
    future = {s: [bs[60]] for s, bs in rows.items() if s in ("S0000", "S0001")}
    return data, gate, instrument, future


def move(gate, at, blocked=False):
    q = gate.quality
    pop = tuple(
        replace(
            s,
            assessed_at=at,
            strategy_eligibility="QUARANTINED"
            if blocked and s.ticker == "S0000"
            else s.strategy_eligibility,
        )
        for s in q.population
    )
    gate.update(replace(q, as_of=at, population=pop))


def fill(engine, quantity=10):
    data, g, instrument, rows = engine
    g.place(instrument, "BUY", quantity, g.quality.as_of, "synthetic-buy")
    move(g, data.opens_at[60])
    fx = FXRate(usd_jpy=Decimal(150), timestamp=data.opens_at[60], source="fixed-simulation")
    return g.execute("synthetic-buy", rows, fx, data.closes_at[60] + data.latency), fx


def test_buy_next_session_costs_and_idempotent_fill(engine):
    data, g, _, rows = engine
    requested = g.quality.as_of
    order, fx = fill(engine)
    assert order.status == "FILLED"
    e = g.broker.get_executions()[0]
    assert e.price_at == data.opens_at[60] and e.price_at > requested
    assert e.commission == 1 and e.price > e.reference_price
    assert g.execute(order.order_id, rows, fx, data.closes_at[60] + data.latency) == order
    assert len(g.broker.get_executions()) == 1
    assert g.broker.get_positions()[0].cost_basis == e.notional + e.commission


@pytest.mark.parametrize("quantity", [100, 100000])
def test_risk_or_cash_rejection(engine, quantity):
    order, _ = fill(engine, quantity)
    assert order.status == "REJECTED" and not engine[1].broker.get_executions()
    assert all(x >= 0 for x in engine[1].broker.get_balance().balances.values())


def test_no_future_bar_pending(engine):
    _, g, instrument, _ = engine
    at = g.quality.as_of
    g.place(instrument, "BUY", 1, at, "pending")
    fx = FXRate(usd_jpy=Decimal(150), timestamp=at, source="fixed-simulation")
    assert g.execute("pending", {}, fx, at).status == "PENDING"
    assert not g.broker.get_executions()


def test_sell_updates_cash_quantity_and_realized_pnl(engine):
    data, g, instrument, _ = engine
    order, _ = fill(engine)
    assert order.status == "FILLED"
    at = data.closes_at[60] + data.latency
    move(g, at)
    g.place(instrument, "SELL", 10, at, "synthetic-sell")
    cash = g.broker.get_balance().balances["USD"]
    move(g, data.opens_at[61])
    _, _, rows = synthetic_inputs(
        data, MVPSettings(symbols=20, sessions=202, review_fixtures=False)
    )
    rows = {s: [bars[61]] for s, bars in rows.items() if s in ("S0000", "S0001")}
    fx = FXRate(usd_jpy=Decimal(150), timestamp=data.opens_at[61], source="fixed-simulation")
    assert (
        g.execute("synthetic-sell", rows, fx, data.closes_at[61] + data.latency).status == "FILLED"
    )
    last = g.broker.get_executions()[-1]
    assert last.price < last.reference_price and last.price_at == data.opens_at[61]
    assert not g.broker.get_positions()
    assert g.broker.get_balance().balances["USD"] == cash + last.notional - last.commission
    assert g.broker.get_balance().realized_pnl["USD"] == last.realized_pnl


def test_cash_floor_is_checked_after_fees(engine):
    _, g, _, _ = engine
    g.broker.rules = TradingRules(min_cash_ratio=Decimal(".99"), commission_usd=Decimal(1))
    order, _ = fill(engine)
    assert order.status == "REJECTED" and "cash ratio" in order.reason
    assert not g.broker.get_executions()


def test_later_quality_cannot_authorize_earlier_open(engine):
    data, g, instrument, rows = engine
    g.place(instrument, "BUY", 1, g.quality.as_of, "future-quality")
    at = data.closes_at[60] + data.latency
    move(g, at)
    fx = FXRate(usd_jpy=Decimal(150), timestamp=data.opens_at[60], source="fixed-simulation")
    with pytest.raises(SecurityQualityStop, match="later quality"):
        g.execute("future-quality", rows, fx, at)
    assert not g.broker.get_executions()


@pytest.mark.parametrize("state", ["UNKNOWN", "QUARANTINED"])
def test_quality_refuses_new_order(engine, state):
    _, g, i, _ = engine
    q = g.quality
    g.update(
        replace(q, population=tuple(replace(s, strategy_eligibility=state) for s in q.population))
    )
    with pytest.raises(SecurityQualityStop):
        g.place(i, "BUY", 1, q.as_of, "denied")
    assert not g.broker.get_orders()


def test_quarantined_holdings_stop_valuation_and_liquidation(engine):
    data, g, i, rows = engine
    order, fx = fill(engine)
    assert order.status == "FILLED"
    at = data.closes_at[60] + data.latency
    move(g, at, True)
    with pytest.raises(SecurityQualityStop):
        g.value(rows, fx, at)
    with pytest.raises(SecurityQualityStop):
        g.place(i, "SELL", 10, at, "unsafe-liquidation")
    assert g.broker.get_positions()[0].quantity == 10


@pytest.mark.parametrize("kind", ["alpaca", "future", "observed_fx", "diagnostic", "remote_repo"])
def test_external_inputs_refused(engine, kind):
    data, g, i, rows = engine
    if kind == "remote_repo":
        with pytest.raises(SecurityQualityStop):
            SyntheticQualityBroker(PaperBroker(Mock()), g.quality)
        return
    g.place(i, "BUY", 1, g.quality.as_of, "no-market")
    move(g, data.opens_at[60])
    at = data.closes_at[60] + data.latency
    fx = FXRate(usd_jpy=Decimal(150), timestamp=data.opens_at[60], source="fixed-simulation")
    if kind == "alpaca":
        rows = {
            s: [b.model_copy(update={"source": "alpaca"}) for b in bs] for s, bs in rows.items()
        }
    elif kind == "future":
        rows = {
            s: [b.model_copy(update={"fetched_at": at + timedelta(seconds=1)}) for b in bs]
            for s, bs in rows.items()
        }
    elif kind == "observed_fx":
        fx = fx.model_copy(update={"source": "observed"})
    else:
        rows = ReadOnlyQualityReport('{"purpose":"diagnostic_only","strategy_approved":false}')
    with pytest.raises(ValueError):
        g.execute("no-market", rows, fx, at)
    assert not g.broker.get_executions()


@pytest.mark.parametrize("which", ["entry", "stop", "trailing", "age", "hold"])
def test_rule_signals(which):
    prices = [100.0] * 40 + list(range(101, 121))
    extras = {}
    expected = "BUY"
    if which != "entry":
        extras = dict(average_cost=100.0, peak=100.0, age=1)
        prices[-1] = 100.0
        expected = "HOLD"
        if which == "stop":
            prices[-1] = 91.0
            expected = "SELL"
        elif which == "trailing":
            prices[-1] = 109.0
            extras["peak"] = 125.0
            expected = "SELL"
        elif which == "age":
            extras["age"] = 30
            expected = "SELL"
    assert signal(prices, prices, MVPSettings(), **extras)[0] == expected


def test_synthetic_100_mvp_no_external_clients(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("external client created"))
    r = run_mvp()
    s = r.summary
    assert s["data_kind"] == "synthetic" and s["status"] == "COMPLETE"
    assert s["quality"]["states"] == {"ELIGIBLE": 97, "QUARANTINED": 1, "UNKNOWN": 2}
    assert s["trade_count"] > 0 and s["closed_trades"] > 0
    assert s["real_api_requests"] == 0 and not s["market_strategy_approved"]
    assert s["excess_return"] == pytest.approx(s["cumulative_return"] - s["benchmark_return"])
    assert all(len(q.population) == 100 for q in r.history.snapshots)
    assert all(d["data_kind"] == "synthetic" for d in r.daily)
    assert min(float(d["cash_ratio"]) for d in r.daily) >= 0.2
    assert s["risk_exit_counts"]["max_holding_sessions"] > 0


def trend_fixture():
    import numpy as np

    from trading_bot.swing.data import synthetic

    d = synthetic(20, 202)
    close = np.tile((40 + np.arange(202) * 0.1)[:, None], (1, 20))
    opening = close - 0.05
    return replace(
        d,
        opening=opening,
        closing=close,
        high=close * 1.001,
        low=opening * 0.999,
        market=close.mean(axis=1),
        hourly_available=False,
    )


def test_late_corporate_discovery_stops_holdings_without_retroactive_removal(tmp_path):
    from trading_bot.swing.models import MarketEvent
    from trading_bot.synthetic_mvp import replay

    d = trend_fixture()
    event = MarketEvent(
        symbol="S0000",
        kind="split",
        effective_at=d.opens_at[182],
        observed_at=d.closes_at[182] + d.latency,
    )
    d = replace(d, events=(event,))
    r = replay(
        d,
        MVPSettings(symbols=20, sessions=202, slow_sma=180, review_fixtures=False),
        SQLiteRepository(tmp_path / "synthetic-late.sqlite3"),
    )
    assert r.summary["status"] == "SAFE_STOP" and r.summary["current_assets_jpy"] is None
    assert r.summary["cumulative_return"] is None and r.summary["benchmark_return"] is None
    assert r.executions and all(e.side == "BUY" for e in r.executions)
    assert r.history.snapshots[0].summary()["states"]["ELIGIBLE"] == 20
    assert r.history.snapshots[-1].summary()["states"]["QUARANTINED"] == 1
    assert r.summary["held_positions"] > 0


def test_future_price_changes_do_not_change_earlier_orders_or_equity(tmp_path):
    from trading_bot.synthetic_mvp import replay

    d = trend_fixture()
    s = MVPSettings(symbols=20, sessions=202, slow_sma=180, review_fixtures=False)
    close = d.closing.copy()
    opening = d.opening.copy()
    high = d.high.copy()
    low = d.low.copy()
    close[195:] *= 0.95
    opening[195:] *= 0.95
    high[195:] *= 0.95
    low[195:] *= 0.95
    modified = replace(
        d, closing=close, opening=opening, high=high, low=low, market=close.mean(axis=1)
    )
    a = replay(d, s, SQLiteRepository(tmp_path / "synthetic-a.sqlite3"))
    b = replay(modified, s, SQLiteRepository(tmp_path / "synthetic-b.sqlite3"))
    assert a.summary["status"] == b.summary["status"] == "COMPLETE"
    assert a.executions and b.executions
    assert a.daily[:15] == b.daily[:15]
    assert [e.model_dump() for e in a.executions if e.price_at < d.opens_at[195]] == [
        e.model_dump() for e in b.executions if e.price_at < d.opens_at[195]
    ]


def test_existing_account_and_licensed_data_are_never_reused(engine):
    from trading_bot.synthetic_mvp import replay

    d, g, _, _ = engine
    settings = MVPSettings(symbols=20, sessions=202)
    before = g.broker.get_balance()
    with pytest.raises(SecurityQualityStop):
        replay(d, settings, g.broker.repository)
    assert g.broker.get_balance() == before

    licensed = replace(d, rights=d.rights.model_copy(update={"kind": "licensed"}))
    with pytest.raises(SecurityQualityStop):
        replay(licensed, settings, g.broker.repository)
    assert g.broker.get_balance() == before


@pytest.mark.parametrize("origin", ["licensed", "wrong_source", "market_ticker"])
def test_ingress_rejects_market_origin_even_before_account_access(inputs, origin):
    data, _, _, _ = inputs
    if origin == "licensed":
        data = replace(data, rights=data.rights.model_copy(update={"kind": "licensed"}))
    elif origin == "wrong_source":
        data = replace(data, rights=data.rights.model_copy(update={"source": "Alpaca IEX"}))
    else:
        s = data.securities[0]
        changed = s.model_copy(
            update={"instrument": s.instrument.model_copy(update={"symbol": "AAPL"})}
        )
        data = replace(data, securities=(changed, *data.securities[1:]))
    with pytest.raises(SecurityQualityStop, match="real data"):
        synthetic_inputs(data, MVPSettings(symbols=20, sessions=202))
