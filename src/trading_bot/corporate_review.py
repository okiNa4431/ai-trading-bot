"""Untrusted/offline event candidates. No discovery API, registry promotion or adjustment."""

from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from trading_bot.portfolio.models import utc


class CorporateActionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[
        "split",
        "reverse_split",
        "dividend",
        "special_dividend",
        "spinoff",
        "ticker_change",
        "other",
        "unknown",
    ]
    provider: str
    evidence_reference: str
    ticker_hint: str  # Not an approved security identity.
    security_id_hint: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    published_at: datetime | None = None
    published_on: date | None = None  # Date-only publication evidence; never invent a time.
    observed_at: datetime
    review_status: Literal["UNKNOWN"] = "UNKNOWN"
    event_id: str | None = None
    revision: int = 1
    supersedes_revision: int | None = None
    issuer: str | None = None
    share_class: str | None = None
    legal_effective_on: date | None = None
    ratio_new: int | None = None
    ratio_old: int | None = None
    official_terms: str | None = None
    additional_sources: tuple[str, ...] = ()
    evidence_status: Literal["UNKNOWN", "PUBLIC_SOURCE_REVIEWED"] = "UNKNOWN"
    provider_application: Literal["UNKNOWN"] = "UNKNOWN"

    _observed = field_validator("observed_at")(utc)

    @field_validator("published_at")
    @classmethod
    def publication_utc(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value is not None else None

    @model_validator(mode="after")
    def provenance(self) -> "CorporateActionCandidate":
        if (
            not self.provider.strip()
            or not self.evidence_reference.strip()
            or not self.ticker_hint.strip()
        ):
            raise ValueError("event provenance required")
        if self.effective_to and (
            self.effective_from is None or self.effective_to < self.effective_from
        ):
            raise ValueError("invalid event validity interval")
        if self.published_at and self.published_at > self.observed_at:
            raise ValueError("publication cannot be observed before publication time")
        if self.published_on and self.published_on > self.observed_at.date():
            raise ValueError("publication date after observation")
        if (
            self.revision < 1
            or (self.supersedes_revision is not None and self.supersedes_revision >= self.revision)
            or (self.revision == 1 and self.supersedes_revision is not None)
        ):
            raise ValueError("invalid event revision")
        if (self.ratio_new is None) != (self.ratio_old is None) or any(
            n is not None and n <= 0 for n in (self.ratio_new, self.ratio_old)
        ):
            raise ValueError("invalid official ratio")
        return self


class PublicActionCatalog(BaseModel):
    """Versioned public evidence, explicitly NOT an approved security/event registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    scope_note: str
    events: tuple[CorporateActionCandidate, ...]

    @model_validator(mode="after")
    def versions(self) -> "PublicActionCatalog":
        seen: dict[tuple[str, int], CorporateActionCandidate] = {}
        for event in self.events:
            if not event.event_id or not event.issuer:
                raise ValueError("catalog event requires stable event and issuer references")
            key = (event.event_id, event.revision)
            if key in seen:
                raise ValueError("duplicate event revision")
            if event.evidence_status == "PUBLIC_SOURCE_REVIEWED" and (
                not event.evidence_reference.startswith("https://")
                or not (event.published_on or event.published_at)
            ):
                raise ValueError("reviewed public evidence requires dated source")
            seen[key] = event
        for event in self.events:
            if event.revision > 1:
                if event.supersedes_revision != event.revision - 1:
                    raise ValueError("event revisions require a linear retained history")
                prior = seen.get((event.event_id or "", event.supersedes_revision or 0))
                if prior is None or prior.observed_at > event.observed_at:
                    raise ValueError("missing or temporally invalid superseded event")
                if (prior.issuer, prior.ticker_hint, prior.share_class) != (
                    event.issuer,
                    event.ticker_hint,
                    event.share_class,
                ):
                    raise ValueError("revision changes event identity")
        return self

    def active(self, at: datetime) -> tuple[CorporateActionCandidate, ...]:
        """Use revisions actually observed by this research time; retain old versions."""
        at = utc(at)
        latest: dict[str, CorporateActionCandidate] = {}
        for event in self.events:
            if event.observed_at <= at and (
                event.event_id not in latest
                or latest[event.event_id or ""].revision < event.revision
            ):
                latest[event.event_id or ""] = event
        return tuple(latest.values())


def load_public_actions() -> PublicActionCatalog:
    return PublicActionCatalog.model_validate(
        yaml.safe_load(Path("config/public-corporate-actions.yaml").read_text(encoding="utf-8"))
    )


class DiscontinuityState(StrEnum):
    UNKNOWN = "UNKNOWN"
    EVENT_CANDIDATE = "EVENT_CANDIDATE"
    AMBIGUOUS = "AMBIGUOUS"
    VERIFIED_CORPORATE_ACTION = "VERIFIED_CORPORATE_ACTION"
    POSSIBLE_MARKET_MOVE = "POSSIBLE_MARKET_MOVE"


def classify_boundary(
    events: tuple[CorporateActionCandidate, ...],
    ticker: str,
    before: date,
    after: date,
) -> tuple[DiscontinuityState, set[str]]:
    """Memory-only candidate matching. NEVER verifies provider treatment or causation."""
    matches = [
        e
        for e in events
        if e.ticker_hint == ticker
        and e.effective_from is not None
        and before < e.effective_from <= after
    ]
    kinds: set[str] = {e.kind for e in matches}
    if not matches:
        # No exhaustive coverage or independent market-move evidence: remain UNKNOWN.
        return DiscontinuityState.UNKNOWN, kinds
    incomplete = any(
        e.evidence_status != "PUBLIC_SOURCE_REVIEWED"
        or not e.official_terms
        or not e.share_class
        or (e.kind in ("split", "reverse_split") and e.ratio_new is None)
        or e.kind in ("unknown", "other")
        for e in matches
    )
    state = (
        DiscontinuityState.AMBIGUOUS
        if len(matches) > 1 or incomplete
        else DiscontinuityState.EVENT_CANDIDATE
    )
    return state, kinds


def new_event_counts() -> dict[str, int]:
    return dict(
        matched_split_candidates=0,
        matched_reverse_split_candidates=0,
        matched_spinoff_candidates=0,
        matched_dividend_candidates=0,
        matched_ticker_change_candidates=0,
        unmatched_discontinuities=0,
        multiple_candidate_matches=0,
        verified_event_count=0,
        unresolved_event_count=0,
        unknown_count=0,
        event_candidate_count=0,
        ambiguous_count=0,
        possible_market_move_count=0,
    )


def count_boundary(
    events: tuple[CorporateActionCandidate, ...],
    ticker: str,
    before: date,
    after: date,
    counts: dict[str, int],
) -> None:
    state, kinds = classify_boundary(events, ticker, before, after)
    counts[state.value.lower() + "_count"] += 1
    counts["unresolved_event_count"] += 1  # No auto-verification path exists.
    counts["unmatched_discontinuities"] += int(not kinds)
    matches = sum(
        e.ticker_hint == ticker
        and e.effective_from is not None
        and before < e.effective_from <= after
        for e in events
    )
    counts["multiple_candidate_matches"] += int(matches > 1)
    for name, accepted in (
        ("split", {"split"}),
        ("reverse_split", {"reverse_split"}),
        ("spinoff", {"spinoff"}),
        ("dividend", {"dividend", "special_dividend"}),
        ("ticker_change", {"ticker_change"}),
    ):
        counts[f"matched_{name}_candidates"] += int(bool(kinds & accepted))


def public_research_candidates() -> tuple[CorporateActionCandidate, ...]:
    """Public source candidates only; no identity clearance or automated adjustment."""
    return (
        CorporateActionCandidate(
            kind="split",
            ticker_hint="NOW",
            provider="issuer",
            evidence_reference="https://www.sec.gov/Archives/edgar/data/1373715/000137371525000321/now-20251205.htm",
            published_on=date(2025, 12, 5),
            effective_from=date(2025, 12, 18),
            observed_at=datetime.now(UTC),
        ),
    )
