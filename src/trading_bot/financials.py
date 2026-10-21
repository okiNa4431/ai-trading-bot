"""JP-3 point-in-time financial contracts. No network, storage, AI or orders."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_bot.data_access import DiagnosticOnly, MarketAccessDisabled
from trading_bot.portfolio.models import utc
from trading_bot.security_quality import ReviewFact


class FinancialStop(ValueError):
    pass


class Contract(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
    )


class Issuer(Contract):
    issuer_id: str = Field(min_length=1)
    edinet_code: str | None = Field(default=None, pattern=r"^E[0-9]{5}$")


class IssuerName(Contract):
    issuer_id: str
    name: str = Field(min_length=1)
    valid_from: date
    valid_until: date | None = None
    evidence: ReviewFact

    @model_validator(mode="after")
    def interval(self) -> "IssuerName":
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise FinancialStop("NAME_INTERVAL_INVALID")
        return self


class IssuerLink(Contract):
    security_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    valid_from: date
    valid_until: date | None = None  # inclusive
    evidence: ReviewFact

    @model_validator(mode="after")
    def interval(self) -> "IssuerLink":
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise FinancialStop("IDENTITY_INTERVAL_INVALID")
        return self


class IssuerRegistry(Contract):
    issuers: tuple[Issuer, ...]
    links: tuple[IssuerLink, ...]
    names: tuple[IssuerName, ...] = ()

    @model_validator(mode="after")
    def unique(self) -> "IssuerRegistry":
        ids = [item.issuer_id for item in self.issuers]
        codes = [item.edinet_code for item in self.issuers if item.edinet_code is not None]
        if len(set(ids)) != len(ids) or len(set(codes)) != len(codes):
            raise FinancialStop("ISSUER_COLLISION")
        items: tuple[IssuerLink | IssuerName, ...] = (*self.links, *self.names)
        for item in items:
            if item.issuer_id not in ids:
                raise FinancialStop("UNKNOWN_ISSUER")
        groups: tuple[tuple[tuple[IssuerLink | IssuerName, ...], str], ...] = (
            (self.links, "security_id"),
            (self.names, "issuer_id"),
        )
        for group, key in groups:
            for index, left in enumerate(group):
                for right in group[index + 1 :]:
                    if getattr(left, key) == getattr(right, key) and (
                        left.valid_from <= (right.valid_until or date.max)
                        and right.valid_from <= (left.valid_until or date.max)
                    ):
                        raise FinancialStop("IDENTITY_OR_NAME_OVERLAP")
        return self

    def resolve(self, security_id: str, day: date, decision_at: datetime) -> str | None:
        decision_at = utc(decision_at)
        links = [
            item
            for item in self.links
            if item.security_id == security_id
            and item.valid_from <= day <= (item.valid_until or date.max)
        ]
        if len(links) != 1:
            return None
        link = links[0]
        if link.evidence.at(decision_at, (day,)) != "PASSED":
            return None
        return link.issuer_id


class AccountingPeriod(Contract):
    start: date
    end: date
    kind: Literal["ANNUAL", "QUARTER_YTD", "QUARTER_STANDALONE", "UNKNOWN"]
    quarter: int | None = Field(default=None, ge=1, le=4)

    @model_validator(mode="after")
    def period(self) -> "AccountingPeriod":
        if self.end < self.start or (self.kind.startswith("QUARTER") and self.quarter is None):
            raise FinancialStop("ACCOUNTING_PERIOD_INVALID")
        if self.kind == "ANNUAL" and self.quarter is not None:
            raise FinancialStop("ACCOUNTING_PERIOD_INVALID")
        return self


Metric = Literal[
    "revenue",
    "operating_income",
    "net_income",
    "total_assets",
    "net_assets",
    "equity_ratio",
    "operating_cash_flow",
    "roe",
    "revenue_growth",
    "profit_growth",
]


class FinancialValue(Contract):
    metric: Metric
    value: Decimal | None
    unit: Literal[
        "CURRENCY", "THOUSAND_CURRENCY", "MILLION_CURRENCY", "FRACTION", "PERCENT", "UNKNOWN"
    ]
    origin: Literal["PROVIDER", "INTERNAL_DERIVED"] = "PROVIDER"
    missing_reason: str | None = None
    formula: str | None = None
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def semantics(self) -> "FinancialValue":
        if self.value is None and not self.missing_reason:
            raise FinancialStop("MISSING_REASON_REQUIRED")
        if self.value is not None and self.missing_reason is not None:
            raise FinancialStop("MISSING_VALUE_CONFLICT")
        if (
            self.value is not None
            and self.metric in ("total_assets", "net_assets")
            and self.value < 0
        ):
            # Net assets can be negative (excess liabilities); total assets cannot.
            if self.metric == "total_assets":
                raise FinancialStop("NEGATIVE_ASSETS")
        if self.origin == "INTERNAL_DERIVED" and (not self.formula or not self.evidence_ids):
            raise FinancialStop("DERIVATION_EVIDENCE_REQUIRED")
        if self.origin == "PROVIDER" and self.formula is not None:
            raise FinancialStop("PROVIDER_FORMULA_CONFLICT")
        return self


class FinancialStatement(Contract, DiagnosticOnly):
    purpose: Literal["diagnostic_only"] = "diagnostic_only"
    strategy_approved: Literal[False] = False
    data_kind: Literal["SYNTHETIC", "REAL"]
    issuer_id: str = Field(min_length=1)
    issuer_name_at_document: str | None = None
    document_id: str | None = None
    revision_id: str | None = None
    supersedes_revision: str | None = None
    period: AccountingPeriod
    standard: Literal["JGAAP", "IFRS", "USGAAP", "UNKNOWN"]
    basis: Literal["CONSOLIDATED", "STANDALONE", "UNKNOWN"]
    currency: Literal["JPY", "USD", "UNKNOWN"]
    published_at: datetime | None = None
    publication_date: date | None = None
    publication_precision: Literal["INSTANT", "DATE_ONLY", "UNKNOWN"] = "UNKNOWN"
    fact_kind: Literal["ACTUAL", "FORECAST", "UNKNOWN"] = "UNKNOWN"
    provider_available_at: datetime | None = None
    retrieved_at: datetime
    revision_available_at: datetime | None = None
    version_evidence: Literal["VERIFIED", "SYNTHETIC", "UNKNOWN"] = "UNKNOWN"
    evidence_ids: tuple[str, ...] = ()
    source: str = Field(min_length=1)
    usage_permission: Literal["UNKNOWN", "PRIVATE_READ_ONLY", "SYNTHETIC_ONLY"] = "UNKNOWN"
    external_ai: Literal["BLOCKED"] = "BLOCKED"
    values: tuple[FinancialValue, ...]

    def __repr__(self) -> str:
        return "FinancialStatement(private diagnostic observation)"

    def __str__(self) -> str:
        return self.__repr__()

    _retrieved = field_validator("retrieved_at")(utc)

    @field_validator("published_at", "provider_available_at", "revision_available_at")
    @classmethod
    def optional_time(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value is not None else None

    @model_validator(mode="after")
    def chronology(self) -> "FinancialStatement":
        if self.publication_precision != "INSTANT" and self.published_at is not None:
            raise FinancialStop("PUBLICATION_PRECISION_CONFLICT")
        if self.publication_precision == "DATE_ONLY" and self.publication_date is None:
            raise FinancialStop("PUBLICATION_DATE_REQUIRED")
        if self.data_kind == "REAL" and (
            self.version_evidence == "SYNTHETIC" or self.usage_permission == "SYNTHETIC_ONLY"
        ):
            raise FinancialStop("REAL_SYNTHETIC_PERMISSION_CONFLICT")
        names = [value.metric for value in self.values]
        if len(names) != len(set(names)):
            raise FinancialStop("DUPLICATE_FINANCIAL_METRIC")
        times = (self.published_at, self.provider_available_at, self.revision_available_at)
        if any(value is not None and value > self.retrieved_at for value in times):
            raise FinancialStop("FUTURE_PUBLICATION_OR_DELIVERY")
        if self.published_at is not None and self.published_at.date() < self.period.end:
            raise FinancialStop("PUBLICATION_BEFORE_PERIOD_END")
        if self.supersedes_revision is not None and self.supersedes_revision == self.revision_id:
            raise FinancialStop("REVISION_CYCLE")
        if self.data_kind == "SYNTHETIC" and (
            not self.source.startswith("synthetic://")
            or not self.issuer_id.startswith("synthetic:")
        ):
            raise FinancialStop("SYNTHETIC_PROVENANCE_REQUIRED")
        return self

    def known_at(self, at: datetime) -> bool:
        at = utc(at)
        required = (self.published_at, self.revision_available_at, self.retrieved_at)
        return bool(
            self.document_id
            and self.revision_id
            and self.evidence_ids
            and self.version_evidence != "UNKNOWN"
            and self.standard != "UNKNOWN"
            and self.basis != "UNKNOWN"
            and self.currency != "UNKNOWN"
            and self.period.kind != "UNKNOWN"
            and self.publication_precision == "INSTANT"
            and self.fact_kind == "ACTUAL"
            and self.usage_permission != "UNKNOWN"
            and all(v.value is None or v.unit != "UNKNOWN" for v in self.values)
            and all(t is not None and t <= at for t in required)
            and (self.provider_available_at is None or self.provider_available_at <= at)
        )

    def metric(self, name: Metric) -> FinancialValue | None:
        return next((value for value in self.values if value.metric == name), None)


def select_as_of(
    observations: tuple[FinancialStatement, ...],
    issuer_id: str,
    period: AccountingPeriod,
    decision_at: datetime,
) -> FinancialStatement | None:
    keys = [
        (row.source, row.document_id, row.revision_id, row.retrieved_at) for row in observations
    ]
    if len(keys) != len(set(keys)):
        raise FinancialStop("DUPLICATE_OBSERVATION")
    visible = [
        row
        for row in observations
        if row.issuer_id == issuer_id and row.period == period and row.known_at(decision_at)
    ]
    if not visible:
        return None
    if any(
        row.issuer_id == issuer_id
        and row.period == period
        and row.retrieved_at <= utc(decision_at)
        and not row.known_at(decision_at)
        for row in observations
    ):
        return None
    # Same revision with disagreeing observations is not silently overwritten.
    versions: dict[str, FinancialStatement] = {}
    for row in visible:
        assert row.revision_id is not None
        previous = versions.get(row.revision_id)
        if previous is not None:
            left = previous.model_dump(exclude={"retrieved_at"})
            right = row.model_dump(exclude={"retrieved_at"})
            if left != right:
                raise FinancialStop("REVISION_CONTENT_CONFLICT")
        versions[row.revision_id] = row
    roots = [row for row in versions.values() if row.supersedes_revision is None]
    if len(roots) != 1:
        return None
    current, seen = roots[0], set()
    while current.revision_id not in seen:
        seen.add(current.revision_id)
        children = [
            row for row in versions.values() if row.supersedes_revision == current.revision_id
        ]
        if not children:
            return current if len(seen) == len(versions) else None
        if len(children) != 1:
            return None
        if children[0].retrieved_at < current.retrieved_at:
            return None
        current = children[0]
    return None


def revenue_growth(
    current: FinancialStatement, previous: FinancialStatement
) -> FinancialValue | None:
    if current.issuer_id != previous.issuer_id:
        raise FinancialStop("ISSUER_MISMATCH")
    if (
        current.standard,
        current.basis,
        current.currency,
        current.period.kind,
        current.period.quarter,
    ) != (
        previous.standard,
        previous.basis,
        previous.currency,
        previous.period.kind,
        previous.period.quarter,
    ) or "UNKNOWN" in (current.standard, current.basis, current.currency, current.period.kind):
        raise FinancialStop("INCOMPARABLE_FINANCIAL_CONTEXT")
    if (
        abs((current.period.end - previous.period.end).days - 365) > 7
        or abs(
            (current.period.end - current.period.start).days
            - (previous.period.end - previous.period.start).days
        )
        > 7
    ):
        raise FinancialStop("INCOMPARABLE_ACCOUNTING_PERIOD")
    left, right = current.metric("revenue"), previous.metric("revenue")
    if (
        left is None
        or right is None
        or left.value is None
        or right.value is None
        or right.value <= 0
    ):
        return None
    if left.unit != right.unit or left.unit not in (
        "CURRENCY",
        "THOUSAND_CURRENCY",
        "MILLION_CURRENCY",
    ):
        raise FinancialStop("INCOMPARABLE_UNITS")
    if not current.evidence_ids or not previous.evidence_ids:
        return None
    return FinancialValue(
        metric="revenue_growth",
        value=(left.value / right.value) - 1,
        unit="FRACTION",
        origin="INTERNAL_DERIVED",
        formula="current_revenue / prior_year_revenue - 1",
        evidence_ids=(*current.evidence_ids, *previous.evidence_ids),
    )


class FinancialProvider(Protocol):
    def fetch(self, issuer_id: str) -> tuple[FinancialStatement, ...]: ...


class SyntheticFinancialInput(Contract):
    data_kind: Literal["SYNTHETIC"] = "SYNTHETIC"
    purpose: Literal["synthetic_schema_test"] = "synthetic_schema_test"
    security_id: str = Field(pattern=r"^synthetic:")
    issuer_id: str = Field(pattern=r"^synthetic:")
    decision_at: datetime
    financial: FinancialStatement
    uncertainty: tuple[str, ...]
    permission: Literal["SYNTHETIC_ONLY"] = "SYNTHETIC_ONLY"
    _time = field_validator("decision_at")(utc)

    @model_validator(mode="after")
    def synthetic_only(self) -> "SyntheticFinancialInput":
        row = self.financial
        if (
            row.data_kind != "SYNTHETIC"
            or row.usage_permission != "SYNTHETIC_ONLY"
            or row.version_evidence != "SYNTHETIC"
            or self.issuer_id != row.issuer_id
            or not row.evidence_ids
            or any(not e.startswith("synthetic://") for e in row.evidence_ids)
            or not row.known_at(self.decision_at)
        ):
            raise MarketAccessDisabled("REAL_FINANCIAL_AI_INPUT_BLOCKED")
        return self


def synthetic_input(
    security_id: str,
    financial: FinancialStatement,
    decision_at: datetime,
    registry: IssuerRegistry,
) -> SyntheticFinancialInput:
    if financial.data_kind != "SYNTHETIC":
        raise MarketAccessDisabled("REAL_FINANCIAL_AI_INPUT_BLOCKED")
    issuer = registry.resolve(security_id, utc(decision_at).date(), decision_at)
    if issuer != financial.issuer_id:
        raise MarketAccessDisabled("FINANCIAL_IDENTITY_UNKNOWN")
    expected: tuple[Metric, ...] = (
        "revenue",
        "operating_income",
        "net_income",
        "total_assets",
        "net_assets",
        "equity_ratio",
        "operating_cash_flow",
        "roe",
        "revenue_growth",
        "profit_growth",
    )
    present = {value.metric: value for value in financial.values}
    missing = tuple(name for name in expected if name not in present or present[name].value is None)
    return SyntheticFinancialInput(
        security_id=security_id,
        issuer_id=issuer,
        decision_at=decision_at,
        financial=financial,
        uncertainty=missing,
    )
