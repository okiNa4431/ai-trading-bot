"""Explicit synthetic contracts only. No market/Gemini/network access in CI."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml

from tests.ai_helpers import JP, NOW, histories
from trading_bot.cycle import require_execution_coverage
from trading_bot.data_access import MarketAccessDisabled, require_live_market_access
from trading_bot.portfolio.models import TradingRules
from trading_bot.portfolio.valuation import validate_fx
from trading_bot.preflight import (
    environment_approval_status,
    expected_execution_alerts,
    next_manual_sessions,
)
from trading_bot.reference_fx import (
    ECBDailyFXProvider,
    FXSourceError,
    TwelveDataFXProvider,
    get_document,
    prepared_fx_provider,
)


def fx_document():
    return {
        "meta": {
            "symbol": "USD/JPY",
            "interval": "5min",
            "currency_base": "US Dollar",
            "currency_quote": "Japanese Yen",
            "exchange_timezone": "UTC",
        },
        "values": [
            {"datetime": "2026-10-15 02:05:00", "close": "151"},
            {"datetime": "2026-10-15 02:00:00", "close": "150"},
        ],
    }


@pytest.mark.parametrize("source", ["twelve_data", "ecb_daily", "unknown", "yahoo"])
def test_sources_never_activate_from_selection_or_environment(source, monkeypatch):
    monkeypatch.setenv("PHASE55_DATA_APPROVED", "true")
    if source in ("unknown", "yahoo"):
        with pytest.raises(FXSourceError):
            prepared_fx_provider(source)
        return
    source = prepared_fx_provider(source, "no-actual-key")
    with pytest.raises(MarketAccessDisabled):
        source.fetch_fx(NOW) if isinstance(source, TwelveDataFXProvider) else source.fetch_daily()


def test_pause_cannot_be_disabled_by_a_workflow_input():
    with pytest.raises(MarketAccessDisabled):
        require_live_market_access()


def test_twelve_data_uses_only_confirmed_fx_and_preserves_observation_time():
    result = TwelveDataFXProvider.parse(
        json.dumps(fx_document()).encode(), NOW, NOW + timedelta(seconds=2)
    )
    assert result.rate.usd_jpy == Decimal("150")
    assert result.rate.timestamp == NOW
    assert result.observed_at == NOW + timedelta(seconds=2)
    assert result.provider == "twelve_data"
    with pytest.raises(ValueError):
        validate_fx(result.rate, NOW + timedelta(seconds=3601), TradingRules())


@pytest.mark.parametrize(
    "fault",
    [
        "nan",
        "inf",
        "negative",
        "missing",
        "duplicate",
        "wrong-pair",
        "wrong-zone",
        "wrong-interval",
        "empty",
        "error",
        "list",
    ],
)
def test_invalid_fx_is_rejected_without_repair(fault):
    data = fx_document()
    if fault in ("nan", "inf", "negative"):
        data["values"][1]["close"] = {"nan": "NaN", "inf": "Infinity", "negative": "-1"}[fault]
    elif fault == "missing":
        del data["values"][1]["close"]
    elif fault == "duplicate":
        data["values"].append(data["values"][1])
    elif fault == "wrong-pair":
        data["meta"]["symbol"] = "JPY/USD"
    elif fault == "wrong-zone":
        del data["meta"]["exchange_timezone"]
    elif fault == "wrong-interval":
        data["meta"]["interval"] = "1day"
    elif fault == "empty":
        data["values"] = []
    elif fault == "error":
        data = {"status": "error", "message": "upstream-secret"}
    else:
        data = []
    with pytest.raises(FXSourceError) as error:
        TwelveDataFXProvider.parse(json.dumps(data).encode(), NOW, NOW)
    assert "upstream-secret" not in str(error.value)


@pytest.mark.parametrize("status", [429, 503, 302])
def test_fx_transport_is_bounded_and_never_logs_upstream_body(status, caplog):
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(
            status, text="secret-upstream-body", headers={"location": "https://other.invalid"}
        )

    with httpx.Client(transport=httpx.MockTransport(response), follow_redirects=True) as client:
        with pytest.raises(FXSourceError) as error:
            get_document(
                "https://example.invalid",
                client=client,
                headers={"Authorization": "apikey secret-key"},
            )
    assert len(calls) == 1
    assert "secret" not in str(error.value) + caplog.text


def test_fx_timeout_does_not_retry_or_expose_request():
    calls = []

    def response(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret-key and url", request=request)

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(FXSourceError, match="no retry") as error:
            get_document("https://example.invalid", client=client)
    assert len(calls) == 1 and "secret" not in str(error.value)


def test_reference_date_is_not_fabricated_into_a_fresh_intraday_timestamp():
    item = ECBDailyFXProvider.parse(
        b'<Envelope><Cube><Cube time="2026-10-15"><Cube currency="USD" rate="2"/>'
        b'<Cube currency="JPY" rate="300"/></Cube></Cube></Envelope>',
        NOW,
    )
    assert item.usd_jpy == 150
    assert item.reference_date.isoformat() == "2026-10-15"
    assert not hasattr(item, "timestamp") and not hasattr(item, "rate")
    assert "ECB" in item.attribution and "calculated" in item.attribution


@pytest.mark.parametrize(
    "raw",
    [
        b"<!DOCTYPE x><x/>",
        b"<x/>",
        b"\x00<x/>",
        b'<x><Cube time="2026-10-20"/></x>',
        b'<x><Cube time="2026-10-15"><Cube currency="USD" rate="0"/>'
        b'<Cube currency="JPY" rate="300"/></Cube></x>',
    ],
)
def test_bad_ecb_input_is_rejected(raw):
    with pytest.raises(FXSourceError):
        ECBDailyFXProvider.parse(raw, NOW)


@pytest.mark.parametrize(
    "present,state,future,code",
    [
        (False, None, False, "MISSING_EXPECTED_RUN"),
        (True, "RUNNING", False, "EXPECTED_RUN_NOT_SUCCESSFUL"),
        (True, "SUCCEEDED", False, None),
        (False, None, True, None),
    ],
)
def test_missing_manual_run_is_detected_only_after_an_explicit_deadline(
    present, state, future, code
):
    expected = [
        {
            "run_id": "manual-slot",
            "deadline": (NOW + timedelta(hours=1 if future else -1)).isoformat(),
        }
    ]
    runs = [{"run_id": "manual-slot", "state": state}] if present else []
    alerts = expected_execution_alerts(expected, runs, NOW)
    assert [a["code"] for a in alerts] == ([code] if code else [])
    assert expected_execution_alerts([], runs, NOW) == []


def test_naive_deadlines_are_not_silently_treated_as_utc():
    with pytest.raises(ValueError):
        expected_execution_alerts([{"run_id": "bad", "deadline": "2026-10-15T02:05:00"}], [], NOW)


@pytest.mark.parametrize(
    "market,local,jst",
    [("JP", "16:45:00+09:00", "16:45:00+09:00"), ("US", "17:15:00-04:00", "06:15:00+09:00")],
)
def test_next_actual_sessions_skip_weekend_and_preserve_market_time(market, local, jst):
    days = next_manual_sessions(market, datetime(2026, 10, 17, 6, tzinfo=UTC))
    assert days[0]["session_date"] == "2026-10-19"
    assert days[0]["decision_from_local"].endswith(local)
    assert days[0]["decision_from_jst"].endswith(jst)
    assert days[0]["first_bar_check_local"].endswith(
        "10:15:00+09:00" if market == "JP" else "10:45:00-04:00"
    )
    assert "PLANNING_ONLY" in days[0]["authorization"]


def test_calendar_has_shortened_session_and_dst_offset():
    days = next_manual_sessions("US", datetime(2026, 11, 27, 12, tzinfo=UTC), 1)
    assert days[0]["decision_from_local"] == "2026-11-27T14:15:00-05:00"
    assert days[0]["decision_from_jst"] == "2026-11-28T04:15:00+09:00"


def test_branch_policy_alone_is_not_administrator_approval():
    report = environment_approval_status(
        {
            "protection_rules": [{"type": "branch_policy"}],
            "deployment_branch_policy": {"protected_branches": True},
        }
    )
    assert report["protected_branch_policy"] and not report["required_reviewers_configured"]


def test_first_stale_future_bar_must_not_be_skipped_for_a_convenient_later_fill(tmp_path):
    from trading_bot.broker.paper import PaperBroker
    from trading_bot.persistence.sqlite_repository import SQLiteRepository

    broker = PaperBroker(SQLiteRepository(tmp_path / "synthetic.sqlite3"))
    broker.initialize(NOW - timedelta(days=2))
    order = broker.place_order(JP, "BUY", 100, NOW, "test")
    # Coverage itself is irrelevant to this edge: no missing bars in the early afternoon.
    data = []
    seed = histories()["1h"][-1]
    for day in (15, 16):
        for hour, minute in [(0, 0), (1, 0), (2, 0), (3, 30), (4, 30), (5, 30), (6, 30)]:
            stamp = NOW.replace(day=day, hour=hour, minute=minute)
            data.append(
                seed.model_copy(
                    update={"timestamp": stamp, "fetched_at": stamp + timedelta(hours=1)}
                )
            )
    with pytest.raises(ValueError, match="first future execution bar is stale"):
        require_execution_coverage(order, data, NOW.replace(day=16, hour=7, minute=40))


def test_phase55_workflows_are_manual_and_do_not_receive_gemini_keys():
    for name in ("phase55-validation.yml", "phase55-monitor.yml"):
        text = (Path(".github/workflows") / name).read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        assert set(document.get("on", document.get(True))) == {"workflow_dispatch"}
        assert "GEMINI_API_KEY" not in text and "upload-artifact" not in text
    assert json.loads(Path("config/phase55-expectations.json").read_text()) == []


def test_legacy_real_data_workflows_are_paused_before_data_or_ai_steps():
    for name in ("fetch-market-data.yml", "persistent-decision.yml", "test-gemini.yml"):
        text = (Path(".github/workflows") / name).read_text(encoding="utf-8")
        assert "require_live_market_access()" in text
    document = yaml.safe_load(Path(".github/workflows/fetch-market-data.yml").read_text())
    artifacts = [
        s
        for s in document["jobs"]["fetch"]["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact")
    ]
    assert [s["with"]["path"] for s in artifacts] == ["access-state.json"]
