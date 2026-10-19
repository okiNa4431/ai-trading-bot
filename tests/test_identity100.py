"""All approved catalogs below are SYNTHETIC fixtures, never evidence upgrades."""

import json
from datetime import UTC, date, datetime, time
from pathlib import Path

import httpx
import pytest
import yaml

from trading_bot.alpaca import NY
from trading_bot.identity100_probe import (
    APPROVAL,
    WORKFLOW,
    IdentityBatchTransport,
    run,
    session_window,
)
from trading_bot.identity_catalog import EvidenceReview, IdentityCatalog, load_catalog

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
ENV = dict(
    GITHUB_ACTIONS="true",
    GITHUB_EVENT_NAME="workflow_dispatch",
    GITHUB_REF="refs/heads/main",
    GITHUB_WORKFLOW_REF=WORKFLOW,
    GITHUB_REPOSITORY="okiNa4431/ai-trading-bot",
    RUNNER_ENVIRONMENT="github-hosted",
    ALPACA_RESEARCH_ENVIRONMENT="alpaca-research",
    ALPACA_MARKET_DATA_LIVE_ENABLED="true",
    ALPACA_100_IDENTITIES_LIVE_ENABLED="true",
    ALPACA_API_KEY_ID="never-log-key",
    ALPACA_API_SECRET_KEY="never-log-secret",
)


@pytest.fixture
def catalog():
    raw = load_catalog().model_dump()
    for record in raw["reviews"]:
        for name in ("identity", "ticker_periods", "corporate_actions"):
            record[name] = dict(
                status="CONFIRMED",
                sources=("https://example.invalid/synthetic-only",),
                checked_on=date(2026, 10, 20),
                coverage_start=date(2025, 12, 1),
                coverage_end=date(2026, 10, 20),
                note="SYNTHETIC fixture, NOT real confirmation",
            )
    for assignment in raw["assignments"]:
        assignment.update(identity_confirmed=True, exchange="NYSE", evidence="synthetic-only")
    raw["events"] = tuple(e for e in raw["events"] if e["kind"] == "ticker_change")
    return IdentityCatalog.model_validate(raw)


@pytest.fixture
def pages(catalog):
    days = session_window(NOW)
    flat = []
    for a in sorted(catalog.assignments, key=lambda a: a.ticker):
        for day in days:
            if a.valid_from <= day and (a.valid_to is None or day < a.valid_to):
                flat.append(
                    (
                        a.ticker,
                        dict(
                            t=datetime.combine(day, time(), NY).isoformat(),
                            o=100,
                            h=102,
                            l=99,
                            c=100,
                            v=0,
                        ),
                    )
                )

    def response(request, size=10000):
        symbols = request.url.params["symbols"].split(",")
        rows = [(s, dict(row)) for s, row in flat if s in symbols]
        start = int(request.url.params.get("page_token", "0"))
        bars = {}
        for s, row in rows[start : start + size]:
            bars.setdefault(s, []).append(row)
        return dict(
            bars=bars, next_page_token=str(start + size) if start + size < len(rows) else None
        )

    return response


def test_shipping_registry_is_honest_and_stops_before_http(monkeypatch):
    catalog = load_catalog()
    assert len(catalog.reviews) == len(set(r.security_id for r in catalog.reviews)) == 100
    assert len(catalog.assignments) == 101
    assert {a.security_id for a in catalog.assignments if a.ticker in ("MMC", "MRSH")} == {
        "sec-000001"
    }
    assert catalog.summary(set(session_window(NOW)))["identity_confirmed"] == 1
    assert catalog.summary(set(session_window(NOW)))["corporate_actions_confirmed"] == 0
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    code, result = run(APPROVAL, ENV, now=lambda: NOW)
    assert code == 1 and result["requests"] == 0 and result["checks"]["evidence"] == "FAILED"
    assert result["checks"]["paging"] == ["NOT_RUN", "NOT_RUN"]
    assert result["repeat_revisions"] is None


@pytest.mark.parametrize("change", ["duplicate_id", "unknown_id", "missing_alias"])
def test_invalid_catalog(catalog, change):
    raw = catalog.model_dump()
    if change == "duplicate_id":
        raw["reviews"][1]["security_id"] = raw["reviews"][0]["security_id"]
    elif change == "unknown_id":
        raw["assignments"][0]["security_id"] = "unknown"
    else:
        raw["assignments"] = raw["assignments"][:-1]
    with pytest.raises(ValueError):
        IdentityCatalog.model_validate(raw)


@pytest.mark.parametrize("kind", ["identity", "ticker_periods", "corporate_actions", "expired"])
def test_unreviewed_or_stale_evidence_blocks(catalog, kind, monkeypatch):
    raw = catalog.model_dump()
    if kind == "expired":
        raw["reviews"][0]["identity"]["coverage_end"] = date(2026, 1, 1)
    else:
        raw["reviews"][0][kind]["status"] = "UNKNOWN"
    c = IdentityCatalog.model_validate(raw)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    code, result = run(APPROVAL, ENV, catalog=c, now=lambda: NOW)
    assert code == 1 and result["requests"] == 0


def test_confirmation_requires_source_date_and_scope():
    with pytest.raises(ValueError):
        EvidenceReview(status="CONFIRMED", note="not evidence")
    with pytest.raises(ValueError):
        EvidenceReview(
            status="CONFIRMED",
            note="future coverage is not reviewed",
            sources=("https://example.invalid/synthetic",),
            checked_on=date(2026, 10, 20),
            coverage_start=date(2026, 10, 1),
            coverage_end=date(2026, 10, 21),
        )


@pytest.mark.parametrize("kind", ["overlap", "gap", "wrong_identity", "unknown_exchange"])
def test_registry_structure_blocks_before_http(catalog, kind, monkeypatch):
    raw = catalog.model_dump()
    old = next(a for a in raw["assignments"] if a["ticker"] == "MMC")
    new = next(a for a in raw["assignments"] if a["ticker"] == "MRSH")
    if kind in ("overlap", "gap"):
        old["valid_to"] = date(2026, 1, 15 if kind == "overlap" else 13)
    elif kind == "wrong_identity":
        new["security_id"] = raw["reviews"][0]["security_id"]
    else:
        old["exchange"] = None
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    code, result = run(APPROVAL, ENV, catalog=IdentityCatalog.model_validate(raw), now=lambda: NOW)
    assert code == 1 and result["requests"] == 0


def test_known_unsupported_corporate_actions_stay_blocked(catalog, monkeypatch):
    approved = catalog.model_copy(update={"events": load_catalog().events})
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    code, result = run(APPROVAL, ENV, catalog=approved, now=lambda: NOW)
    assert code == 1 and result["requests"] == 0
    assert result["checks"]["evidence"] == "FAILED"


def test_normal_multiple_aliases_two_passes(catalog, pages):
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(200, json=pages(r))

    code, result = run(
        APPROVAL, ENV, catalog=catalog, mock=httpx.MockTransport(handler), now=lambda: NOW
    )
    assert code == 0 and result["complete"] and result["received_rows_per_pass"] == 22100
    assert result["security_count"] == 100 and result["alias_count"] == 101
    assert result["pages"] == result["requests"] == 8
    assert result["missing_rows"] == result["duplicate_rows"] == result["repeat_revisions"] == 0
    assert {len(r.url.params["symbols"].split(",")) for r in calls} == {100, 1}
    assert result["checks"]["completeness"] == ["PASSED", "PASSED"]


@pytest.mark.parametrize(
    "kind", ["missing", "duplicate", "ohlcv", "unknown", "outside", "revision", "jump", "token"]
)
def test_data_quality_stops_without_leaking(catalog, pages, kind, caplog):
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        data = pages(r)
        rows = next(iter(data["bars"].values()))
        if kind == "missing" and calls == 1:
            rows.pop()
        elif kind == "duplicate" and calls == 1:
            rows.append(dict(rows[0]))
        elif kind == "ohlcv":
            rows[0]["v"] = -1
        elif kind == "unknown":
            data["bars"]["UNREVIEWED"] = rows
        elif kind == "outside" and "MRSH" in data["bars"]:
            data["bars"]["MRSH"][0]["t"] = datetime(2025, 12, 2, 0, tzinfo=NY).isoformat()
        elif kind == "revision" and calls == 5:
            rows[0]["c"] = 101
        elif kind == "jump":
            rows[0].update(o=200, h=202, l=199, c=200)
        elif kind == "token":
            data["next_page_token"] = "10000"
        return httpx.Response(200, json=data)

    code, result = run(
        APPROVAL, ENV, catalog=catalog, mock=httpx.MockTransport(handler), now=lambda: NOW
    )
    assert code == 1 and result["status"] == "STOPPED" and not result["complete"]
    text = json.dumps(result) + caplog.text
    assert "never-log" not in text and '"close"' not in text and '"AAPL"' not in text
    if kind == "revision":
        assert result["repeat_revisions"] == 1
    if kind == "missing":
        assert result["missing_rows"] == 1
        assert result["received_rows_per_pass"] == 22099


@pytest.mark.parametrize("status", [401, 403, 429, 503, 0])
def test_bounded_errors(catalog, status):
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        if status == 0:
            raise httpx.ReadTimeout("never-log-secret RAW BODY")
        return httpx.Response(
            status, text="never-log-secret RAW BODY", headers={"Retry-After": "0"}
        )

    code, result = run(
        APPROVAL, ENV, catalog=catalog, mock=httpx.MockTransport(handler), now=lambda: NOW
    )
    assert code == 1 and calls == (1 if status in (401, 403) else 3)
    assert "never-log" not in json.dumps(result) and "RAW BODY" not in json.dumps(result)


@pytest.mark.parametrize(
    "key,value",
    [
        ("ALPACA_100_IDENTITIES_LIVE_ENABLED", "false"),
        ("GITHUB_WORKFLOW_REF", "other"),
        ("ALPACA_MARKET_DATA_LIVE_ENABLED", ""),
        ("GITHUB_REF", "refs/heads/other"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
    ],
)
def test_gates_before_http(catalog, key, value, monkeypatch):
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    code, result = run(APPROVAL, ENV | {key: value}, catalog=catalog, now=lambda: NOW)
    assert code == 1 and result["requests"] == 0
    assert result["checks"]["evidence"] == "NOT_RUN"


def test_wrong_approval_and_missing_secret(catalog, monkeypatch):
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    for approval, env in [
        ("READ_ONLY_100_NO_STORAGE", ENV),
        (APPROVAL, ENV | {"ALPACA_API_KEY_ID": ""}),
    ]:
        code, result = run(approval, env, catalog=catalog, now=lambda: NOW)
        assert code == 1 and result["requests"] == 0


def test_time_and_total_budget(catalog):
    aliases = tuple(a.ticker for a in catalog.assignments)
    clock = [0.0]
    guard = IdentityBatchTransport(
        httpx.MockTransport(lambda _: httpx.Response(429)), aliases, clock=lambda: clock[0]
    )
    request = httpx.Request(
        "GET",
        "https://data.alpaca.markets/v2/stocks/bars",
        params=dict(
            symbols=",".join(aliases[:100]),
            feed="iex",
            adjustment="raw",
            timeframe="1Day",
            currency="USD",
            sort="asc",
            asof="-",
            limit="10000",
        ),
    )
    for _ in range(24):
        assert guard.handle_request(request).status_code == 429
    with pytest.raises(ValueError, match="budget"):
        guard.handle_request(request)
    clock[0] = 300
    with pytest.raises(ValueError, match="time budget"):
        guard.handle_request(request)


def test_new_workflow_is_manual_and_private():
    raw = Path(".github/workflows/alpaca-identities-100.yml").read_text()
    w = yaml.safe_load(raw)
    assert set(w.get("on", w.get(True))) == {"workflow_dispatch"}
    j = w["jobs"]["validate"]
    assert j["environment"] == "alpaca-research" and "refs/heads/main" in j["if"]
    assert APPROVAL in raw and "upload-artifact" not in raw and "enable-cache: false" in raw
    assert (
        j["steps"][-1]["env"]["ALPACA_100_IDENTITIES_LIVE_ENABLED"]
        == "${{ vars.ALPACA_100_IDENTITIES_LIVE_ENABLED }}"
    )
    assert all("secrets." not in str(s) for s in j["steps"][:-1])
    old = yaml.safe_load(Path(".github/workflows/alpaca-readonly-100.yml").read_text())
    assert w["concurrency"] == old["concurrency"]
