"""Phase 6: bounded, transient read-only research, never a trading activation gate."""

import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandas_market_calendars as mcal
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_bot.alpaca import (
    NY,
    AlpacaBatchError,
    AlpacaDailyProvider,
    DailyBatch,
    RequestPacer,
    aware,
    confirmed_at,
)
from trading_bot.config import Instrument
from trading_bot.data_access import MarketAccessDisabled
from trading_bot.provider import DataError

APPROVAL = "READ_ONLY_20_NO_STORAGE"
WORKFLOW_REF = "okiNa4431/ai-trading-bot/.github/workflows/alpaca-readonly.yml@refs/heads/main"


class ResearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    live_enabled: bool = False
    contract_reference: str = ""
    sessions: int = Field(default=221, ge=201, le=221)
    symbols: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid_symbols(self) -> "ResearchPolicy":
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("duplicate research symbols")
        for symbol in self.symbols:
            Instrument(symbol=symbol, market="US", currency="USD", display_name=symbol)
            if not symbol.isalpha() or not symbol.isascii():
                raise ValueError("research requires reviewed plain US symbols")
        return self


def authorize(policy: ResearchPolicy, approval: str, environment: Mapping[str, str]) -> None:
    required = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
        "GITHUB_REPOSITORY": "okiNa4431/ai-trading-bot",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "ALPACA_RESEARCH_ENVIRONMENT": "alpaca-research",
    }
    if not policy.live_enabled or not policy.contract_reference.strip():
        raise MarketAccessDisabled("Phase 6 contract confirmation and reviewed activation pending")
    if approval != APPROVAL or any(environment.get(k) != v for k, v in required.items()):
        raise MarketAccessDisabled("Phase 6 requires the approved main read-only manual workflow")


class ReadOnlyTransport(httpx.BaseTransport):
    """Check every request before it reaches the underlying network transport."""

    def __init__(self, inner: httpx.BaseTransport, symbols: list[str]) -> None:
        self.inner, self.symbols = inner, set(symbols)
        self.events: list[dict[str, int | float]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        u, p = request.url, request.url.params
        required = {
            "feed": "iex",
            "adjustment": "raw",
            "timeframe": "1Day",
            "currency": "USD",
            "asof": "-",
            "limit": "10000",
            "sort": "asc",
        }
        if (
            request.method != "GET"
            or u.scheme != "https"
            or u.host != "data.alpaca.markets"
            or u.port not in (None, 443)
            or u.path != "/v2/stocks/bars"
            or u.username
            or u.password
            or u.fragment
            or any(p.get(k) != v for k, v in required.items())
            or set(p) - (set(required) | {"symbols", "start", "end", "page_token"})
            or len(p.multi_items()) != len(p)
            or set(p.get("symbols", "").split(",")) != self.symbols
            or len(p.get("symbols", "").split(",")) != len(self.symbols)
            or len(p.get("symbols", "").split(",")) > 20
            or request.content
        ):
            raise DataError("Phase 6 blocked non-read-only request")
        if len(self.events) >= 24:
            raise DataError("Phase 6 total request budget exhausted")
        start, status = time.perf_counter(), 0
        try:
            response = self.inner.handle_request(request)
            status = response.status_code
            return response
        finally:
            # No URL, body, headers, exception message or ticker-level values.
            self.events.append(dict(status=status, seconds=round(time.perf_counter() - start, 4)))

    def close(self) -> None:
        self.inner.close()


class ResearchProvider(AlpacaDailyProvider):
    def __init__(
        self,
        policy: ResearchPolicy,
        approval: str,
        environment: Mapping[str, str],
        *,
        mock: httpx.MockTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        authorize(policy, approval, environment)
        key, secret = environment.get("ALPACA_API_KEY_ID"), environment.get("ALPACA_API_SECRET_KEY")
        if not key or not secret:
            raise MarketAccessDisabled("Phase 6 Environment Secrets are missing")
        if mock is not None and not isinstance(mock, httpx.MockTransport):
            raise ValueError("research test transport must be MockTransport")
        self.guard = ReadOnlyTransport(mock or httpx.HTTPTransport(retries=0), policy.symbols)
        self._initialize(
            key,
            secret,
            self.guard,
            now,
            RequestPacer(sleep=lambda _: None) if mock else None,
            (lambda _: None) if mock else time.sleep,
            12,
            4,
            2,
            "mock" if mock else "real",
        )
        self.allowed_symbols = tuple(policy.symbols)
        self.session_count = policy.sessions
        self.last_batch: DailyBatch | None = None

    def fetch_many(
        self,
        instruments: Sequence[Instrument],
        start: datetime,
        end: datetime,
    ) -> DailyBatch:
        if tuple(i.symbol for i in instruments) != self.allowed_symbols:
            raise DataError("Phase 6 symbol list differs from reviewed configuration")
        sessions = mcal.get_calendar("NYSE").schedule(
            start_date=aware(start).astimezone(NY).date(),
            end_date=aware(end).astimezone(NY).date(),
        )
        if len(sessions) != self.session_count:
            raise DataError("Phase 6 requires the reviewed 201-221 session window")
        try:
            self.last_batch = super().fetch_many(instruments, start, end)
        except AlpacaBatchError as exc:
            self.last_batch = exc.partial
            raise
        return self.last_batch


def run_probe(policy: ResearchPolicy, provider: ResearchProvider) -> dict[str, object]:
    at = aware(provider.now())
    calendar = mcal.get_calendar("NYSE").schedule(
        start_date=(at - timedelta(days=500)).date(),
        end_date=at.date(),
    )
    days = [d.date() for d in calendar.index if confirmed_at(d.date()) <= at][-policy.sessions :]
    if len(days) != policy.sessions:
        raise DataError("Phase 6 insufficient completed sessions")
    start = datetime.combine(days[0], datetime.min.time(), NY)
    end = datetime.combine(days[-1], datetime.min.time(), NY)
    instruments = [
        Instrument(symbol=s, market="US", currency="USD", display_name=s) for s in policy.symbols
    ]
    begin = time.perf_counter()
    first = provider.fetch_many(instruments, start, end).require_complete()
    second = provider.fetch_many(instruments, start, end).require_complete()
    if any(
        abs(b.close / a.close - 1) > 0.35
        for bars in second.bars.values()
        for a, b in zip(bars[:-1], bars[1:], strict=True)
    ):
        raise DataError("Phase 6 discontinuity; corporate-action review required")
    # Compare only market observations, never fetched_at (which must advance).
    revisions = sum(
        a.model_dump(exclude={"fetched_at"}) != b.model_dump(exclude={"fetched_at"})
        for s in policy.symbols
        for a, b in zip(first.bars[s], second.bars[s], strict=True)
    )
    if revisions:
        raise DataError("Phase 6 repeated observations changed; manual review required")
    rows = sum(map(len, first.bars.values()))
    stamps = [b.fetched_at for bars in second.bars.values() for b in bars]
    if any(t < at or t > provider.now().astimezone(UTC) for t in stamps):
        raise DataError("Phase 6 invalid observation times")
    return dict(
        phase=6,
        data_kind=provider.data_kind,
        symbol_count=len(instruments),
        sessions=len(days),
        expected_rows=len(instruments) * len(days),
        received_rows_per_pass=rows,
        successful_symbols=len(instruments),
        success_rate=1.0,
        missing_rate=0.0,
        repeat_revisions=revisions,
        feed="iex",
        adjustment="raw",
        requests=len(provider.guard.events),
        http_status_counts=dict(Counter(str(e["status"]) for e in provider.guard.events)),
        http_response_seconds=[e["seconds"] for e in provider.guard.events],
        total_seconds=round(time.perf_counter() - begin, 3),
        observed_at_min=min(stamps).isoformat(),
        observed_at_max=max(stamps).isoformat(),
        first_session=days[0].isoformat(),
        last_session=days[-1].isoformat(),
        confirmation_cutoff_utc=confirmed_at(days[-1]).isoformat(),
        strategy_ready=False,
        corporate_actions_verified=False,
        full_market_volume=False,
        retained_market_data=False,
        zero_volume_rows=sum(b.volume == 0 for bars in second.bars.values() for b in bars),
        excluded_unconfirmed=first.excluded_unconfirmed + second.excluded_unconfirmed,
    )


def main() -> int:
    # This entry point prints only an allowlisted aggregate on success; no tracebacks on failure.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    provider: ResearchProvider | None = None
    try:
        policy = ResearchPolicy.model_validate(
            yaml.safe_load(Path("config/alpaca-readonly.yaml").read_text(encoding="utf-8"))
        )
        provider = ResearchProvider(
            policy, os.environ.get("ALPACA_READONLY_APPROVAL", ""), os.environ
        )
        print(json.dumps(run_probe(policy, provider), sort_keys=True))
        return 0
    except Exception:
        events = provider.guard.events if provider is not None else []
        batch = provider.last_batch if provider is not None else None
        print(
            json.dumps(
                dict(
                    phase=6,
                    status="STOPPED",
                    reason="authorization_or_data_quality_failure",
                    requests=len(events),
                    http_status_counts=dict(Counter(str(e["status"]) for e in events)),
                    received_rows=sum(map(len, batch.bars.values())) if batch else 0,
                    incomplete_symbols=len(batch.missing) if batch else None,
                )
            )
        )
        return 1
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass  # Cleanup must never disclose request objects or credentials.
            provider.last_batch = None
        logging.disable(previous_logging)
