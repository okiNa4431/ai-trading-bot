"""J-Quants V2 Free: bounded GET-only, transient diagnostic observations, no trading adapter."""

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

import httpx
import pandas_market_calendars as mcal
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_bot.broker.execution import session
from trading_bot.data_access import DiagnosticOnly, MarketAccessDisabled, ReadOnlyQualityReport
from trading_bot.portfolio.models import utc

if TYPE_CHECKING:
    from trading_bot.jquants_local import LocalProbePolicy

APPROVAL = "READ_ONLY_JQUANTS_20_NO_STORAGE"
ENDPOINT = "https://api.jquants.com/v2/equities/bars/daily"
WORKFLOW = "okiNa4431/ai-trading-bot/.github/workflows/jquants-readonly-20.yml@refs/heads/main"
JST = ZoneInfo("Asia/Tokyo")
QUOTE_KEYS = ("O", "H", "L", "C", "Vo")
REQUIRED = {
    "Date",
    "Code",
    "Va",
    "AdjFactor",
    "ExRT",
    *QUOTE_KEYS,
    *("Adj" + k for k in QUOTE_KEYS),
}
ALLOWED = REQUIRED | {"UL", "LL", "MktCap"}
MAX_REQUESTS, MAX_PAGES, MAX_SECONDS, INTERVAL = 100, 2, 2100, 21.0


class JQuantsStop(ValueError):
    """Only fixed error categories, never HTTP objects or response content."""


@lru_cache(maxsize=16)
def expected_sessions(start: date, end: date) -> tuple[date, ...]:
    """Calendar computation only. No market observations are cached."""
    return tuple(mcal.get_calendar("JPX").valid_days(start, end).date)


def two_years_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 2)
    except ValueError:  # Leap day: the conservative interior margin is applied by the caller.
        return day.replace(year=day.year - 2, day=28)


class ResearchSecurity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    security_id: str = Field(pattern=r"^jp-research-\d{3}$")
    code: str = Field(pattern=r"^\d{4}$")
    name: str = Field(min_length=1)


class JQuantsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    live_enabled: bool = Field(default=False, strict=True)
    plan: Literal["Free"] = "Free"
    from_date: date
    to_date: date
    reviewed_at: date
    review_valid_until: date
    contract_reference: str = Field(min_length=1)
    listing_source: Literal[
        "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
    ]
    listing_as_of: date
    listing_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    securities: tuple[ResearchSecurity, ...] = Field(min_length=20, max_length=20)

    @model_validator(mode="after")
    def valid(self) -> "JQuantsPolicy":
        if (
            len({s.code for s in self.securities}) != 20
            or len({s.security_id for s in self.securities}) != 20
            or not self.from_date <= self.to_date
            or not self.reviewed_at
            <= self.review_valid_until
            <= self.reviewed_at + timedelta(days=30)
            or self.listing_as_of > self.reviewed_at
        ):
            raise ValueError("invalid fixed research scope")
        return self

    def validate_dates(self, at: datetime) -> None:
        today = utc(at).astimezone(JST).date()
        latest = today - timedelta(weeks=12)
        # Stay one day inside both interpretations at leap-year boundaries.
        oldest = max(two_years_before(latest), two_years_before(today) - timedelta(weeks=12))
        if not (
            self.reviewed_at <= today <= self.review_valid_until
            and oldest < self.from_date <= self.to_date < latest
        ):
            raise JQuantsStop("FREE_WINDOW_OR_REVIEW_EXPIRED")

    def sessions(self) -> tuple[date, ...]:
        days = expected_sessions(self.from_date, self.to_date)
        if not days or len(days) > 260:
            raise JQuantsStop("FIXED_PERIOD_INVALID")
        return days


def load_policy() -> JQuantsPolicy:
    return JQuantsPolicy.model_validate(
        yaml.safe_load(Path("config/jquants-readonly-20.yaml").read_text(encoding="utf-8-sig"))
    )


def authorize(policy: JQuantsPolicy, approval: str, env: Mapping[str, str], at: datetime) -> str:
    JQuantsPolicy.model_validate(policy.model_dump())
    policy.validate_dates(at)
    policy.sessions()
    required = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "okiNa4431/ai-trading-bot",
        "GITHUB_WORKFLOW_REF": WORKFLOW,
        "RUNNER_ENVIRONMENT": "github-hosted",
        "JQUANTS_RESEARCH_ENVIRONMENT": "jquants-research",
        "JQUANTS_PLAN": "Free",
        "JQUANTS_MARKET_DATA_LIVE_ENABLED": "true",
        "JQUANTS_TERMS_CONFIRMED": "true",
    }
    if (
        not policy.live_enabled
        or approval != APPROVAL
        or any(env.get(k) != v for k, v in required.items())
    ):
        raise MarketAccessDisabled("JQUANTS_READONLY_APPROVAL_REQUIRED")
    return require_api_key(env)


def require_api_key(env: Mapping[str, str]) -> str:
    key = env.get("JQUANTS_API_KEY", "")
    if not key.strip() or key != key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise MarketAccessDisabled("JQUANTS_API_KEY_REQUIRED")
    return key


class Guard(httpx.BaseTransport):
    def __init__(
        self,
        inner: httpx.BaseTransport,
        policy: JQuantsPolicy,
        key: str,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        now: Callable[[], datetime],
    ):
        self.inner, self.policy, self._key = inner, policy, key
        self.clock, self.sleep, self.now = clock, sleep, now
        self.started = clock()
        self.last: float | None = None
        self.requests = 0
        self.http_status_counts: Counter[str] = Counter()
        self.header_seconds = 0.0

    def remaining(self) -> float:
        remaining = MAX_SECONDS - (self.clock() - self.started)
        if remaining <= 0:
            raise JQuantsStop("TIME_BUDGET")
        return remaining

    def pause(self, seconds: float) -> None:
        if seconds >= self.remaining():
            raise JQuantsStop("TIME_BUDGET")
        self.sleep(seconds)
        self.remaining()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        u, p = request.url, request.url.params
        if (
            request.method != "GET"
            or u.scheme != "https"
            or u.host != "api.jquants.com"
            or u.port not in (None, 443)
            or u.path != "/v2/equities/bars/daily"
            or u.username
            or u.password
            or u.fragment
            or request.content
            or set(p) - {"code", "from", "to", "pagination_key"}
            or len(p.multi_items()) != len(p)
            or p.get("code") not in {s.code for s in self.policy.securities}
            or p.get("from") != self.policy.from_date.isoformat()
            or p.get("to") != self.policy.to_date.isoformat()
            or request.headers.get("x-api-key") != self._key
            or ("pagination_key" in p and not 0 < len(p["pagination_key"]) <= 4096)
        ):
            raise JQuantsStop("REQUEST_SCOPE")
        self.policy.validate_dates(self.now())
        self.remaining()
        if self.requests >= MAX_REQUESTS:
            raise JQuantsStop("REQUEST_BUDGET")
        if self.last is not None:
            self.pause(max(0, INTERVAL - (self.clock() - self.last)))
        self.last = self.clock()
        self.requests += 1
        began = self.clock()
        try:
            response = self.inner.handle_request(request)
        except httpx.TransportError:
            self.http_status_counts["TRANSPORT_ERROR"] += 1
            raise JQuantsStop("TRANSPORT_ERROR") from None
        finally:
            self.header_seconds += self.clock() - began
        self.http_status_counts[str(response.status_code)] += 1
        self.remaining()
        return response

    def close(self) -> None:
        self.inner.close()


def number(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("invalid number")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("invalid number")
    return result


@dataclass(frozen=True, repr=False)
class Quote(DiagnosticOnly):
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    purpose: Literal["diagnostic_only"] = "diagnostic_only"

    @classmethod
    def parse(cls, row: dict[str, object], prefix: str = "") -> "Quote":
        opening, high, low, closing, volume = (number(row[prefix + k]) for k in QUOTE_KEYS)
        q = cls(opening, high, low, closing, volume)
        if (
            min(q.open, q.high, q.low, q.close) <= 0
            or q.volume < 0
            or q.high < max(q.open, q.low, q.close)
            or q.low > min(q.open, q.high, q.close)
        ):
            raise ValueError("invalid OHLCV")
        return q


@dataclass(frozen=True, repr=False)
class RequestConditions(DiagnosticOnly):
    code: str
    from_date: date
    to_date: date
    endpoint: str = ENDPOINT
    plan: Literal["Free"] = "Free"


@dataclass(frozen=True, repr=False)
class Observation(DiagnosticOnly):
    code: str
    trade_date: date
    raw: Quote | None
    adjusted: Quote | None
    factor: Decimal | None
    ex_right_type: str | None
    fetched_at: datetime
    available_at: datetime  # Observation upper bound; provider publication is NOT known.
    confirmed_at: datetime
    issues: tuple[str, ...]
    signature: str = field(repr=False)
    request_conditions: RequestConditions
    provider: Literal["jquants_v2"] = "jquants_v2"
    market: Literal["JP"] = "JP"
    currency: Literal["JPY"] = "JPY"
    interval: Literal["1d"] = "1d"
    availability_basis: Literal["observed_by"] = "observed_by"
    provider_published_at: None = None
    purpose: Literal["diagnostic_only"] = "diagnostic_only"

    @property
    def data_quality_status(self) -> str:
        return "FAILED" if self.issues else "OBSERVATION_VALID"


def observation(
    row: object,
    code: str,
    days: tuple[date, ...],
    fetched: datetime,
    conditions: RequestConditions,
) -> Observation:
    if not isinstance(row, dict) or not REQUIRED <= set(row) <= ALLOWED:
        raise JQuantsStop("RESPONSE_SCHEMA")
    if row["Code"] != code + "0" or not isinstance(row["Date"], str):
        raise JQuantsStop("RESPONSE_IDENTITY")
    try:
        day = date.fromisoformat(row["Date"])
    except ValueError:
        raise JQuantsStop("RESPONSE_DATE") from None
    if day not in days or row["Date"] != day.isoformat():
        raise JQuantsStop("RESPONSE_PERIOD_OR_CALENDAR")
    closed = session("JP", day.isoformat())["market_close"]
    if utc(fetched) < closed:
        raise JQuantsStop("UNCONFIRMED_OBSERVATION")
    issues, raw, adjusted, factor = [], None, None, None
    nulls = [row[k] is None for k in (*QUOTE_KEYS, "Va")]
    if all(nulls):
        issues.append("NO_TRADE_NULLS")
        if any(row["Adj" + k] is not None for k in QUOTE_KEYS):
            issues.append("INVALID_OHLCV")
    else:
        try:
            raw, adjusted = Quote.parse(row), Quote.parse(row, "Adj")
            if number(row["Va"]) < 0:
                raise ValueError("invalid turnover")
        except ValueError:
            issues.append("INVALID_OHLCV")
    try:
        factor = number(row["AdjFactor"])
        if factor <= 0:
            raise ValueError("invalid factor")
    except ValueError:
        issues.append("INVALID_FACTOR")
        factor = None
    exrt = row["ExRT"]
    if exrt not in (None, "1", "2", "3"):
        issues.append("UNKNOWN_CORPORATE_ACTION")
    elif exrt is not None:
        issues.append("UNREVIEWED_CORPORATE_ACTION")
    if factor is not None:
        if (exrt == "1" and factor >= 1) or (exrt == "2" and factor <= 1):
            issues.append("FACTOR_EVENT_CONFLICT")
        if factor != 1 and exrt is None:
            issues.append("UNKNOWN_CORPORATE_ACTION")
    signature = hashlib.sha256(
        json.dumps({k: row[k] for k in sorted(REQUIRED)}, default=str, sort_keys=True).encode()
    ).hexdigest()
    return Observation(
        code,
        day,
        raw,
        adjusted,
        factor,
        str(exrt) if exrt is not None else None,
        utc(fetched),
        utc(fetched),
        closed,
        tuple(sorted(set(issues))),
        signature,
        conditions,
    )


class JQuantsProvider:
    """Reviewed workflow or Windows-local authorization; one shared diagnostic engine."""

    def __init__(
        self,
        policy: JQuantsPolicy,
        approval: str,
        env: Mapping[str, str],
        *,
        local_policy: "LocalProbePolicy | None" = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if local_policy is None:
            key = authorize(policy, approval, env, now())
        else:
            from trading_bot.jquants_local import authorize_local

            key = authorize_local(policy, local_policy, approval, env, now())
        self.policy, self.now = policy, now
        self.guard = Guard(
            transport if transport is not None else httpx.HTTPTransport(retries=0),
            policy,
            key,
            clock,
            sleep,
            now,
        )
        self.client = httpx.Client(
            transport=self.guard,
            headers={"x-api-key": key},
            timeout=10,
            trust_env=False,
            follow_redirects=False,
        )
        self.pages = self.retries = 0
        self._used = False
        self.paging = ["NOT_RUN", "NOT_RUN"]
        self.quality_checks = ["NOT_RUN", "NOT_RUN"]
        self.checks: dict[str, object] = dict(
            scope="PASSED",
            paging=self.paging,
            quality=self.quality_checks,
            repeat_consistency="NOT_RUN",
        )

    def close(self) -> None:
        self.client.close()

    def request(self, params: dict[str, str]) -> dict[str, object]:
        for attempt in range(3):
            try:
                with self.client.stream(
                    "GET", ENDPOINT, params=params, timeout=min(10, self.guard.remaining())
                ) as response:
                    status = response.status_code
                    if status == 429 or 500 <= status <= 599:
                        raise JQuantsStop("RETRYABLE_HTTP")
                    if status != 200:
                        raise JQuantsStop(
                            "AUTHENTICATION" if status in (401, 403) else "HTTP_FAILURE"
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        self.guard.remaining()
                        data.extend(chunk)
                        if len(data) > 2 * 1024 * 1024:
                            raise JQuantsStop("RESPONSE_SIZE")
                value = json.loads(data, parse_float=Decimal)
                if (
                    not isinstance(value, dict)
                    or "data" not in value
                    or set(value) - {"data", "pagination_key"}
                ):
                    raise JQuantsStop("RESPONSE_SCHEMA")
                self.guard.remaining()
                return value
            except (httpx.TransportError, JQuantsStop) as exc:
                retryable = isinstance(exc, httpx.TransportError) or str(exc) in (
                    "RETRYABLE_HTTP",
                    "TRANSPORT_ERROR",
                )
                if not retryable or attempt == 2:
                    raise JQuantsStop(
                        str(exc) if isinstance(exc, JQuantsStop) else "TRANSPORT_ERROR"
                    ) from None
                self.retries += 1
                # Bound and pace all retries; do not trust an unbounded Retry-After.
                self.guard.pause(21 * (attempt + 1))
            except (ValueError, TypeError):
                raise JQuantsStop("RESPONSE_SCHEMA") from None
        raise JQuantsStop("RETRY_EXHAUSTED")

    def _read_pass(self) -> dict[str, tuple[Observation, ...]]:
        result = {}
        days = self.policy.sessions()
        for security in self.policy.securities:
            rows: list[Observation] = []
            token, seen = "", set()
            for _page in range(MAX_PAGES):
                params = {
                    "code": security.code,
                    "from": self.policy.from_date.isoformat(),
                    "to": self.policy.to_date.isoformat(),
                }
                if token:
                    params["pagination_key"] = token
                value = self.request(params)
                data = value["data"]
                if not isinstance(data, list):
                    raise JQuantsStop("RESPONSE_SCHEMA")
                self.pages += 1
                fetched = self.now()
                conditions = RequestConditions(
                    security.code, self.policy.from_date, self.policy.to_date
                )
                rows.extend(
                    observation(row, security.code, days, fetched, conditions) for row in data
                )
                new_token = value.get("pagination_key")
                if new_token is None or new_token == "":
                    break
                if (
                    not isinstance(new_token, str)
                    or len(new_token) > 4096
                    or new_token in seen
                    or not data
                ):
                    raise JQuantsStop("PAGING_INCONSISTENT")
                seen.add(new_token)
                token = new_token
            else:
                raise JQuantsStop("PAGE_BUDGET")
            result[security.code] = tuple(rows)
        return result

    @staticmethod
    def _quality(
        rows: dict[str, tuple[Observation, ...]], days: tuple[date, ...]
    ) -> dict[str, object]:
        missing = duplicates = invalid = nulls = discontinuities = unknown = zero_volume = 0
        actions: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        quarantined = 0
        for values in rows.values():
            dates = [r.trade_date for r in values]
            issues = set()
            count = len(set(days) - set(dates))
            missing += count
            duplicate = len(dates) - len(set(dates))
            duplicates += duplicate
            if count:
                issues.add("MISSING")
            if duplicate:
                issues.add("DUPLICATE")
            if dates != sorted(dates):
                issues.add("OUT_OF_ORDER")
            prior: Observation | None = None
            for row in values:
                issues.update(row.issues)
                invalid += any(
                    i in row.issues
                    for i in ("INVALID_OHLCV", "INVALID_FACTOR", "FACTOR_EVENT_CONFLICT")
                )
                nulls += "NO_TRADE_NULLS" in row.issues
                unknown += "UNKNOWN_CORPORATE_ACTION" in row.issues
                if row.ex_right_type in ("1", "2", "3"):
                    actions[
                        {"1": "split", "2": "reverse_split", "3": "rights_issue"}[row.ex_right_type]
                    ] += 1
                zero_volume += row.raw is not None and row.raw.volume == 0
                if prior is not None and prior.raw is not None and row.raw is not None:
                    if abs(row.raw.close / prior.raw.close - 1) > Decimal(".35"):
                        discontinuities += 1
                        issues.add("DISCONTINUITY")
                prior = row
            if issues:
                quarantined += 1
                reasons.update(issues)
        return dict(
            received_rows=sum(map(len, rows.values())),
            missing_rows=missing,
            duplicate_rows=duplicates,
            invalid_rows=invalid,
            no_trade_rows=nulls,
            discontinuities=discontinuities,
            unknown_action_rows=unknown,
            event_candidates=dict(actions),
            zero_volume_rows=zero_volume,
            quarantined_securities=quarantined,
            unknown_securities=len(rows) - quarantined,
            eligible_securities=0,
            reasons=dict(reasons),
        )

    def run(self) -> ReadOnlyQualityReport:
        if self._used:
            raise JQuantsStop("PROBE_ALREADY_USED")
        self._used = True
        days = self.policy.sessions()
        summaries = []
        observations: list[dict[str, tuple[Observation, ...]]] = []
        status, reason, revisions = "STOPPED", None, None
        try:
            for index in range(2):
                try:
                    rows = self._read_pass()
                except Exception:
                    self.paging[index] = "FAILED"
                    raise
                self.paging[index] = "PASSED"
                q = self._quality(rows, days)
                summaries.append(q)
                self.quality_checks[index] = (
                    "PASSED" if q["quarantined_securities"] == 0 else "FAILED"
                )
                observations.append(rows)
            revisions = sum(
                tuple((r.trade_date, r.signature) for r in observations[0][s.code])
                != tuple((r.trade_date, r.signature) for r in observations[1][s.code])
                for s in self.policy.securities
            )
            self.checks["repeat_consistency"] = "PASSED" if revisions == 0 else "FAILED"
            if revisions == 0 and all(q["quarantined_securities"] == 0 for q in summaries):
                status = "READ_ONLY_COMPLETE"
            else:
                reason = "SECURITY_QUALITY_OR_REPEAT_FAILURE"
        except Exception as exc:
            reason = str(exc) if isinstance(exc, JQuantsStop) else "BATCH_FAILURE"
        finally:
            observations.clear()
        return ReadOnlyQualityReport(
            json.dumps(
                dict(
                    phase="JP-2",
                    purpose="diagnostic_only",
                    strategy_approved=False,
                    status=status,
                    reason=reason,
                    security_count=20,
                    sessions=len(days),
                    expected_rows_per_pass=20 * len(days),
                    received_rows_per_pass=[q["received_rows"] for q in summaries],
                    quality_per_pass=summaries,
                    repeat_revised_securities=revisions,
                    requests=self.guard.requests,
                    pages=self.pages,
                    retries=self.retries,
                    http_status_counts=dict(self.guard.http_status_counts),
                    header_seconds=round(self.guard.header_seconds, 3),
                    total_seconds=round(self.guard.clock() - self.guard.started, 3),
                    checks=self.checks,
                    data_retained=False,
                    original_publication_times_known=False,
                    historical_identity_review="UNKNOWN",
                    corporate_action_accounting="UNKNOWN",
                    external_ai=False,
                )
            )
        )
