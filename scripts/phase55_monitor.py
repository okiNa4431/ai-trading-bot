"""Manual, independent observer. No dispatch, notifications, recovery or mutation."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from trading_bot.operations import Operation
from trading_bot.persistence.supabase_repository import SupabaseAPI
from trading_bot.preflight import (
    REAL_ACCOUNTS,
    environment_approval_status,
    expected_execution_alerts,
    inspect_account,
)

REPOSITORY = "okiNa4431/ai-trading-bot"


def github(path):
    response = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if response.returncode:
        return None  # no upstream stderr/URLs/credentials in public logs
    return json.loads(response.stdout)


def main():
    now = datetime.now(UTC)
    api = SupabaseAPI.environment()
    expectations = json.loads(Path("config/phase55-expectations.json").read_text(encoding="utf-8"))
    alerts = []
    scopes = []
    for market, scope in REAL_ACCOUNTS.items():
        state = Operation(api, scope, "inspect").read()
        scheduled = [e for e in expectations if e["account"] == scope]
        alerts.extend(expected_execution_alerts(scheduled, state["runs"], now))
        audit = inspect_account(api, market, now)
        scopes.append(audit)
        if audit["unresolved"] or audit["stopped"] or not audit["pre_migration_data_preserved"]:
            alerts.append({"code": "DATABASE_INSPECTION_REQUIRED", "account": scope})
    workflows = github("actions/workflows/phase55-validation.yml/runs?per_page=30&branch=main")
    if workflows is None:
        alerts.append({"code": "GITHUB_STATUS_UNAVAILABLE"})
    else:
        for run in workflows["workflow_runs"]:
            if run["conclusion"] in ("failure", "cancelled", "timed_out", "action_required"):
                alerts.append({"code": "ABNORMAL_WORKFLOW_END", "run_id": str(run["id"])})
    environments = github("environments?per_page=100")
    protection = {}
    for name in ("phase55-validation", "phase5-recovery"):
        document = next(
            (e for e in (environments or {}).get("environments", []) if e["name"] == name), None
        )
        protection[name] = (
            {"exists": True, **environment_approval_status(document)}
            if document
            else {"exists": False, "required_reviewers_configured": False}
        )
    print(
        json.dumps(
            {
                "observed_at": now.isoformat(),
                "scopes": scopes,
                "alerts": alerts,
                "expectation_count": len(expectations),
                "missing_run_monitor": "CONFIGURED" if expectations else "NOT_CONFIGURED",
                "external_watcher": "NOT_CONFIGURED; GitHub cannot detect its own total outage",
                "environment_check_available": environments is not None,
                "environment_protection": protection,
                "readiness": "NOT_READY; access paused; capacity/egress/backup unverified",
                "scheduled": False,
                "financial_mutations": False,
            }
        )
    )
    return 1 if alerts else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "state": "MONITOR_UNAVAILABLE",
                    "error_code": type(exc).__name__,
                    "action": "inspect; no automatic retry or mutation",
                }
            )
        )
        raise SystemExit(1) from None
