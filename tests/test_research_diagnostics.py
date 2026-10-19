"""Synthetic diagnosis examples, not observed trading results."""

import json
from datetime import UTC, datetime, time
from pathlib import Path

import httpx
import pandas_market_calendars as mcal
import pytest
import yaml

from trading_bot.alpaca import NY, DailyBatch
from trading_bot.alpaca_research import ResearchPolicy, ResearchProvider, main
from trading_bot.models import PriceBar
from trading_bot.research_diagnostics import missing_diagnostics


def batch_for(indices, *, symbol="MMC", complete=True):
    days = tuple(
        d.date() for d in mcal.get_calendar("NYSE").schedule("2025-12-01", "2026-10-16").index
    )
    bars = [
        PriceBar(
            symbol=symbol,
            market="US",
            currency="USD",
            interval="1d",
            source="alpaca",
            timestamp=datetime.combine(days[i], time(), NY),
            fetched_at=datetime(2026, 10, 19, tzinfo=UTC),
            open=100,
            high=102,
            low=99,
            close=100,
            volume=0,
        )
        for i in indices
    ]
    return DailyBatch({symbol: bars}, days, complete_response=complete)


@pytest.mark.parametrize(
    "indices,kind",
    [
        (range(30, 221), "leading"),
        (range(30), "trailing"),
        ([0, 220], "interior_or_mixed"),
        ([], "entire"),
    ],
)
def test_exclusive_missing_classifications(indices, kind):
    result = missing_diagnostics(batch_for(indices))
    assert result["missing_pattern_symbols"][kind] == 1
    assert sum(result["missing_pattern_symbols"].values()) == 1


def test_public_change_match_is_exact_and_not_inferred_for_other_symbols():
    result = missing_diagnostics(batch_for(range(30)))
    assert result["prefix_30_trailing_191_symbols"] == 1
    assert result["known_change_boundary_matches"] == 1
    assert (
        missing_diagnostics(batch_for(range(30), symbol="AAPL"))["known_change_boundary_matches"]
        == 0
    )
    assert missing_diagnostics(batch_for(range(29)))["known_change_boundary_matches"] == 0
    assert (
        missing_diagnostics(batch_for(range(30), complete=False))["known_change_boundary_matches"]
        == 0
    )
    assert "MMC" not in json.dumps(result) and "2026-01-14" not in json.dumps(result)


def test_no_batch_is_not_reported_as_checked():
    assert missing_diagnostics(None)["diagnostic_basis"] == "unavailable"


@pytest.mark.parametrize("count,missing", [(20, False), (100, True)])
def test_cli_synthetic_success_or_191_missing(count, missing, monkeypatch, capsys):
    import trading_bot.alpaca_research as module

    path = "config/alpaca-readonly.yaml" if count == 20 else "config/alpaca-readonly-100.yaml"
    policy = ResearchPolicy.model_validate(yaml.safe_load(Path(path).read_text()))
    base = batch_for(range(221))
    flat = [
        (symbol, dict(t=bar.timestamp.isoformat(), o=100, h=102, l=99, c=100, v=0))
        for symbol in sorted(policy.symbols)
        for bar in base.bars["MMC"][: 30 if missing and symbol == "MMC" else 221]
    ]
    calls = []

    def handler(request):
        calls.append(request)
        start = int(request.url.params.get("page_token", "0"))
        bars = {}
        for symbol, row in flat[start : start + 10000]:
            bars.setdefault(symbol, []).append(row)
        return httpx.Response(
            200,
            json=dict(
                bars=bars, next_page_token=str(start + 10000) if start + 10000 < len(flat) else None
            ),
        )

    env = dict(
        GITHUB_ACTIONS="true",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/main",
        GITHUB_REPOSITORY="okiNa4431/ai-trading-bot",
        GITHUB_WORKFLOW_REF=module.WORKFLOW_REF
        if count == 20
        else module.WORKFLOW_REF.replace("alpaca-readonly.yml", "alpaca-readonly-100.yml"),
        RUNNER_ENVIRONMENT="github-hosted",
        ALPACA_RESEARCH_ENVIRONMENT="alpaca-research",
        ALPACA_MARKET_DATA_LIVE_ENABLED="true",
        ALPACA_API_KEY_ID="never-log-key",
        ALPACA_API_SECRET_KEY="never-log-secret",
    )
    p = ResearchProvider(
        policy,
        f"READ_ONLY_{count}_NO_STORAGE",
        env,
        mock=httpx.MockTransport(handler),
        now=lambda: datetime(2026, 10, 19, 12, tzinfo=UTC),
    )
    monkeypatch.setattr(module, "ResearchProvider", lambda *args: p)
    assert main(profile=count) == int(missing)
    output = capsys.readouterr().out
    result = json.loads(output)
    assert "never-log" not in output and '"MMC"' not in output and '"close"' not in output
    if missing:
        assert len(calls) == 3 and result["received_rows"] == 21909
        assert result["missing_rows"] == 191
        assert result["prefix_30_trailing_191_symbols"] == 1
        assert result["known_change_boundary_matches"] == 1
        assert result["checks"] == dict(
            paging=["PASSED", "NOT_RUN"],
            completeness=["FAILED", "NOT_RUN"],
            discontinuity="NOT_RUN",
            repeat_consistency="NOT_RUN",
        )
        assert result["discontinuities"] is None and result["repeat_revisions"] is None
    else:
        assert len(calls) == 2 and result["received_rows_per_pass"] == 4420
        assert result["checks"] == dict(
            paging=["PASSED", "PASSED"],
            completeness=["PASSED", "PASSED"],
            discontinuity="PASSED",
            repeat_consistency="PASSED",
        )
    assert p.last_batch is None
