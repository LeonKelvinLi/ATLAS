"""Cycle watchdog: bound any hang, whatever its cause.

Observed 2026-07-28: the runner wedged for 20+ minutes at 0% CPU with no
exception, no log line and no _call timeout firing - blocked inside the
vendored library where the daemon-thread timeout could not reach it. Only the
57-minute relaunch recovered it, costing a market-hours session. The watchdog
converts that into ~2 minutes of downtime regardless of root cause.
"""

import time

import live_h2_runner as r


def test_progress_marker_advances():
    before = r._LAST_PROGRESS
    time.sleep(0.01)
    r._note_progress()
    assert r._LAST_PROGRESS > before


def test_watchdog_stays_quiet_while_progress_continues(monkeypatch):
    """It must never fire on a healthy runner - a false exit mid-cycle would
    drop an in-flight order."""
    killed = []
    monkeypatch.setattr(r.os, "_exit", lambda code: killed.append(code))
    r._note_progress()
    # A generous deadline versus fresh progress: no kill.
    stalled = time.time() - r._LAST_PROGRESS
    assert stalled < r.CYCLE_DEADLINE_S
    assert killed == []


def test_deadline_is_far_above_a_normal_cycle():
    """Cycles run on a 60s bar boundary and finish in seconds; the deadline
    must not be tight enough to trip on an ordinary slow cycle."""
    assert r.CYCLE_DEADLINE_S >= 240


def test_watchdog_thread_is_daemon_and_starts(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target=None, daemon=None):
            started["daemon"] = daemon
            started["target"] = target

        def start(self):
            started["started"] = True

    monkeypatch.setattr(r.threading, "Thread", FakeThread)
    r._start_cycle_watchdog()
    assert started["started"] is True
    assert started["daemon"] is True, "a non-daemon watchdog would block exit"


def test_watchdog_uses_hard_exit_not_sys_exit():
    """The main thread is blocked uninterruptibly when this fires, so an
    exception or sys.exit would never be delivered."""
    import inspect

    src = inspect.getsource(r._start_cycle_watchdog)
    # Strip the docstring: it explains WHY sys.exit is unusable here, so
    # scanning the whole source would match its own explanation.
    body = src.split('"""')[-1]
    assert "os._exit" in body
    assert "sys.exit" not in body


def test_watchdog_exits_on_shutdown_without_killing_stalled_main(monkeypatch):
    started = {}
    killed = []

    class FakeThread:
        def __init__(self, target, daemon):
            started["target"] = target

        def start(self):
            pass

    class StoppingEvent:
        def wait(self, timeout):
            assert timeout == 30
            return True

    monkeypatch.setattr(r.threading, "Thread", FakeThread)
    monkeypatch.setattr(r.os, "_exit", lambda code: killed.append(code))
    monkeypatch.setattr(r, "_LAST_PROGRESS", time.time() - 1000)
    r._start_cycle_watchdog(stop_event=StoppingEvent())
    started["target"]()
    assert killed == []
