from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from trading_bot.alpaca import AlpacaBatchError, AlpacaDailyProvider, RequestPacer
from trading_bot.alpaca_probe import mock_probe
from trading_bot.config import Instrument
from trading_bot.data_access import MarketAccessDisabled
from trading_bot.provider import DataError, MarketDataProvider

START = datetime(2024, 3, 8, 5, tzinfo=UTC)
END = datetime(2024, 3, 12, 4, tzinfo=UTC)
NOW = datetime(2024, 3, 13, 5, tzinfo=UTC)
STAMPS = ["2024-03-08T05:00:00Z", "2024-03-11T04:00:00Z", "2024-03-12T04:00:00Z"]
INSTRUMENT = Instrument(symbol="AAPL", market="US", currency="USD", display_name="Apple")


def bar(t: str = STAMPS[0], **changes: object) -> dict[str, object]:
    return dict(t=t, o=100, h=102, l=99, c=101, v=0) | changes


def provider(handler, **kwargs):  # type: ignore[no-untyped-def]
    return AlpacaDailyProvider(
        mock_transport=httpx.MockTransport(handler),
        now=lambda: NOW,
        pacer=RequestPacer(sleep=lambda _: None),
        sleep=lambda _: None,
        **kwargs,
    )


def test_live_gate_precedes_credentials(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALPACA_API_KEY_ID", "do-not-send")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "do-not-send")
    with pytest.raises(MarketAccessDisabled):
        AlpacaDailyProvider()
    with pytest.raises(ValueError, match="MockTransport"):
        AlpacaDailyProvider(mock_transport=httpx.HTTPTransport())  # type: ignore[arg-type]


def test_pages_short_pages_dst_and_contract():
    requests = []

    def handler(request):
        requests.append(request)
        index = int(request.url.params.get("page_token", "0"))
        return httpx.Response(
            200,
            json={
                "bars": {"AAPL": [bar(STAMPS[index])]},
                "next_page_token": str(index + 1) if index < 2 else None,
            },
        )

    p: MarketDataProvider = provider(handler)
    rows = p.fetch(INSTRUMENT, START, END, "1d")
    assert len(rows) == 3 and rows[1].timestamp.hour == 4  # DST; no weekend missing.
    assert all(b.source == "alpaca" and b.fetched_at == NOW and b.volume == 0 for b in rows)
    assert len(requests) == 3
    assert all(r.url.params["feed"] == "iex" and r.url.params["asof"] == "-" for r in requests)


@pytest.mark.parametrize("size", [20, 100, 500, 2000])
def test_mock_scale(size):
    result = mock_probe(size, sessions=3)
    assert result["rows"] == size * 3 and result["missing"] == 0
    assert result["requests"] == max(1, size // 100)


def test_twenty_symbol_cold_history_and_idempotent_parquet(tmp_path: Path):
    result = mock_probe(20, root=tmp_path / "new-mock-only")
    assert result["rows"] == result["persisted_rows"] == 4420


@pytest.mark.parametrize(
    "change",
    [
        dict(o=None),
        dict(c=float("nan")),
        dict(h=float("inf")),
        dict(l=-1),
        dict(h=90),
        dict(v=-1),
        dict(t="2024-03-08"),
        dict(o=True),
        dict(c="101"),
    ],
)
def test_invalid_prices_are_not_repaired(change):
    p = provider(
        lambda _: httpx.Response(
            200,
            json={
                "bars": {"AAPL": [bar(**change)]},
                "next_page_token": None,
            },
        )
    )
    # NaN/Inf cannot be encoded as JSON by httpx, so inject malformed Python JSON instead.
    if any(isinstance(v, float) for v in change.values()):
        response = httpx.Response(200)
        response.json = lambda: {"bars": {"AAPL": [bar(**change)]}}
        p.client.close()
        p = provider(lambda _: response)
    with pytest.raises(AlpacaBatchError, match="invalid"):
        p.fetch_many([INSTRUMENT], START, END)


def test_missing_day_and_empty_symbol_fail_closed():
    p = provider(lambda _: httpx.Response(200, json={"bars": {"AAPL": [bar()]}}))
    report = p.fetch_many([INSTRUMENT], START, END)
    assert len(report.missing["AAPL"]) == 2
    with pytest.raises(DataError, match="incomplete"):
        report.require_complete()
    p = provider(lambda _: httpx.Response(200, json={"bars": {}}))
    with pytest.raises(DataError, match="incomplete"):
        p.fetch(INSTRUMENT, START, END, "1d")


@pytest.mark.parametrize("status,attempts", [(401, 1), (403, 1), (429, 3), (503, 3), (302, 1)])
def test_bounded_failure_redacts_credentials(status, attempts, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="offline-mock-secret SERVER BODY",
            headers={
                "Location": "https://example.com/steal",
                "Retry-After": "0",
            },
        )

    with pytest.raises(AlpacaBatchError) as error:
        provider(handler).fetch_many([INSTRUMENT], START, END)
    assert len(calls) == attempts
    assert "offline-mock-secret" not in str(error.value) + caplog.text
    assert "SERVER BODY" not in str(error.value) + caplog.text


def test_timeout_partial_batch_and_request_budget():
    def handler(request):
        if "page_token" in request.url.params:
            raise httpx.ReadTimeout("secret", request=request)
        return httpx.Response(200, json={"bars": {"AAPL": [bar()]}, "next_page_token": "next"})

    with pytest.raises(AlpacaBatchError) as error:
        provider(handler).fetch_many([INSTRUMENT], START, END)
    assert error.value.partial.requests == 4 and len(error.value.partial.bars["AAPL"]) == 1
    assert "secret" not in str(error.value)
    with pytest.raises(AlpacaBatchError, match="budget"):
        provider(handler, max_requests=1).fetch_many([INSTRUMENT], START, END)


@pytest.mark.parametrize(
    "payload",
    [
        {"bars": {"AAPL": [bar(), bar()]}},
        {"bars": {"MSFT": [bar()]}},
        {"bars": []},
        {"bars": {"AAPL": [bar()]}, "next_page_token": 1},
    ],
)
def test_bad_response_rejected(payload):
    with pytest.raises(AlpacaBatchError):
        provider(lambda _: httpx.Response(200, json=payload)).fetch_many([INSTRUMENT], START, END)


def test_looping_token_and_page_limit():
    p = provider(lambda _: httpx.Response(200, json={"bars": {}, "next_page_token": "same"}))
    with pytest.raises(AlpacaBatchError, match="repeated"):
        p.fetch_many([INSTRUMENT], START, END)
    p = provider(
        lambda _: httpx.Response(200, json={"bars": {}, "next_page_token": "next"}), max_pages=1
    )
    with pytest.raises(AlpacaBatchError, match="page budget"):
        p.fetch_many([INSTRUMENT], START, END)


def test_unconfirmed_day_is_withheld_and_not_missing():
    p = provider(lambda _: httpx.Response(200, json={"bars": {"AAPL": [bar(t) for t in STAMPS]}}))
    p.now = lambda: datetime(2024, 3, 12, 23, tzinfo=UTC)
    report = p.fetch_many([INSTRUMENT], START, END).require_complete()
    assert len(report.bars["AAPL"]) == 2 and report.excluded_unconfirmed == 1


def test_pacing_and_retry_after_bound():
    ticks = [0.0]

    def sleep(delay):
        ticks[0] += delay

    pacer = RequestPacer(clock=lambda: ticks[0], sleep=sleep)
    for _ in range(121):
        pacer.wait()
    assert ticks[0] == 60
    with pytest.raises(AlpacaBatchError, match="wait budget"):
        provider(lambda _: httpx.Response(429, headers={"Retry-After": "999999"})).fetch_many(
            [INSTRUMENT], START, END
        )


def test_invalid_request_never_calls_http():
    def handler(_):
        pytest.fail("unexpected HTTP")

    p = provider(handler)
    for items, start, end in [
        ([], START, END),
        ([INSTRUMENT] * 2, START, END),
        ([INSTRUMENT], START.replace(tzinfo=None), END),
        ([INSTRUMENT], END, START),
    ]:
        with pytest.raises(DataError):
            p.fetch_many(items, start, end)
    with pytest.raises(DataError, match="daily"):
        p.fetch(INSTRUMENT, START, END, "1h")


def test_transient_recovery_and_malformed_json():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"bars": {"AAPL": [bar(t) for t in STAMPS]}})

    report = provider(handler).fetch_many([INSTRUMENT], START, END).require_complete()
    assert report.requests == 2 and len(report.bars["AAPL"]) == 3
    with pytest.raises(AlpacaBatchError, match="JSON"):
        provider(lambda _: httpx.Response(200, text="secret broken json")).fetch_many(
            [INSTRUMENT], START, END
        )


def test_second_batch_failure_preserves_first_hundred():
    instruments = [INSTRUMENT.model_copy(update={"symbol": f"S{i:04d}"}) for i in range(101)]

    def handler(request):
        symbols = request.url.params["symbols"].split(",")
        if symbols == ["S0100"]:
            return httpx.Response(403)
        return httpx.Response(200, json={"bars": {s: [bar(t) for t in STAMPS] for s in symbols}})

    with pytest.raises(AlpacaBatchError) as error:
        provider(handler).fetch_many(instruments, START, END)
    assert sum(map(len, error.value.partial.bars.values())) == 300
    assert "S0100" in error.value.partial.missing


def test_full_coverage_before_page_failure_is_still_not_complete():
    def handler(request):
        if "page_token" in request.url.params:
            return httpx.Response(403)
        return httpx.Response(
            200, json={"bars": {"AAPL": [bar(t) for t in STAMPS]}, "next_page_token": "more"}
        )

    with pytest.raises(AlpacaBatchError) as error:
        provider(handler).fetch_many([INSTRUMENT], START, END)
    assert not error.value.partial.missing
    with pytest.raises(DataError, match="incomplete"):
        error.value.partial.require_complete()


def test_holiday_and_short_session_are_not_missing():
    p = provider(
        lambda _: httpx.Response(
            200, json={"bars": {"AAPL": [bar("2024-11-27T05:00:00Z"), bar("2024-11-29T05:00:00Z")]}}
        )
    )
    p.now = lambda: datetime(2024, 12, 2, 6, tzinfo=UTC)
    report = p.fetch_many(
        [INSTRUMENT], datetime(2024, 11, 27, 5, tzinfo=UTC), datetime(2024, 11, 29, 5, tzinfo=UTC)
    ).require_complete()
    assert len(report.expected_sessions) == 2
