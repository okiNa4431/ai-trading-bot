"""Local authorization tests: synthetic responses only, never real HTTP."""

import json
import subprocess
import sys
import warnings
from datetime import date, timedelta
from unittest.mock import Mock

import httpx
import pytest

from tests.test_jquants import AT, KEY, Clock, row
from trading_bot import jquants_local as local
from trading_bot.data_access import MarketAccessDisabled, reject_diagnostic
from trading_bot.jquants import APPROVAL, JQuantsProvider, load_policy


@pytest.fixture
def scope(monkeypatch):
    monkeypatch.setattr(local, "is_windows", lambda: True)
    monkeypatch.setattr(local, "interactive_console", lambda: True)
    policy = load_policy().model_copy(update={"to_date": date(2025, 1, 10)})
    permission = local.load_local_policy().model_copy(
        update={"local_live_enabled": True, "scope_sha256": local.scope_digest(policy)}
    )
    env = dict(
        JQUANTS_PLAN="Free",
        JQUANTS_TERMS_CONFIRMED="true",
        JQUANTS_LOCAL_LIVE_ENABLED="true",
        JQUANTS_ACCOUNT_IDLE_CONFIRMED="true",
        JQUANTS_API_KEY=KEY,
    )
    return policy, permission, env


def test_default_closed_and_scope_bound():
    policy = load_policy()
    permission = local.load_local_policy()
    assert not permission.local_live_enabled and not policy.live_enabled
    assert permission.scope_sha256 == local.scope_digest(policy)
    assert len(policy.securities) == 20


@pytest.mark.parametrize(
    "name",
    [
        "JQUANTS_PLAN",
        "JQUANTS_TERMS_CONFIRMED",
        "JQUANTS_LOCAL_LIVE_ENABLED",
        "JQUANTS_ACCOUNT_IDLE_CONFIRMED",
        "JQUANTS_API_KEY",
    ],
)
def test_missing_environment_before_transport(scope, monkeypatch, name):
    policy, permission, env = scope
    env.pop(name)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: pytest.fail("transport constructed"))
    with pytest.raises(MarketAccessDisabled):
        JQuantsProvider(policy, local.LOCAL_APPROVAL, env, local_policy=permission, now=lambda: AT)


@pytest.mark.parametrize("approval", ["", APPROVAL, "READ_ONLY_JQUANTS_LOCAL_100_NO_STORAGE"])
def test_wrong_approval_before_transport(scope, monkeypatch, approval):
    policy, permission, env = scope
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: pytest.fail("transport constructed"))
    with pytest.raises(MarketAccessDisabled):
        JQuantsProvider(policy, approval, env, local_policy=permission, now=lambda: AT)


@pytest.mark.parametrize("gate", ["is_windows", "interactive_console"])
def test_platform_and_interactive_required(scope, monkeypatch, gate):
    monkeypatch.setattr(local, gate, lambda: False)
    with pytest.raises(MarketAccessDisabled):
        local.authorize_local(*scope[:2], local.LOCAL_APPROVAL, scope[2], AT)


@pytest.mark.parametrize("marker", ["CI", "GITHUB_ACTIONS", "GITHUB_RUN_ID", "TF_BUILD"])
def test_ci_rejected(scope, marker):
    policy, permission, env = scope
    env[marker] = "true"
    with pytest.raises(MarketAccessDisabled):
        local.authorize_local(policy, permission, local.LOCAL_APPROVAL, env, AT)


@pytest.mark.parametrize("change", ["local_disabled", "cloud_enabled", "scope", "expired", "free"])
def test_policy_rejection(scope, change):
    policy, permission, env = scope
    at = AT
    if change == "local_disabled":
        permission = permission.model_copy(update={"local_live_enabled": False})
    elif change == "cloud_enabled":
        policy = policy.model_copy(update={"live_enabled": True})
    elif change == "scope":
        policy = policy.model_copy(update={"to_date": date(2025, 1, 9)})
    elif change == "expired":
        at = AT + timedelta(days=31)
    elif change == "free":
        at = AT + timedelta(days=730)
    with pytest.raises((MarketAccessDisabled, ValueError)):
        local.authorize_local(policy, permission, local.LOCAL_APPROVAL, env, at)


def test_cloud_flag_does_not_authorize_local(scope):
    policy, permission, env = scope
    env["JQUANTS_MARKET_DATA_LIVE_ENABLED"] = "true"
    with pytest.raises(MarketAccessDisabled):
        local.authorize_local(policy, permission, local.LOCAL_APPROVAL, env, AT)


def test_shared_engine_two_passes_no_leak_and_downstream_rejected(scope, capsys):
    policy, permission, env = scope
    clock = Clock()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json={"data": [row(request.url.params["code"], day) for day in policy.sessions()]}
        )

    provider = JQuantsProvider(
        policy,
        local.LOCAL_APPROVAL,
        env,
        local_policy=permission,
        transport=httpx.MockTransport(respond),
        now=lambda: AT,
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        provider.guard.pause(local.STARTUP_QUIET_SECONDS)
        report = provider.run()
        summary = report.summary()
        assert summary["status"] == "READ_ONLY_COMPLETE"
        assert summary["strategy_approved"] is False
        assert len(calls) == 40
        assert clock.value >= 61 + 39 * 21
        assert [str(r.url) for r in calls[:20]] == [str(r.url) for r in calls[20:]]
        with pytest.raises(MarketAccessDisabled):
            reject_diagnostic(report)
        text = json.dumps(summary)
        assert KEY not in text and "700123" not in text and "1234567" not in text
        assert capsys.readouterr().out == ""
    finally:
        provider.close()


def test_main_default_stops_without_prompt_transport_or_secret(scope, monkeypatch, capsys):
    monkeypatch.setattr(local, "load_policy", load_policy)
    monkeypatch.setattr(local.getpass, "getpass", lambda *a: pytest.fail("prompt"))
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: pytest.fail("HTTP"))
    assert local.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output)["requests"] == 0
    assert KEY not in output and "Traceback" not in output


def test_main_shared_engine_and_cleanup(scope, monkeypatch, capsys):
    policy, permission, env = scope
    clock = Clock()
    api = Mock()
    api.CreateMutexW.return_value = 42
    api.GetLastError.return_value = 0
    monkeypatch.setattr(local, "_kernel32", lambda: api)
    monkeypatch.setattr(local, "load_policy", lambda: policy)
    monkeypatch.setattr(local, "load_local_policy", lambda: permission)
    monkeypatch.setattr(local, "datetime", Mock(now=lambda zone: AT))
    monkeypatch.setattr(local.os, "environ", env)
    monkeypatch.setattr(local.getpass, "getpass", lambda prompt: local.LOCAL_APPROVAL)
    instances = []

    def factory(*args, **kwargs):
        provider = JQuantsProvider(
            *args,
            **kwargs,
            now=lambda: AT,
            clock=clock,
            sleep=clock.sleep,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"data": [row(request.url.params["code"], d) for d in policy.sessions()]},
                )
            ),
        )
        instances.append(provider)
        return provider

    monkeypatch.setattr(local, "JQuantsProvider", factory)
    assert local.main() == 0
    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["execution_context"] == "windows_local"
    assert summary["startup_quiet_seconds"] == 61
    assert KEY not in output and "700123" not in output
    api.ReleaseMutex.assert_called_once_with(42)
    api.CloseHandle.assert_called_once_with(42)
    assert instances[0].client.is_closed


@pytest.mark.parametrize("existing", [False, True])
def test_mutex_cleanup_and_contention(monkeypatch, existing):
    api = Mock()
    api.CreateMutexW.return_value = 42
    api.GetLastError.return_value = 183 if existing else 0
    monkeypatch.setattr(local, "is_windows", lambda: True)
    monkeypatch.setattr(local, "_kernel32", lambda: api)
    if existing:
        with pytest.raises(MarketAccessDisabled):
            with local.WindowsProbeMutex():
                pytest.fail("entered competing lock")
        api.ReleaseMutex.assert_not_called()
    else:
        with pytest.raises(RuntimeError), local.WindowsProbeMutex():
            raise RuntimeError("synthetic interruption")
        api.ReleaseMutex.assert_called_once_with(42)
    api.CloseHandle.assert_called_once_with(42)


@pytest.mark.parametrize("failure", ["create", "release", "close"])
def test_mutex_errors_fail_closed(monkeypatch, failure):
    api = Mock()
    api.CreateMutexW.return_value = None if failure == "create" else 42
    api.GetLastError.return_value = 0
    api.ReleaseMutex.return_value = failure != "release"
    api.CloseHandle.return_value = failure != "close"
    monkeypatch.setattr(local, "is_windows", lambda: True)
    monkeypatch.setattr(local, "_kernel32", lambda: api)
    with pytest.raises(MarketAccessDisabled):
        with local.WindowsProbeMutex():
            pass
    if failure != "create":
        api.CloseHandle.assert_called_once_with(42)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel mutex")
def test_real_windows_mutex_contention_and_reacquire():
    with local.WindowsProbeMutex():
        with pytest.raises(MarketAccessDisabled):
            with local.WindowsProbeMutex():
                pytest.fail("duplicate process allowed")
    with local.WindowsProbeMutex():
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel mutex across processes")
def test_windows_other_process_cannot_start():
    child = (
        "from trading_bot.jquants_local import WindowsProbeMutex; "
        "from trading_bot.data_access import MarketAccessDisabled\n"
        "try:\n with WindowsProbeMutex(): print('ACQUIRED')\n"
        "except MarketAccessDisabled:\n print('BLOCKED')\n"
    )
    options = dict(capture_output=True, text=True, timeout=20, creationflags=0x08000000)
    with local.WindowsProbeMutex():
        result = subprocess.run([sys.executable, "-c", child], **options)
        assert result.returncode == 0 and result.stdout.strip() == "BLOCKED"
    result = subprocess.run([sys.executable, "-c", child], **options)
    assert result.returncode == 0 and result.stdout.strip() == "ACQUIRED"


@pytest.mark.parametrize("interactive", [True, False])
def test_diagnose_never_prompts_or_constructs_provider(scope, monkeypatch, capsys, interactive):
    policy, permission, env = scope
    monkeypatch.setattr(local, "load_policy", lambda: policy)
    monkeypatch.setattr(local, "load_local_policy", lambda: permission)
    monkeypatch.setattr(local, "datetime", Mock(now=lambda zone: AT))
    monkeypatch.setattr(local.os, "environ", env)
    monkeypatch.setattr(local, "interactive_console", lambda: interactive)
    api = Mock()
    api.CreateMutexW.return_value = 42
    api.GetLastError.return_value = 0
    monkeypatch.setattr(local, "_kernel32", lambda: api)
    monkeypatch.setattr(local, "JQuantsProvider", lambda *a, **kw: pytest.fail("provider"))
    monkeypatch.setattr(local.getpass, "getpass", lambda *a: pytest.fail("prompt"))
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: pytest.fail("transport"))
    assert local.main(["--diagnose"]) == (0 if interactive else 1)
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["requests"] == 0
    assert report["network"] == report["provider"] == report["prompt"] == "NOT_RUN"
    assert report["preflight"]["LOCAL_INTERACTIVE_REQUIRED"] == (
        "PASSED" if interactive else "FAILED"
    )
    assert KEY not in output and "700123" not in output
    api.CloseHandle.assert_called_once_with(42)


@pytest.mark.parametrize(
    "code",
    [
        "LOCAL_FREE_PLAN_REQUIRED",
        "LOCAL_API_KEY_REQUIRED",
        "LOCAL_INTERACTIVE_REQUIRED",
        "LOCAL_ENABLE_FLAG_REQUIRED",
    ],
)
def test_preflight_fixed_reason(scope, monkeypatch, code):
    policy, permission, env = scope
    if code == "LOCAL_FREE_PLAN_REQUIRED":
        env["JQUANTS_PLAN"] = "not-secret-output"
    elif code == "LOCAL_API_KEY_REQUIRED":
        env.pop("JQUANTS_API_KEY")
    elif code == "LOCAL_INTERACTIVE_REQUIRED":
        monkeypatch.setattr(local, "interactive_console", lambda: False)
    else:
        env.pop("JQUANTS_LOCAL_LIVE_ENABLED")
    with pytest.raises(local.LocalGateError) as error:
        local.preflight_local(policy, permission, env, AT)
    assert error.value.code == code


def test_mutex_access_denied_fixed_code(monkeypatch):
    api = Mock()
    api.CreateMutexW.return_value = None
    api.GetLastError.return_value = 5
    monkeypatch.setattr(local, "is_windows", lambda: True)
    monkeypatch.setattr(local, "_kernel32", lambda: api)
    with pytest.raises(local.LocalGateError) as error:
        with local.WindowsProbeMutex():
            pytest.fail("lock entered")
    assert error.value.code == "LOCAL_MUTEX_ACCESS_DENIED"


@pytest.mark.parametrize("stage", ["load", "mutex", "prompt", "provider", "hidden_input"])
def test_startup_exception_is_sanitized(scope, monkeypatch, capsys, stage):
    policy, permission, env = scope
    monkeypatch.setattr(local, "load_policy", lambda: policy)
    monkeypatch.setattr(local, "load_local_policy", lambda: permission)
    monkeypatch.setattr(local, "datetime", Mock(now=lambda zone: AT))
    monkeypatch.setattr(local.os, "environ", env)
    api = Mock()
    api.CreateMutexW.return_value = 42
    api.GetLastError.return_value = 0
    monkeypatch.setattr(local, "_kernel32", lambda: api)
    monkeypatch.setattr(local.getpass, "getpass", lambda *a: local.LOCAL_APPROVAL)

    def fail(*a, **kw):
        raise RuntimeError(KEY + " raw-response-body 700123")

    target = {"load": "load_policy", "mutex": "_kernel32", "provider": "JQuantsProvider"}
    if stage == "hidden_input":

        def fallback(*args):
            warnings.warn("synthetic fallback", local.getpass.GetPassWarning, stacklevel=1)
            pytest.fail("must not reach echoed input")

        monkeypatch.setattr(local.getpass, "getpass", fallback)
    elif stage == "prompt":
        monkeypatch.setattr(local.getpass, "getpass", fail)
    else:
        monkeypatch.setattr(local, target[stage], fail)
    assert local.main() == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert (
        report["reason"]
        == {
            "load": "LOCAL_CONFIG_LOAD_FAILED",
            "mutex": "LOCAL_MUTEX_INITIALIZATION_FAILED",
            "prompt": "LOCAL_APPROVAL_INPUT_FAILED",
            "provider": "LOCAL_PROVIDER_INITIALIZATION_FAILED",
            "hidden_input": "LOCAL_HIDDEN_INPUT_UNAVAILABLE",
        }[stage]
    )
    assert report["requests"] == 0
    assert KEY not in output and "700123" not in output and "raw-response" not in output
