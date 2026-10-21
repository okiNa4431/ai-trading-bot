"""JP-3 disabled live adapter and explicitly synthetic transport contract.

Fixture envelopes are OUR internal schema, not a claim about EDINET DB wire fields.
No live activation flag, API-key reader, real HTTP transport or persistence exists.
"""

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from trading_bot.data_access import MarketAccessDisabled, ReadOnlyQualityReport
from trading_bot.financials import FinancialStatement, FinancialStop
from trading_bot.jquants import load_policy


class ResearchPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: Literal["edinetdb"]
    plan: Literal["Free"]
    live_enabled: Literal[False]
    phase: Literal["MOCK_INTERFACE_ONLY"]
    reviewed_at: date
    review_valid_until: date
    review_reference: Literal["docs/edinetdb-free-review.md"]

    @model_validator(mode="after")
    def review_interval(self) -> "ResearchPolicy":
        if not self.reviewed_at <= self.review_valid_until <= self.reviewed_at + timedelta(days=30):
            raise ValueError("EDINETDB_REVIEW_INTERVAL_INVALID")
        return self


def load_research_policy() -> ResearchPolicy:
    return ResearchPolicy.model_validate(
        yaml.safe_load(Path("config/edinetdb-research.yaml").read_text(encoding="utf-8-sig"))
    )


class EdinetDBProvider:
    """Implements the interface while failing before any HTTP initialization."""

    def fetch(self, issuer_id: str) -> tuple[FinancialStatement, ...]:
        raise MarketAccessDisabled("EDINETDB_LIVE_ADAPTER_NOT_APPROVED")


class FixtureFinancialProvider:
    """Offline adapter testing quota/retry/paging. It cannot accept real observations."""

    def __init__(
        self,
        transport: httpx.MockTransport,
        *,
        remaining_daily: int = 60,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(transport) is not httpx.MockTransport or not 1 <= remaining_daily <= 100:
            raise FinancialStop("SYNTHETIC_TRANSPORT_OR_QUOTA_REQUIRED")
        self.client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False)
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.last_request: float | None = None
        self.budget = min(60, remaining_daily)
        self.requests = self.retries = self.pages = 0
        self.statuses: Counter[int] = Counter()

    def close(self) -> None:
        self.client.close()

    def _page(self, issuer_id: str, token: str) -> dict[str, object]:
        for attempt in range(3):
            if self.last_request is not None:
                self.sleep(max(0, self.last_request + 1 - self.clock()))
            if self.requests >= self.budget or self.clock() - self.started >= 120:
                raise FinancialStop("FIXTURE_QUOTA_OR_DEADLINE")
            self.requests += 1
            self.last_request = self.clock()
            try:
                response = self.client.get(
                    "https://fixture.invalid/financials",
                    params={"issuer": issuer_id, "page": token},
                    timeout=5,
                )
                self.statuses[response.status_code] += 1
                if response.status_code == 429:
                    # Account daily quota, not permission for repeated retries or paid fallback.
                    raise FinancialStop("FIXTURE_DAILY_QUOTA_STOP")
                if response.status_code in (401, 403):
                    raise FinancialStop("FIXTURE_AUTH_STOP")
                if response.status_code >= 500:
                    raise httpx.TransportError("synthetic retryable status")
                if response.status_code != 200 or len(response.content) > 1024 * 1024:
                    raise FinancialStop("FIXTURE_RESPONSE_STOP")
                value = response.json()
                if not isinstance(value, dict) or set(value) != {"records", "next_page"}:
                    raise FinancialStop("FIXTURE_SCHEMA_STOP")
                if self.clock() - self.started >= 120:
                    raise FinancialStop("FIXTURE_QUOTA_OR_DEADLINE")
                return value
            except FinancialStop:
                raise
            except httpx.TransportError:
                if attempt == 2:
                    raise FinancialStop("FIXTURE_RETRY_EXHAUSTED") from None
                self.retries += 1
                self.sleep(attempt + 1)
            except (ValueError, TypeError):
                raise FinancialStop("FIXTURE_SCHEMA_STOP") from None
        raise FinancialStop("FIXTURE_RETRY_EXHAUSTED")

    def fetch(self, issuer_id: str) -> tuple[FinancialStatement, ...]:
        if not issuer_id.startswith("synthetic:"):
            raise FinancialStop("SYNTHETIC_ISSUER_REQUIRED")
        rows: list[FinancialStatement] = []
        token, tokens = "", set()
        try:
            for _ in range(2):
                value = self._page(issuer_id, token)
                records = value["records"]
                if not isinstance(records, list) or not records:
                    raise FinancialStop("FIXTURE_EMPTY_OR_INCOMPLETE")
                for item in records:
                    row = FinancialStatement.model_validate(item)
                    if row.data_kind != "SYNTHETIC" or row.issuer_id != issuer_id:
                        raise FinancialStop("FIXTURE_PROVENANCE_STOP")
                    rows.append(row)
                self.pages += 1
                following = value["next_page"]
                if following is None:
                    keys = [(r.document_id, r.revision_id, r.retrieved_at) for r in rows]
                    if len(keys) != len(set(keys)):
                        raise FinancialStop("FIXTURE_DUPLICATE_STOP")
                    return tuple(rows)
                if (
                    not isinstance(following, str)
                    or not following
                    or len(following) > 256
                    or following in tokens
                ):
                    raise FinancialStop("FIXTURE_PAGING_STOP")
                tokens.add(following)
                token = following
            raise FinancialStop("FIXTURE_PAGE_LIMIT")
        except FinancialStop:
            raise
        except Exception:
            raise FinancialStop("FIXTURE_VALIDATION_STOP") from None

    def summary(self) -> ReadOnlyQualityReport:
        return ReadOnlyQualityReport(
            json.dumps(
                dict(
                    purpose="diagnostic_only",
                    data_kind="SYNTHETIC",
                    strategy_approved=False,
                    requests=self.requests,
                    retries=self.retries,
                    pages=self.pages,
                    http_status_counts=dict(self.statuses),
                    data_retained=False,
                )
            )
        )


def main(args: list[str]) -> int:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if args != ["--diagnose"]:
            raise MarketAccessDisabled("EDINETDB_DIAGNOSE_ONLY")
        policy = load_research_policy()
        if not policy.reviewed_at <= datetime.now(UTC).date() <= policy.review_valid_until:
            raise MarketAccessDisabled("EDINETDB_REVIEW_EXPIRED")
        targets = load_policy().securities  # IDs/codes only; no J-Quants key or approval reuse.
        print(
            json.dumps(
                dict(
                    phase="JP-3",
                    status="MOCK_INTERFACE_READY",
                    purpose="diagnostic_only",
                    plan=policy.plan,
                    target_count=len(targets),
                    issuer_mapping="UNKNOWN",
                    live_enabled=policy.live_enabled,
                    requests=0,
                    api_key_required=False,
                    live_adapter="NOT_IMPLEMENTED",
                    external_ai=False,
                    strategy_approved=False,
                    storage=False,
                    historical_point_in_time="UNAPPROVED",
                ),
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        print(
            json.dumps(
                dict(
                    status="STOPPED",
                    reason="EDINETDB_DIAGNOSIS_OR_ARGUMENTS_INVALID",
                    purpose="diagnostic_only",
                    requests=0,
                    strategy_approved=False,
                )
            )
        )
        return 1
    finally:
        logging.disable(previous)
