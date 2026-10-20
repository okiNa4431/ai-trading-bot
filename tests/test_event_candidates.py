"""Public-source metadata and SYNTHETIC raw boundary matching, never live requests."""

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from tests.test_discontinuity_diagnostic import ENV9
from tests.test_identity100 import NOW
from tests.test_quality_diagnostic import catalog as catalog
from tests.test_quality_diagnostic import pages as pages
from trading_bot.corporate_review import (
    CorporateActionCandidate,
    DiscontinuityState,
    PublicActionCatalog,
    classify_boundary,
    count_boundary,
    load_public_actions,
    new_event_counts,
)
from trading_bot.quality_diagnostic import EVENT_APPROVAL, EVENT_WORKFLOW, run

ENV10 = ENV9 | {
    "GITHUB_WORKFLOW_REF": EVENT_WORKFLOW,
    "ALPACA_EVENT_CANDIDATES_LIVE_ENABLED": "true",
}
DAY = date(2026, 6, 29)
BEFORE = date(2026, 6, 26)


def candidate(kind="split", **changes):
    values = dict(
        event_id="synthetic-event",
        kind=kind,
        ticker_hint="HON",
        issuer="SYNTHETIC issuer",
        share_class="synthetic common",
        provider="synthetic",
        revision=1,
        evidence_reference="https://example.invalid/synthetic-only",
        published_on=BEFORE,
        effective_from=DAY,
        official_terms="SYNTHETIC ONLY",
        ratio_new=5,
        ratio_old=1,
        evidence_status="PUBLIC_SOURCE_REVIEWED",
        observed_at=NOW,
    )
    return CorporateActionCandidate.model_validate(values | changes)


def public(*events):
    return PublicActionCatalog(scope_note="SYNTHETIC test catalog", events=events)


@pytest.mark.parametrize(
    "kind", ["split", "reverse_split", "spinoff", "special_dividend", "dividend", "ticker_change"]
)
def test_date_match_never_verifies(kind):
    event = candidate(kind)
    state, kinds = classify_boundary((event,), "HON", BEFORE, DAY)
    assert state == DiscontinuityState.EVENT_CANDIDATE and kinds == {kind}
    counts = new_event_counts()
    count_boundary((event,), "HON", BEFORE, DAY, counts)
    assert counts["unresolved_event_count"] == 1 and counts["verified_event_count"] == 0
    if kind == "ticker_change":
        assert counts["matched_split_candidates"] == 0
        assert counts["matched_ticker_change_candidates"] == 1


@pytest.mark.parametrize("missing", ["official_terms", "ratio_new", "share_class", "source"])
def test_incomplete_information_is_ambiguous(missing):
    changes = {missing: None}
    if missing == "ratio_new":
        changes["ratio_old"] = None
    if missing == "source":
        changes = dict(evidence_status="UNKNOWN")
    assert classify_boundary((candidate(**changes),), "HON", BEFORE, DAY)[0] == "AMBIGUOUS"


def test_two_events_not_two_resolved_observations():
    events = (candidate(), candidate("spinoff", event_id="synthetic-spinoff"))
    counts = new_event_counts()
    count_boundary(events, "HON", BEFORE, DAY, counts)
    assert counts["multiple_candidate_matches"] == counts["ambiguous_count"] == 1
    assert counts["matched_split_candidates"] == counts["matched_spinoff_candidates"] == 1
    assert counts["unresolved_event_count"] == 1 and counts["verified_event_count"] == 0


def test_unknown_and_market_move_cannot_be_inferred_from_price():
    # A real market drop and an unlisted action have identical boundary evidence.
    assert classify_boundary((), "HON", BEFORE, DAY)[0] == "UNKNOWN"
    assert classify_boundary((candidate(ticker_hint="NOW"),), "HON", BEFORE, DAY)[0] == "UNKNOWN"
    assert classify_boundary((candidate(effective_from=None),), "HON", BEFORE, DAY)[0] == "UNKNOWN"


def test_revision_preserves_historical_knowledge():
    first = candidate(effective_from=date(2026, 6, 30), observed_at=NOW - timedelta(days=2))
    second = candidate(revision=2, supersedes_revision=1, observed_at=NOW)
    catalog = public(first, second)
    assert catalog.active(NOW - timedelta(days=1)) == (first,)
    assert catalog.active(NOW) == (second,)
    assert classify_boundary(catalog.active(NOW), "HON", BEFORE, DAY)[0] == "EVENT_CANDIDATE"
    with pytest.raises(ValueError):
        public(second)
    with pytest.raises(ValueError):
        public(first, first)
    with pytest.raises(ValueError):
        public(first, candidate(revision=2, supersedes_revision=1, issuer="wrong issuer"))


def test_official_catalog_is_partial_and_not_provider_verified():
    catalog = load_public_actions()
    assert len(catalog.events) == 7
    assert all(e.review_status == e.provider_application == "UNKNOWN" for e in catalog.events)
    assert any(e.effective_from is None for e in catalog.events)
    with pytest.raises(ValueError):
        candidate(provider_application="CONFIRMED")
    with pytest.raises(ValueError):
        candidate(review_status="CONFIRMED")


@pytest.mark.parametrize("revision", [False, True])
def test_event_matching_repeat_aggregates_only(catalog, pages, revision, caplog):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        body = pages(request)
        for row in body["bars"].get("HON", []):
            if row["t"][:10] >= "2026-06-29":
                row.update(o=50, h=51, l=49, c=50)
        if revision and calls >= 5 and "HON" in body["bars"]:
            body["bars"]["HON"][0]["c"] = 101
        return httpx.Response(200, json=body)

    code, report = run(
        EVENT_APPROVAL,
        ENV10,
        catalog=catalog,
        public_catalog=public(candidate()),
        event_matching=True,
        mock=httpx.MockTransport(handler),
        now=lambda: NOW,
    )
    s = report.summary()
    assert calls == 8 and code == 1 and s["status"] == "STOPPED"
    assert s["purpose"] == "diagnostic_only" and s["strategy_approved"] is False
    assert s["checks"]["repeat_consistency"] == ("FAILED" if revision else "PASSED")
    assert s["matched_split_candidates"] == s["event_candidate_count"] == 1
    assert s["unresolved_event_count"] == 1 and s["verified_event_count"] == 0
    assert not s["automatic_verification_enabled"] and not s["retained_market_data"]
    output = report.aggregate_json + caplog.text
    for forbidden in ('"HON"', '"close"', "2026-06-29", "never-log", "SYNTHETIC issuer"):
        assert forbidden not in output


@pytest.mark.parametrize("gate", ["approval", "workflow", "new", "old", "secret"])
def test_event_gate_before_http(catalog, gate):
    env, approval = dict(ENV10), EVENT_APPROVAL
    if gate == "approval":
        approval = "READ_ONLY_100_DISCONTINUITIES_NO_STORAGE"
    elif gate == "workflow":
        env["GITHUB_WORKFLOW_REF"] = ENV9["GITHUB_WORKFLOW_REF"]
    else:
        env.pop(
            {
                "new": "ALPACA_EVENT_CANDIDATES_LIVE_ENABLED",
                "old": "ALPACA_DISCONTINUITIES_LIVE_ENABLED",
                "secret": "ALPACA_API_SECRET_KEY",
            }[gate]
        )
    code, report = run(
        approval,
        env,
        catalog=catalog,
        event_matching=True,
        mock=httpx.MockTransport(lambda _: pytest.fail("HTTP reached")),
        now=lambda: NOW,
    )
    assert code == 1 and report.summary()["requests"] == 0


def test_workflow_has_no_export_and_scoped_secrets():
    raw = Path(".github/workflows/alpaca-events-100.yml").read_text()
    w = yaml.safe_load(raw)
    assert set(w.get("on", w.get(True))) == {"workflow_dispatch"}
    assert w["concurrency"]["group"] == "alpaca-readonly-main"
    j = w["jobs"]["validate"]
    assert "refs/heads/main" in j["if"] and j["environment"] == "alpaca-research"
    assert j["runs-on"] == "ubuntu-latest" and j["timeout-minutes"] == 10
    assert "enable-cache: false" in raw and EVENT_APPROVAL in raw
    assert not any(s in raw for s in ("upload-artifact", "SUPABASE", "GEMINI", "schedule:"))
    assert all("secrets." not in json.dumps(step) for step in j["steps"][:-1])


@pytest.mark.parametrize("fault", ["missing", "duplicate", "invalid"])
def test_bad_raw_data_never_reaches_event_classification(catalog, pages, fault):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        body = pages(request)
        if calls == 1:
            rows = next(iter(body["bars"].values()))
            if fault == "missing":
                rows.pop()
            elif fault == "duplicate":
                rows.append(dict(rows[0]))
            else:
                rows[0]["v"] = -1
        return httpx.Response(200, json=body)

    code, report = run(
        EVENT_APPROVAL,
        ENV10,
        catalog=catalog,
        public_catalog=public(candidate()),
        event_matching=True,
        mock=httpx.MockTransport(handler),
        now=lambda: NOW,
    )
    s = report.summary()
    assert code == 1 and calls <= 4
    assert s["checks"]["event_classification"] == ["NOT_RUN", "NOT_RUN"]
    assert s["checks"]["repeat_consistency"] == "NOT_RUN"
    assert "matched_split_candidates" not in s
