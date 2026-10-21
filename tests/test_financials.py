"""JP-3: explicitly synthetic internal contracts, never vendor response fixtures."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import jsonschema
import pytest
from pydantic import ValidationError

from tests.test_jquants import Clock
from trading_bot.data_access import MarketAccessDisabled, reject_diagnostic
from trading_bot.edinetdb import EdinetDBProvider, FixtureFinancialProvider, main
from trading_bot.financials import (
    AccountingPeriod,
    FinancialStatement,
    FinancialStop,
    FinancialValue,
    Issuer,
    IssuerLink,
    IssuerName,
    IssuerRegistry,
    SyntheticFinancialInput,
    revenue_growth,
    select_as_of,
    synthetic_input,
)
from trading_bot.security_quality import ReviewFact

AT = datetime(2025, 6, 30, tzinfo=UTC)
START, END = date(2024, 4, 1), date(2025, 3, 31)


def statement(**changes):
    values = dict(
        data_kind="SYNTHETIC",
        issuer_id="synthetic:issuer:01",
        document_id="synthetic-doc-1",
        revision_id="r1",
        period=dict(start=START, end=END, kind="ANNUAL"),
        standard="JGAAP",
        basis="CONSOLIDATED",
        publication_precision="INSTANT",
        fact_kind="ACTUAL",
        currency="JPY",
        published_at=AT - timedelta(days=5),
        retrieved_at=AT,
        revision_available_at=AT - timedelta(days=5),
        version_evidence="SYNTHETIC",
        evidence_ids=("synthetic://filing/1",),
        source="synthetic://edinetdb-contract",
        usage_permission="SYNTHETIC_ONLY",
        values=[
            dict(metric="revenue", value="100000", unit="CURRENCY"),
            dict(
                metric="operating_income",
                value=None,
                unit="CURRENCY",
                missing_reason="NOT_DISCLOSED",
            ),
        ],
    )
    values.update(changes)
    return FinancialStatement.model_validate(values)


def registry(**changes):
    fact = ReviewFact(
        status="PASSED",
        observed_at=AT,
        coverage_start=START,
        coverage_end=AT.date(),
        sources=("synthetic://identity",),
    )
    values = dict(
        issuers=(Issuer(issuer_id="synthetic:issuer:01"),),
        links=(
            IssuerLink(
                security_id="synthetic:jp:0001",
                issuer_id="synthetic:issuer:01",
                valid_from=START,
                evidence=fact,
            ),
        ),
    )
    values.update(changes)
    return IssuerRegistry(**values)


def provider(handler, **kw):
    clock = Clock()
    instance = FixtureFinancialProvider(
        httpx.MockTransport(handler), clock=clock, sleep=clock.sleep, **kw
    )
    return instance, clock


def payload(row=None, next_page=None):
    return {"records": [(row or statement()).model_dump(mode="json")], "next_page": next_page}


def test_mock_normal_20_issuers_no_data_output(capsys):
    def response(request):
        assert request.method == "GET" and request.url.host == "fixture.invalid"
        return httpx.Response(200, json=payload(statement(issuer_id=request.url.params["issuer"])))

    client, clock = provider(response)
    try:
        for index in range(20):
            assert len(client.fetch(f"synthetic:issuer:{index:02}")) == 1
        assert client.requests == client.pages == 20 and clock.value >= 19
        summary = client.summary().summary()
        assert summary["data_kind"] == "SYNTHETIC" and not summary["strategy_approved"]
        assert "100000" not in json.dumps(summary) and "synthetic-doc" not in json.dumps(summary)
        with pytest.raises(MarketAccessDisabled):
            reject_diagnostic(client.summary())
        assert capsys.readouterr().out == ""
    finally:
        client.close()


@pytest.mark.parametrize("status", [401, 403, 429, 400, 302])
def test_auth_quota_and_other_status_fail_closed(status):
    client, _ = provider(lambda r: httpx.Response(status, text="private-response-do-not-log"))
    try:
        with pytest.raises(FinancialStop) as error:
            client.fetch("synthetic:issuer:01")
        assert client.requests == 1
        assert "private-response" not in str(error.value)
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["timeout", "500", "503"])
def test_bounded_retry(failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private-response", request=request)
        return httpx.Response(int(failure), text="private-response")

    client, _ = provider(respond)
    try:
        with pytest.raises(FinancialStop, match="RETRY_EXHAUSTED"):
            client.fetch("synthetic:issuer:01")
        assert client.requests == 3 and client.retries == 2
    finally:
        client.close()


def test_transient_recovery_and_remaining_free_budget():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=payload())

    client, _ = provider(respond, remaining_daily=2)
    try:
        assert len(client.fetch("synthetic:issuer:01")) == 1
        with pytest.raises(FinancialStop, match="QUOTA"):
            client.fetch("synthetic:issuer:01")
        assert len(calls) == 2
    finally:
        client.close()


def test_paging_complete_and_duplicate_token_or_row_rejected():
    first, second = statement(), statement(revision_id="r2", supersedes_revision="r1")
    client, _ = provider(
        lambda request: httpx.Response(
            200,
            json=(payload(first, "next") if not request.url.params["page"] else payload(second)),
        )
    )
    try:
        assert len(client.fetch(first.issuer_id)) == 2 and client.pages == 2
    finally:
        client.close()
    for repeated_token in (True, False):
        client, _ = provider(
            lambda r, repeated_token=repeated_token: httpx.Response(
                200,
                json=payload(first, "next" if repeated_token or not r.url.params["page"] else None),
            )
        )
        try:
            with pytest.raises(FinancialStop):
                client.fetch(first.issuer_id)
        finally:
            client.close()


@pytest.mark.parametrize(
    "body", [{}, {"records": [], "next_page": None}, {"records": "invalid", "next_page": None}]
)
def test_incomplete_payload_rejected(body):
    client, _ = provider(lambda r: httpx.Response(200, json=body))
    try:
        with pytest.raises(FinancialStop):
            client.fetch("synthetic:issuer:01")
    finally:
        client.close()


def test_deadline_and_real_transport_rejected():
    with pytest.raises(FinancialStop):
        FixtureFinancialProvider(object())
    client, clock = provider(lambda r: httpx.Response(200, json=payload()))
    clock.value = 120
    try:
        with pytest.raises(FinancialStop):
            client.fetch("synthetic:issuer:01")
        assert client.requests == 0
    finally:
        client.close()


def test_identity_many_securities_one_issuer_and_unknown():
    r = registry()
    assert r.resolve("synthetic:jp:0001", AT.date(), AT) == "synthetic:issuer:01"
    assert r.resolve("unknown", AT.date(), AT) is None
    assert r.resolve("synthetic:jp:0001", AT.date(), AT - timedelta(days=1)) is None
    second = r.links[0].model_copy(update={"security_id": "synthetic:jp:0002"})
    assert len(registry(links=(*r.links, second)).links) == 2
    with pytest.raises(ValidationError, match="IDENTITY_OR_NAME_OVERLAP"):
        registry(links=(*r.links, r.links[0]))
    unknown = r.links[0].model_copy(
        update={"evidence": r.links[0].evidence.model_copy(update={"status": "UNKNOWN"})}
    )
    assert registry(links=(unknown,)).resolve("synthetic:jp:0001", AT.date(), AT) is None


def test_issuer_name_change_retains_document_name():
    r = registry()
    old = IssuerName(
        issuer_id=r.issuers[0].issuer_id,
        name="Synthetic Old",
        valid_from=START,
        valid_until=date(2025, 1, 31),
        evidence=r.links[0].evidence,
    )
    new = old.model_copy(
        update={"name": "Synthetic New", "valid_from": date(2025, 2, 1), "valid_until": None}
    )
    updated = registry(names=(old, new))
    assert updated.names[0].name != updated.names[1].name
    assert (
        statement(issuer_name_at_document="Synthetic Old").issuer_name_at_document
        == "Synthetic Old"
    )


def test_point_in_time_unknown_publication_and_postdated_retrieval():
    row = statement()
    assert row.known_at(AT)
    assert not row.known_at(AT - timedelta(seconds=1))
    assert not statement(published_at=None).known_at(AT)
    assert not statement(version_evidence="UNKNOWN").known_at(AT)
    with pytest.raises(ValidationError, match="FUTURE_PUBLICATION_OR_DELIVERY"):
        statement(published_at=AT + timedelta(days=1))
    with pytest.raises(ValueError):
        statement(retrieved_at=AT.replace(tzinfo=None))


def test_correction_not_used_before_observed_and_original_not_overwritten():
    original = statement()
    correction = statement(
        revision_id="r2",
        supersedes_revision="r1",
        retrieved_at=AT + timedelta(days=2),
        published_at=AT + timedelta(days=1),
        revision_available_at=AT + timedelta(days=1),
    )
    rows = original, correction
    assert select_as_of(rows, original.issuer_id, original.period, AT) == original
    assert (
        select_as_of(rows, original.issuer_id, original.period, AT + timedelta(days=2))
        == correction
    )
    assert original.revision_id == "r1"
    unknown = statement(revision_id="r3", supersedes_revision="r1", published_at=None)
    assert select_as_of((original, unknown), original.issuer_id, original.period, AT) is None
    with pytest.raises(FinancialStop):
        select_as_of((original, original), original.issuer_id, original.period, AT)


@pytest.mark.parametrize(
    "difference", ["standard", "basis", "currency", "quarter", "period", "unit"]
)
def test_context_mismatch_not_silently_converted(difference):
    current = statement()
    prior = statement(period=dict(start=date(2023, 4, 1), end=date(2024, 3, 31), kind="ANNUAL"))
    changes = {
        "standard": {"standard": "IFRS"},
        "basis": {"basis": "STANDALONE"},
        "currency": {"currency": "USD"},
        "quarter": {
            "period": AccountingPeriod(
                start=date(2023, 4, 1), end=date(2023, 6, 30), kind="QUARTER_YTD", quarter=1
            )
        },
        "period": {"period": AccountingPeriod(start=START, end=END, kind="ANNUAL")},
        "unit": {
            "values": (
                FinancialValue(metric="revenue", value=Decimal(100), unit="MILLION_CURRENCY"),
            )
        },
    }
    prior = prior.model_copy(update=changes[difference])
    with pytest.raises(FinancialStop):
        revenue_growth(current, prior)


def test_growth_and_missing_values_are_explicit():
    current = statement()
    prior = statement(period=dict(start=date(2023, 4, 1), end=date(2024, 3, 31), kind="ANNUAL"))
    result = revenue_growth(current, prior)
    assert result.value == 0 and result.origin == "INTERNAL_DERIVED" and result.formula
    assert current.metric("operating_income").value is None
    assert current.metric("roe") is None
    missing = prior.model_copy(update={"values": ()})
    assert revenue_growth(current, missing) is None
    with pytest.raises(ValidationError, match="MISSING_REASON_REQUIRED"):
        FinancialValue(metric="revenue", value=None, unit="CURRENCY")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_rejected(bad):
    with pytest.raises(ValidationError):
        FinancialValue(metric="revenue", value=Decimal(bad), unit="CURRENCY")


def test_synthetic_schema_and_real_financials_blocked_before_json():
    row = statement()
    result = synthetic_input("synthetic:jp:0001", row, AT, registry())
    jsonschema.validate(result.model_dump(mode="json"), SyntheticFinancialInput.model_json_schema())
    assert "operating_income" in result.uncertainty and "roe" in result.uncertainty
    real = statement(
        data_kind="REAL",
        issuer_id="real-issuer",
        source="edinetdb",
        version_evidence="UNKNOWN",
        usage_permission="PRIVATE_READ_ONLY",
    )
    with pytest.raises(MarketAccessDisabled):
        synthetic_input("synthetic:jp:0001", real, AT, registry())
    for value in (real, real.model_dump(), {"nested": [real]}):
        with pytest.raises(MarketAccessDisabled):
            reject_diagnostic(value)


def test_live_adapter_and_diagnose_never_construct_http(monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: pytest.fail("HTTP client"))
    monkeypatch.setenv("JQUANTS_LOCAL_LIVE_ENABLED", "true")
    monkeypatch.setenv("EDINETDB_LIVE_ENABLED", "true")
    with pytest.raises(MarketAccessDisabled):
        EdinetDBProvider().fetch("E00000")
    assert main(["--diagnose"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["requests"] == 0 and report["target_count"] == 20
    assert not report["external_ai"] and report["issuer_mapping"] == "UNKNOWN"
    assert main([]) == 1
    assert main(["--live"]) == 1


@pytest.mark.parametrize("serialized", [True, False])
def test_actual_ai_broker_and_storage_refuse_financial_diagnostics(serialized):
    from unittest.mock import Mock

    from trading_bot.broker.paper import PaperBroker
    from trading_bot.persistence.research import PrivateResearchStore
    from trading_bot.strategy.gemini import GeminiTradingStrategy

    row = statement(
        data_kind="REAL",
        issuer_id="real-issuer",
        source="edinetdb",
        version_evidence="UNKNOWN",
        usage_permission="PRIVATE_READ_ONLY",
    )
    value = row.model_dump() if serialized else row
    repository, factory = Mock(), Mock()
    strategy = GeminiTradingStrategy(Mock(), repository, "denied", offline_sdk_factory=factory)
    with pytest.raises(MarketAccessDisabled, match="diagnostic-only"):
        strategy.decide(value)
    factory.assert_not_called()
    repository.reserve.assert_not_called()
    broker = PaperBroker(repository)
    for action in (
        lambda: broker.place_order(value, "BUY", 100, AT),
        lambda: broker.execute("id", {"7203": [value]}, None, AT),
        lambda: broker.value(value, None, AT),
    ):
        with pytest.raises(MarketAccessDisabled, match="diagnostic-only"):
            action()
    repository.transaction.assert_not_called()
    store = Mock()
    with pytest.raises(MarketAccessDisabled, match="diagnostic-only"):
        PrivateResearchStore.commit(store, "id", 0, None, [], {}, value)
    store.api.rpc.assert_not_called()
    assert "100000" not in repr(row) and "100000" not in str(row)


def test_ambiguous_versions_and_date_only_are_unknown():
    original = statement()
    changed = statement(
        values=[dict(metric="revenue", value="999", unit="CURRENCY")],
        retrieved_at=AT + timedelta(days=1),
    )
    with pytest.raises(FinancialStop, match="REVISION_CONTENT_CONFLICT"):
        select_as_of(
            (original, changed), original.issuer_id, original.period, AT + timedelta(days=1)
        )
    date_only = statement(
        published_at=None, publication_precision="DATE_ONLY", publication_date=AT.date()
    )
    assert not date_only.known_at(AT)
    forecast = statement(fact_kind="FORECAST")
    assert not forecast.known_at(AT)
    unrelated = statement(issuer_id="synthetic:issuer:other")
    with pytest.raises(FinancialStop, match="ISSUER_MISMATCH"):
        revenue_growth(original, unrelated)


def test_real_observations_cannot_be_loaded_by_fixture_transport():
    real = statement(
        data_kind="REAL",
        issuer_id="synthetic:issuer:01",
        source="edinetdb",
        version_evidence="UNKNOWN",
        usage_permission="PRIVATE_READ_ONLY",
    )
    client, _ = provider(lambda r: httpx.Response(200, json=payload(real)))
    try:
        with pytest.raises(FinancialStop, match="PROVENANCE_STOP"):
            client.fetch(real.issuer_id)
    finally:
        client.close()
