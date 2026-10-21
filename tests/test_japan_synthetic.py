"""JP-1 synthetic contracts; no network or real financial inputs."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from trading_bot.broker.execution import session
from trading_bot.broker.paper import PaperBroker
from trading_bot.data_access import ReadOnlyQualityReport
from trading_bot.japan_synthetic import (
    FinancialScore,
    JapanSettings,
    SyntheticNAV,
    benchmark_value,
    generate,
    quality,
    replay,
    validate_scope,
)
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import TradingRules


@pytest.fixture(scope="module")
def settings():
    return JapanSettings(sessions=105)


@pytest.fixture(scope="module")
def data(settings):
    return generate(settings)


def repo(tmp_path):
    return SQLiteRepository(tmp_path / "jp.sqlite")


def test_generated_scope_calendar_and_financial_json(data, settings):
    validate_scope(data, settings)
    assert len(data.instruments) == 20
    for i in data.instruments:
        assert i.currency == "JPY" and i.market == "JP"
        assert TradingRules().lot(i) == 100
    assert session("JP", "2024-01-08") == {}  # Japanese public holiday
    for score in data.financials:
        assert FinancialScore.model_validate_json(score.model_dump_json()) == score
        assert score.provider == "gemini-mock"
        assert "quantity" not in score.model_dump()


@pytest.mark.parametrize("change", ["source", "currency", "identity", "calendar", "nav", "real"])
def test_unsafe_input_rejected(data, settings, change):
    s = data.instruments[0].symbol
    if change == "source":
        bars = (data.rows[s][0].model_copy(update={"source": "alpaca"}),) + data.rows[s][1:]
        altered = replace(data, rows={**data.rows, s: bars})
        with pytest.raises(ValueError):
            quality(altered, 60, data.closes[60] + timedelta(minutes=2), 60)
        return
    if change == "currency":
        altered = replace(
            data,
            instruments=(data.instruments[0].model_copy(update={"currency": "USD"}),)
            + data.instruments[1:],
        )
    elif change == "identity":
        altered = replace(data, financials=tuple(reversed(data.financials)))
    elif change == "calendar":
        altered = replace(data, opens=(data.opens[0] + timedelta(days=1),) + data.opens[1:])
    elif change == "nav":
        altered = replace(data, navs=data.navs[1:])
    else:
        altered = replace(data, data_kind="MARKET")
    with pytest.raises(ValueError):
        validate_scope(altered, settings)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "discontinuity", "future_financials"])
def test_quality_rejects_bad_or_unknown_security(data, kind):
    s = data.instruments[0].symbol
    rows = data.rows[s]
    if kind == "missing":
        altered = replace(data, rows={**data.rows, s: rows[1:]})
    elif kind == "duplicate":
        altered = replace(data, rows={**data.rows, s: (rows[0],) + rows})
    elif kind == "discontinuity":
        b = rows[60]
        crash = b.model_copy(
            update={"open": b.open / 2, "high": b.high / 2, "low": b.low / 2, "close": b.close / 2}
        )
        altered = replace(data, rows={**data.rows, s: rows[:60] + (crash,) + rows[61:]})
    else:
        future = data.financials[0].model_copy(update={"as_of": data.closes[-1]})
        altered = replace(data, financials=(future,) + data.financials[1:])
    q = quality(altered, 60, data.closes[60] + timedelta(minutes=2), 60)
    with pytest.raises(ValueError):
        q.require(s)
    assert q.population[1].strategy_eligibility == "ELIGIBLE"


def test_nav_waits_for_application_and_publication(data):
    nav = data.navs[61]
    with pytest.raises(ValueError):
        benchmark_value(nav, 1000000, Decimal(0), data.closes[60])
    assert benchmark_value(nav, 10000, Decimal(5), nav.published_at) == nav.per_10000_jpy + 5
    with pytest.raises(ValueError):
        SyntheticNAV(nav.nav_date, Decimal("NaN"), nav.published_at)


def test_future_financial_evidence_is_invalid(data):
    score = data.financials[0]
    with pytest.raises(ValidationError):
        FinancialScore.model_validate(
            score.model_dump() | {"retrieved_at": score.as_of + timedelta(days=1)}
        )
    with pytest.raises(ValidationError):
        FinancialScore.model_validate(score.model_dump() | {"quantity": 100})


def test_end_to_end_jpy_costs_next_session_and_no_network(data, settings, tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("external communication prohibited")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    repository = repo(tmp_path)
    result = replay(data, settings, repository)
    assert result["status"] == "COMPLETE"
    assert result["real_api_calls"] == 0 and result["data_kind"] == "SYNTHETIC"
    assert result["real_market_approved"] is False
    broker = PaperBroker(repository)
    orders = {o.order_id: o for o in broker.get_orders()}
    fills = broker.get_executions()
    assert fills and any(f.side == "SELL" for f in fills)
    for fill in fills:
        assert fill.quantity % 100 == 0 and fill.currency == "JPY"
        assert fill.price_at > orders[fill.order_id].requested_at
        after = [o for o in data.opens if o > orders[fill.order_id].requested_at]
        assert fill.price_at == min(after)
        assert fill.commission == 55
        assert fill.slippage_cost > 0
        assert (
            fill.price > fill.reference_price
            if fill.side == "BUY"
            else fill.price < fill.reference_price
        )
    assert broker.history()["fx_exchanges"] == []
    assert broker.get_balance().balances["USD"] == 0
    assert all(Decimal(d["cash_ratio"]) >= Decimal(".2") for d in result["daily"])
    assert result["nav_application_date"] > result["started_at"][:10]
    assert result["nav_units"] > 0
    assert len(result["daily"]) == settings.sessions - settings.timing.slow_sma
    assert (
        len({o.symbol for o in broker.get_orders() if o.status == "PENDING"})
        == result["pending_orders"]
    )
    # Idempotent execution does not create an additional fill.
    f = fills[0]
    before = len(fills)
    broker.execute(f.order_id, {}, None, data.closes[-1] + timedelta(minutes=2))
    assert len(broker.get_executions()) == before
    with pytest.raises(ValueError):
        replay(data, settings, repository)


def test_diagnostic_cannot_enter_replay(settings, tmp_path):
    with pytest.raises(ValueError):
        replay(ReadOnlyQualityReport('{"purpose":"diagnostic_only"}'), settings, repo(tmp_path))


def test_future_prices_do_not_change_prior_orders(data, settings, tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    a, b = repo(root_a), repo(root_b)
    replay(data, settings, a)
    rows = {}
    for symbol, bars in data.rows.items():
        updated = []
        for i, bar in enumerate(bars):
            updated.append(
                bar
                if i < 80
                else bar.model_copy(
                    update={k: getattr(bar, k) * 1.03 for k in ("open", "high", "low", "close")}
                )
            )
        rows[symbol] = tuple(updated)
    replay(replace(data, rows=rows), settings, b)

    def early(repository):
        return [
            (o.order_id, o.quantity, o.side)
            for o in PaperBroker(repository).get_orders()
            if o.requested_at < data.opens[80]
        ]

    assert early(a) == early(b)


@pytest.mark.parametrize("size", [200, 2000])
def test_generator_expands_without_real_api(size):
    config = JapanSettings(symbols=size, sessions=90)
    fixture = generate(config)
    assert len(fixture.rows) == size
    assert len(fixture.financials) == size
    assert all(b.source == "synthetic" for bars in fixture.rows.values() for b in bars)


def test_missing_held_price_stops_without_invented_valuation(data, settings, tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    database = repo(baseline)
    replay(data, settings, database)
    fills = PaperBroker(database).get_executions()
    entry = next(f for f in fills if f.side == "BUY")
    index = data.opens.index(entry.price_at) + 1
    rows = data.rows[entry.symbol]
    altered = replace(data, rows={**data.rows, entry.symbol: rows[:index] + rows[index + 1 :]})
    stopped = tmp_path / "stopped"
    stopped.mkdir()
    result = replay(altered, settings, repo(stopped))
    assert result["status"] == "SAFE_STOP"
    assert result["current_assets_jpy"] is None
    assert result["cumulative_return"] is None
    assert result["real_market_approved"] is False
    assert result["quality"]["states"]["QUARANTINED"] >= 1
