"""Phase 6: bounded, transient read-only research, never a trading activation gate."""

import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

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
from trading_bot.research_diagnostics import missing_diagnostics, new_checks
from trading_bot.research_universe import validate_research_symbols

APPROVAL = "READ_ONLY_20_NO_STORAGE"
WORKFLOW_REF = "okiNa4431/ai-trading-bot/.github/workflows/alpaca-readonly.yml@refs/heads/main"


class ResearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    live_enabled: bool = False
    contract_reference: str = ""
    sessions: int = Field(default=221, ge=201, le=221)
    profile: Literal[20, 100] = 20
    symbols: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_symbols(self) -> "ResearchPolicy":
        if (self.profile == 20 and len(self.symbols) > 20) or (
            self.profile == 100 and (len(self.symbols) != 100 or self.sessions != 221)
        ):
            raise ValueError("research profile/symbol count mismatch")
        validate_research_symbols(self.symbols, self.profile)
        return self


def authorize(policy: ResearchPolicy, approval: str, environment: Mapping[str, str]) -> None:
    ResearchPolicy.model_validate(policy.model_dump())
    required = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": WORKFLOW_REF
        if policy.profile == 20
        else WORKFLOW_REF.replace("alpaca-readonly.yml", "alpaca-readonly-100.yml"),
        "GITHUB_REPOSITORY": "okiNa4431/ai-trading-bot",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "ALPACA_RESEARCH_ENVIRONMENT": "alpaca-research",
        "ALPACA_MARKET_DATA_LIVE_ENABLED": "true",
    }
    if not policy.live_enabled or not policy.contract_reference.strip():
        raise MarketAccessDisabled("Phase 6 contract confirmation and reviewed activation pending")
    expected_approval = APPROVAL if policy.profile == 20 else "READ_ONLY_100_NO_STORAGE"
    if approval != expected_approval or any(environment.get(k) != v for k, v in required.items()):
        raise MarketAccessDisabled("Phase 6 requires the approved main read-only manual workflow")


class ReadOnlyTransport(httpx.BaseTransport):
    """Check every request before it reaches the underlying network transport."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        symbols: list[str],
        *,
        limit: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit not in (20, 100) or not 1 <= len(symbols) <= limit:
            raise DataError("invalid read-only symbol scope")
        self.inner, self.symbols = inner, set(symbols)
        self.limit, self.clock, self.deadline = limit, clock, clock() + 300
        self.events: list[dict[str, int | float]] = []

    def check_deadline(self) -> None:
        if self.clock() >= self.deadline:
            raise DataError("read-only elapsed time budget exhausted")

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.check_deadline()
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
            or len(p.get("symbols", "").split(",")) > self.limit
            or request.content
        ):
            raise DataError("Phase 6 blocked non-read-only request")
        if len(self.events) >= 24:
            raise DataError("Phase 6 total request budget exhausted")
        start, status = time.perf_counter(), 0
        try:
            response = self.inner.handle_request(request)
            status = response.status_code
            self.check_deadline()
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
        if mock is None and len(policy.symbols) != policy.profile:
            raise MarketAccessDisabled("live research approval requires the exact symbol count")
        if mock is not None and not isinstance(mock, httpx.MockTransport):
            raise ValueError("research test transport must be MockTransport")
        self.guard = ReadOnlyTransport(
            mock or httpx.HTTPTransport(retries=0), policy.symbols, limit=policy.profile
        )
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
        self.total_pages = self.total_retries = 0
        self.quality: dict[str, int | None] = dict(repeat_revisions=None, discontinuities=None)
        self.checks = new_checks()

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
        self.last_batch = None
        try:
            self.last_batch = super().fetch_many(instruments, start, end)
        except AlpacaBatchError as exc:
            self.last_batch = exc.partial
            raise
        finally:
            if self.last_batch is not None:
                self.total_pages += self.last_batch.pages
                self.total_retries += self.last_batch.retries
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
    paging = ["NOT_RUN", "NOT_RUN"]
    completeness = ["NOT_RUN", "NOT_RUN"]
    provider.checks.update(paging=paging, completeness=completeness)

    def checked_fetch(index: int) -> DailyBatch:
        paging[index] = "FAILED"
        batch = provider.fetch_many(instruments, start, end)
        paging[index] = "PASSED"
        completeness[index] = "FAILED"
        batch.require_complete()
        completeness[index] = "PASSED"
        return batch

    first = checked_fetch(0)
    second = checked_fetch(1)
    if first.excluded_unconfirmed or second.excluded_unconfirmed:
        raise DataError("Phase 6 unconfirmed observations; stop")
    provider.checks["discontinuity"] = "FAILED"
    provider.quality["discontinuities"] = sum(
        abs(b.close / a.close - 1) > 0.35
        for bars in second.bars.values()
        for a, b in zip(bars[:-1], bars[1:], strict=True)
    )
    if provider.quality["discontinuities"]:
        raise DataError("Phase 6 discontinuity; corporate-action review required")
    provider.checks["discontinuity"] = "PASSED"
    # Compare only market observations, never fetched_at (which must advance).
    provider.checks["repeat_consistency"] = "FAILED"
    revisions = sum(
        a.model_dump(exclude={"fetched_at"}) != b.model_dump(exclude={"fetched_at"})
        for s in policy.symbols
        for a, b in zip(first.bars[s], second.bars[s], strict=True)
    )
    provider.quality["repeat_revisions"] = revisions
    if revisions:
        raise DataError("Phase 6 repeated observations changed; manual review required")
    provider.checks["repeat_consistency"] = "PASSED"
    rows = sum(map(len, first.bars.values()))
    stamps = [b.fetched_at for bars in second.bars.values() for b in bars]
    if any(t < at or t > provider.now().astimezone(UTC) for t in stamps):
        raise DataError("Phase 6 invalid observation times")
    provider.guard.check_deadline()
    return dict(
        phase=8 if policy.profile == 100 else 6,
        profile=policy.profile,
        data_kind=provider.data_kind,
        symbol_count=len(instruments),
        sessions=len(days),
        expected_rows=len(instruments) * len(days),
        received_rows_per_pass=rows,
        successful_symbols=len(instruments),
        success_rate=1.0,
        missing_rate=0.0,
        missing_rows=0,
        duplicate_rows=0,
        invalid_rows=0,
        discontinuities=0,
        complete=True,
        repeat_revisions=revisions,
        feed="iex",
        adjustment="raw",
        requests=len(provider.guard.events),
        pages=first.pages + second.pages,
        retries=first.retries + second.retries,
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
        low_iex_volume_rows=sum(b.volume < 100 for bars in second.bars.values() for b in bars),
        excluded_unconfirmed=first.excluded_unconfirmed + second.excluded_unconfirmed,
        checks=provider.checks,
        **missing_diagnostics(second),
    )


def main(*, profile: Literal[20, 100] = 20) -> int:
    # This entry point prints only an allowlisted aggregate on success; no tracebacks on failure.
    started = time.perf_counter()
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    provider: ResearchProvider | None = None
    try:
        policy = ResearchPolicy.model_validate(
            yaml.safe_load(
                Path(
                    "config/alpaca-readonly.yaml"
                    if profile == 20
                    else "config/alpaca-readonly-100.yaml"
                ).read_text(encoding="utf-8")
            )
        )
        if policy.profile != profile:
            raise MarketAccessDisabled("entry point/profile mismatch")
        provider = ResearchProvider(
            policy, os.environ.get("ALPACA_READONLY_APPROVAL", ""), os.environ
        )
        print(json.dumps(run_probe(policy, provider), sort_keys=True))
        return 0
    except Exception:
        events = provider.guard.events if provider is not None else []
        batch = provider.last_batch if provider is not None else None
        expected = len(batch.bars) * len(batch.expected_sessions) if batch else 0
        missing = sum(map(len, batch.missing.values())) if batch else 0
        print(
            json.dumps(
                dict(
                    phase=8 if profile == 100 else 6,
                    profile=profile,
                    status="STOPPED",
                    reason="authorization_or_data_quality_failure",
                    requests=len(events),
                    pages=provider.total_pages if provider else 0,
                    retries=provider.total_retries if provider else 0,
                    total_seconds=round(time.perf_counter() - started, 3),
                    last_pass_pages=batch.pages if batch else 0,
                    last_pass_retries=batch.retries if batch else 0,
                    http_status_counts=dict(Counter(str(e["status"]) for e in events)),
                    received_rows=sum(map(len, batch.bars.values())) if batch else 0,
                    incomplete_symbols=len(batch.missing) if batch else None,
                    symbol_count=len(batch.bars) if batch else None,
                    sessions=len(batch.expected_sessions) if batch else None,
                    expected_rows=expected,
                    missing_rows=missing,
                    missing_rate=missing / expected if expected else None,
                    successful_symbols=len(batch.bars) - len(batch.missing) if batch else 0,
                    empty_symbols=sum(not bars for bars in batch.bars.values()) if batch else 0,
                    duplicate_rows=batch.duplicate_rows if batch else 0,
                    invalid_rows=batch.invalid_rows if batch else 0,
                    complete=False,
                    checks=provider.checks if provider else new_checks(),
                    **missing_diagnostics(batch),
                    **(provider.quality if provider else {}),
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
