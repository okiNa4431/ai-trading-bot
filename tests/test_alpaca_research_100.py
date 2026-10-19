"""Offline-only staged research tests; no observed market data in these fixtures."""

import json
from datetime import UTC, datetime, time
from pathlib import Path

import httpx
import pandas_market_calendars as mcal
import pytest
import yaml

from trading_bot.alpaca import NY
from trading_bot.alpaca_research import (
    APPROVAL,
    WORKFLOW_REF,
    ReadOnlyTransport,
    ResearchPolicy,
    ResearchProvider,
    main,
    run_probe,
)
from trading_bot.provider import DataError

NOW = datetime(2025, 1, 2, 12, tzinfo=UTC)
APPROVAL100 = "READ_ONLY_100_NO_STORAGE"
ENV = dict(
    GITHUB_ACTIONS="true",
    GITHUB_EVENT_NAME="workflow_dispatch",
    GITHUB_REF="refs/heads/main",
    GITHUB_REPOSITORY="okiNa4431/ai-trading-bot",
    GITHUB_WORKFLOW_REF=WORKFLOW_REF.replace("alpaca-readonly.yml", "alpaca-readonly-100.yml"),
    RUNNER_ENVIRONMENT="github-hosted",
    ALPACA_RESEARCH_ENVIRONMENT="alpaca-research",
    ALPACA_MARKET_DATA_LIVE_ENABLED="true",
    ALPACA_API_KEY_ID="never-log-key",
    ALPACA_API_SECRET_KEY="never-log-secret",
)


@pytest.fixture
def policy100():
    return ResearchPolicy.model_validate(
        yaml.safe_load(Path("config/alpaca-readonly-100.yaml").read_text(encoding="utf-8"))
    )


@pytest.fixture
def pages(policy100):
    days = mcal.get_calendar("NYSE").schedule("2023-08-01", "2025-01-01").index[-221:]
    flat = [
        (
            symbol,
            dict(
                t=datetime.combine(d.date(), time(), NY).isoformat(),
                o=100,
                h=102,
                l=99,
                c=100,
                v=10,
            ),
        )
        for symbol in sorted(policy100.symbols)
        for d in days
    ]

    def page(request, size=10000):
        offset = int(request.url.params.get("page_token", "0"))
        bars = {}
        for symbol, row in flat[offset : offset + size]:
            bars.setdefault(symbol, []).append(dict(row))
        return dict(
            bars=bars, next_page_token=str(offset + size) if offset + size < len(flat) else None
        )

    return page


def provider(policy, handler):
    return ResearchProvider(
        policy, APPROVAL100, ENV, mock=httpx.MockTransport(handler), now=lambda: NOW
    )


def test_fixed_universe_contains_existing_twenty(policy100):
    old = yaml.safe_load(Path("config/alpaca-readonly.yaml").read_text())
    assert len(policy100.symbols) == len(set(policy100.symbols)) == 100
    assert set(old["symbols"]) <= set(policy100.symbols)
    assert policy100.symbols[:20] == old["symbols"]


def test_live_twenty_requires_exact_count_and_matching_approval(monkeypatch):
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    old = ResearchPolicy.model_validate(
        yaml.safe_load(Path("config/alpaca-readonly.yaml").read_text())
    )
    environment = ENV | {"GITHUB_WORKFLOW_REF": WORKFLOW_REF}
    with pytest.raises(ValueError, match="exact symbol count"):
        ResearchProvider(
            old.model_copy(update={"symbols": old.symbols[:10]}), APPROVAL, environment
        )
    with pytest.raises(ValueError):
        ResearchProvider(old, APPROVAL100, environment)


def test_hundred_three_pages_twice(policy100, pages):
    calls = []

    def handler(r):
        calls.append(r)
        assert len(r.url.params["symbols"].split(",")) == 100
        assert r.url.params["feed"] == "iex" and r.url.params["adjustment"] == "raw"
        return httpx.Response(200, json=pages(r))

    p = provider(policy100, handler)
    try:
        result = run_probe(policy100, p)
        assert result["received_rows_per_pass"] == result["expected_rows"] == 22100
        assert result["requests"] == result["pages"] == 6
        assert result["complete"] and result["successful_symbols"] == 100
        assert result["low_iex_volume_rows"] == 22100
        assert result["retries"] == result["missing_rows"] == result["duplicate_rows"] == 0
        assert calls[0].url == calls[3].url
        assert '"close"' not in json.dumps(result) and "never-log" not in json.dumps(result)
    finally:
        p.close()


@pytest.mark.parametrize("size,complete", [(6000, True), (4000, False)])
def test_short_pages_follow_tokens_with_fixed_budget(policy100, pages, size, complete):
    p = provider(policy100, lambda r: httpx.Response(200, json=pages(r, size)))
    try:
        if complete:
            result = run_probe(policy100, p)
            assert result["pages"] == 8 and result["received_rows_per_pass"] == 22100
        else:
            with pytest.raises(DataError, match="page budget"):
                run_probe(policy100, p)
            assert len(p.guard.events) == 4
    finally:
        p.close()


def test_rate_limit_recovery_is_counted(policy100, pages):
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=pages(r))

    p = provider(policy100, handler)
    try:
        result = run_probe(policy100, p)
        assert result["retries"] == 1 and result["requests"] == 7
        assert result["pages"] == 6
    finally:
        p.close()


@pytest.mark.parametrize("change", ["approval", "workflow", "gate", "secret", "count"])
def test_cross_profile_or_missing_gate_stops_before_http(policy100, change, monkeypatch):
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    env = dict(ENV)
    approval = APPROVAL100
    if change == "approval":
        approval = APPROVAL
    elif change == "workflow":
        env["GITHUB_WORKFLOW_REF"] = WORKFLOW_REF
    elif change == "gate":
        env["ALPACA_MARKET_DATA_LIVE_ENABLED"] = "false"
    elif change == "secret":
        env["ALPACA_API_KEY_ID"] = ""
    else:
        policy100 = policy100.model_copy(update={"symbols": policy100.symbols[:20]})
    with pytest.raises(ValueError):
        ResearchProvider(policy100, approval, env)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "ohlcv", "token", "revision", "jump"])
def test_quality_failure_is_aggregate_only(policy100, pages, kind, monkeypatch, capsys):
    import trading_bot.alpaca_research as module

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
        elif kind == "token":
            data["next_page_token"] = "10000"
        elif kind == "revision" and calls == 4:
            rows[0]["c"] = 101
        elif kind == "jump":
            rows[0].update(o=200, h=202, l=199, c=200)
        return httpx.Response(200, json=data)

    p = provider(policy100, handler)
    monkeypatch.setattr(module, "ResearchProvider", lambda *args: p)
    assert main(profile=100) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert not result["complete"] and result["status"] == "STOPPED"
    assert "never-log" not in output and '"AAPL"' not in output and '"close"' not in output
    if kind == "missing":
        assert result["missing_rows"] == 1 and result["successful_symbols"] == 99
    if kind == "duplicate":
        assert result["duplicate_rows"] == 1
    if kind == "ohlcv":
        assert result["invalid_rows"] == 1
    if kind == "revision":
        assert result["repeat_revisions"] == 1
    if kind == "jump":
        assert result["discontinuities"] > 0


@pytest.mark.parametrize("status", [401, 403, 429, 503, 0])
def test_bounded_http_failure(policy100, status):
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        if status == 0:
            raise httpx.ReadTimeout("never-log-secret response body")
        return httpx.Response(status, text="never-log-secret body", headers={"Retry-After": "0"})

    p = provider(policy100, handler)
    try:
        with pytest.raises(DataError):
            run_probe(policy100, p)
        assert calls == (1 if status in (401, 403) else 3)
    finally:
        p.close()


@pytest.mark.parametrize("during_response", [False, True])
def test_elapsed_budget_stops_before_next_request(during_response):
    clock = [0.0]
    calls = []

    def handler(r):
        calls.append(r)
        clock[0] = 301
        return httpx.Response(200)

    guard = ReadOnlyTransport(httpx.MockTransport(handler), ["AAPL"], clock=lambda: clock[0])
    if not during_response:
        clock[0] = 300
    request = httpx.Request(
        "GET",
        "https://data.alpaca.markets/v2/stocks/bars",
        params=dict(
            symbols="AAPL",
            feed="iex",
            adjustment="raw",
            timeframe="1Day",
            currency="USD",
            sort="asc",
            asof="-",
            limit="10000",
        ),
    )
    with pytest.raises(DataError, match="time budget"):
        guard.handle_request(request)
    assert len(calls) == int(during_response)


def test_workflows_separated_but_share_rate_concurrency():
    old = yaml.safe_load(Path(".github/workflows/alpaca-readonly.yml").read_text())
    raw = Path(".github/workflows/alpaca-readonly-100.yml").read_text()
    new = yaml.safe_load(raw)
    assert new["concurrency"] == old["concurrency"]
    assert set(new.get("on", new.get(True))) == {"workflow_dispatch"}
    job = new["jobs"]["validate"]
    assert job["environment"] == "alpaca-research" and "refs/heads/main" in job["if"]
    assert job["timeout-minutes"] == 10
    assert "READ_ONLY_100_NO_STORAGE" in raw and "READ_ONLY_20_NO_STORAGE" not in raw
    assert "alpaca_readonly_100.py" in job["steps"][-1]["run"]
    assert (
        job["steps"][-1]["env"]["ALPACA_MARKET_DATA_LIVE_ENABLED"]
        == "${{ vars.ALPACA_MARKET_DATA_LIVE_ENABLED }}"
    )
    assert all("secrets." not in str(s) for s in job["steps"][:-1])
    assert "upload-artifact" not in raw and "enable-cache: false" in raw
    assert "SUPABASE" not in raw and "GEMINI" not in raw
