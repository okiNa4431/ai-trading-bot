"""Aggregate-only observations; never repair or map market data."""

from datetime import date

from trading_bot.alpaca import NY, DailyBatch

# Public issuer announcement. Reviewed fact, not an inferred provider mapping.
# https://www.marsh.com/en/corp/about/news/marsh-mclennan-to-change-nyse-symbol-to-mrsh.html
MMC_CHANGE_DATE = date(2026, 1, 14)


def new_checks() -> dict[str, object]:
    return dict(
        paging=["NOT_RUN", "NOT_RUN"],
        completeness=["NOT_RUN", "NOT_RUN"],
        discontinuity="NOT_RUN",
        repeat_consistency="NOT_RUN",
    )


def missing_diagnostics(batch: DailyBatch | None) -> dict[str, object]:
    counts = dict(leading=0, trailing=0, interior_or_mixed=0, entire=0)
    pattern = event_match = 0
    if batch is not None:
        days = batch.expected_sessions
        expected = set(days)
        for symbol, bars in batch.bars.items():
            found = {b.timestamp.astimezone(NY).date() for b in bars} & expected
            missing = expected - found
            if not missing:
                continue
            if missing == expected:
                counts["entire"] += 1
            elif missing == set(days[: len(missing)]):
                counts["leading"] += 1
            elif missing == set(days[-len(missing) :]):
                counts["trailing"] += 1
            else:
                counts["interior_or_mixed"] += 1
            if len(days) == 221 and found == set(days[:30]) and len(missing) == 191:
                pattern += 1
            before = {d for d in days if d < MMC_CHANGE_DATE}
            after = expected - before
            if (
                batch.complete_response
                and symbol == "MMC"
                and before
                and after
                and found == before
                and missing == after
            ):
                event_match += 1
    return dict(
        missing_pattern_symbols=counts,
        prefix_30_trailing_191_symbols=pattern,
        known_change_boundary_matches=event_match,
        diagnostic_basis=("complete_response" if batch.complete_response else "partial_response")
        if batch is not None
        else "unavailable",
    )
