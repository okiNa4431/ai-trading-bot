"""Alias-only quality investigation. UNKNOWN evidence never becomes trading approval."""

import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

from trading_bot.alpaca import NY, AlpacaBatchError, DailyBatch, aware
from trading_bot.alpaca_research import WORKFLOW_REF
from trading_bot.config import Instrument
from trading_bot.corporate_review import public_research_candidates
from trading_bot.data_access import MarketAccessDisabled, ReadOnlyQualityReport
from trading_bot.identity100_probe import (
    IdentityBatchTransport,
    _TransientDailyProvider,
    session_window,
)
from trading_bot.identity_catalog import IdentityCatalog, load_catalog
from trading_bot.provider import DataError
from trading_bot.security_identity import SecurityRegistry

APPROVAL = "READ_ONLY_100_DIAGNOSTIC_NO_STORAGE"
WORKFLOW = WORKFLOW_REF.replace("alpaca-readonly.yml", "alpaca-quality-diagnostic-100.yml")
CONTINUATION_APPROVAL = "READ_ONLY_100_DISCONTINUITIES_NO_STORAGE"
CONTINUATION_WORKFLOW = WORKFLOW_REF.replace(
    "alpaca-readonly.yml", "alpaca-discontinuities-100.yml"
)


def diagnostic_scope(
    catalog: IdentityCatalog, days: tuple[date, ...], observed_on: date
) -> dict[str, set[date]]:
    """Do not resolve provisional aliases into securities. Validate known boundaries only."""
    IdentityCatalog.model_validate(catalog.model_dump())
    scopes = {}
    for review in catalog.reviews:
        items = tuple(a for a in catalog.assignments if a.security_id == review.security_id)
        if len(items) > 1:
            if any(
                r.status != "CONFIRMED" or r.checked_on is None or r.checked_on > observed_on
                for r in (review.identity, review.ticker_periods)
            ):
                raise DataError("ambiguous unreviewed multi-alias identity")
            # Reuse structural/event validation, never set provisional flags to True.
            SecurityRegistry(
                security_ids=(review.security_id,),
                assignments=items,
                events=tuple(e for e in catalog.events if e.security_id == review.security_id),
            )
        for item in items:
            known = item.identity_confirmed and all(
                r.status == "CONFIRMED" and r.checked_on is not None and r.checked_on <= observed_on
                for r in (review.identity, review.ticker_periods)
            )
            if not known and (item.valid_to is not None or item.valid_from > min(days)):
                raise DataError("unconfirmed temporal boundary; manual review required")
            scopes[item.ticker] = {
                d
                for d in days
                if not known
                or (item.valid_from <= d and (item.valid_to is None or d < item.valid_to))
            }
    return scopes


def inspect_aliases(
    batch: DailyBatch,
    scopes: dict[str, set[date]],
    stats: dict[str, int],
    *,
    catalog: IdentityCatalog | None = None,
    continue_discontinuities: bool = False,
) -> None:
    if not batch.complete_response or batch.excluded_unconfirmed or set(batch.bars) != set(scopes):
        raise DataError("incomplete or unexpected diagnostic response")
    missing = duplicates = invalid = discontinuities = 0
    affected = multiple = upward = downward = near = aligned = 0
    for alias, bars in batch.bars.items():
        found = set()
        for bar in bars:
            day = bar.timestamp.astimezone(NY).date()
            invalid += int(bar.symbol != alias or day not in scopes[alias])
            duplicates += int(day in found)
            found.add(day)
        missing += len(scopes[alias] - found)
        hits = 0
        # An assignment here selects public event candidates only, never resolves raw identity.
        sids = (
            {a.security_id for a in catalog.assignments if a.ticker == alias} if catalog else set()
        )
        events = [e for e in catalog.events if e.security_id in sids] if catalog else []
        event_days = [e.effective_date for e in events]
        event_days.extend(
            c.effective_from
            for c in public_research_candidates()
            if c.ticker_hint == alias and c.effective_from is not None
        )
        for left, right in zip(bars[:-1], bars[1:], strict=True):
            change = right.close / left.close - 1
            if abs(change) <= 0.35:
                continue
            hits += 1
            upward += int(change > 0)
            downward += int(change < 0)
            before, after = (b.timestamp.astimezone(NY).date() for b in (left, right))
            # Count observations, not events: split + spinoff on one day is one match.
            near += int(any(abs((day - after).days) <= 3 for day in event_days))
            aligned += int(any(before < day <= after for day in event_days))
        discontinuities += hits
        affected += int(hits > 0)
        multiple += max(0, hits - 1)
    stats.update(
        missing_rows=missing,
        duplicate_rows=duplicates,
        invalid_rows=invalid,
        discontinuities=discontinuities,
        discontinuity_count=discontinuities,
        discontinuity_symbol_count=affected,
        upward_discontinuity_count=upward,
        downward_discontinuity_count=downward,
        additional_same_symbol_discontinuities=multiple,
        known_event_window_matches=near,
        published_event_boundary_matches=aligned,
        unexplained_discontinuities=discontinuities - aligned,
    )
    if missing or duplicates or invalid or (discontinuities and not continue_discontinuities):
        raise DataError("alias quality failure; no repair or event adjustment")


def run(
    approval: str,
    env: Mapping[str, str],
    *,
    catalog: IdentityCatalog | None = None,
    mock: httpx.MockTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    continue_discontinuities: bool = False,
) -> tuple[int, ReadOnlyQualityReport]:
    started = time.perf_counter()
    checks: dict[str, object] = dict(
        scope="NOT_RUN",
        paging=["NOT_RUN", "NOT_RUN"],
        alias_quality=["NOT_RUN", "NOT_RUN"],
        repeat_consistency="NOT_RUN",
        security_completeness="NOT_RUN",
        corporate_action_clearance="NOT_RUN",
        strategy_approval="NOT_RUN",
    )
    result: dict[str, object] = dict(
        purpose="diagnostic_only",
        strategy_approved=False,
        diagnostic_complete=False,
        requested_security_count=100,
        alias_count=101,
        sessions=221,
        expected_alias_rows=None,
        received_rows_per_pass=None,
        repeat_revisions=None,
        missing_rows=None,
        duplicate_rows=None,
        invalid_rows=None,
        discontinuities=None,
        checks=checks,
        retained_market_data=False,
    )
    provider = None
    guard = None
    last = None
    stats: dict[str, int] = {}
    pages = retries = 0
    try:
        required = dict(
            GITHUB_ACTIONS="true",
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_REF="refs/heads/main",
            GITHUB_WORKFLOW_REF=CONTINUATION_WORKFLOW if continue_discontinuities else WORKFLOW,
            GITHUB_REPOSITORY="okiNa4431/ai-trading-bot",
            RUNNER_ENVIRONMENT="github-hosted",
            ALPACA_RESEARCH_ENVIRONMENT="alpaca-research",
            ALPACA_MARKET_DATA_LIVE_ENABLED="true",
            ALPACA_100_DIAGNOSTIC_LIVE_ENABLED="true",
        )
        expected_approval = CONTINUATION_APPROVAL if continue_discontinuities else APPROVAL
        if continue_discontinuities:
            required["ALPACA_DISCONTINUITIES_LIVE_ENABLED"] = "true"
        if approval != expected_approval or any(env.get(k) != v for k, v in required.items()):
            raise MarketAccessDisabled("separate read-only diagnostic approval required")
        checks["scope"] = "FAILED"
        catalog = catalog or load_catalog()
        at = aware(now())
        days = session_window(at)
        scopes = diagnostic_scope(catalog, days, at.astimezone(ZoneInfo("Asia/Tokyo")).date())
        result.update(
            catalog.summary(set(days)),
            expected_alias_rows=sum(map(len, scopes.values())),
            known_non_ticker_events=sum(
                e.kind != "ticker_change" and min(days) <= e.effective_date <= max(days)
                for e in catalog.events
            ),
            known_ticker_events=sum(
                e.kind == "ticker_change" and min(days) <= e.effective_date <= max(days)
                for e in catalog.events
            ),
        )
        checks["scope"] = "PASSED"
        key, secret = env.get("ALPACA_API_KEY_ID"), env.get("ALPACA_API_SECRET_KEY")
        if not key or not secret:
            raise MarketAccessDisabled("missing Environment Secrets")
        if mock is not None and not isinstance(mock, httpx.MockTransport):
            raise ValueError("offline MockTransport required")
        guard = IdentityBatchTransport(mock or httpx.HTTPTransport(retries=0), tuple(scopes))
        provider = _TransientDailyProvider(key, secret, guard, now, mock is not None)
        result["data_kind"] = provider.data_kind
        instruments = [
            Instrument(symbol=s, market="US", currency="USD", display_name=s) for s in scopes
        ]
        start, end = (datetime.combine(d, datetime.min.time(), NY) for d in (days[0], days[-1]))
        batches = []
        pass_stats = []
        paging, quality = ["NOT_RUN", "NOT_RUN"], ["NOT_RUN", "NOT_RUN"]
        checks.update(paging=paging, alias_quality=quality)
        for index in range(2):
            guard.begin_pass()
            last = None
            stats = {}
            paging[index] = "FAILED"
            try:
                last = provider.fetch_many(instruments, start, end)
            except AlpacaBatchError as exc:
                last = exc.partial
                raise
            finally:
                if last:
                    pages += last.pages
                    retries += last.retries
            paging[index] = "PASSED"
            quality[index] = "FAILED"
            inspect_aliases(
                last,
                scopes,
                stats,
                catalog=catalog,
                continue_discontinuities=continue_discontinuities,
            )
            if any(
                b.fetched_at < at or b.fetched_at > aware(now())
                for bars in last.bars.values()
                for b in bars
            ):
                raise DataError("invalid diagnostic observation times")
            quality[index] = "FAILED" if stats["discontinuities"] else "PASSED"
            pass_stats.append(dict(stats))
            if continue_discontinuities:
                result["quality_counts_per_pass"] = pass_stats
            batches.append(last)
        checks["repeat_consistency"] = "FAILED"
        first, second = batches
        revisions = sum(
            a.model_dump(exclude={"fetched_at"}) != b.model_dump(exclude={"fetched_at"})
            for alias in scopes
            for a, b in zip(first.bars[alias], second.bars[alias], strict=True)
        )
        result["repeat_revisions"] = revisions
        if revisions:
            raise DataError("diagnostic observations changed")
        checks["repeat_consistency"] = "PASSED"
        guard.check_deadline()
        if any(s["discontinuities"] for s in pass_stats):
            result["reason"] = "discontinuities_retained_after_repeat_diagnosis"
            raise DataError("discontinuities remain quality violations")
        result.update(status="DIAGNOSTIC_PASSED", diagnostic_complete=True)
        code = 0
    except Exception:
        result.update(status="STOPPED")
        result.setdefault("reason", "authorization_scope_or_alias_quality_failure")
        code = 1
    finally:
        result.update(stats)
        if last:
            result.update(
                received_rows_per_pass=sum(map(len, last.bars.values())),
                duplicate_rows=last.duplicate_rows + stats.get("duplicate_rows", 0),
                invalid_rows=last.invalid_rows + stats.get("invalid_rows", 0),
            )
        result.update(
            requests=len(guard.events) if guard else 0,
            pages=pages,
            retries=retries,
            http_status_counts=dict(Counter(str(e["status"]) for e in guard.events))
            if guard
            else {},
            total_seconds=round(time.perf_counter() - started, 3),
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
    # Only aggregate JSON escapes this function. Raw bars never cross the diagnostic boundary.
    return code, ReadOnlyQualityReport(json.dumps(result, sort_keys=True))


def main(*, continue_discontinuities: bool = False) -> int:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        code, report = run(
            os.environ.get("ALPACA_READONLY_APPROVAL", ""),
            os.environ,
            continue_discontinuities=continue_discontinuities,
        )
        print(report.aggregate_json)
        return code
    finally:
        logging.disable(previous)
