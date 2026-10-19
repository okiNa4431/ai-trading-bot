"""Separate, explicitly approved two-alias read-only experiment; no storage or trading."""

import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

import httpx
import pandas_market_calendars as mcal

from trading_bot.alpaca import NY, AlpacaDailyProvider, DailyBatch, RequestPacer, confirmed_at
from trading_bot.alpaca_research import WORKFLOW_REF, ReadOnlyTransport
from trading_bot.config import Instrument
from trading_bot.data_access import MarketAccessDisabled
from trading_bot.provider import DataError
from trading_bot.security_identity import identify_batch, marsh_registry, validate_window

APPROVAL = "READ_ONLY_MMC_MRSH_NO_STORAGE"
WORKFLOW = WORKFLOW_REF.replace("alpaca-readonly.yml", "alpaca-ticker-history.yml")


class TickerHistoryProvider(AlpacaDailyProvider):
    def __init__(
        self,
        approval: str,
        environment: Mapping[str, str],
        *,
        mock: httpx.MockTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        required = dict(
            GITHUB_ACTIONS="true",
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_REF="refs/heads/main",
            GITHUB_WORKFLOW_REF=WORKFLOW,
            GITHUB_REPOSITORY="okiNa4431/ai-trading-bot",
            RUNNER_ENVIRONMENT="github-hosted",
            ALPACA_RESEARCH_ENVIRONMENT="alpaca-research",
            ALPACA_MARKET_DATA_LIVE_ENABLED="true",
            ALPACA_TICKER_HISTORY_LIVE_ENABLED="true",
        )
        if approval != APPROVAL or any(environment.get(k) != v for k, v in required.items()):
            raise MarketAccessDisabled("separate ticker-history approval required")
        self.registry = marsh_registry()
        key, secret = environment.get("ALPACA_API_KEY_ID"), environment.get("ALPACA_API_SECRET_KEY")
        if not key or not secret:
            raise MarketAccessDisabled("missing Environment Secrets")
        if mock is not None and not isinstance(mock, httpx.MockTransport):
            raise ValueError("only offline MockTransport is allowed")
        self.guard = ReadOnlyTransport(mock or httpx.HTTPTransport(retries=0), ["MMC", "MRSH"])
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
        self.stage = "READY"

    def fetch_many(
        self, instruments: Sequence[Instrument], start: datetime, end: datetime
    ) -> DailyBatch:
        if tuple(i.symbol for i in instruments) != ("MMC", "MRSH"):
            raise DataError("ticker-history scope mismatch")
        sessions = mcal.get_calendar("NYSE").schedule(
            start.astimezone(NY).date(), end.astimezone(NY).date()
        )
        if len(sessions) != 221:
            raise DataError("ticker-history requires 221 sessions")
        return super().fetch_many(instruments, start, end)


def probe(provider: TickerHistoryProvider) -> dict[str, object]:
    at = provider.now()
    if at.tzinfo is None:
        raise DataError("aware observation time required")
    schedule = mcal.get_calendar("NYSE").schedule((at - timedelta(days=500)).date(), at.date())
    days = [d.date() for d in schedule.index if confirmed_at(d.date()) <= at][-221:]
    if len(days) != 221:
        raise DataError("insufficient confirmed sessions")
    provider.stage = "REGISTRY"
    validate_window(provider.registry, set(days))
    start, end = (datetime.combine(d, datetime.min.time(), NY) for d in (days[0], days[-1]))
    instruments = [
        Instrument(symbol=s, market="US", currency="USD", display_name=s) for s in ("MMC", "MRSH")
    ]
    observations = []
    batches = []
    for _ in range(2):
        provider.stage = "FETCH"
        batch = provider.fetch_many(instruments, start, end)
        batches.append(batch)
        provider.stage = "IDENTITY"
        observations.append(identify_batch(provider.registry, batch))
    first, second = observations
    provider.stage = "REPEAT_COMPARISON"
    if any(
        a.security_id != b.security_id
        or a.raw.model_dump(exclude={"fetched_at"}) != b.raw.model_dump(exclude={"fetched_at"})
        for a, b in zip(first, second, strict=True)
    ):
        raise DataError("repeated identity observations changed")
    provider.stage = "DISCONTINUITY"
    if any(
        abs(b.raw.close / a.raw.close - 1) > 0.35
        for a, b in zip(second[:-1], second[1:], strict=True)
    ):
        raise DataError("raw price discontinuity; corporate-action review required")
    provider.stage = "OBSERVATION_TIME"
    if any(o.raw.fetched_at < at or o.raw.fetched_at > provider.now() for o in second):
        raise DataError("invalid observation times")
    provider.guard.check_deadline()
    return dict(
        status="PASSED",
        complete=True,
        data_kind=provider.data_kind,
        security_count=1,
        alias_count=2,
        sessions=221,
        received_rows_per_pass=len(first),
        requests=len(provider.guard.events),
        pages=sum(b.pages for b in batches),
        retries=sum(b.retries for b in batches),
        http_status_counts=dict(Counter(str(e["status"]) for e in provider.guard.events)),
        retained_market_data=False,
        strategy_ready=False,
        corporate_actions_adjusted=False,
    )


def main() -> int:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    provider = None
    try:
        provider = TickerHistoryProvider(os.environ.get("ALPACA_READONLY_APPROVAL", ""), os.environ)
        print(json.dumps(probe(provider), sort_keys=True))
        return 0
    except Exception:
        events = provider.guard.events if provider else []
        print(
            json.dumps(
                dict(
                    status="STOPPED",
                    complete=False,
                    reason="authorization_identity_or_data_quality_failure",
                    failed_stage=provider.stage if provider else "AUTHORIZATION",
                    requests=len(events),
                    http_status_counts=dict(Counter(str(e["status"]) for e in events)),
                )
            )
        )
        return 1
    finally:
        if provider:
            try:
                provider.close()
            except Exception:
                pass
        logging.disable(previous)
