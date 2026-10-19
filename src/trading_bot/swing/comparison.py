"""Internal aggregates only: publication needs a separate reconstructability review."""

from typing import Any


def compare_private(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not results:
        raise ValueError("comparison requires independent strategy results")
    sample = next(iter(results.values()))
    rows = {}
    for name, result in results.items():
        if (
            result["evaluation_window"] != sample["evaluation_window"]
            or result["initial_jpy"] != sample["initial_jpy"]
            or result["data_history"] != sample["data_history"]
            or result["input"]["context"]["rules"] != sample["input"]["context"]["rules"]
        ):
            raise ValueError("comparison requires the same window and starting capital")
        if not result["private_only"] or result["external_ai_sent"]:
            raise ValueError("only private offline results may be compared")
        rows[name] = dict(
            performance=result["performance"],
            all_country={k: result["all_country"].get(k) for k in ("status", "excess_return")},
        )
    return dict(private_only=True, publication_reviewed=False, strategies=rows)
