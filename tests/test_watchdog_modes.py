"""Watchdog reporting never starts apps unless explicitly requested.

All health input is synthetic and every subprocess boundary is blocked or
replaced with a fake. These tests never inspect or start a live runtime.
"""

import json

import pytest

import watchdog


NOW = 1_785_000_000


@pytest.fixture
def isolated_watchdog(tmp_path, monkeypatch):
    logs = tmp_path / "external" / "nested" / "logs"
    monkeypatch.setattr(watchdog, "LOGS", logs)
    monkeypatch.setattr(watchdog, "LOG_PATH", logs / "watchdog.log")
    monkeypatch.setattr(watchdog, "STATE_PATH", logs / "watchdog_state.json")
    clock = {"now": NOW}
    monkeypatch.setattr(watchdog.time, "time", lambda: clock["now"])
    monkeypatch.setattr(watchdog, "build_status", lambda **kwargs: {
        "tier": "CRITICAL", "reasons": ["fixture heartbeat stale"],
    })
    monkeypatch.setattr(watchdog, "toast", lambda *args: False)

    def forbidden(*args, **kwargs):
        raise AssertionError("a health test must not start real processes")

    monkeypatch.setattr(watchdog.subprocess, "run", forbidden)
    monkeypatch.setattr(watchdog.subprocess, "Popen", forbidden)
    return clock


def test_repeated_default_health_checks_do_not_start_or_recover_apps(
        isolated_watchdog, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("application control requires explicit opt-in")

    monkeypatch.setattr(watchdog, "restart_supervisor", forbidden)
    monkeypatch.setattr(watchdog, "ensure_dashboard", forbidden)
    for _ in range(watchdog.RECOVERY_AFTER_CONSECUTIVE + 2):
        assert watchdog._run() == 0
    state = json.loads(watchdog.STATE_PATH.read_text())
    assert state["tier"] == "CRITICAL"
    assert state["consecutive_critical"] > watchdog.RECOVERY_AFTER_CONSECUTIVE
    assert "last_recovery_ts" not in state
    assert "tier=CRITICAL" in watchdog.LOG_PATH.read_text()


def test_recovery_opt_in_retains_threshold_and_cooldown(
        isolated_watchdog, monkeypatch):
    attempts = []

    def restart():
        attempts.append(isolated_watchdog["now"])
        return True, "fake task restarted"

    monkeypatch.setattr(watchdog, "restart_supervisor", restart)
    for _ in range(watchdog.RECOVERY_AFTER_CONSECUTIVE - 1):
        watchdog._run(recover=True)
    assert attempts == []
    watchdog._run(recover=True)
    assert attempts == [NOW]
    isolated_watchdog["now"] = NOW + watchdog.RECOVERY_COOLDOWN_S - 1
    watchdog._run(recover=True)
    assert attempts == [NOW]
    isolated_watchdog["now"] += 1
    watchdog._run(recover=True)
    assert attempts == [NOW, NOW + watchdog.RECOVERY_COOLDOWN_S]


def test_dashboard_opt_in_does_not_enable_supervisor_recovery(
        isolated_watchdog, monkeypatch):
    starts = []
    monkeypatch.setattr(watchdog, "dashboard_alive", lambda port: (
        False, "nothing listening"))
    monkeypatch.setattr(watchdog.subprocess, "Popen", lambda *args, **kwargs:
                        starts.append((args, kwargs)))
    for _ in range(watchdog.RECOVERY_AFTER_CONSECUTIVE):
        watchdog._run(start_dashboard=True)
    assert len(starts) == watchdog.RECOVERY_AFTER_CONSECUTIVE
    assert starts[0][0][0][-1] == str(watchdog.PROJECT_DIR / "dashboard.py")
    assert starts[0][1]["cwd"] == watchdog.PROJECT_DIR
    assert "last_recovery_ts" not in json.loads(watchdog.STATE_PATH.read_text())


@pytest.mark.parametrize("argv,recover,dashboard", [
    ([], False, False),
    (["--recover"], True, False),
    (["--ensure-dashboard"], False, True),
    (["--recover", "--ensure-dashboard"], True, True),
])
def test_cli_controls_are_independent(argv, recover, dashboard, monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(watchdog, "_run", run)
    assert watchdog.main(argv) == 0
    assert calls == [{"recover": recover, "start_dashboard": dashboard}]


def test_error_barrier_preserves_failure_exit_code(isolated_watchdog, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("synthetic report failure")

    monkeypatch.setattr(watchdog, "_run", fail)
    assert watchdog.main([]) == 2
    assert "WATCHDOG ERROR RuntimeError: synthetic report failure" in (
        watchdog.LOG_PATH.read_text())
