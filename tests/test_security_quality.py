"""SYNTHETIC fixtures: local isolation never grants market trading permission."""

from dataclasses import replace
from datetime import timedelta

import pytest

from trading_bot.alpaca import DailyBatch
from trading_bot.identity_catalog import load_catalog
from trading_bot.security_quality import (
    BatchQualityStop,
    QualityHistory,
    QualityUniverse,
    SecurityQualityStop,
    evaluate_quality,
)
from trading_bot.swing.data import synthetic
from trading_bot.synthetic_mvp import MVPSettings, synthetic_inputs


@pytest.fixture(scope="module")
def inputs():
    data = synthetic(20, 202)
    u, f, r = synthetic_inputs(data, MVPSettings(symbols=20, sessions=202, review_fixtures=False))
    return (
        data,
        QualityUniverse(u.assignments[:2]),
        {a.security_id: f[a.security_id] for a in u.assignments[:2]},
        {s: list(r[s][:60]) for s in list(r)[:2]},
    )


def assessment(inputs, fault=None, origin="synthetic"):
    data, u, base_facts, base_rows = inputs
    rows, facts = {s: list(r) for s, r in base_rows.items()}, dict(base_facts)
    symbol = next(iter(rows))
    sid = u.assignments[0].security_id
    days = tuple(t.date() for t in data.opens_at[:60])
    at = data.closes_at[59] + data.latency
    batch = DailyBatch(rows, days, complete_response=True)
    if fault == "missing":
        rows[symbol].pop()
    elif fault == "duplicate":
        rows[symbol].append(rows[symbol][-1])
    elif fault == "jump":
        rows[symbol][-1] = rows[symbol][-1].model_copy(
            update=dict(open=200.0, high=201.0, low=199.0, close=200.0)
        )
    elif fault == "invalid":
        rows[symbol][-1] = rows[symbol][-1].model_copy(update={"volume": -1})
    elif fault == "zero":
        rows[symbol][-1] = rows[symbol][-1].model_copy(update={"volume": 0})
    elif fault == "identity":
        facts[sid] = facts[sid].model_copy(
            update={"identity": facts[sid].identity.model_copy(update={"status": "UNKNOWN"})}
        )
    elif fault in ("corporate_unknown", "corporate_unsupported"):
        facts[sid] = facts[sid].model_copy(
            update={
                "corporate_actions": facts[sid].corporate_actions.model_copy(
                    update={"status": "UNKNOWN" if fault.endswith("unknown") else "UNSUPPORTED"}
                )
            }
        )
    elif fault == "future_evidence":
        facts[sid] = facts[sid].model_copy(
            update={"permission_observed_at": at + timedelta(seconds=1)}
        )
    elif fault == "short":
        batch.expected_sessions = days[:-1]
        batch.bars = {s: r[:-1] for s, r in rows.items()}
    elif fault == "paging":
        batch.complete_response = False
    elif fault == "unknown_ticker":
        batch.bars["UNKNOWN"] = rows[symbol]
    elif fault == "metadata":
        rows[symbol][0] = rows[symbol][0].model_copy(update={"symbol": "S0001"})
    elif fault == "origin":
        rows[symbol][0] = rows[symbol][0].model_copy(update={"source": "alpaca"})
    elif fault == "future":
        rows[symbol][-1] = rows[symbol][-1].model_copy(
            update={"fetched_at": at + timedelta(seconds=1)}
        )
    elif fault == "unconfirmed":
        rows[symbol][-1] = rows[symbol][-1].model_copy(update={"fetched_at": data.opens_at[59]})
    elif fault == "feed":
        batch.feed = "sip"
    elif fault == "unattributed_invalid":
        batch.invalid_rows = 1
    if origin == "market":
        batch.bars = {
            s: [b.model_copy(update={"source": "alpaca"}) for b in r] for s, r in batch.bars.items()
        }
        facts = {
            sid: f.model_copy(update={"permission": "PRIVATE_READ_ONLY"})
            for sid, f in facts.items()
        }
    return evaluate_quality(u, batch, facts, at, origin=origin)


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "jump", "invalid", "corporate_unsupported"]
)
def test_local_defects_do_not_reduce_population(inputs, fault):
    q = assessment(inputs, fault)
    assert len(q.population) == 2
    assert q.population[0].strategy_eligibility == "QUARANTINED"
    assert q.population[1].strategy_eligibility == "ELIGIBLE"
    with pytest.raises(SecurityQualityStop):
        q.require("S0000")
    q.require("S0001")


@pytest.mark.parametrize("fault", ["identity", "corporate_unknown", "future_evidence", "short"])
def test_clean_prices_are_not_evidence(inputs, fault):
    q = assessment(inputs, fault)
    assert q.population[0].strategy_eligibility == "UNKNOWN"
    with pytest.raises(SecurityQualityStop):
        q.require("S0000")


@pytest.mark.parametrize(
    "fault",
    [
        "paging",
        "unknown_ticker",
        "metadata",
        "origin",
        "future",
        "unconfirmed",
        "feed",
        "unattributed_invalid",
    ],
)
def test_batch_failures_not_local_exclusions(inputs, fault):
    with pytest.raises(BatchQualityStop):
        assessment(inputs, fault)


def test_market_permission_and_zero_volume(inputs):
    assert assessment(inputs, "zero").population[0].strategy_eligibility == "ELIGIBLE"
    q = assessment(inputs, origin="market")
    assert all(s.strategy_eligibility == "UNKNOWN" for s in q.population)
    with pytest.raises(SecurityQualityStop):
        q.require("S0000")
    r = q.diagnostic_report().summary()
    assert r["purpose"] == "diagnostic_only" and r["strategy_approved"] is False
    assert "S0000" not in str(r)


def test_history_no_retroactive_exclusion_or_auto_release(inputs):
    h = QualityHistory()
    clean = assessment(inputs)
    h.observe(clean)
    bad = assessment(inputs, "jump")
    later = bad.as_of + timedelta(seconds=1)
    h.observe(
        replace(
            bad,
            as_of=later,
            population=tuple(replace(s, assessed_at=later) for s in bad.population),
        )
    )
    assert h.snapshots[0].population[0].strategy_eligibility == "ELIGIBLE"
    later += timedelta(seconds=1)
    q = h.observe(
        replace(
            clean,
            as_of=later,
            population=tuple(replace(s, assessed_at=later) for s in clean.population),
        )
    )
    assert q.population[0].strategy_eligibility == "QUARANTINED"
    with pytest.raises(BatchQualityStop):
        h.observe(clean)
    later += timedelta(seconds=1)
    with pytest.raises(BatchQualityStop):
        h.observe(
            replace(
                clean, as_of=later, population=(replace(clean.population[0], assessed_at=later),)
            )
        )


def test_existing_100_security_101_alias_registry_reused():
    u = QualityUniverse.from_catalog(load_catalog())
    assert len(u.validate()) == 100 and len(u.assignments) == 101
    values = list(u.assignments)
    values[0] = values[0].model_copy(update={"ticker": values[1].ticker})
    with pytest.raises(BatchQualityStop):
        QualityUniverse(tuple(values)).validate()


def test_unconfirmed_listing_boundaries_do_not_become_confirmed_defects(inputs):
    data, u, facts, rows = inputs
    provisional = u.assignments[0].model_copy(
        update={"identity_confirmed": False, "valid_from": data.opens_at[100].date()}
    )
    universe = QualityUniverse((provisional, u.assignments[1]))
    batch = DailyBatch(rows, tuple(t.date() for t in data.opens_at[:60]), complete_response=True)
    q = evaluate_quality(
        universe, batch, facts, data.closes_at[59] + data.latency, origin="synthetic"
    )
    assert q.population[0].strategy_eligibility == "UNKNOWN"
    assert "out_of_period" not in q.population[0].reasons
