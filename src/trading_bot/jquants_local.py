"""Windows-only preflight and machine mutex; no HTTP or quality logic lives here."""

import ctypes
import getpass
import hashlib
import json
import logging
import os
import sys
import warnings
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


class LocalGateError(MarketAccessDisabled):
    """Only program-defined codes, never exception messages or environment values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
    checks = preflight_checks(policy, local, env, at)
    for code, result in checks.items():
        if result != "PASSED":
            raise LocalGateError(code)


def preflight_checks(
    policy: JQuantsPolicy, local: LocalProbePolicy, env: Mapping[str, str], at: datetime
) -> dict[str, str]:
    checks = {}
    try:
        JQuantsPolicy.model_validate(policy.model_dump())
        LocalProbePolicy.model_validate(local.model_dump())
        checks["LOCAL_CONFIG_INVALID"] = "PASSED"
    except Exception:
        checks["LOCAL_CONFIG_INVALID"] = "FAILED"
    try:
        policy.validate_dates(at)
        policy.sessions()
        checks["LOCAL_FREE_PERIOD_OR_SPEC_REVIEW_INVALID"] = "PASSED"
    except Exception:
        checks["LOCAL_FREE_PERIOD_OR_SPEC_REVIEW_INVALID"] = "FAILED"
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
    conditions = {
        "LOCAL_WINDOWS_REQUIRED": is_windows(),
        "LOCAL_INTERACTIVE_REQUIRED": interactive_console(),
        "LOCAL_CI_FORBIDDEN": not any(env.get(key) for key in ci_keys),
        "LOCAL_CLOUD_CONFIG_MUST_REMAIN_DISABLED": not policy.live_enabled,
        "LOCAL_CONFIG_ENABLE_REQUIRED": local.local_live_enabled,
        "LOCAL_SCOPE_MISMATCH": scope_digest(policy) == local.scope_sha256,
        "LOCAL_REVIEW_EXPIRED_OR_INVALID": (
            local.reviewed_at <= today <= local.review_valid_until <= policy.review_valid_until
        ),
        "LOCAL_FREE_PLAN_REQUIRED": env.get("JQUANTS_PLAN") == "Free",
        "LOCAL_TERMS_CONFIRMATION_REQUIRED": env.get("JQUANTS_TERMS_CONFIRMED") == "true",
        "LOCAL_ENABLE_FLAG_REQUIRED": env.get("JQUANTS_LOCAL_LIVE_ENABLED") == "true",
        "LOCAL_ACCOUNT_IDLE_CONFIRMATION_REQUIRED": (
            env.get("JQUANTS_ACCOUNT_IDLE_CONFIRMED") == "true"
        ),
        "LOCAL_CLOUD_FLAG_CONFLICT": env.get("JQUANTS_MARKET_DATA_LIVE_ENABLED") != "true",
    }
    checks.update({code: "PASSED" if passed else "FAILED" for code, passed in conditions.items()})
    try:
        require_api_key(env)
        checks["LOCAL_API_KEY_REQUIRED"] = "PASSED"
    except Exception:
        checks["LOCAL_API_KEY_REQUIRED"] = "FAILED"
    return checks


def authorize_local(
    policy: JQuantsPolicy,
    local: LocalProbePolicy,
    approval: str,
    env: Mapping[str, str],
    at: datetime,
) -> str:
    preflight_local(policy, local, env, at)
    if approval != LOCAL_APPROVAL:
        raise LocalGateError("JQUANTS_LOCAL_APPROVAL_REQUIRED")
    return require_api_key(env)


def _kernel32() -> Any:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise LocalGateError("WINDOWS_MUTEX_REQUIRED")
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
            raise LocalGateError("WINDOWS_MUTEX_REQUIRED")
        self.api = _kernel32()
        handle = self.api.CreateMutexW(None, True, MUTEX_NAME)
        if not handle:
            code = (
                "LOCAL_MUTEX_ACCESS_DENIED"
                if self.api.GetLastError() == 5
                else "LOCAL_MUTEX_UNAVAILABLE"
            )
            raise LocalGateError(code)
        if self.api.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: never wait or auto-retry.
            self.api.CloseHandle(handle)
            raise LocalGateError("LOCAL_PROBE_ALREADY_RUNNING")
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
                    raise LocalGateError("LOCAL_MUTEX_RELEASE_FAILED")
            finally:
                handle, self.handle = self.handle, None
                if not self.api.CloseHandle(handle):
                    raise LocalGateError("LOCAL_MUTEX_CLOSE_FAILED")


def terminal_status() -> dict[str, bool]:
    def tty(stream: Any) -> bool:
        try:
            return bool(stream is not None and stream.isatty())
        except Exception:
            return False

    return dict(
        stdin_tty=tty(sys.stdin),
        stdout_tty=tty(sys.stdout),
        stderr_tty=tty(sys.stderr),
        original_stdin=sys.stdin is sys.__stdin__,
        windows_getpass=getattr(getpass.getpass, "__name__", "") == "win_getpass",
    )


def diagnose() -> int:
    """No provider/client/prompt construction, even when every gate passes."""
    report: dict[str, object] = dict(
        purpose="diagnostic_only",
        mode="precommunication_diagnosis",
        requests=0,
        strategy_approved=False,
        data_retained=False,
        terminal=terminal_status(),
        prompt="NOT_RUN",
        provider="NOT_RUN",
        network="NOT_RUN",
        mutex="NOT_RUN",
    )
    try:
        policy, local = load_policy(), load_local_policy()
        checks = preflight_checks(policy, local, os.environ, datetime.now(UTC))
        report["preflight"] = checks
        if is_windows():
            try:
                with WindowsProbeMutex():
                    pass
                report["mutex"] = "PASSED"
            except LocalGateError as exc:
                report["mutex"] = "FAILED"
                report["mutex_reason"] = exc.code
            except Exception:
                report["mutex"] = "FAILED"
                report["mutex_reason"] = "LOCAL_MUTEX_INITIALIZATION_FAILED"
        passed = all(result == "PASSED" for result in checks.values())
        passed = passed and report["mutex"] == "PASSED"
        report["status"] = "PREFLIGHT_PASSED" if passed else "STOPPED"
    except Exception:
        report.update(status="STOPPED", reason="LOCAL_CONFIG_LOAD_OR_DIAGNOSIS_FAILED")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "PREFLIGHT_PASSED" else 1


def main(args: list[str] | None = None) -> int:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    provider = None
    stage = "ARGUMENTS"
    preflight_passed = False
    report: dict[str, object]
    try:
        if args == ["--diagnose"]:
            return diagnose()
        if args:
            raise LocalGateError("LOCAL_ARGUMENTS_INVALID")
        stage = "CONFIG_LOAD"
        policy, local = load_policy(), load_local_policy()
        stage = "PREFLIGHT"
        preflight_local(policy, local, os.environ, datetime.now(UTC))
        preflight_passed = True
        stage = "MUTEX"
        with WindowsProbeMutex():
            stage = "APPROVAL_INPUT"
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                approval = getpass.getpass(
                    "本人のWindows PC・他クライアント停止を確認し、"
                    + LOCAL_APPROVAL
                    + " を入力（非表示）: "
                )
            try:
                stage = "PROVIDER_INITIALIZATION"
                provider = JQuantsProvider(policy, approval, os.environ, local_policy=local)
                # Cross-process restart cannot inherit a pacing clock. Wait a full minute first.
                stage = "STARTUP_WAIT"
                provider.guard.pause(STARTUP_QUIET_SECONDS)
                stage = "EXECUTION"
                report = provider.run().summary()
                report["execution_context"] = "windows_local"
                report["startup_quiet_seconds"] = STARTUP_QUIET_SECONDS
            finally:
                if provider is not None:
                    provider.close()
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "READ_ONLY_COMPLETE" else 1
    except (Exception, KeyboardInterrupt) as exc:
        reason = (
            exc.code
            if isinstance(exc, LocalGateError)
            else "LOCAL_INPUT_INTERRUPTED"
            if isinstance(exc, (EOFError, KeyboardInterrupt))
            else "LOCAL_HIDDEN_INPUT_UNAVAILABLE"
            if isinstance(exc, getpass.GetPassWarning)
            else {
                "CONFIG_LOAD": "LOCAL_CONFIG_LOAD_FAILED",
                "PREFLIGHT": "LOCAL_PREFLIGHT_CHECK_FAILED",
                "MUTEX": "LOCAL_MUTEX_INITIALIZATION_FAILED",
                "APPROVAL_INPUT": "LOCAL_APPROVAL_INPUT_FAILED",
                "PROVIDER_INITIALIZATION": "LOCAL_PROVIDER_INITIALIZATION_FAILED",
                "STARTUP_WAIT": "LOCAL_STARTUP_WAIT_FAILED",
            }.get(stage, "LOCAL_EXECUTION_FAILED")
        )
        print(
            json.dumps(
                dict(
                    phase="JP-2.1",
                    purpose="diagnostic_only",
                    execution_context="windows_local",
                    status="STOPPED",
                    reason=reason,
                    stage=stage,
                    requests=provider.guard.requests if provider else 0,
                    strategy_approved=False,
                    data_retained=False,
                    checks=provider.checks
                    if provider
                    else dict(
                        scope="PASSED" if preflight_passed else "FAILED",
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
