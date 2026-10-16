"""Buffered shared Session contract, atomically committed by a versioned PostgreSQL RPC.

No accounting is implemented here. A stale transaction fails before ANY mutation;
callers must explicitly retry the whole idempotent operation, never its last write.
"""

import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel

from trading_bot.persistence.sqlite_repository import TABLES, Session
from trading_bot.portfolio.models import Account
from trading_bot.strategy.models import AISettings, APIUsage


class RepositoryConflict(ValueError):
    """The entire operation must be repeated against the latest version."""


class SupabaseError(ValueError):
    """Safe public diagnostic, never an HTTP body, URL, headers or credentials."""


class SupabaseAPI:
    def __init__(self, url: str, key: str, client: httpx.Client | None = None) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query:
            raise ValueError("Supabase requires an HTTPS project URL without credentials")
        if parsed.path not in ("", "/") or parsed.fragment:
            raise ValueError("invalid Supabase project URL")
        if not key:
            raise ValueError("Supabase API key unavailable")
        self.url = url.rstrip("/")
        self.key = key
        self.client = client
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)

    @classmethod
    def environment(cls, public: bool = False) -> "SupabaseAPI":
        load_dotenv(override=False)
        return cls(
            os.environ.get("SUPABASE_URL", ""),
            os.environ.get("SUPABASE_PUBLISHABLE_KEY" if public else "SUPABASE_SECRET_KEY", ""),
        )

    def request(self, method: str, path: str, body: object = None) -> Any:
        # New sb_secret/publishable keys are apikey headers, NOT JWT Bearer tokens.
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if self.key.startswith("eyJ"):
            headers["Authorization"] = "Bearer " + self.key
        try:
            if self.client:
                response = self.client.request(
                    method, self.url + "/rest/v1/" + path, headers=headers, json=body, timeout=30
                )
            else:
                with httpx.Client(follow_redirects=False, trust_env=False) as client:
                    response = client.request(
                        method,
                        self.url + "/rest/v1/" + path,
                        headers=headers,
                        json=body,
                        timeout=30,
                    )
        except httpx.HTTPError:
            raise SupabaseError(
                "Supabase transport unavailable; operation may need inspection"
            ) from None
        if response.is_error:
            try:
                code = response.json().get("code")
            except (ValueError, AttributeError):
                code = None
            if code == "40001":
                raise RepositoryConflict("Supabase version conflict; no partial writes")
            if code in ("PGRST202", "42P01"):
                raise SupabaseError("Supabase migration missing; apply tracked SQL in SQL Editor")
            raise SupabaseError(f"Supabase request rejected (HTTP {response.status_code})")
        try:
            return response.json() if response.content else None
        except ValueError:
            raise SupabaseError("Supabase returned an invalid response") from None

    def rpc(self, name: str, body: dict[str, object]) -> Any:
        if not re.fullmatch(r"bot_[a-z_]+", name):
            raise ValueError("unknown RPC")
        return self.request("POST", "rpc/" + name, body)


class BufferedSession:
    def __init__(self, state: dict[str, Any]) -> None:
        self.value = Account.model_validate(state["account"]) if state.get("account") else None
        self.rows: dict[str, dict[str, str]] = {table: {} for table in TABLES}
        for row in state.get("records", []):
            self.rows[self.table(row["kind"])][row["key"]] = json.dumps(row["payload"])
        self.writes: dict[tuple[str, str], dict[str, object]] = {}
        self.account_changed = False

    @staticmethod
    def table(table: str) -> str:
        if table not in TABLES:
            raise ValueError("unknown repository table")
        return table

    def initialized(self) -> bool:
        return self.value is not None

    def account(self) -> Account:
        if self.value is None:
            raise ValueError("paper account is not initialized")
        return Account.model_validate(self.value.model_dump())

    def save_account(self, account: Account) -> None:
        self.value = Account.model_validate(account.model_dump())
        self.account_changed = True

    def get(self, table: str, key: str) -> str | None:
        return self.rows[self.table(table)].get(key)

    def records(self, table: str) -> list[str]:
        return list(self.rows[self.table(table)].values())

    def put(self, table: str, key: str, value: BaseModel) -> None:
        previous = self.get(table, key)
        raw = value.model_dump_json()
        if previous and table not in {"orders", "decisions", "api_usage", "snapshots", "dashboard"}:
            raise ValueError("append-only event already exists")
        self.rows[table][key] = raw
        self.writes[table, key] = {"kind": table, "key": key, "payload": json.loads(raw)}


class SupabaseRepository:
    def __init__(self, api: SupabaseAPI, account_id: str, *, test: bool = False) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", account_id):
            raise ValueError("invalid account scope")
        if test != account_id.startswith("test-"):
            raise ValueError("test accounts require the test- prefix and explicit test flag")
        self.api, self.account_id, self.test = api, account_id, test

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        state = self.api.rpc("bot_read", {"p_account": self.account_id})
        session = BufferedSession(state)
        yield session
        if session.account_changed or session.writes:
            self.api.rpc(
                "bot_commit",
                {
                    "p_account": self.account_id,
                    "p_test": self.test,
                    "p_version": state["version"],
                    "p_value": session.value.model_dump(mode="json") if session.value else None,
                    "p_writes": list(session.writes.values()),
                },
            )

    def reserve_api(self, decision_id: str, settings: AISettings) -> APIUsage:
        result = self.api.rpc(
            "bot_reserve_api",
            {
                "p_account": self.account_id,
                "p_decision": decision_id,
                "p_request": str(uuid4()),
                "p_model": settings.model,
                "p_day_limit": settings.daily_request_limit,
                "p_month_limit": settings.monthly_request_limit,
                "p_run_limit": settings.max_requests_per_run,
            },
        )
        return APIUsage.model_validate(result)

    def delete_test(self, confirmation: str) -> None:
        if not self.test or confirmation != self.account_id:
            raise ValueError("only the explicitly confirmed test account can be deleted")
        self.api.rpc("bot_delete_test", {"p_account": self.account_id})
