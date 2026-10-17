"""Phase 5.5 preparation/inspection without market-data or Gemini communication."""

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.broker.execution import session
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Market
from trading_bot.operations import Operation, fingerprint, health_summary
from trading_bot.persistence.supabase_repository import SupabaseAPI
from trading_bot.phase4_cli import verify
from trading_bot.portfolio.models import utc

REAL_ACCOUNTS = {"JP": "test-phase5-real-jp", "US": "test-phase5-real-us"}
PROTECTED = ("paper-live", "test-phase5-fixture-jp", "test-phase5-fixture-us")


def prepare_account(api: SupabaseAPI, market: Market, owner: str, at: datetime) -> dict[str, Any]:
    """Fenced, idempotent initial 1M JPY only; no funding, data, orders or AI."""
    at = utc(at)
    protected = {a: fingerprint(api.rpc("bot_read", {"p_account": a})) for a in PROTECTED}
    op = Operation(api, REAL_ACCOUNTS[market], "phase55:prepare:v1")
    metadata: dict[str, object] = {
        "purpose": "phase55-preparation-v1",
        "market": market,
        "data_enabled": False,
    }
    if not op.claim(owner, metadata):
        return {"state": "ALREADY_PREPARED", "account": op.account, "market_validated": False}
    try:
        broker = PaperBroker(op.repository)
        with op.repository.transaction() as tx:
            if tx.initialized():
                raise ValueError("new real-market scope already has an account; inspect it")
        broker.initialize(at)
        op.step(1)
        op.data(
            "phase55-policy",
            {
                **metadata,
                "prepared_at": at.isoformat(),
                "protected_hashes": protected,
                "synthetic": False,
                "observed_market_data": False,
                "strategy": "controlled-mock-v1; HOLD first, BUY target 5%, SELL held only",
                "usd_funding": "explicit 100000 JPY maximum; never during preparation",
                "publication": "private audit only; no production dashboard",
            },
        )
        for scope, before in protected.items():
            if fingerprint(api.rpc("bot_read", {"p_account": scope})) != before:
                raise ValueError("protected scope changed during preparation; inspect")
        if not api.rpc("bot_operation_preserved", {}):
            raise ValueError("pre-migration data preservation check failed")
        for stage in range(2, 8):
            op.step(stage)
        result = {
            "state": "PREPARED_NOT_VALIDATED",
            "account": op.account,
            "prepared_at": at.isoformat(),
            "orders": 0,
            "executions": 0,
            "data_access": "DISABLED",
            "gemini_requests": 0,
            "market_validated": False,
            "protected_scopes_unchanged": True,
            "verified": verify(op.repository)["verified"],
        }
        op.step(8, "SUCCEEDED", summary=result)
        return result
    except Exception as exc:
        try:
            op.step(op.stage, "NEEDS_REVIEW", error_code=type(exc).__name__)
        except Exception:
            pass
        raise


def inspect_account(api: SupabaseAPI, market: Market, at: datetime) -> dict[str, Any]:
    """Only metadata leaves the private store: no source prices/AI text/P&L."""
    op = Operation(api, REAL_ACCOUNTS[market], "inspect")
    state = op.read()
    broker = PaperBroker(op.repository)
    with op.repository.transaction() as tx:
        initialized = tx.initialized()
        usages = [json.loads(s) for s in tx.records("api_usage")]
        decisions = [json.loads(s) for s in tx.records("decisions")]
    now = utc(at)
    return {
        "account": op.account,
        **health_summary(state, now),
        "initialized": initialized,
        "verified": verify(op.repository)["verified"] if initialized else None,
        "orders": len(broker.get_orders()) if initialized else 0,
        "executions": len(broker.get_executions()) if initialized else 0,
        "pre_migration_data_preserved": api.rpc("bot_operation_preserved", {}),
        "latest_scope_json_bytes": len(json.dumps(state, default=str).encode()),
        "database_total_bytes": None,
        "billable_egress_bytes": None,
        "capacity_verified": False,
        "egress_verified": False,
        "api_usage_scope": "THIS_ACCOUNT_ONLY; shared limit still enforced by DB reservation",
        "api_reservations": len(usages),
        "api_errors": sum(r["status"] == "ERROR" for r in usages),
        "api_unresolved": sum(r["status"] == "RESERVED" for r in usages),
        "decision_count": len(decisions),
        "data_access": "DISABLED",
        "market_validated": False,
    }


def next_manual_sessions(market: Market, at: datetime, count: int = 4) -> list[dict[str, str]]:
    if not 1 <= count <= 10:
        raise ValueError("session count outside bounded planning horizon")
    zone = ZoneInfo("Asia/Tokyo" if market == "JP" else "America/New_York")
    at = utc(at)
    day = at.astimezone(zone).date()
    result = []
    for offset in range(40):
        candidate = day + timedelta(days=offset)
        times = session(market, candidate.isoformat())
        if not times:
            continue
        due = times["market_close"] + timedelta(minutes=75)
        if due + timedelta(minutes=30) < at:
            continue
        result.append(
            {
                "session_date": candidate.isoformat(),
                "market_open": times["market_open"].isoformat(),
                "market_close": times["market_close"].isoformat(),
                "decision_from_local": due.astimezone(zone).isoformat(),
                "decision_until_local": (due + timedelta(minutes=30)).astimezone(zone).isoformat(),
                "decision_from_jst": due.astimezone(ZoneInfo("Asia/Tokyo")).isoformat(),
                "first_bar_check_local": (times["market_open"] + timedelta(minutes=75))
                .astimezone(zone)
                .isoformat(),
                "authorization": "PLANNING_ONLY; rights review and activation required",
            }
        )
        if len(result) == count:
            return result
    raise ValueError("insufficient exchange sessions in planning horizon")


def expected_execution_alerts(
    expectations: list[dict[str, str]], runs: list[dict[str, Any]], at: datetime
) -> list[dict[str, str]]:
    """An explicitly due manual run can be missing; an unplanned run cannot."""
    now = utc(at)
    recorded = {r["run_id"]: r for r in runs}
    alerts = []
    for item in expectations:
        key = item["run_id"]
        deadline = utc(datetime.fromisoformat(item["deadline"]))
        if deadline > now:
            continue
        row = recorded.get(key)
        if row is None:
            alerts.append({"run_id": key, "code": "MISSING_EXPECTED_RUN"})
        elif row["state"] != "SUCCEEDED":
            alerts.append({"run_id": key, "code": "EXPECTED_RUN_NOT_SUCCESSFUL"})
    return alerts


def environment_approval_status(document: dict[str, Any]) -> dict[str, bool]:
    rules = document.get("protection_rules", [])
    return {
        "required_reviewers_configured": any(
            r.get("type") == "required_reviewers" and bool(r.get("reviewers")) for r in rules
        ),
        "protected_branch_policy": bool(document.get("deployment_branch_policy")),
    }
