"""Synthetic HTTP responses only. No J-Quants key or real network is used."""

import importlib.util
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml

from trading_bot.data_access import MarketAccessDisabled, reject_diagnostic
from trading_bot.jquants import (
    APPROVAL,
    ENDPOINT,
    INTERVAL,
    MAX_REQUESTS,
    MAX_SECONDS,
    WORKFLOW,
    JQuantsProvider,
    RequestConditions,
    authorize,
    load_policy,
    observation,
)

AT = datetime(2026, 10, 21, 0, tzinfo=UTC)
KEY = "synthetic-secret-never-print"


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, value):
        assert value >= 0
        self.sleeps.append(value)
        self.value += value


@pytest.fixture
def policy():
    return load_policy().model_copy(
        update=dict(live_enabled=True, from_date=date(2025, 1, 6), to_date=date(2025, 1, 10))
    )


@pytest.fixture
def environment():
    return dict(
        GITHUB_ACTIONS="true",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/main",
        GITHUB_REPOSITORY="okiNa4431/ai-trading-bot",
        GITHUB_WORKFLOW_REF=WORKFLOW,
        RUNNER_ENVIRONMENT="github-hosted",
        JQUANTS_RESEARCH_ENVIRONMENT="jquants-research",
        JQUANTS_PLAN="Free",
        JQUANTS_MARKET_DATA_LIVE_ENABLED="true",
        JQUANTS_TERMS_CONFIRMED="true",
        JQUANTS_API_KEY=KEY,
    )


def row(code, day):
    return dict(
        Date=day.isoformat(),
        Code=code + "0",
        O=700123,
        H=700125,
        L=700120,
        C=700124,
        Vo=1234567,
        Va=864345000000,
        AdjFactor=1,
        AdjO=700123,
        AdjH=700125,
        AdjL=700120,
        AdjC=700124,
        AdjVo=1234567,
        ExRT=None,
    )


def create(policy, environment, handler=None):
    clock = Clock()
    calls = []

    def responder(request):
        calls.append(request)
        if handler:
            return handler(request, len(calls))
        return httpx.Response(
            200, json={"data": [row(request.url.params["code"], d) for d in policy.sessions()]}
        )

    provider = JQuantsProvider(
        policy,
        APPROVAL,
        environment,
        transport=httpx.MockTransport(responder),
        clock=clock,
        sleep=clock.sleep,
        now=lambda: AT,
    )
    return provider, calls, clock


def test_config_fixed_scope_and_closed_live_gate():
    p = load_policy()
    assert p.live_enabled is False
    assert (
        len(p.securities)
        == len({s.code for s in p.securities})
        == len({s.security_id for s in p.securities})
        == 20
    )
    assert p.from_date == date(2025, 1, 6) and p.to_date == date(2025, 12, 30)
    p.validate_dates(AT)
    assert len(p.sessions()) > 220
    assert all(not s.security_id.startswith("synthetic") for s in p.securities)


def test_normal_20_securities_twice_aggregate_only(policy, environment):
    provider, calls, clock = create(policy, environment)
    result = provider.run()
    report = result.summary()
    provider.close()
    assert report["status"] == "READ_ONLY_COMPLETE"
    assert report["received_rows_per_pass"] == [100, 100]
    assert report["requests"] == report["pages"] == 40
    assert report["strategy_approved"] is False
    assert report["checks"]["repeat_consistency"] == "PASSED"
    assert report["quality_per_pass"][0]["eligible_securities"] == 0
    assert report["quality_per_pass"][0]["unknown_securities"] == 20
    assert clock() == (40 - 1) * INTERVAL
    assert all(
        r.method == "GET" and str(r.url).startswith(ENDPOINT) and r.headers["x-api-key"] == KEY
        for r in calls
    )
    for forbidden in (KEY, "700123", "1234567", "AdjO", "7203", "JPTEST"):
        assert forbidden not in result.aggregate_json
    with pytest.raises(MarketAccessDisabled):
        reject_diagnostic(result)
    with pytest.raises(MarketAccessDisabled):
        reject_diagnostic(report)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_REPOSITORY", "other/repo"),
        ("GITHUB_WORKFLOW_REF", WORKFLOW.replace("jquants-readonly-20", "alpaca-readonly")),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("JQUANTS_RESEARCH_ENVIRONMENT", "alpaca-research"),
        ("JQUANTS_PLAN", "Light"),
        ("JQUANTS_MARKET_DATA_LIVE_ENABLED", "false"),
        ("JQUANTS_TERMS_CONFIRMED", ""),
        ("JQUANTS_API_KEY", ""),
    ],
)
def test_approval_failures_before_transport(policy, environment, key, bad):
    environment[key] = bad
    with pytest.raises(ValueError):
        create(policy, environment)


def test_bad_approval_and_closed_policy(policy, environment):
    for p, approval in (
        (policy, "READ_ONLY_20_NO_STORAGE"),
        (policy.model_copy(update={"live_enabled": False}), APPROVAL),
    ):
        with pytest.raises(ValueError):
            authorize(p, approval, environment, AT)


@pytest.mark.parametrize(
    "changes",
    [
        dict(from_date=date(2023, 1, 1)),
        dict(to_date=date(2026, 10, 20)),
        dict(to_date=date(2026, 7, 29)),
        dict(review_valid_until=date(2026, 10, 20)),
    ],
)
def test_free_window_or_stale_review_blocks(policy, environment, changes):
    with pytest.raises(ValueError):
        create(policy.model_copy(update=changes), environment)


@pytest.mark.parametrize(
    "method,url",
    [
        ("POST", ENDPOINT),
        ("GET", ENDPOINT.replace("api.jquants.com", "evil.invalid")),
        ("GET", ENDPOINT.replace("/equities/bars/daily", "/orders")),
        ("GET", ENDPOINT.replace("https:", "http:")),
        ("GET", ENDPOINT + "?date=2025-01-06"),
    ],
)
def test_guard_rejects_wrong_egress(policy, environment, method, url):
    provider, calls, _ = create(policy, environment)
    request = httpx.Request(method, url, headers={"x-api-key": KEY})
    with pytest.raises(ValueError):
        provider.guard.handle_request(request)
    assert not calls
    provider.close()


@pytest.mark.parametrize("status", [401, 403, 400, 302])
def test_terminal_http_does_not_retry_or_disclose_body(policy, environment, status):
    provider, calls, _ = create(
        policy,
        environment,
        lambda r, n: httpx.Response(
            status, text=KEY + " 700123", headers={"Location": "https://evil.invalid"}
        ),
    )
    report = provider.run().summary()
    provider.close()
    assert len(calls) == 1 and report["status"] == "STOPPED"
    assert report["checks"]["paging"] == ["FAILED", "NOT_RUN"]
    assert report["checks"]["quality"] == ["NOT_RUN", "NOT_RUN"]
    assert KEY not in json.dumps(report) and "700123" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["429", "503", "timeout"])
def test_retry_bound_and_pacing(policy, environment, failure):
    def handler(r, n):
        if failure == "timeout":
            raise httpx.ReadTimeout(KEY)
        return httpx.Response(int(failure), text=KEY, headers={"Retry-After": "999999"})

    provider, calls, clock = create(policy, environment, handler)
    report = provider.run().summary()
    provider.close()
    assert len(calls) == 3 and report["retries"] == 2
    assert clock() >= 2 * INTERVAL and clock() < MAX_SECONDS
    assert report["status"] == "STOPPED" and KEY not in json.dumps(report)


def test_pagination_all_securities_and_repeat(policy, environment):
    def handler(r, n):
        code = r.url.params["code"]
        days = policy.sessions()
        if "pagination_key" not in r.url.params:
            return httpx.Response(
                200, json={"data": [row(code, d) for d in days[:2]], "pagination_key": "next"}
            )
        assert r.url.params["pagination_key"] == "next"
        return httpx.Response(200, json={"data": [row(code, d) for d in days[2:]]})

    provider, calls, _ = create(policy, environment, handler)
    report = provider.run().summary()
    provider.close()
    assert report["status"] == "READ_ONLY_COMPLETE" and len(calls) == 80
    assert report["received_rows_per_pass"] == [100, 100]


@pytest.mark.parametrize(
    "kind",
    ["token_loop", "page_limit", "empty_page", "wrong_code", "holiday", "schema", "future_date"],
)
def test_batch_failures_stop_without_second_pass(policy, environment, kind):
    def handler(r, n):
        values = [row(r.url.params["code"], d) for d in policy.sessions()]
        payload = {"data": values}
        if kind == "token_loop":
            payload["pagination_key"] = "loop"
        elif kind == "page_limit":
            payload["pagination_key"] = str(n)
        elif kind == "empty_page":
            payload = {"data": [], "pagination_key": "next"}
        elif kind == "wrong_code":
            values[0]["Code"] = "00000"
        elif kind == "holiday":
            values[0]["Date"] = "2025-01-11"
        elif kind == "future_date":
            values[0]["Date"] = "2026-10-22"
        else:
            payload = {"data": {"bad": KEY}}
        return httpx.Response(200, json=payload)

    provider, calls, _ = create(policy, environment, handler)
    report = provider.run().summary()
    provider.close()
    assert report["status"] == "STOPPED" and len(calls) <= 2
    assert report["checks"]["paging"][1] == "NOT_RUN"
    assert report["repeat_revised_securities"] is None


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "duplicate",
        "order",
        "invalid",
        "nan",
        "partial_null",
        "all_null",
        "split",
        "reverse_split",
        "rights",
        "unknown",
        "factor",
        "factor_conflict",
        "adjusted",
    ],
)
def test_individual_quality_is_quarantined_and_other_securities_preserved(
    policy, environment, kind
):
    def handler(r, n):
        values = [row(r.url.params["code"], d) for d in policy.sessions()]
        if r.url.params["code"] == policy.securities[0].code:
            if kind == "missing":
                values.pop(0)
            elif kind == "duplicate":
                values.append(values[0])
            elif kind == "order":
                values[0], values[1] = values[1], values[0]
            elif kind == "invalid":
                values[0]["H"] = 1
            elif kind == "nan":
                values[0]["C"] = float("nan")
            elif kind == "partial_null":
                values[0]["O"] = None
            elif kind == "all_null":
                for k in ["O", "H", "L", "C", "Vo", "Va", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo"]:
                    values[0][k] = None
            elif kind in ("split", "reverse_split", "rights"):
                values[1]["ExRT"] = {"split": "1", "reverse_split": "2", "rights": "3"}[kind]
                values[1]["AdjFactor"] = 2 if kind == "reverse_split" else 0.5
            elif kind == "unknown":
                values[1]["AdjFactor"] = 0.6
            elif kind == "factor":
                values[0]["AdjFactor"] = 0
            elif kind == "factor_conflict":
                values[0].update(ExRT="1", AdjFactor=2)
            else:
                values[0]["AdjL"] = float("inf")
        # Use content serialization to permit invalid NaN/Infinity fixtures for validation.
        return httpx.Response(200, content=json.dumps({"data": values}))

    provider, calls, _ = create(policy, environment, handler)
    report = provider.run().summary()
    provider.close()
    assert report["status"] == "STOPPED"
    q = report["quality_per_pass"][0]
    assert (
        q["quarantined_securities"] == 1
        and q["unknown_securities"] == 19
        and q["eligible_securities"] == 0
    )
    assert report["checks"]["paging"] == ["PASSED", "PASSED"]
    assert report["checks"]["repeat_consistency"] == "PASSED"
    assert len(calls) == 40


def test_raw_discontinuity_not_adjusted_away(policy, environment):
    def handler(r, n):
        values = [row(r.url.params["code"], d) for d in policy.sessions()]
        if r.url.params["code"] == policy.securities[0].code:
            for k in ("O", "H", "L", "C"):
                values[2][k] /= 2
            values[2].update(AdjFactor=0.5, ExRT="1")
        return httpx.Response(200, json={"data": values})

    provider, _, _ = create(policy, environment, handler)
    report = provider.run().summary()
    provider.close()
    assert report["quality_per_pass"][0]["discontinuities"] == 2
    assert report["quality_per_pass"][0]["event_candidates"] == {"split": 1}
    assert report["strategy_approved"] is False


def test_zero_volume_alone_is_not_invalid(policy, environment):
    def handler(r, n):
        values = [row(r.url.params["code"], d) for d in policy.sessions()]
        values[0]["Vo"] = values[0]["AdjVo"] = 0
        return httpx.Response(200, json={"data": values})

    provider, _, _ = create(policy, environment, handler)
    report = provider.run().summary()
    provider.close()
    assert report["quality_per_pass"][0]["invalid_rows"] == 0
    assert report["quality_per_pass"][0]["zero_volume_rows"] == 20


def test_revision_is_separate_observation_not_overwrite(policy, environment):
    def handler(r, n):
        values = [row(r.url.params["code"], d) for d in policy.sessions()]
        if n > 20 and r.url.params["code"] == policy.securities[0].code:
            values[0]["Va"] += 1
        return httpx.Response(200, json={"data": values})

    provider, _, _ = create(policy, environment, handler)
    report = provider.run().summary()
    provider.close()
    assert report["repeat_revised_securities"] == 1
    assert report["status"] == "STOPPED" and report["checks"]["repeat_consistency"] == "FAILED"


def test_observation_provenance_and_downstream_rejection(policy):
    day = policy.sessions()[0]
    conditions = RequestConditions("7203", policy.from_date, policy.to_date)
    record = observation(row("7203", day), "7203", policy.sessions(), AT, conditions)
    assert record.available_at == record.fetched_at == AT
    assert record.provider_published_at is None and record.availability_basis == "observed_by"
    assert record.raw is not record.adjusted and record.raw.close == Decimal(700124)
    assert KEY not in repr(record) and "700124" not in repr(record)
    for item in (record, record.raw, record.adjusted):
        with pytest.raises(ValueError):
            reject_diagnostic(item)
    with pytest.raises(ValueError):
        observation(
            row("7203", day),
            "7203",
            policy.sessions(),
            datetime(2025, 1, 6, 0, tzinfo=UTC),
            conditions,
        )


def test_budgets_and_rate(policy, environment):
    provider, calls, clock = create(policy, environment)
    provider.guard.requests = MAX_REQUESTS
    report = provider.run().summary()
    assert not calls and report["reason"] == "REQUEST_BUDGET"
    provider.close()
    provider, calls, clock = create(policy, environment)
    clock.value = MAX_SECONDS
    report = provider.run().summary()
    provider.close()
    assert not calls and report["reason"] == "TIME_BUDGET"


def test_actual_entrypoint_without_approval_never_constructs_transport(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("jq_entry", "scripts/jquants_readonly.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setenv("JQUANTS_API_KEY", KEY)
    monkeypatch.setattr(
        httpx, "HTTPTransport", lambda **kwargs: pytest.fail("network transport created")
    )
    assert entry.main() == 1
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    assert json.loads(output.out)["requests"] == 0


def test_workflow_is_manual_secret_scoped_no_storage():
    text = Path(".github/workflows/jquants-readonly-20.yml").read_text(encoding="utf-8-sig")
    workflow = yaml.safe_load(text)
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_dispatch"}
    assert "jquants-research" in text and "refs/heads/main" in text
    steps = workflow["jobs"]["validate"]["steps"]
    keyed = [s for s in steps if "JQUANTS_API_KEY" in s.get("env", {})]
    assert len(keyed) == 1 and keyed[0]["run"] == "uv run python scripts/jquants_readonly.py"
    assert "enable-cache: false" in text
    for forbidden in ("upload-artifact", "save-cache", "schedule:", "supabase", "gemini"):
        assert (
            forbidden
            not in "\n".join(line for line in text.splitlines() if not line.startswith("#")).lower()
        )


@pytest.mark.parametrize("kind", ["record", "raw", "report", "serialized", "serialized_raw"])
def test_diagnostic_rejected_by_actual_consumers_before_side_effects(policy, environment, kind):
    from dataclasses import asdict
    from unittest.mock import Mock

    from trading_bot.broker.paper import PaperBroker
    from trading_bot.jquants import RequestConditions
    from trading_bot.persistence.research import PrivateResearchStore
    from trading_bot.strategy.gemini import GeminiTradingStrategy
    from trading_bot.swing.observed import ObservedDailyData

    record = observation(
        row("7203", policy.sessions()[0]),
        "7203",
        policy.sessions(),
        AT,
        RequestConditions("7203", policy.from_date, policy.to_date),
    )
    provider, _, _ = create(policy, environment)
    report = provider.run()
    provider.close()
    value = {
        "record": record,
        "raw": record.raw,
        "report": report,
        "serialized": report.summary(),
        "serialized_raw": asdict(record.raw),
    }[kind]
    repository, factory = Mock(), Mock()
    strategy = GeminiTradingStrategy(Mock(), repository, "denied", offline_sdk_factory=factory)
    with pytest.raises(ValueError, match="diagnostic-only"):
        strategy.decide(value)
    factory.assert_not_called()
    repository.reserve.assert_not_called()
    broker = PaperBroker(repository)
    with pytest.raises(ValueError, match="diagnostic-only"):
        broker.place_order(value, "BUY", 100, AT)
    with pytest.raises(ValueError, match="diagnostic-only"):
        broker.execute("id", {"7203": [value]}, None, AT)
    with pytest.raises(ValueError, match="diagnostic-only"):
        broker.value(value, None, AT)
    repository.transaction.assert_not_called()
    with pytest.raises(ValueError, match="diagnostic-only"):
        ObservedDailyData([], value, [], [], None)
    store = Mock()
    with pytest.raises(ValueError, match="diagnostic-only"):
        PrivateResearchStore.commit(store, "id", 0, None, [], {}, value)
    store.api.rpc.assert_not_called()


def test_transient_recovery_and_full_duration_budget(policy, environment):
    def handler(r, n):
        if n == 1:
            return httpx.Response(503, text=KEY)
        return httpx.Response(
            200, json={"data": [row(r.url.params["code"], d) for d in policy.sessions()]}
        )

    provider, calls, clock = create(policy, environment, handler)
    report = provider.run().summary()
    provider.close()
    assert report["status"] == "READ_ONLY_COMPLETE" and len(calls) == 41 and report["retries"] == 1
    assert clock() >= 40 * INTERVAL
    provider, calls, clock = create(policy, environment)
    clock.value = MAX_SECONDS - 1
    provider.guard.last = clock.value
    report = provider.run().summary()
    provider.close()
    assert not calls and report["reason"] == "TIME_BUDGET"


def test_probe_cannot_implicitly_repeat_after_completion(policy, environment):
    provider, calls, _ = create(policy, environment)
    assert provider.run().summary()["status"] == "READ_ONLY_COMPLETE"
    before = len(calls)
    with pytest.raises(ValueError, match="PROBE_ALREADY_USED"):
        provider.run()
    assert len(calls) == before
    provider.close()
