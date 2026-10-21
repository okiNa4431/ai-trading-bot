"""Windows-only preflight and machine mutex; no HTTP or quality logic lives here."""

import ctypes
import getpass
import hashlib
import json
import logging
import os
import sys
from collections.abc import Mapping
from ctypes import wintypes
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_bot.data_access import MarketAccessDisabled
from trading_bot.jquants import JST, JQuantsPolicy, JQuantsProvider, load_policy, require_api_key
from trading_bot.portfolio.models import utc

LOCAL_APPROVAL = "READ_ONLY_JQUANTS_LOCAL_20_NO_STORAGE"
MUTEX_NAME = r"Global\AITradingBotJQuantsFreeReadOnly"
STARTUP_QUIET_SECONDS = 61.0


class LocalProbePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    local_live_enabled: bool = Field(default=False, strict=True)
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewed_at: date
    review_valid_until: date
    approval_reference: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self) -> "LocalProbePolicy":
        if not self.reviewed_at <= self.review_valid_until <= self.reviewed_at + timedelta(days=30):
            raise ValueError("invalid local review period")
        return self


def scope_digest(policy: JQuantsPolicy) -> str:
    # Canonical validated settings, not platform-dependent CRLF bytes or any market observations.
    return hashlib.sha256(
        json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def load_local_policy() -> LocalProbePolicy:
    return LocalProbePolicy.model_validate(
        yaml.safe_load(Path("config/jquants-local-probe.yaml").read_text(encoding="utf-8-sig"))
    )


def is_windows() -> bool:
    return sys.platform == "win32" and os.name == "nt"


def interactive_console() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def preflight_local(
    policy: JQuantsPolicy, local: LocalProbePolicy, env: Mapping[str, str], at: datetime
) -> None:
    JQuantsPolicy.model_validate(policy.model_dump())
    LocalProbePolicy.model_validate(local.model_dump())
    policy.validate_dates(at)
    policy.sessions()
    today = utc(at).astimezone(JST).date()
    ci_keys = (
        "CI",
        "GITHUB_ACTIONS",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_RUN_ID",
        "GITLAB_CI",
        "TF_BUILD",
        "JENKINS_URL",
    )
    if (
        not is_windows()
        or not interactive_console()
        or any(env.get(key) for key in ci_keys)
        or policy.live_enabled
        or not local.local_live_enabled
        or scope_digest(policy) != local.scope_sha256
        or not local.reviewed_at <= today <= local.review_valid_until <= policy.review_valid_until
        or env.get("JQUANTS_PLAN") != "Free"
        or env.get("JQUANTS_TERMS_CONFIRMED") != "true"
        or env.get("JQUANTS_LOCAL_LIVE_ENABLED") != "true"
        or env.get("JQUANTS_ACCOUNT_IDLE_CONFIRMED") != "true"
        or env.get("JQUANTS_MARKET_DATA_LIVE_ENABLED") == "true"
    ):
        raise MarketAccessDisabled("JQUANTS_LOCAL_PREFLIGHT_REQUIRED")
    require_api_key(env)


def authorize_local(
    policy: JQuantsPolicy,
    local: LocalProbePolicy,
    approval: str,
    env: Mapping[str, str],
    at: datetime,
) -> str:
    preflight_local(policy, local, env, at)
    if approval != LOCAL_APPROVAL:
        raise MarketAccessDisabled("JQUANTS_LOCAL_APPROVAL_REQUIRED")
    return require_api_key(env)


def _kernel32() -> Any:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise MarketAccessDisabled("WINDOWS_MUTEX_REQUIRED")
    api = loader("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    api.CreateMutexW.restype = wintypes.HANDLE
    api.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    api.ReleaseMutex.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    api.GetLastError.argtypes = ()
    api.GetLastError.restype = wintypes.DWORD
    return api


class WindowsProbeMutex:
    """No file or key-derived name. Contention, permission errors and cleanup errors stop."""

    def __init__(self) -> None:
        self.api: Any = None
        self.handle: int | None = None

    def __enter__(self) -> "WindowsProbeMutex":
        if not is_windows():
            raise MarketAccessDisabled("WINDOWS_MUTEX_REQUIRED")
        self.api = _kernel32()
        handle = self.api.CreateMutexW(None, True, MUTEX_NAME)
        if not handle:
            raise MarketAccessDisabled("LOCAL_MUTEX_UNAVAILABLE")
        if self.api.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: never wait or auto-retry.
            self.api.CloseHandle(handle)
            raise MarketAccessDisabled("LOCAL_PROBE_ALREADY_RUNNING")
        self.handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.handle is not None:
            try:
                if not self.api.ReleaseMutex(self.handle):
                    raise MarketAccessDisabled("LOCAL_MUTEX_RELEASE_FAILED")
            finally:
                handle, self.handle = self.handle, None
                if not self.api.CloseHandle(handle):
                    raise MarketAccessDisabled("LOCAL_MUTEX_CLOSE_FAILED")


def main() -> int:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    provider = None
    report: dict[str, object]
    try:
        policy, local = load_policy(), load_local_policy()
        preflight_local(policy, local, os.environ, datetime.now(UTC))
        with WindowsProbeMutex():
            approval = getpass.getpass(
                "本人のWindows PC・他クライアント停止を確認し、"
                + LOCAL_APPROVAL
                + " を入力（非表示）: "
            )
            try:
                provider = JQuantsProvider(policy, approval, os.environ, local_policy=local)
                # Cross-process restart cannot inherit a pacing clock. Wait a full minute first.
                provider.guard.pause(STARTUP_QUIET_SECONDS)
                report = provider.run().summary()
                report["execution_context"] = "windows_local"
                report["startup_quiet_seconds"] = STARTUP_QUIET_SECONDS
            finally:
                if provider is not None:
                    provider.close()
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "READ_ONLY_COMPLETE" else 1
    except (Exception, KeyboardInterrupt):
        print(
            json.dumps(
                dict(
                    phase="JP-2.1",
                    purpose="diagnostic_only",
                    execution_context="windows_local",
                    status="STOPPED",
                    reason="LOCAL_APPROVAL_SCOPE_OR_EXECUTION_FAILURE",
                    requests=provider.guard.requests if provider else 0,
                    strategy_approved=False,
                    data_retained=False,
                    checks=provider.checks
                    if provider
                    else dict(
                        scope="FAILED",
                        paging=["NOT_RUN", "NOT_RUN"],
                        quality=["NOT_RUN", "NOT_RUN"],
                        repeat_consistency="NOT_RUN",
                    ),
                )
            )
        )
        return 1
    finally:
        logging.disable(previous)
