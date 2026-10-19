"""Review evidence layered over the existing security registry, not a second resolver."""

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from trading_bot.provider import DataError
from trading_bot.security_identity import (
    CorporateEvent,
    SecurityRegistry,
    TickerAssignment,
    validate_window,
)


class EvidenceReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["UNKNOWN", "CONFIRMED"] = "UNKNOWN"
    sources: tuple[str, ...] = ()
    checked_on: date | None = None
    coverage_start: date | None = None
    coverage_end: date | None = None  # Inclusive review scope, NOT alias validity.
    note: str

    @model_validator(mode="after")
    def evidence_required(self) -> "EvidenceReview":
        if self.status == "CONFIRMED" and (
            not self.sources
            or any(not s.startswith("https://") for s in self.sources)
            or self.checked_on is None
            or self.coverage_start is None
            or self.coverage_end is None
            or self.coverage_start > self.coverage_end
            or self.coverage_end > self.checked_on
            or not self.note.strip()
        ):
            raise ValueError("confirmed review requires dated scoped evidence")
        return self

    def covers(self, days: set[date]) -> bool:
        return (
            bool(days)
            and self.status == "CONFIRMED"
            and self.coverage_start is not None
            and self.coverage_end is not None
            and self.coverage_start <= min(days)
            and max(days) <= self.coverage_end
        )


class SecurityReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    security_id: str
    identity: EvidenceReview
    ticker_periods: EvidenceReview
    corporate_actions: EvidenceReview
    observed_history: EvidenceReview


class IdentityCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    selection_reference: str
    reviews: tuple[SecurityReview, ...]
    assignments: tuple[TickerAssignment, ...]
    events: tuple[CorporateEvent, ...]

    @model_validator(mode="after")
    def unique_records(self) -> "IdentityCatalog":
        ids = [r.security_id for r in self.reviews]
        if len(ids) != 100 or len(set(ids)) != 100 or any(not sid.strip() for sid in ids):
            raise ValueError("catalog requires 100 unique securities")
        if {a.security_id for a in self.assignments} != set(ids):
            raise ValueError("catalog assignment identities mismatch")
        if len(self.assignments) != 101 or len({a.ticker for a in self.assignments}) != 101:
            raise ValueError("reviewed sample requires 101 distinct aliases")
        if any(e.security_id not in ids for e in self.events):
            raise ValueError("unknown corporate-event identity")
        return self

    def summary(self, days: set[date]) -> dict[str, int]:
        result = {}
        for name in ("identity", "ticker_periods", "corporate_actions", "observed_history"):
            result[name + "_confirmed"] = sum(
                getattr(r, name).status == "CONFIRMED" for r in self.reviews
            )
            result[name + "_scoped_confirmed"] = sum(
                getattr(r, name).covers(days) for r in self.reviews
            )
        return result

    def require_reviewed(self, days: set[date], observed_on: date) -> SecurityRegistry:
        IdentityCatalog.model_validate(self.model_dump())
        for record in self.reviews:
            # Actual history is the output of this experiment, not a prerequisite.
            for review in (record.identity, record.ticker_periods, record.corporate_actions):
                if (
                    not review.covers(days)
                    or review.checked_on is None
                    or review.checked_on > observed_on
                ):
                    raise DataError("insufficient scoped security evidence; stop before HTTP")
        registry = SecurityRegistry(
            security_ids=tuple(r.security_id for r in self.reviews),
            assignments=self.assignments,
            events=self.events,
        )
        validate_window(registry, days)
        if any(
            e.kind != "ticker_change" and min(days) <= e.effective_date <= max(days)
            for e in self.events
        ):
            raise DataError("unsupported reviewed corporate action; stop")
        return registry


def load_catalog() -> IdentityCatalog:
    catalog = IdentityCatalog.model_validate(
        yaml.safe_load(Path("config/security-identities-100.yaml").read_text(encoding="utf-8"))
    )
    selected = yaml.safe_load(Path("config/alpaca-readonly-100.yaml").read_text(encoding="utf-8"))
    if {a.ticker for a in catalog.assignments} != set(selected["symbols"]) | {"MRSH"}:
        raise ValueError("catalog differs from the fixed reviewed sample")
    if {a.security_id for a in catalog.assignments if a.ticker in ("MMC", "MRSH")} != {
        "sec-000001"
    }:
        raise ValueError("existing Marsh identity must be preserved")
    return catalog
