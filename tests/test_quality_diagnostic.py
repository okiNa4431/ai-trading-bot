"""UNKNOWN-preserving, synthetic-only read diagnostics and downstream denial."""

import hashlib
import json
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml

from tests.test_identity100 import ENV as STRICT_ENV
from tests.test_identity100 import NOW
from tests.test_identity100 import pages as pages
from trading_bot.broker.paper import PaperBroker
from trading_bot.corporate_review import CorporateActionCandidate
from trading_bot.data_access import ReadOnlyQualityReport
from trading_bot.identity100_probe import run as strict_run
from trading_bot.identity_catalog import IdentityCatalog, load_catalog
from trading_bot.persistence.research import PrivateResearchStore
from trading_bot.quality_diagnostic import APPROVAL, WORKFLOW, run
from trading_bot.strategy.gemini import GeminiTradingStrategy
from trading_bot.swing.observed import ObservedDailyData

ENV = STRICT_ENV | {"GITHUB_WORKFLOW_REF": WORKFLOW, "ALPACA_100_DIAGNOSTIC_LIVE_ENABLED": "true"}


@pytest.fixture
def catalog():
    return load_catalog()  # Actual UNKNOWN metadata; all OHLCV responses remain synthetic.


def test_unknowns_allowed_only_in_diagnostic(catalog, pages):
    before = catalog.model_dump_json()
    manifest = Path("config/security-identities-100.yaml")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    code, report = run(
        APPROVAL,
        ENV,
        catalog=catalog,
        mock=httpx.MockTransport(lambda r: httpx.Response(200, json=pages(r))),
        now=lambda: NOW,
    )
    summary = report.summary()
    assert isinstance(report, ReadOnlyQualityReport) and code == 0
    assert summary["status"] == "DIAGNOSTIC_PASSED" and summary["diagnostic_complete"]
    assert summary["received_rows_per_pass"] == summary["expected_alias_rows"] == 22100
    assert summary["requests"] == summary["pages"] == 8
    assert summary["identity_confirmed"] == 1 and summary["corporate_actions_confirmed"] == 0
    assert summary["known_non_ticker_events"] == 2 and not summary["strategy_approved"]
    assert summary["checks"]["security_completeness"] == "NOT_RUN"
    assert summary["checks"]["strategy_approval"] == "NOT_RUN"
    assert not hasattr(report, "bars") and not hasattr(report, "model_dump")
    assert '"close"' not in report.aggregate_json and '"MMC"' not in report.aggregate_json
    assert catalog.model_dump_json() == before
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == digest
    code, strict = strict_run(
        "READ_ONLY_100_IDENTITIES_NO_STORAGE",
        STRICT_ENV,
        catalog=catalog,
        mock=httpx.MockTransport(lambda _: pytest.fail("strict HTTP reached")),
        now=lambda: NOW,
    )
    assert code == 1 and strict["requests"] == 0


@pytest.mark.parametrize(
    "kind", ["missing", "duplicate", "invalid", "outside", "unknown", "revision", "jump", "token"]
)
def test_diagnostic_quality_is_not_relaxed(catalog, pages, kind, caplog):
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        data = pages(r)
        rows = next(iter(data["bars"].values()))
        if kind == "missing" and calls == 1:
            rows.pop()
        elif kind == "duplicate":
            rows.append(dict(rows[0]))
        elif kind == "invalid":
            rows[0]["v"] = -1
        elif kind == "outside" and "MRSH" in data["bars"]:
            data["bars"]["MRSH"][0]["t"] = "2025-12-02T00:00:00-05:00"
        elif kind == "unknown":
            data["bars"]["UNREVIEWED"] = rows
        elif kind == "revision" and calls == 5:
            rows[0]["c"] = 101
        elif kind == "jump":
            rows[0].update(o=200, h=202, l=199, c=200)
        elif kind == "token":
            data["next_page_token"] = "10000"
        return httpx.Response(200, json=data)

    code, report = run(
        APPROVAL, ENV, catalog=catalog, mock=httpx.MockTransport(handler), now=lambda: NOW
    )
    assert code == 1 and not report.summary()["diagnostic_complete"]
    assert not report.summary()["strategy_approved"]
    assert "never-log" not in report.aggregate_json + caplog.text
    assert '"AAPL"' not in report.aggregate_json and '"close"' not in report.aggregate_json


@pytest.mark.parametrize("kind", ["unconfirmed_pair", "overlap", "gap", "unknown_boundary"])
def test_ambiguous_scope_stops_before_http(catalog, kind, monkeypatch):
    raw = catalog.model_dump()
    record = next(r for r in raw["reviews"] if r["security_id"] == "sec-000001")
    old = next(a for a in raw["assignments"] if a["ticker"] == "MMC")
    if kind == "unconfirmed_pair":
        record["identity"]["status"] = "UNKNOWN"
    elif kind in ("overlap", "gap"):
        old["valid_to"] = date(2026, 1, 15 if kind == "overlap" else 13)
    else:
        raw["assignments"][0]["valid_to"] = date(2026, 1, 14)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    code, report = run(APPROVAL, ENV, catalog=IdentityCatalog.model_validate(raw), now=lambda: NOW)
    assert code == 1 and report.summary()["requests"] == 0


@pytest.mark.parametrize("kind", ["approval", "gate", "old_gate", "workflow", "secret"])
def test_independent_gate_before_http(catalog, kind, monkeypatch):
    env, approval = dict(ENV), APPROVAL
    if kind == "approval":
        approval = "READ_ONLY_100_IDENTITIES_NO_STORAGE"
    elif kind == "gate":
        env.pop("ALPACA_100_DIAGNOSTIC_LIVE_ENABLED")
    elif kind == "old_gate":
        env["ALPACA_MARKET_DATA_LIVE_ENABLED"] = "false"
    elif kind == "workflow":
        env["GITHUB_WORKFLOW_REF"] = STRICT_ENV["GITHUB_WORKFLOW_REF"]
    else:
        env.pop("ALPACA_API_SECRET_KEY")
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    code, report = run(approval, env, catalog=catalog, now=lambda: NOW)
    assert code == 1 and report.summary()["requests"] == 0


@pytest.mark.parametrize("status", [401, 403, 429, 503, 0])
def test_error_body_never_escapes(catalog, status):
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        if not status:
            raise httpx.ReadTimeout("never-log-secret RAW BODY")
        return httpx.Response(
            status, text="never-log-secret RAW BODY", headers={"Retry-After": "0"}
        )

    code, report = run(
        APPROVAL, ENV, catalog=catalog, mock=httpx.MockTransport(handler), now=lambda: NOW
    )
    assert code == 1 and calls == (1 if status in (401, 403) else 3)
    assert "never-log" not in report.aggregate_json and "RAW BODY" not in report.aggregate_json


@pytest.mark.parametrize("serialized", [False, True])
def test_report_rejected_before_ai_orders_execution_or_storage(serialized):
    report = ReadOnlyQualityReport(
        json.dumps(dict(purpose="diagnostic_only", strategy_approved=False))
    )
    if serialized:
        report = report.summary()
    repo, factory = Mock(), Mock()
    strategy = GeminiTradingStrategy(Mock(), repo, "denied", offline_sdk_factory=factory)
    with pytest.raises(ValueError, match="diagnostic-only"):
        strategy.decide(report)
    factory.assert_not_called()
    repo.reserve.assert_not_called()
    broker = PaperBroker(repo)
    with pytest.raises(ValueError, match="diagnostic-only"):
        broker.execute("id", {"AAPL": [report]}, None, NOW)
    with pytest.raises(ValueError, match="diagnostic-only"):
        broker.place_order(report, "BUY", 1, NOW)
    with pytest.raises(ValueError, match="diagnostic-only"):
        broker.value(report, None, NOW)
    repo.transaction.assert_not_called()
    with pytest.raises(ValueError, match="diagnostic-only"):
        ObservedDailyData([], report, [], [], None)
    store = Mock()
    with pytest.raises(ValueError, match="diagnostic-only"):
        PrivateResearchStore.commit(store, "id", 0, None, [report], {}, {})
    with pytest.raises(ValueError, match="diagnostic-only"):
        PrivateResearchStore.commit(store, "id", 0, None, [], {}, report)
    store.check.assert_not_called()
    store.api.rpc.assert_not_called()


@pytest.mark.parametrize("kind", ["split", "dividend", "spinoff", "ticker_change", "unknown"])
def test_event_candidates_cannot_promote_themselves(kind):
    values = dict(
        kind=kind,
        provider="synthetic",
        evidence_reference="synthetic://observation",
        ticker_hint="AAPL",
        effective_from=date(2026, 1, 14),
        observed_at=NOW,
    )
    candidate = CorporateActionCandidate(**values)
    assert candidate.review_status == "UNKNOWN"
    with pytest.raises(ValueError):
        CorporateActionCandidate(**values, review_status="CONFIRMED")


def test_manual_workflow_keeps_secrets_scoped_and_exports_nothing():
    raw = Path(".github/workflows/alpaca-quality-diagnostic-100.yml").read_text()
    w = yaml.safe_load(raw)
    assert set(w.get("on", w.get(True))) == {"workflow_dispatch"}
    job = w["jobs"]["validate"]
    assert job["environment"] == "alpaca-research" and "refs/heads/main" in job["if"]
    assert (
        job["steps"][-1]["env"]["ALPACA_100_DIAGNOSTIC_LIVE_ENABLED"]
        == "${{ vars.ALPACA_100_DIAGNOSTIC_LIVE_ENABLED }}"
    )
    assert APPROVAL in raw and "enable-cache: false" in raw and "upload-artifact" not in raw
    assert all("secrets." not in str(s) for s in job["steps"][:-1])
    assert "SUPABASE" not in raw and "GEMINI" not in raw
