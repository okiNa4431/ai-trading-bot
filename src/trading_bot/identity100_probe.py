"""Evidence-gated, memory-only 100-security history validation."""

import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo

import httpx
import pandas_market_calendars as mcal

from trading_bot.alpaca import (
    NY,
    AlpacaBatchError,
    AlpacaDailyProvider,
    RequestPacer,
    aware,
    confirmed_at,
)
from trading_bot.alpaca_research import WORKFLOW_REF, ReadOnlyTransport
from trading_bot.config import Instrument
from trading_bot.data_access import MarketAccessDisabled
from trading_bot.identity_catalog import IdentityCatalog, load_catalog
from trading_bot.provider import DataError
from trading_bot.research_diagnostics import new_checks
from trading_bot.security_identity import identify_batch

APPROVAL = "READ_ONLY_100_IDENTITIES_NO_STORAGE"
WORKFLOW = WORKFLOW_REF.replace("alpaca-readonly.yml", "alpaca-identities-100.yml")


def session_window(at: datetime) -> tuple[date, ...]:
    at = aware(at)
    schedule = mcal.get_calendar("NYSE").schedule((at - timedelta(days=500)).date(), at.date())
    days = tuple(d.date() for d in schedule.index if confirmed_at(d.date()) <= at)[-221:]
    if len(days) != 221:
        raise DataError("insufficient confirmed sessions")
    return days


class IdentityBatchTransport(httpx.BaseTransport):
    """Reuse the original GET guard per exact chunk; share its 24-request/300s ceiling."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        aliases: tuple[str, ...],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(aliases) != 101 or len(set(aliases)) != 101:
            raise DataError("identity alias scope mismatch")
        self.inner = inner
        self.events: list[dict[str, int | float]] = []
        self.guards = {}
        self.budgets = {}
        for offset in range(0, len(aliases), 100):
            chunk = aliases[offset : offset + 100]
            guard = ReadOnlyTransport(inner, list(chunk), limit=100, clock=clock)
            guard.events = self.events
            self.guards[chunk] = guard
            # 10,000 bars/page for ALL symbols, plus one short-page allowance per chunk.
            self.budgets[chunk] = ceil(len(chunk) * 221 / 10000) + 1
        deadline = min(g.deadline for g in self.guards.values())
        for guard in self.guards.values():
            guard.deadline = deadline
        self.begin_pass()

    def begin_pass(self) -> None:
        self.pages = dict.fromkeys(self.guards, 0)

    def check_deadline(self) -> None:
        next(iter(self.guards.values())).check_deadline()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        chunk = tuple(request.url.params.get("symbols", "").split(","))
        if chunk not in self.guards or self.pages[chunk] >= self.budgets[chunk]:
            raise DataError("unapproved chunk or page budget exhausted")
        response = self.guards[chunk].handle_request(request)
        if response.status_code == 200:
            self.pages[chunk] += 1
        return response

    def close(self) -> None:
        self.inner.close()


def authorize(approval: str, env: Mapping[str, str]) -> None:
    required = dict(
        GITHUB_ACTIONS="true",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/main",
        GITHUB_WORKFLOW_REF=WORKFLOW,
        GITHUB_REPOSITORY="okiNa4431/ai-trading-bot",
        RUNNER_ENVIRONMENT="github-hosted",
        ALPACA_RESEARCH_ENVIRONMENT="alpaca-research",
        ALPACA_MARKET_DATA_LIVE_ENABLED="true",
        ALPACA_100_IDENTITIES_LIVE_ENABLED="true",
    )
    if approval != APPROVAL or any(env.get(k) != v for k, v in required.items()):
        raise MarketAccessDisabled("separate 100-security approval required")


class _ReviewedProvider(AlpacaDailyProvider):
    """Internal setup used only after approval and catalog evidence checks."""

    def __init__(
        self,
        key: str,
        secret: str,
        guard: IdentityBatchTransport,
        now: Callable[[], datetime],
        offline: bool,
    ) -> None:
        self._initialize(
            key,
            secret,
            guard,
            now,
            RequestPacer(sleep=lambda _: None) if offline else None,
            (lambda _: None) if offline else time.sleep,
            12,
            4,
            2,
            "mock" if offline else "real",
        )


def run(
    approval: str,
    env: Mapping[str, str],
    *,
    catalog: IdentityCatalog | None = None,
    mock: httpx.MockTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[int, dict[str, object]]:
    started = time.perf_counter()
    checks = new_checks()
    checks["evidence"] = "NOT_RUN"
    result: dict[str, object] = dict(
        security_count=100,
        alias_count=101,
        sessions=221,
        expected_rows=22100,
        received_rows_per_pass=None,
        complete=False,
        missing_rows=None,
        duplicate_rows=None,
        invalid_rows=None,
        repeat_revisions=None,
        discontinuities=None,
        checks=checks,
    )
    provider = None
    guard = None
    last = None
    identity_stats: dict[str, int] = {}
    totals = dict(pages=0, retries=0)
    try:
        authorize(approval, env)
        checks["evidence"] = "FAILED"
        catalog = catalog or load_catalog()
        at = aware(now())
        days = session_window(at)
        result.update(catalog.summary(set(days)))
        registry = catalog.require_reviewed(set(days), at.astimezone(ZoneInfo("Asia/Tokyo")).date())
        checks["evidence"] = "PASSED"
        key, secret = env.get("ALPACA_API_KEY_ID"), env.get("ALPACA_API_SECRET_KEY")
        if not key or not secret:
            raise MarketAccessDisabled("missing Environment Secrets")
        if mock is not None and not isinstance(mock, httpx.MockTransport):
            raise ValueError("only offline MockTransport allowed")
        aliases = tuple(a.ticker for a in registry.assignments)
        guard = IdentityBatchTransport(mock or httpx.HTTPTransport(retries=0), aliases)
        provider = _ReviewedProvider(key, secret, guard, now, mock is not None)
        result["data_kind"] = provider.data_kind
        start, end = (datetime.combine(d, datetime.min.time(), NY) for d in (days[0], days[-1]))
        instruments = [
            Instrument(symbol=s, market="US", currency="USD", display_name=s) for s in aliases
        ]
        observations = []
        paging, completeness = ["NOT_RUN", "NOT_RUN"], ["NOT_RUN", "NOT_RUN"]
        checks.update(paging=paging, completeness=completeness)
        for index in range(2):
            guard.begin_pass()
            last = None
            identity_stats = {}
            paging[index] = "FAILED"
            try:
                last = provider.fetch_many(instruments, start, end)
            except AlpacaBatchError as exc:
                last = exc.partial
                raise
            finally:
                if last is not None:
                    totals["pages"] += last.pages
                    totals["retries"] += last.retries
            paging[index] = "PASSED"
            completeness[index] = "FAILED"
            observations.append(identify_batch(registry, last, diagnostics=identity_stats))
            completeness[index] = "PASSED"
            result["received_rows_per_pass"] = len(observations[-1])
        first, second = observations
        checks["repeat_consistency"] = "FAILED"
        revisions = sum(
            a.security_id != b.security_id
            or a.raw.model_dump(exclude={"fetched_at"}) != b.raw.model_dump(exclude={"fetched_at"})
            for a, b in zip(first, second, strict=True)
        )
        result["repeat_revisions"] = revisions
        if revisions:
            raise DataError("repeated observations changed")
        checks["repeat_consistency"] = "PASSED"
        checks["discontinuity"] = "FAILED"
        discontinuities = sum(
            a.security_id == b.security_id and abs(b.raw.close / a.raw.close - 1) > 0.35
            for a, b in zip(second[:-1], second[1:], strict=True)
        )
        result["discontinuities"] = discontinuities
        if discontinuities:
            raise DataError("raw discontinuity; stop")
        checks["discontinuity"] = "PASSED"
        if any(o.raw.fetched_at < at or o.raw.fetched_at > aware(now()) for o in second):
            raise DataError("invalid observation times")
        guard.check_deadline()
        result.update(complete=True, status="PASSED")
        code = 0
    except Exception:
        result.update(status="STOPPED", reason="authorization_evidence_or_data_quality_failure")
        code = 1
    finally:
        result.update(totals)
        if "missing_rows" in identity_stats:
            result["missing_rows"] = identity_stats["missing_rows"]
        if "identified_rows" in identity_stats:
            result["received_rows_per_pass"] = identity_stats["identified_rows"]
        result["received_raw_rows"] = sum(map(len, last.bars.values())) if last else None
        result.update(
            requests=len(guard.events) if guard else 0,
            http_status_counts=dict(Counter(str(e["status"]) for e in guard.events))
            if guard
            else {},
            total_seconds=round(time.perf_counter() - started, 3),
            duplicate_rows=last.duplicate_rows + identity_stats.get("duplicate_rows", 0)
            if last
            else None,
            invalid_rows=last.invalid_rows + identity_stats.get("invalid_rows", 0)
            if last
            else None,
        )
        if provider:
            try:
                provider.close()
            except Exception:
                pass
        elif guard:
            try:
                guard.close()
            except Exception:
                pass
    return code, result


def main() -> int:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        code, result = run(os.environ.get("ALPACA_READONLY_APPROVAL", ""), os.environ)
        print(json.dumps(result, sort_keys=True))
        return code
    finally:
        logging.disable(previous)
