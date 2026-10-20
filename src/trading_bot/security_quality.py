"""Offline per-security observations. Research quality is never a market trading grant."""

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from trading_bot.alpaca import NY, DailyBatch
from trading_bot.broker.execution import session, window
from trading_bot.data_access import ReadOnlyQualityReport, reject_diagnostic
from trading_bot.identity_catalog import IdentityCatalog
from trading_bot.models import PriceBar
from trading_bot.portfolio.models import utc
from trading_bot.security_identity import CorporateEvent, SecurityRegistry, TickerAssignment


class BatchQualityStop(ValueError):
    pass


class SecurityQualityStop(ValueError):
    pass


class ReviewFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["PASSED", "UNKNOWN", "UNSUPPORTED"] = "UNKNOWN"
    observed_at: datetime
    coverage_start: date | None = None
    coverage_end: date | None = None
    sources: tuple[str, ...] = ()
    _time = field_validator("observed_at")(utc)

    def at(self, at: datetime, days: tuple[date, ...], *, synthetic_contract: bool = False) -> str:
        if self.observed_at > at:
            return "UNKNOWN"
        if self.status == "UNSUPPORTED":
            return "UNSUPPORTED"
        if (
            self.status == "PASSED"
            and self.sources
            and self.coverage_start is not None
            and self.coverage_end is not None
            and self.coverage_start <= min(days) <= max(days) <= self.coverage_end
            and (
                self.coverage_end <= self.observed_at.date()
                or (synthetic_contract and all(s.startswith("synthetic://") for s in self.sources))
            )
        ):
            return "PASSED"
        return "UNKNOWN"


class SecurityEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    identity: ReviewFact
    corporate_actions: ReviewFact
    permission: Literal["UNKNOWN", "PRIVATE_READ_ONLY", "SYNTHETIC_ONLY"] = "UNKNOWN"
    permission_observed_at: datetime
    _time = field_validator("permission_observed_at")(utc)


@dataclass(frozen=True)
class QualityUniverse:
    assignments: tuple[TickerAssignment, ...]
    events: tuple[CorporateEvent, ...] = ()

    @classmethod
    def from_catalog(cls, catalog: IdentityCatalog) -> "QualityUniverse":
        # Metadata reuse only. No live adapter, history joining or review promotion.
        return cls(catalog.assignments, catalog.events)

    def validate(self) -> dict[str, tuple[TickerAssignment, ...]]:
        grouped: dict[str, list[TickerAssignment]] = {}
        aliases = set()
        for a in self.assignments:
            TickerAssignment.model_validate(a.model_dump())
            if not a.security_id or a.ticker in aliases:
                raise BatchQualityStop("ambiguous security or alias collision")
            aliases.add(a.ticker)
            grouped.setdefault(a.security_id, []).append(a)
        if not grouped:
            raise BatchQualityStop("empty population")
        for values in grouped.values():
            if len(values) > 1:
                # Existing registry supplies the known interval/event semantics. Unknown
                # multi-alias mappings are not merged by this new quality layer.
                try:
                    SecurityRegistry(
                        security_ids=(values[0].security_id,),
                        assignments=tuple(values),
                        events=tuple(
                            e for e in self.events if e.security_id == values[0].security_id
                        ),
                    )
                except ValueError:
                    raise BatchQualityStop("unreviewed multi-alias identity") from None
        return {sid: tuple(values) for sid, values in grouped.items()}


@dataclass(frozen=True)
class SecurityQuality:
    security_id: str
    ticker: str
    assessed_at: datetime
    data_quality_status: str
    identity_review_status: str
    corporate_action_status: str
    data_usage_permission: str
    strategy_eligibility: Literal["ELIGIBLE", "QUARANTINED", "UNKNOWN"]
    reasons: tuple[str, ...]
    evidence_times: tuple[datetime, ...]


@dataclass(frozen=True)
class QualityAssessment:
    as_of: datetime
    origin: Literal["synthetic", "market"]
    population: tuple[SecurityQuality, ...]

    def __post_init__(self) -> None:
        utc(self.as_of)
        if (
            len({s.security_id for s in self.population}) != len(self.population)
            or len({s.ticker for s in self.population}) != len(self.population)
            or any(s.assessed_at != self.as_of for s in self.population)
        ):
            raise BatchQualityStop("inconsistent quality population or observation time")

    def require(self, ticker: str) -> None:
        matching = [s for s in self.population if s.ticker == ticker]
        if self.origin != "synthetic" or len(matching) != 1:
            raise SecurityQualityStop("synthetic eligible security required")
        s = matching[0]
        if (
            s.strategy_eligibility != "ELIGIBLE"
            or s.data_quality_status != "PASSED"
            or s.identity_review_status != "PASSED"
            or s.corporate_action_status != "PASSED"
            or s.data_usage_permission != "SYNTHETIC_ONLY"
        ):
            raise SecurityQualityStop("UNKNOWN or QUARANTINED security rejected")

    def summary(self) -> dict[str, object]:
        return dict(
            data_kind=self.origin,
            population=len(self.population),
            states={
                state: sum(s.strategy_eligibility == state for s in self.population)
                for state in ("ELIGIBLE", "QUARANTINED", "UNKNOWN")
            },
            reasons=dict(Counter(r for s in self.population for r in s.reasons)),
            as_of=self.as_of.isoformat(),
        )

    def diagnostic_report(self) -> ReadOnlyQualityReport:
        import json

        return ReadOnlyQualityReport(
            json.dumps(self.summary() | dict(purpose="diagnostic_only", strategy_approved=False))
        )


@dataclass
class QualityHistory:
    """Append-only simulation audit. No retrospective removal or automatic release."""

    snapshots: list[QualityAssessment] = field(default_factory=list)

    def observe(self, assessment: QualityAssessment) -> QualityAssessment:
        if self.snapshots:
            prior = self.snapshots[-1]
            if assessment.as_of <= prior.as_of or assessment.origin != prior.origin:
                raise BatchQualityStop("quality history must advance in observation time")
            if {s.security_id for s in assessment.population} != {
                s.security_id for s in prior.population
            }:
                raise BatchQualityStop("population changed; exclusion bias risk")
            blocked = {
                s.security_id: s
                for s in prior.population
                if s.strategy_eligibility == "QUARANTINED"
            }
            assessment = replace(
                assessment,
                population=tuple(
                    replace(
                        s,
                        strategy_eligibility="QUARANTINED",
                        reasons=tuple(
                            sorted(
                                set(s.reasons)
                                | set(blocked[s.security_id].reasons)
                                | {"prior_quarantine_requires_review"}
                            )
                        ),
                    )
                    if s.security_id in blocked
                    else s
                    for s in assessment.population
                ),
            )
        self.snapshots.append(assessment)
        return assessment


def evaluate_quality(
    universe: QualityUniverse,
    batch: DailyBatch,
    evidence: dict[str, SecurityEvidence],
    at: datetime,
    *,
    origin: Literal["synthetic", "market"],
    minimum_sessions: int = 60,
) -> QualityAssessment:
    """No I/O. Attribute local defects only after validating batch and provenance."""
    reject_diagnostic(batch)
    at = utc(at)
    grouped = universe.validate()
    aliases = {a.ticker for a in universe.assignments}
    days = batch.expected_sessions
    if (
        not batch.complete_response
        or batch.feed != "iex"
        or batch.adjustment != "raw"
        or batch.calendar != "NYSE"
        or batch.excluded_unconfirmed
        or batch.invalid_rows
        or batch.duplicate_rows
        or not days
        or list(days) != sorted(set(days))
        or set(batch.bars) - aliases
        or set(evidence) - set(grouped)
    ):
        raise BatchQualityStop("batch protocol, paging, conditions or scope failure")
    for day in days:
        calendar_span = session("US", day.isoformat())
        if not calendar_span or calendar_span["market_close"] > at:
            raise BatchQualityStop("invalid or unconfirmed expected session")
    results = []
    for sid, assignments in grouped.items():
        fact = evidence.get(sid)
        identity = (
            fact.identity.at(at, days, synthetic_contract=origin == "synthetic")
            if fact
            else "UNKNOWN"
        )
        a = next(
            (
                a
                for a in assignments
                if a.valid_from <= max(days) and (a.valid_to is None or max(days) < a.valid_to)
            ),
            assignments[-1],
        )
        reasons = []
        valid = []
        found = set()
        observations = [
            (binding, raw) for binding in assignments for raw in batch.bars.get(binding.ticker, [])
        ]
        for binding, raw in observations:
            # Never assign a malformed/foreign record to a different security.
            if (raw.symbol, raw.market, raw.currency) != (
                binding.ticker,
                binding.market,
                binding.currency,
            ):
                raise BatchQualityStop("unattributable provenance mismatch")
            if (origin == "synthetic" and raw.source != "synthetic") or (
                origin == "market" and raw.source != "alpaca"
            ):
                raise BatchQualityStop("data origin mismatch")
            if raw.interval != "1d" or raw.adjusted:
                raise BatchQualityStop("unexpected bar conditions")
            if raw.fetched_at > at:
                raise BatchQualityStop("future observations forbidden")
            span = window(raw)
            if span is None or span[1] > min(raw.fetched_at, at):
                raise BatchQualityStop("unconfirmed or invalid session bar")
            try:
                bar = PriceBar.model_validate(raw.model_dump())
            except ValueError:
                reasons.append("invalid_ohlcv")
                continue
            day = bar.timestamp.astimezone(NY).date()
            if day not in days or (
                binding.identity_confirmed
                and identity == "PASSED"
                and (day < binding.valid_from or (binding.valid_to and day >= binding.valid_to))
            ):
                reasons.append("out_of_period")
            if day in found:
                reasons.append("duplicate")
            found.add(day)
            valid.append(bar)
        if set(days) - found:
            reasons.append("missing")
        valid.sort(key=lambda b: b.timestamp)
        if any(
            abs(b.close / a.close - 1) > 0.35 for a, b in zip(valid[:-1], valid[1:], strict=True)
        ):
            reasons.append("discontinuity")
        quality = "FAILED" if reasons else "PASSED"
        actions = (
            fact.corporate_actions.at(at, days, synthetic_contract=origin == "synthetic")
            if fact
            else "UNKNOWN"
        )
        permission = fact.permission if fact and fact.permission_observed_at <= at else "UNKNOWN"
        if not all(
            a.identity_confirmed and a.exchange is not None and a.evidence for a in assignments
        ):
            identity = "UNKNOWN"
        if len(days) < minimum_sessions:
            quality = "UNKNOWN" if not reasons else "FAILED"
            reasons.append("insufficient_history")
        if actions == "UNSUPPORTED":
            reasons.append("unsupported_corporate_action")
        if identity != "PASSED":
            reasons.append("identity_unknown")
        if actions == "UNKNOWN":
            reasons.append("corporate_action_unknown")
        if permission != "SYNTHETIC_ONLY" or origin != "synthetic":
            reasons.append("strategy_permission_missing")
        quarantined = quality == "FAILED" or actions == "UNSUPPORTED"
        eligibility: Literal["ELIGIBLE", "QUARANTINED", "UNKNOWN"] = (
            "QUARANTINED" if quarantined else "UNKNOWN" if reasons else "ELIGIBLE"
        )
        results.append(
            SecurityQuality(
                sid,
                a.ticker,
                at,
                quality,
                identity,
                actions,
                permission,
                eligibility,
                tuple(sorted(set(reasons))),
                (
                    fact.identity.observed_at,
                    fact.corporate_actions.observed_at,
                    fact.permission_observed_at,
                )
                if fact
                else (),
            )
        )
    return QualityAssessment(at, origin, tuple(results))
