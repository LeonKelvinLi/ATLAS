"""Automated recovery policy.

2026-07-28: the runner wedged mid-cycle, the supervisor waited forever on a
live-but-stuck child, and the outage ran 80 minutes because the watchdog could
only send a toast. Detection without recovery is not monitoring. But an
auto-restarter is dangerous in the other direction, so the policy is tested
directly: never on a transient reading, never in a restart loop.
"""

from watchdog import (RECOVERY_AFTER_CONSECUTIVE, RECOVERY_COOLDOWN_S,
                      should_attempt_recovery)

NOW = 1_785_000_000


def test_never_acts_on_a_single_critical_reading():
    """One bad check can be a transient API blip; bouncing a healthy trader
    mid-cycle would drop an in-flight order."""
    assert should_attempt_recovery(1, NOW, 0) is False


def test_acts_once_the_failure_persists():
    assert should_attempt_recovery(RECOVERY_AFTER_CONSECUTIVE, NOW, 0) is True
    assert should_attempt_recovery(RECOVERY_AFTER_CONSECUTIVE + 5, NOW, 0) is True


def test_cooldown_prevents_a_restart_loop():
    """A genuinely broken system must not be restarted every 15 minutes
    forever - that hides the problem and churns the broker connection."""
    just_tried = NOW - 60
    assert should_attempt_recovery(9, NOW, just_tried) is False
    long_ago = NOW - RECOVERY_COOLDOWN_S - 1
    assert should_attempt_recovery(9, NOW, long_ago) is True


def test_cooldown_boundary_is_inclusive():
    assert should_attempt_recovery(9, NOW, NOW - RECOVERY_COOLDOWN_S) is True


def test_policy_thresholds_are_sane():
    # ~30 min of confirmed failure before acting, at most one attempt an hour
    assert RECOVERY_AFTER_CONSECUTIVE >= 2
    assert RECOVERY_COOLDOWN_S >= 1800


def test_restart_does_not_kill_processes():
    """Killing a stray session-0 process needs elevation the watchdog lacks;
    a half-killed tree is worse than a clean retry."""
    import inspect

    import watchdog

    body = inspect.getsource(watchdog.restart_supervisor).split('"""')[-1]
    assert "/end" in body and "/run" in body
    assert "taskkill" not in body
    assert "Stop-Process" not in body


def test_restart_is_unavailable_off_windows_without_running_commands(monkeypatch):
    import watchdog

    def forbidden(*args, **kwargs):
        raise AssertionError("Task Scheduler must not be invoked off Windows")

    monkeypatch.setattr(watchdog.sys, "platform", "linux")
    monkeypatch.setattr(watchdog.subprocess, "run", forbidden)
    ok, detail = watchdog.restart_supervisor()
    assert not ok
    assert "unavailable" in detail
    assert "Windows Task Scheduler" in detail


def test_windows_restart_uses_task_scheduler_and_reports_failure(monkeypatch):
    from types import SimpleNamespace

    import watchdog

    calls = []
    delays = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=int("/run" in command),
                               stdout="", stderr="fixture failure")

    monkeypatch.setattr(watchdog.sys, "platform", "win32")
    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    monkeypatch.setattr(watchdog.time, "sleep", delays.append)
    ok, detail = watchdog.restart_supervisor()
    assert not ok
    assert calls == [
        ["schtasks", "/end", "/tn", watchdog.SUPERVISOR_TASK],
        ["schtasks", "/run", "/tn", watchdog.SUPERVISOR_TASK],
    ]
    assert delays == [5]
    assert "fixture failure" in detail
