"""Local licensed NAV bridge; preserve existing application/publication constraints."""

from datetime import datetime

from trading_bot.benchmark import (
    BenchmarkInvestment,
    BenchmarkProvider,
    benchmark_value,
    validate_application,
)
from trading_bot.portfolio.models import Account


def compare_all_country(
    provider: BenchmarkProvider,
    investment: BenchmarkInvestment | None,
    account: Account,
    equity: list[tuple[datetime, float]],
    *,
    internal_use_allowed: bool = False,
    publish: bool = False,
) -> dict[str, object]:
    if not internal_use_allowed:
        raise ValueError("NAV internal use rights must be confirmed before access")
    if investment is None:
        return {"status": "NOT_INITIALIZED", "observations": [], "excess_return": None}
    validate_application(
        account.created_at,
        investment.purchase_date,
        investment.accepted_on,
        investment.application_calendar,
    )
    values = provider.observations()
    if publish and (
        not investment.nav.derivative_publication_allowed
        or any(not nav.derivative_publication_allowed for nav in values)
    ):
        raise ValueError("NAV-derived publication is not authorized")
    rows = []
    for at, total in equity:
        value, nav, reason = benchmark_value(investment, values, account, at)
        rows.append(
            {
                "at": at.isoformat(),
                "equity_jpy": total,
                "benchmark_jpy": None if value is None else str(value),
                "nav_date": None if nav is None else nav.nav_date.isoformat(),
                "reason": reason,
            }
        )
    complete = bool(rows) and all(row["benchmark_jpy"] is not None for row in rows)
    difference = None
    if complete:
        difference = (
            equity[-1][1] / float(account.initial_jpy)
            - 1
            - (float(str(rows[-1]["benchmark_jpy"])) / float(account.initial_jpy) - 1)
        )
    return {
        "status": "COMPLETE" if complete else "UNAVAILABLE_MATCHED_NAV",
        "observations": rows,
        "excess_return": difference,
        "publication": "authorized" if publish else "private only",
    }
