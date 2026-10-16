"""A narrow public DTO; never serialize account/decision/SDK payloads wholesale."""

import json
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from trading_bot.benchmark import JST
from trading_bot.performance import Snapshot, performance
from trading_bot.persistence.sqlite_repository import PortfolioRepository
from trading_bot.persistence.supabase_repository import SupabaseRepository
from trading_bot.portfolio.models import Account, Contract, Execution
from trading_bot.strategy.models import APIUsage, DecisionRun
from trading_bot.strategy.security import redact


class PublicDocument(Contract):
    schema_version: Literal[1] = 1
    state: Literal["PREPARING", "ACTIVE"]
    updated_at: datetime
    started_at: datetime
    summary: dict[str, object]
    series: list[dict[str, object]] = Field(max_length=5000)
    holdings: list[dict[str, object]] = Field(max_length=300)
    trades: list[dict[str, object]] = Field(max_length=100)
    decisions: list[dict[str, object]] = Field(max_length=100)
    usage: dict[str, object]
    warnings: list[str]


def build_document(repository: PortfolioRepository, at: datetime) -> PublicDocument:
    with repository.transaction() as tx:
        account = tx.account()
        snapshots = sorted(
            [Snapshot.model_validate_json(s) for s in tx.records("snapshots")],
            key=lambda s: s.as_of,
        )
        executions = [Execution.model_validate_json(s) for s in tx.records("executions")]
        decisions = [DecisionRun.model_validate_json(s) for s in tx.records("decisions")]
        usage = [APIUsage.model_validate_json(s) for s in tx.records("api_usage")]
    return public_document(account, snapshots, executions, decisions, usage, at)


def public_document(
    account: Account,
    snapshots: list[Snapshot],
    executions: list[Execution],
    decisions: list[DecisionRun],
    usage: list[APIUsage],
    at: datetime,
) -> PublicDocument:
    days = {s.as_of.astimezone(JST).date(): s for s in sorted(snapshots, key=lambda s: s.as_of)}
    values = list(days.values())[-5000:]
    latest = values[-1] if values else None
    summary = performance(account, values, executions)
    warnings = ["paper_trading_only", "tax_before_costs_after", "no_scheduled_trading"]
    if latest:
        warnings.extend(latest.warnings)
        summary.update(
            unrealized_pnl={
                c: str(
                    sum(
                        (p.unrealized_pnl for p in latest.valuation.positions if p.currency == c),
                        Decimal(0),
                    )
                )
                for c in ("JPY", "USD")
            },
            stock_ratio=str(latest.valuation.stock_ratio),
            cash_ratio=str(latest.valuation.cash_ratio),
            market_ratios={
                m: str(
                    sum(
                        (
                            p.ratio
                            for p in latest.valuation.positions
                            if account.positions[p.symbol].instrument.market == m
                        ),
                        Decimal(0),
                    )
                )
                for m in ("JP", "US")
            },
            cash_balances={c: str(v) for c, v in account.balances.items()},
            fx_rate=str(latest.valuation.fx.usd_jpy) if latest.valuation.fx else None,
            fx_at=latest.valuation.fx.timestamp.isoformat() if latest.valuation.fx else None,
        )
    # Disclosure is opt-in per imported NAV; no raw NAVs or OHLCV are public.
    publish_benchmark = bool(latest and latest.nav and latest.nav.derivative_publication_allowed)
    if not publish_benchmark:
        summary.update(
            benchmark_jpy=None, benchmark_return=None, excess_return=None, excess_pnl_jpy=None
        )
        warnings.append("benchmark_publication_not_authorized_or_unavailable")
    series: list[dict[str, object]] = [
        {
            "at": s.as_of.isoformat(),
            "bot_jpy": str(s.bot_jpy),
            "bot_return": str(s.bot_return),
            "benchmark_jpy": str(s.benchmark_jpy)
            if s.benchmark_jpy is not None and s.nav and s.nav.derivative_publication_allowed
            else None,
            "benchmark_return": str(s.benchmark_return)
            if s.benchmark_return is not None and s.nav and s.nav.derivative_publication_allowed
            else None,
            "complete": s.complete,
            "warnings": s.warnings,
        }
        for s in values
    ]
    holdings = (
        [
            {
                "symbol": p.symbol,
                "name": account.positions[p.symbol].instrument.display_name,
                "market": account.positions[p.symbol].instrument.market,
                "currency": p.currency,
                "quantity": p.quantity,
                "average_cost": str(account.positions[p.symbol].average_cost),
                "market_value_jpy": str(p.market_value_jpy),
                "unrealized_pnl": str(p.unrealized_pnl),
                "ratio": str(p.ratio),
                "at": latest.as_of.isoformat(),
            }
            for p in latest.valuation.positions
        ]
        if latest
        else []
    )
    reasons = {
        o.order_id: redact(
            next((d.reason for d in record.result.decisions if d.symbol == o.symbol), "")
        )
        for record in decisions
        if record.result
        for o in record.registered_orders
    }
    trades = [
        {
            "at": e.processed_at.isoformat(),
            "symbol": e.symbol,
            "side": e.side,
            "quantity": e.quantity,
            "price": str(e.price),
            "currency": e.currency,
            "commission": str(e.commission),
            "realized_pnl": str(e.realized_pnl),
            "reason": reasons.get(e.order_id, "手動テスト・判断理由なし"),
        }
        for e in sorted(executions, key=lambda e: e.processed_at, reverse=True)[:100]
    ]
    history = [
        {
            "at": r.as_of.isoformat(),
            "market": r.market,
            "provider": r.provider,
            "status": r.status,
            "registered_count": len(r.registered_orders),
            "items": [
                {
                    "symbol": d.symbol,
                    "action": d.action,
                    "reason": redact(d.reason),
                    "skipped": r.plan.skipped.get(d.symbol) if r.plan else None,
                }
                for d in r.result.decisions
            ]
            if r.result
            else [],
            # No raw SDK/input/error, hashes, IDs or account metadata are public.
        }
        for r in sorted(decisions, key=lambda r: r.as_of, reverse=True)[:100]
    ]
    totals = {
        name: sum(getattr(r, name) for r in usage if getattr(r, name) is not None)
        if usage and all(getattr(r, name) is not None for r in usage)
        else None
        for name in ("input_tokens", "output_tokens", "total_tokens")
    }
    cost = sum(
        (r.estimated_cost_usd for r in usage if r.estimated_cost_usd is not None), Decimal(0)
    )
    dto = PublicDocument(
        state="ACTIVE" if executions else "PREPARING",
        updated_at=at,
        started_at=account.created_at,
        summary=summary,
        series=series,
        holdings=holdings,
        trades=trades,
        decisions=history,
        usage={
            "models": sorted({r.model for r in usage}),
            "decision_count": len(decisions),
            "requests": len(usage),
            **totals,
            "estimated_cost_usd": str(cost)
            if usage and all(r.estimated_cost_usd is not None for r in usage)
            else None,
            "reserved": sum(r.status == "RESERVED" for r in usage),
        },
        warnings=list(dict.fromkeys(warnings)),
    )
    return PublicDocument.model_validate_json(redact(dto.model_dump_json()))


def publish(repository: PortfolioRepository, at: datetime) -> PublicDocument:
    if isinstance(repository, SupabaseRepository):
        if repository.test or repository.account_id != "paper-live":
            raise ValueError("test accounts cannot publish to the public dashboard")
        # Build a consistent projection from one read; pin publication to the same version.
        state = repository.api.rpc("bot_read", {"p_account": repository.account_id})
        from trading_bot.orchestration.engine import BoundRepository
        from trading_bot.persistence.supabase_repository import BufferedSession

        document = build_document(BoundRepository(BufferedSession(state)), at)
        repository.api.rpc(
            "bot_publish",
            {
                "p_account": repository.account_id,
                "p_version": state["version"],
                "p_document": json.loads(document.model_dump_json()),
            },
        )
        return document
    document = build_document(repository, at)
    with repository.transaction() as tx:
        tx.put("dashboard", "public", document)
    return document
