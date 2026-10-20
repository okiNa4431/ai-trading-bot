"""Synthetic discontinuities only; no real provider requests or evidence promotion."""

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import yaml

from tests.test_identity100 import NOW
from tests.test_quality_diagnostic import ENV
from tests.test_quality_diagnostic import catalog as catalog
from tests.test_quality_diagnostic import pages as pages
from trading_bot.alpaca import NY, DailyBatch
from trading_bot.models import PriceBar
from trading_bot.quality_diagnostic import (
    APPROVAL,
    CONTINUATION_APPROVAL,
    CONTINUATION_WORKFLOW,
    inspect_aliases,
    run,
)

ENV9 = ENV | {
    "GITHUB_WORKFLOW_REF": CONTINUATION_WORKFLOW,
    "ALPACA_DISCONTINUITIES_LIVE_ENABLED": "true",
}


@pytest.mark.parametrize(
    "symbol,day,factor,matched",
    [
        ("HON", "2026-06-29", 2, 1),
        ("HON", "2026-06-29", 0.5, 1),
        ("NOW", "2025-12-18", 0.2, 1),
        ("AAPL", "2026-06-29", 0.5, 0),
        ("HON", "2026-06-29", 1.1, 0),
    ],
)
def test_split_spinoff_and_unknown_are_only_candidates(catalog, symbol, day, factor, matched):
    after = date.fromisoformat(day)
    before = date(2025, 12, 17) if symbol == "NOW" else date(2026, 6, 26)
    bars = [
        PriceBar(
            symbol=symbol,
            market="US",
            currency="USD",
            interval="1d",
            source="synthetic",
            timestamp=datetime.combine(d, datetime.min.time(), NY),
            fetched_at=NOW,
            open=v,
            high=v,
            low=v,
            close=v,
            volume=0,
        )
        for d, v in ((before, 100), (after, 100 * factor))
    ]
    batch = DailyBatch({symbol: bars}, (before, after), complete_response=True)
    stats = {}
    inspect_aliases(
        batch, {symbol: {before, after}}, stats, catalog=catalog, continue_discontinuities=True
    )
    assert stats["discontinuities"] == int(factor != 1.1)
    assert stats["published_event_boundary_matches"] == matched
    assert stats["unexplained_discontinuities"] == int(factor != 1.1) - matched


@pytest.mark.parametrize("fault", ["none", "jump", "revision", "missing", "duplicate", "invalid"])
def test_repeat_diagnosis_retains_violations(catalog, pages, fault, caplog):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        body = pages(request)
        for ticker, rows in body["bars"].items():
            for row in rows:
                day = date.fromisoformat(row["t"][:10])
                if fault != "none" and ticker == "HON" and day == date(2026, 6, 29):
                    row.update(o=200, h=202, l=199, c=200)
            if fault == "revision" and calls >= 5 and ticker == "HON":
                rows[0]["c"] = 101
        rows = next(iter(body["bars"].values()))
        if calls == 1:
            if fault == "missing":
                rows.pop()
            if fault == "duplicate":
                rows.append(dict(rows[0]))
            if fault == "invalid":
                rows[0]["v"] = -1
        return httpx.Response(200, json=body)

    code, report = run(
        CONTINUATION_APPROVAL,
        ENV9,
        catalog=catalog,
        mock=httpx.MockTransport(handler),
        now=lambda: NOW,
        continue_discontinuities=True,
    )
    s = report.summary()
    assert not s["strategy_approved"]
    if fault in ("none", "jump", "revision"):
        assert calls == 8 and s["checks"]["paging"] == ["PASSED", "PASSED"]
        assert s["checks"]["repeat_consistency"] == ("FAILED" if fault == "revision" else "PASSED")
    else:
        assert calls <= 4 and s["checks"]["paging"][1] == "NOT_RUN"
    if fault in ("jump", "revision"):
        assert s["checks"]["alias_quality"] == ["FAILED", "FAILED"]
        assert s["discontinuities"] == 2 and s["discontinuity_symbol_count"] == 1
        assert s["upward_discontinuity_count"] == s["downward_discontinuity_count"] == 1
        assert s["additional_same_symbol_discontinuities"] == 1
        assert s["published_event_boundary_matches"] == 1  # two public events, one observation
        assert s["known_event_window_matches"] == 2
        assert s["unexplained_discontinuities"] == 1
        assert not s["diagnostic_complete"] and s["status"] == "STOPPED"
    assert code == (0 if fault == "none" else 1)
    output = report.aggregate_json + caplog.text
    for forbidden in ('"HON"', '"close"', '"o"', '"t"', "never-log", "2026-06-29"):
        assert forbidden not in output


@pytest.mark.parametrize("fault", ["approval", "workflow", "new_gate", "old_gate"])
def test_continuation_requires_its_own_approval(catalog, fault):
    env, approval = dict(ENV9), CONTINUATION_APPROVAL
    if fault == "approval":
        approval = APPROVAL
    elif fault == "workflow":
        env["GITHUB_WORKFLOW_REF"] = ENV["GITHUB_WORKFLOW_REF"]
    else:
        env.pop(
            "ALPACA_DISCONTINUITIES_LIVE_ENABLED"
            if fault == "new_gate"
            else "ALPACA_100_DIAGNOSTIC_LIVE_ENABLED"
        )
    code, report = run(
        approval,
        env,
        catalog=catalog,
        now=lambda: NOW,
        mock=httpx.MockTransport(lambda _: pytest.fail("HTTP reached")),
        continue_discontinuities=True,
    )
    assert code == 1 and report.summary()["requests"] == 0


def test_new_workflow_is_independent_and_ephemeral():
    raw = Path(".github/workflows/alpaca-discontinuities-100.yml").read_text()
    workflow = yaml.safe_load(raw)
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_dispatch"}
    assert workflow["concurrency"]["group"] == "alpaca-readonly-main"
    job = workflow["jobs"]["validate"]
    assert job["environment"] == "alpaca-research" and job["runs-on"] == "ubuntu-latest"
    assert "refs/heads/main" in job["if"]
    assert CONTINUATION_APPROVAL in raw and "enable-cache: false" in raw
    assert "ALPACA_DISCONTINUITIES_LIVE_ENABLED" in raw
    assert all("secrets." not in json.dumps(s) for s in job["steps"][:-1])
    assert not any(word in raw for word in ("upload-artifact", "SUPABASE", "GEMINI"))
