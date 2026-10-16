"""Test-only operation leases. No local fallback and no uncertain-attempt replay."""

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from trading_bot.broker.execution import session
from trading_bot.config import Market
from trading_bot.persistence.supabase_repository import SupabaseAPI, SupabaseRepository
from trading_bot.portfolio.models import utc


def test_scope(account: str) -> None:
    if not re.fullmatch(r"test-phase5-[a-z0-9-]{1,50}", account):
        raise ValueError("Phase 5 requires an isolated test-phase5- account")


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def market_slot(
    market: Market, at: datetime, kind: Literal["decision", "execute", "observe"]
) -> str:
    at = utc(at)
    zone = ZoneInfo("Asia/Tokyo" if market == "JP" else "America/New_York")
    day = at.astimezone(zone).date().isoformat()
    times = session(market, day)
    if not times:
        raise ValueError("SKIPPED: market holiday")
    if kind == "decision":
        # Conservative final nominal-hour confirmation, plus 15 min source latency.
        due = times["market_close"] + timedelta(hours=1, minutes=15)
        if not due <= at <= due + timedelta(minutes=30):
            raise ValueError("SKIPPED: outside decision window")
        slot = "close"
    else:
        if not times["market_open"] <= at <= times["market_close"] + timedelta(hours=2):
            raise ValueError("SKIPPED: outside execution window")
        if "break_start" in times and times["break_start"] <= at < times["break_end"]:
            raise ValueError("SKIPPED: JP lunch break")
        slot = at.strftime("%H")
    return f"{market}:{day}:{kind}:{slot}"


class FencedAPI(SupabaseAPI):
    def __init__(self, control: "Operation") -> None:
        self.control = control

    def rpc(self, name: str, body: dict[str, object]) -> Any:
        c = self.control
        if body.get("p_account") != c.account:
            raise ValueError("operation scope mismatch")
        if name in ("bot_commit", "bot_reserve_api"):
            name = "bot_operation_commit" if name == "bot_commit" else "bot_operation_reserve"
            body = {**body, "p_run": c.run_id, "p_token": c.token}
        elif name != "bot_read":
            raise ValueError("operation repository RPC not allowed")
        return c.api.rpc(name, body)


class Operation:
    def __init__(self, api: SupabaseAPI, account: str, run_id: str) -> None:
        test_scope(account)
        self.api, self.account, self.run_id = api, account, run_id
        self.token = 0
        self.stage = 0
        self.repository = SupabaseRepository(FencedAPI(self), account, test=True)

    def claim(self, owner: str, metadata: dict[str, object], seconds: int = 600) -> bool:
        row = self.api.rpc(
            "bot_operation_claim",
            {
                "p_account": self.account,
                "p_run": self.run_id,
                "p_owner": owner,
                "p_metadata": metadata,
                "p_seconds": seconds,
            },
        )
        self.token, self.stage = row["token"], row["stage"]
        return bool(row["state"] == "RUNNING")

    def step(self, stage: int, state: str = "RUNNING", **outcome: object) -> None:
        self.api.rpc(
            "bot_operation_step",
            {
                "p_account": self.account,
                "p_run": self.run_id,
                "p_token": self.token,
                "p_stage": stage,
                "p_state": state,
                "p_outcome": outcome,
            },
        )
        self.stage = stage

    def read(self) -> dict[str, Any]:
        result: dict[str, Any] = self.api.rpc("bot_operation_read", {"p_account": self.account})
        return result

    def data(self, key: str, payload: dict[str, object]) -> None:
        self.api.rpc(
            "bot_operation_data",
            {
                "p_account": self.account,
                "p_run": self.run_id,
                "p_token": self.token,
                "p_key": key,
                "p_payload": payload,
            },
        )
