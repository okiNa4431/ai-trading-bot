import json
from datetime import UTC, datetime, time
from pathlib import Path

import httpx
import pandas_market_calendars as mcal
import pytest
import yaml

from trading_bot.alpaca import NY, AlpacaDailyProvider
from trading_bot.alpaca_research import (
    APPROVAL,
    WORKFLOW_REF,
    ReadOnlyTransport,
    ResearchPolicy,
    ResearchProvider,
    authorize,
    main,
    run_probe,
)
from trading_bot.data_access import MarketAccessDisabled, require_live_market_access
from trading_bot.provider import DataError

NOW = datetime(2025, 1, 2, 12, tzinfo=UTC)
ENV = dict(
    GITHUB_ACTIONS="true",
    GITHUB_EVENT_NAME="workflow_dispatch",
    GITHUB_REF="refs/heads/main",
    GITHUB_WORKFLOW_REF=WORKFLOW_REF,
    GITHUB_REPOSITORY="okiNa4431/ai-trading-bot",
    RUNNER_ENVIRONMENT="github-hosted",
    ALPACA_RESEARCH_ENVIRONMENT="alpaca-research",
    ALPACA_MARKET_DATA_LIVE_ENABLED="true",
    ALPACA_API_KEY_ID="never-log-key",
    ALPACA_API_SECRET_KEY="never-log-secret",
)


def policy(**changes):
    return ResearchPolicy(
        live_enabled=True,
        contract_reference="mock-contract-only",
        symbols=["AAPL", "MSFT"],
        **changes,
    )


def response(request, *, revision=False, missing=False):
    days = mcal.get_calendar("NYSE").schedule("2023-08-01", "2025-01-01").index[-221:]
    rows = [
        {
            "t": datetime.combine(d.date(), time(), NY).isoformat(),
            "o": 100,
            "h": 102,
            "l": 99,
            "c": 100 + int(revision),
            "v": 0,
        }
        for d in days
    ]
    return httpx.Response(
        200,
        json={
            "bars": {
                s: rows[:-1] if missing else rows for s in request.url.params["symbols"].split(",")
            },
            "next_page_token": None,
        },
    )


def make_provider(handler=response):
    return ResearchProvider(
        policy(), APPROVAL, ENV, mock=httpx.MockTransport(handler), now=lambda: NOW
    )


def request(**changes):
    return httpx.Request(
        **(
            dict(
                method="GET",
                url="https://data.alpaca.markets/v2/stocks/bars",
                params=dict(
                    symbols="AAPL,MSFT",
                    feed="iex",
                    adjustment="raw",
                    timeframe="1Day",
                    currency="USD",
                    asof="-",
                    limit="10000",
                    sort="asc",
                ),
            )
            | changes
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "POST"},
        {"method": "DELETE"},
        {"content": b"forbidden"},
        {"url": "https://paper-api.alpaca.markets/v2/orders"},
        {"url": "https://api.alpaca.markets/v2/orders"},
        {"url": "https://example.com/v2/stocks/bars"},
        {"url": "http://data.alpaca.markets/v2/stocks/bars"},
        {"url": "https://data.alpaca.markets:444/v2/stocks/bars"},
        {"url": "https://data.alpaca.markets/v1/corporate-actions"},
        {"url": "https://user:secret@data.alpaca.markets/v2/stocks/bars"},
    ],
)
def test_transport_blocks_before_underlying_call(changes):
    guard = ReadOnlyTransport(
        httpx.MockTransport(lambda _: pytest.fail("network reached")), ["AAPL", "MSFT"]
    )
    with pytest.raises(DataError, match="blocked"):
        guard.handle_request(request(**changes))
    assert guard.events == []


@pytest.mark.parametrize(
    "change",
    [
        dict(feed="sip"),
        dict(adjustment="all"),
        dict(timeframe="1Hour"),
        dict(symbols="AAPL"),
        dict(symbols="AAPL,MSFT,AAPL"),
        dict(unknown="secret"),
    ],
)
def test_query_scope_rejected(change):
    r = request()
    r.url = r.url.copy_merge_params(change)
    with pytest.raises(DataError):
        ReadOnlyTransport(
            httpx.MockTransport(lambda _: pytest.fail("network reached")), ["AAPL", "MSFT"]
        ).handle_request(r)


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REF", "refs/heads/other"),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_WORKFLOW_REF", "other.yml"),
        ("GITHUB_REPOSITORY", "evil/fork"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("ALPACA_RESEARCH_ENVIRONMENT", "other"),
    ],
)
def test_authorization_context(key, value):
    with pytest.raises(MarketAccessDisabled):
        authorize(policy(), APPROVAL, ENV | {key: value})


@pytest.mark.parametrize("approval", ["", "true", "READ_ONLY_20_NO_STORAGE; echo secret"])
def test_bad_approval_and_missing_secrets(approval):
    with pytest.raises(MarketAccessDisabled):
        ResearchProvider(policy(), approval, ENV, mock=httpx.MockTransport(response))


def test_missing_secret_stops_before_transport(monkeypatch):
    monkeypatch.setattr(
        httpx, "HTTPTransport", lambda **_: pytest.fail("created network transport")
    )
    with pytest.raises(MarketAccessDisabled, match="Secrets"):
        ResearchProvider(policy(), APPROVAL, ENV | {"ALPACA_API_SECRET_KEY": ""})


@pytest.mark.parametrize("value", [None, "", "false", "TRUE", "1", " true", "true "])
def test_second_gate_stops_before_transport(value, monkeypatch):
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network transport"))
    environment = dict(ENV)
    environment.pop("ALPACA_MARKET_DATA_LIVE_ENABLED")
    if value is not None:
        environment["ALPACA_MARKET_DATA_LIVE_ENABLED"] = value
    with pytest.raises(MarketAccessDisabled):
        ResearchProvider(policy(), APPROVAL, environment)


def test_retry_and_page_aggregate_counts():
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return response(r)

    p = make_provider(handler)
    try:
        result = run_probe(policy(), p)
        assert result["requests"] == 3
        assert result["pages"] == 2 and result["retries"] == 1
        assert result["http_status_counts"] == {"429": 1, "200": 2}
    finally:
        p.close()


@pytest.mark.parametrize("kind", ["duplicate", "invalid", "paging", "unconfirmed"])
def test_probe_rejects_incomplete_or_invalid_observations(kind):
    def handler(r):
        payload = response(r).json()
        if kind == "duplicate":
            payload["bars"]["AAPL"].append(payload["bars"]["AAPL"][0])
        elif kind == "invalid":
            payload["bars"]["AAPL"][0]["h"] = -1
        elif kind == "paging":
            payload["next_page_token"] = "repeated-token"
        return httpx.Response(200, json=payload)

    p = make_provider(handler)
    if kind == "unconfirmed":
        original = p.fetch_many

        def fetch(*args):
            batch = original(*args)
            batch.excluded_unconfirmed = 1
            return batch

        p.fetch_many = fetch
    try:
        with pytest.raises(DataError):
            run_probe(policy(), p)
        assert len(p.guard.events) <= 2
    finally:
        p.close()


@pytest.mark.parametrize("symbols", [["AAPL"] * 21, ["AAPL", "AAPL"], [], ["7203.T"]])
def test_bad_policy(symbols):
    with pytest.raises(ValueError):
        ResearchPolicy(symbols=symbols)


def test_shipping_policy_requires_second_gate_and_old_gate_unchanged():
    p = ResearchPolicy.model_validate(
        yaml.safe_load(Path("config/alpaca-readonly.yaml").read_text())
    )
    assert p.live_enabled and "#376491" in p.contract_reference and len(p.symbols) == 20
    assert p.sessions == 221
    authorize(p, APPROVAL, ENV)
    with pytest.raises(MarketAccessDisabled):
        authorize(p, APPROVAL, ENV | {"ALPACA_MARKET_DATA_LIVE_ENABLED": ""})
    with pytest.raises(MarketAccessDisabled):
        require_live_market_access()
    with pytest.raises(MarketAccessDisabled):
        AlpacaDailyProvider()


def test_twenty_symbols_two_pass_transient_aggregate():
    p = ResearchProvider(
        policy().model_copy(update={"symbols": [f"S{chr(65 + i)}" for i in range(20)]}),
        APPROVAL,
        ENV,
        mock=httpx.MockTransport(response),
        now=lambda: NOW,
    )
    try:
        result = run_probe(policy().model_copy(update={"symbols": list(p.allowed_symbols)}), p)
        assert result["received_rows_per_pass"] == 4420 and result["requests"] == 2
        assert result["pages"] == 2 and result["retries"] == 0
        assert result["repeat_revisions"] == 0 and result["strategy_ready"] is False
        encoded = json.dumps(result)
        assert "never-log" not in encoded and '"close"' not in encoded and '"AAPL"' not in encoded
        assert result["retained_market_data"] is False
    finally:
        p.close()


@pytest.mark.parametrize("kind", ["revision", "missing", "401", "403", "429", "503", "timeout"])
def test_failure_closed(kind, caplog):
    calls = []

    def handler(r):
        calls.append(r)
        if kind.isdigit():
            return httpx.Response(
                int(kind), text="never-log-secret RAW BODY", headers={"Retry-After": "0"}
            )
        if kind == "timeout":
            raise httpx.ReadTimeout("never-log-secret RAW BODY")
        return response(
            r, revision=kind == "revision" and len(calls) == 2, missing=kind == "missing"
        )

    p = make_provider(handler)
    try:
        with pytest.raises(DataError) as error:
            run_probe(policy(), p)
        assert len(calls) <= 3
        assert "never-log-secret" not in str(error.value) + caplog.text
        assert "RAW BODY" not in str(error.value) + caplog.text
    finally:
        p.close()


def test_main_disabled_emits_only_safe_counts(capsys):
    assert main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["requests"] == 0 and result["status"] == "STOPPED"


def test_workflow_has_no_untrusted_shell_or_data_export():
    raw = Path(".github/workflows/alpaca-readonly.yml").read_text()
    workflow = yaml.safe_load(raw)
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    job = workflow["jobs"]["validate"]
    assert job["environment"] == "alpaca-research" and "refs/heads/main" in job["if"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["permissions"] == {"contents": "read"}
    for step in job["steps"]:
        assert "${{ inputs." not in step.get("run", "")
        assert "upload-artifact" not in step.get("uses", "")
    assert "SUPABASE" not in raw and "GEMINI" not in raw
    assert "enable-cache: false" in raw
    execution = job["steps"][-1]["env"]
    assert execution["ALPACA_MARKET_DATA_LIVE_ENABLED"] == (
        "${{ vars.ALPACA_MARKET_DATA_LIVE_ENABLED }}"
    )
    assert "env" not in workflow and "env" not in job
    assert all("secrets." not in str(step) for step in job["steps"][:-1])


def test_total_request_budget_and_redirect_never_followed():
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(302, headers={"Location": "https://example.com/steal"})

    guard = ReadOnlyTransport(httpx.MockTransport(handler), ["AAPL", "MSFT"])
    for _ in range(24):
        assert guard.handle_request(request()).status_code == 302
    with pytest.raises(DataError, match="budget"):
        guard.handle_request(request())
    assert len(calls) == 24
    p = make_provider(handler)
    try:
        with pytest.raises(DataError):
            run_probe(policy(), p)
        assert len(p.guard.events) == 1
    finally:
        p.close()


def test_old_single_symbol_path_cannot_expand_research_scope():
    p = make_provider(lambda _: pytest.fail("network reached"))
    try:
        from trading_bot.config import Instrument

        instrument = Instrument(symbol="AAPL", market="US", currency="USD", display_name="Apple")
        with pytest.raises(DataError, match="symbol list"):
            p.fetch(instrument, NOW, NOW, "1d")
        instruments = [instrument, instrument.model_copy(update={"symbol": "MSFT"})]
        with pytest.raises(DataError, match="session window"):
            p.fetch_many(instruments, datetime(2024, 12, 30, tzinfo=UTC), NOW)
    finally:
        p.close()


def test_discontinuity_stops_without_claiming_corporate_actions_clear():
    def handler(r):
        res = response(r)
        payload = res.json()
        for rows in payload["bars"].values():
            rows[-1].update(o=200, h=202, l=199, c=200)
        return httpx.Response(200, json=payload)

    p = make_provider(handler)
    try:
        with pytest.raises(DataError, match="discontinuity"):
            run_probe(policy(), p)
    finally:
        p.close()


def test_cli_failure_suppresses_response_and_headers(monkeypatch, capsys):
    import trading_bot.alpaca_research as module

    p = make_provider(lambda _: httpx.Response(403, text="never-log-secret RAW BODY"))
    monkeypatch.setattr(module, "ResearchProvider", lambda *args: p)
    monkeypatch.setattr(
        module, "run_probe", lambda _policy, provider: run_probe(policy(), provider)
    )
    assert main() == 1
    output = capsys.readouterr().out
    assert "RAW BODY" not in output and "never-log" not in output and '"AAPL"' not in output
    assert json.loads(output)["http_status_counts"] == {"403": 1}
    assert p.last_batch is None
