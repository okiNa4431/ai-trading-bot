"""Decimal comparisons and immutable revisions of point-in-time daily snapshots."""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from pydantic import Field

from trading_bot.benchmark import JST, NAV, BenchmarkInvestment, benchmark_value, content_hash
from trading_bot.persistence.sqlite_repository import PortfolioRepository
from trading_bot.portfolio.models import Account, Contract, Execution, Valuation, utc


class Snapshot(Contract):
    as_of: datetime
    recorded_at: datetime
    revision: int = Field(default=1, gt=0)
    bot_jpy: Decimal
    benchmark_jpy: Decimal | None
    bot_return: Decimal
    benchmark_return: Decimal | None
    excess_return: Decimal | None
    valuation: Valuation
    nav: NAV | None
    complete: bool
    warnings: list[str]


class SnapshotRevision(Contract):
    previous: Snapshot
    replaced_at: datetime
    reason: str


def snapshot(
    repository: PortfolioRepository,
    valuation: Valuation,
    at: datetime,
    correction_reason: str | None = None,
) -> Snapshot:
    as_of = utc(valuation.as_of)
    recorded_at = utc(at)
    if recorded_at < as_of:
        raise ValueError("cannot save a future valuation")
    with repository.transaction() as tx:
        account = tx.account()
        if as_of < (account.last_event_at or account.created_at):
            raise ValueError("snapshot predates current account state")
        # Do not accept an arbitrary valuation disconnected from the current account.
        if valuation.realized_pnl != account.realized_pnl or {
            p.symbol: p.quantity for p in valuation.positions
        } != {s: p.quantity for s, p in account.positions.items() if p.quantity}:
            raise ValueError("valuation does not match account state")
        from trading_bot.portfolio.models import TradingRules
        from trading_bot.portfolio.valuation import value_portfolio

        recomputed = value_portfolio(
            account,
            {m.symbol: m for m in valuation.marks},
            valuation.fx,
            as_of,
            TradingRules(),
        )
        if recomputed.model_dump(exclude={"valuation_id"}) != valuation.model_dump(
            exclude={"valuation_id"}
        ):
            raise ValueError("valuation must be recomputed from observed marks")
        investment_raw = tx.get("benchmark_investments", "all-country")
        investment = (
            BenchmarkInvestment.model_validate_json(investment_raw) if investment_raw else None
        )
        navs = [NAV.model_validate_json(s) for s in tx.records("benchmark_prices")]
        benchmark, nav, warning = benchmark_value(investment, navs, account, as_of)
        bot_return = valuation.total_jpy / account.initial_jpy - 1
        benchmark_return = benchmark / account.initial_jpy - 1 if benchmark is not None else None
        result = Snapshot(
            as_of=as_of,
            recorded_at=recorded_at,
            bot_jpy=valuation.total_jpy,
            benchmark_jpy=benchmark,
            bot_return=bot_return,
            benchmark_return=benchmark_return,
            excess_return=bot_return - benchmark_return if benchmark_return is not None else None,
            valuation=valuation,
            nav=nav,
            complete=warning is None and not valuation.breaches,
            warnings=([warning] if warning else []) + valuation.breaches,
        )
        key = as_of.isoformat()
        raw = tx.get("snapshots", key)
        if raw:
            old = Snapshot.model_validate_json(raw)
            exclusions = {"recorded_at", "revision", "valuation"}
            if old.model_dump(exclude=exclusions) == result.model_dump(exclude=exclusions) and (
                old.valuation.model_dump(exclude={"valuation_id"})
                == valuation.model_dump(exclude={"valuation_id"})
            ):
                return old
            if not correction_reason:
                raise ValueError("snapshot correction requires an audit reason")
            tx.put(
                "snapshot_revisions",
                str(uuid4()),
                SnapshotRevision(
                    previous=old,
                    replaced_at=recorded_at,
                    reason=correction_reason,
                ),
            )
            result = result.model_copy(update={"revision": old.revision + 1})
        tx.put("snapshots", key, result)
        return result


def maximum_drawdown(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    peak = values[0]
    worst = Decimal(0)
    for value in values:
        if not value.is_finite() or value <= 0:
            raise ValueError("positive finite asset values required")
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def performance(
    account: Account, snapshots: list[Snapshot], executions: list[Execution]
) -> dict[str, object]:
    # One last evaluation per JST day; no interpolation across missing days.
    days = {s.as_of.astimezone(JST).date(): s for s in sorted(snapshots, key=lambda s: s.as_of)}
    series = list(days.values())
    sales = [e for e in executions if e.side == "SELL"]
    costs = {
        c: sum(
            (
                e.commission + e.slippage_cost + e.rounding_cost
                for e in executions
                if e.currency == c
            ),
            Decimal(0),
        )
        for c in ("JPY", "USD")
    }
    last = series[-1] if series else None
    return {
        "bot_jpy": str(last.bot_jpy) if last else None,
        "bot_pnl_jpy": str(last.bot_jpy - account.initial_jpy) if last else None,
        "excess_pnl_jpy": str(last.bot_jpy - last.benchmark_jpy)
        if last and last.benchmark_jpy is not None
        else None,
        "benchmark_jpy": str(last.benchmark_jpy)
        if last and last.benchmark_jpy is not None
        else None,
        "bot_return": str(last.bot_return) if last else None,
        "benchmark_return": str(last.benchmark_return)
        if last and last.benchmark_return is not None
        else None,
        "excess_return": str(last.excess_return)
        if last and last.excess_return is not None
        else None,
        "maximum_drawdown": str(maximum_drawdown([s.bot_jpy for s in series])) if series else None,
        "trade_count": len(executions),
        "sell_count": len(sales),
        "win_rate": str(Decimal(sum(e.realized_pnl > 0 for e in sales)) / len(sales))
        if sales
        else None,
        "costs": {c: str(v) for c, v in costs.items()},
        "realized_pnl": {c: str(v) for c, v in account.realized_pnl.items()},
        "annualized_return": None,
        "sharpe_ratio": None,
        "snapshot_count": len(series),
    }


def snapshot_digest(value: Snapshot) -> str:
    return content_hash(value.model_dump_json())
