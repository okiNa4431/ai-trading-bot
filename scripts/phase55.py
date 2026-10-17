"""Manual preparation/inspection only. No data, Gemini, broker or publication API."""

import argparse
import json
import os
from datetime import UTC, datetime

from trading_bot.data_access import require_live_market_access
from trading_bot.persistence.supabase_repository import SupabaseAPI
from trading_bot.preflight import inspect_account, next_manual_sessions, prepare_account


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["prepare", "audit", "calendar", "hold"], default="audit")
    parser.add_argument("--market", choices=["JP", "US"], required=True)
    args = parser.parse_args()
    now = datetime.now(UTC)
    if args.mode == "hold":
        require_live_market_access()  # before credentials, initialization or upstream requests
    if args.mode == "calendar":
        result = {"planned_sessions": next_manual_sessions(args.market, now), "scheduled": False}
    else:
        api = SupabaseAPI.environment()
        result = (
            prepare_account(api, args.market, os.environ.get("GITHUB_RUN_ID", "manual"), now)
            if args.mode == "prepare"
            else inspect_account(api, args.market, now)
        )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("unresolved") or result.get("stopped") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "state": "BLOCKED_OR_UNKNOWN",
                    "error_code": type(exc).__name__,
                    "action": "inspect; no automatic retry; real-data collection remains disabled",
                }
            )
        )
        raise SystemExit(1) from None
