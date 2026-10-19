"""Private immutable observations plus one CAS commit for the whole simulated cycle."""

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

from trading_bot.models import PriceBar
from trading_bot.persistence.supabase_repository import BufferedSession, SupabaseAPI


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class ResearchMemory:
    """Explicit staging transaction, not a Supabase outage fallback."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = deepcopy(state or {"account": None, "records": []})

    @contextmanager
    def transaction(self) -> Iterator[BufferedSession]:
        tx = BufferedSession(self.state)
        yield tx
        records = {(r["kind"], r["key"]): r for r in self.state["records"]}
        records.update(tx.writes)
        self.state = {
            "account": tx.value.model_dump(mode="json") if tx.value else None,
            "records": list(records.values()),
        }


class PrivateResearchStore:
    def __init__(self, api: SupabaseAPI, account_id: str, *, approved: bool = False) -> None:
        if not re.fullmatch(r"test-integration-[a-z0-9-]{1,40}", account_id):
            raise ValueError("new isolated integration account required")
        self.api, self.account_id, self.approved = api, account_id, approved

    def check(self) -> None:
        if not self.approved:
            raise ValueError("production research reads/writes require explicit approval")

    def read(self) -> dict[str, Any]:
        self.check()
        return dict(self.api.rpc("bot_research_read", {"p_account": self.account_id}))

    def reconcile(self, run_id: str) -> dict[str, Any] | None:
        self.check()
        value = self.api.rpc("bot_research_run", {"p_account": self.account_id, "p_run": run_id})
        return dict(value) if value is not None else None

    def commit(
        self,
        run_id: str,
        version: int,
        request: object,
        bars: list[PriceBar],
        state: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        self.check()
        observations = {}
        for bar in bars:
            payload = PriceBar.model_validate(bar.model_dump()).model_dump(mode="json")
            identity = digest({k: v for k, v in payload.items() if k != "fetched_at"})
            observations[identity] = dict(id=identity, payload=payload)
        return dict(
            self.api.rpc(
                "bot_research_commit",
                dict(
                    p_account=self.account_id,
                    p_run=run_id,
                    p_version=version,
                    p_request_hash=digest(request),
                    p_bars=list(observations.values()),
                    p_state=state,
                    p_result=result,
                ),
            )
        )
