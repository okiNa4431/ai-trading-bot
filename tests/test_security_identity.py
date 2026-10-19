"""Synthetic identity-boundary cases, never observed market data."""

import json
from datetime import UTC, date, datetime, time
from pathlib import Path

import httpx
import pandas_market_calendars as mcal
import pytest
import yaml

from trading_bot.alpaca import NY, DailyBatch
from trading_bot.models import PriceBar
from trading_bot.provider import DataError
from trading_bot.security_identity import (
    CorporateEvent,
    SecurityRegistry,
    identify_batch,
    marsh_registry,
)
from trading_bot.ticker_history_probe import APPROVAL, WORKFLOW, TickerHistoryProvider, probe

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
ENV = dict(
    GITHUB_ACTIONS="true",
    GITHUB_EVENT_NAME="workflow_dispatch",
    GITHUB_REF="refs/heads/main",
    GITHUB_WORKFLOW_REF=WORKFLOW,
    GITHUB_REPOSITORY="okiNa4431/ai-trading-bot",
    RUNNER_ENVIRONMENT="github-hosted",
    ALPACA_RESEARCH_ENVIRONMENT="alpaca-research",
    ALPACA_MARKET_DATA_LIVE_ENABLED="true",
    ALPACA_TICKER_HISTORY_LIVE_ENABLED="true",
    ALPACA_API_KEY_ID="never-log-key",
    ALPACA_API_SECRET_KEY="never-log-secret",
)


@pytest.fixture
def batch():
    days = tuple(
        d.date() for d in mcal.get_calendar("NYSE").schedule("2025-12-01", "2026-10-16").index
    )
    bars = {"MMC": [], "MRSH": []}
    for day in days:
        ticker = "MMC" if day < date(2026, 1, 14) else "MRSH"
        bars[ticker].append(
            PriceBar(
                symbol=ticker,
                market="US",
                currency="USD",
                interval="1d",
                source="alpaca",
                timestamp=datetime.combine(day, time(), NY),
                fetched_at=NOW,
                open=100,
                high=102,
                low=99,
                close=100,
                volume=0,
            )
        )
    return DailyBatch(bars, days, complete_response=True)


def test_boundary_preserves_raw_and_provenance(batch):
    observations = identify_batch(marsh_registry(), batch)
    assert len(observations) == 221
    assert observations[0].raw is batch.bars["MMC"][0]
    assert observations[30].raw is batch.bars["MRSH"][0]
    assert len({o.security_id for o in observations}) == 1
    assert observations[29].raw.symbol == "MMC" and observations[30].raw.symbol == "MRSH"


@pytest.mark.parametrize("kind", ["overlap", "gap", "unknown", "unconfirmed", "wrong_event"])
def test_invalid_registry_rejected(kind):
    data = marsh_registry().model_dump()
    if kind in ("overlap", "gap"):
        data["assignments"][0]["valid_to"] = date(2026, 1, 15 if kind == "overlap" else 13)
    elif kind == "unknown":
        data["assignments"][1]["security_id"] = "unknown-security"
    elif kind == "unconfirmed":
        data["assignments"][1]["identity_confirmed"] = False
    else:
        data["events"][0]["new_ticker"] = "WRONG"
    with pytest.raises(ValueError):
        SecurityRegistry.model_validate(data)


@pytest.mark.parametrize(
    "kind", ["missing", "duplicate", "outside", "wrongbucket", "partial", "split"]
)
def test_strict_identity_observations(batch, kind):
    registry = marsh_registry()
    if kind == "missing":
        batch.bars["MRSH"].pop(0)
    elif kind == "duplicate":
        batch.bars["MMC"].append(batch.bars["MMC"][0])
    elif kind == "outside":
        batch.bars["MRSH"].append(batch.bars["MMC"][-1].model_copy(update={"symbol": "MRSH"}))
    elif kind == "wrongbucket":
        batch.bars["MMC"].append(batch.bars["MRSH"].pop(0))
    elif kind == "partial":
        batch.complete_response = False
    else:
        registry = registry.model_copy(
            update={
                "events": registry.events
                + (
                    CorporateEvent(
                        security_id="sec-000001",
                        kind="split",
                        effective_date=date(2026, 2, 2),
                        evidence="synthetic",
                    ),
                )
            }
        )
    with pytest.raises(DataError):
        identify_batch(registry, batch)


def response_for(batch):
    return {
        s: [
            dict(t=b.timestamp.isoformat(), o=b.open, h=b.high, l=b.low, c=b.close, v=b.volume)
            for b in bars
        ]
        for s, bars in batch.bars.items()
    }


@pytest.mark.parametrize("kind", ["success", "missing_new", "mapped_both", "revision"])
def test_separate_probe_two_passes_or_safe_stop(batch, kind):
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        assert r.method == "GET" and r.url.host == "data.alpaca.markets"
        assert r.url.params["symbols"] == "MMC,MRSH" and r.url.params["asof"] == "-"
        bars = response_for(batch)
        if kind == "missing_new":
            bars["MRSH"] = []
        elif kind == "mapped_both":
            bars["MMC"] += bars["MRSH"]
        elif kind == "revision" and calls == 2:
            bars["MRSH"][0]["c"] = 101
        return httpx.Response(200, json=dict(bars=bars, next_page_token=None))

    p = TickerHistoryProvider(APPROVAL, ENV, mock=httpx.MockTransport(handler), now=lambda: NOW)
    try:
        if kind == "success":
            result = probe(p)
            assert result["complete"] and result["received_rows_per_pass"] == 221
            assert result["requests"] == 2 and result["security_count"] == 1
            assert "never-log" not in json.dumps(result) and '"MMC"' not in json.dumps(result)
        else:
            with pytest.raises(DataError):
                probe(p)
            assert p.stage == ("REPEAT_COMPARISON" if kind == "revision" else "IDENTITY")
    finally:
        p.close()


@pytest.mark.parametrize("change", ["approval", "workflow", "gate", "secret"])
def test_new_approval_gate_before_network(change, monkeypatch):
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: pytest.fail("network created"))
    env, approval = dict(ENV), APPROVAL
    if change == "approval":
        approval = "READ_ONLY_100_NO_STORAGE"
    elif change == "workflow":
        env["GITHUB_WORKFLOW_REF"] = WORKFLOW.replace(
            "alpaca-ticker-history.yml", "alpaca-readonly-100.yml"
        )
    elif change == "gate":
        env.pop("ALPACA_TICKER_HISTORY_LIVE_ENABLED")
    else:
        env.pop("ALPACA_API_SECRET_KEY")
    with pytest.raises(ValueError):
        TickerHistoryProvider(approval, env)


def test_cli_failure_redacts_body_and_credentials(monkeypatch, capsys):
    import trading_bot.ticker_history_probe as module

    p = TickerHistoryProvider(
        APPROVAL,
        ENV,
        mock=httpx.MockTransport(lambda _: httpx.Response(403, text="never-log-secret RAW BODY")),
        now=lambda: NOW,
    )
    monkeypatch.setattr(module, "TickerHistoryProvider", lambda *args: p)
    assert module.main() == 1
    output = capsys.readouterr().out
    assert "never-log" not in output and "RAW BODY" not in output and '"MMC"' not in output
    assert json.loads(output)["failed_stage"] == "FETCH"


def test_workflow_has_independent_gate_and_no_storage():
    raw = Path(".github/workflows/alpaca-ticker-history.yml").read_text()
    w = yaml.safe_load(raw)
    assert set(w.get("on", w.get(True))) == {"workflow_dispatch"}
    j = w["jobs"]["validate"]
    assert j["environment"] == "alpaca-research" and "refs/heads/main" in j["if"]
    assert APPROVAL in raw and "upload-artifact" not in raw and "enable-cache: false" in raw
    assert (
        j["steps"][-1]["env"]["ALPACA_TICKER_HISTORY_LIVE_ENABLED"]
        == "${{ vars.ALPACA_TICKER_HISTORY_LIVE_ENABLED }}"
    )
    assert all("secrets." not in str(s) for s in j["steps"][:-1])
    old = yaml.safe_load(Path(".github/workflows/alpaca-readonly-100.yml").read_text())
    assert w["concurrency"] == old["concurrency"]
