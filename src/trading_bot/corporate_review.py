"""Untrusted/offline event candidates. No discovery API, registry promotion or adjustment."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from trading_bot.portfolio.models import utc


class CorporateActionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["split", "dividend", "spinoff", "ticker_change", "unknown"]
    provider: str
    evidence_reference: str
    ticker_hint: str  # Not an approved security identity.
    security_id_hint: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    published_at: datetime | None = None
    observed_at: datetime
    review_status: Literal["UNKNOWN"] = "UNKNOWN"

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
        return self
