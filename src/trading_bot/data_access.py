"""Phase 5.5 access policy. Researching a provider never activates data collection."""

import json
from dataclasses import dataclass, field


class MarketAccessDisabled(ValueError):
    pass


class DiagnosticOnly:
    """Marker for private observations as well as aggregate diagnostic reports."""


@dataclass(frozen=True)
class ReadOnlyQualityReport(DiagnosticOnly):
    """Aggregate-only diagnostic result. No bars, strategy context or conversion API."""

    aggregate_json: str = field(repr=False)

    def summary(self) -> dict[str, object]:
        return dict(json.loads(self.aggregate_json))


def reject_diagnostic(value: object) -> None:
    if isinstance(value, DiagnosticOnly) or (
        isinstance(value, dict) and value.get("purpose") == "diagnostic_only"
    ):
        raise MarketAccessDisabled(
            "diagnostic-only reports cannot enter strategy, execution or storage"
        )
    if isinstance(value, dict):
        for item in value.values():
            reject_diagnostic(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_diagnostic(item)


def require_live_market_access() -> None:
    # Owner explicitly paused ALL automated real-market collection on 2026-10-17.
    # No environment variable, Workflow input or alternate account overrides it.
    raise MarketAccessDisabled(
        "Real-market collection is disabled pending provider rights and explicit activation"
    )
