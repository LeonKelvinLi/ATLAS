"""Offline process-lifecycle tests; no runner, broker, or service is started."""

import io
import signal
import subprocess
import threading
from types import SimpleNamespace

import pytest

import supervisor


class Clock:
    def __init__(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds


class StopEvent(threading.Event):
    """Advance a virtual clock instead of sleeping in the supervision loop."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.waits = []
        self.on_wait = lambda timeout: self.set()

    def wait(self, timeout=None):
        assert timeout is not None, "supervisor used an unbounded wait"
        self.waits.append(timeout)
        self.on_wait(timeout)
        self.clock.advance(timeout)
        return self.is_set()


class FakeProcess:
    next_pid = 100

    def __init__(self, command, *, clock=None, lifetime=None, stubborn=False,
                 events=None, pipes=False):
        self.command = command
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        self.clock = clock
        self.created_at = clock.now if clock else 0
        self.lifetime = lifetime
        self.stubborn = stubborn
        self.events = events if events is not None else []
        self.stdout = io.StringIO() if pipes else None
        self.stderr = io.StringIO() if pipes else None
        self.communicate_hook = None

    def poll(self):
        if (self.returncode is None and self.lifetime is not None
                and self.clock.now - self.created_at >= self.lifetime):
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.events.append((self.pid, "terminate"))
        if not self.stubborn:
            self.returncode = -15

    def kill(self):
        self.events.append((self.pid, "kill"))
        self.returncode = -9

    def wait(self, timeout=None):
        assert timeout is not None, "child cleanup used an unbounded wait"
        self.events.append((self.pid, "wait", timeout))
        if self.poll() is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode

    def communicate(self, timeout=None):
        assert timeout is not None
        if self.communicate_hook is not None:
            return self.communicate_hook(timeout)
        self.returncode = 0
        return "collected", ""


class BackgroundThread:
    """Simulate an indefinitely busy collector, without executing its target."""

    def __init__(self, *, target, args, daemon):
        assert daemon is True
        self.target = target
        self.args = args
        self.started = False
        self.alive = False
        self.join_timeout = None

    def start(self):
        self.started = self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        assert timeout is not None
        self.join_timeout = timeout
        self.alive = False


class Harness:
    def __init__(self, monkeypatch, thread_class=BackgroundThread):
        self.clock = Clock()
        self.stop = StopEvent(self.clock)
        self.logs = []
        self.commands = []
        self.processes = []
        self.threads = []
        self.process_events = []
        self.handlers = {signal.SIGINT: object(), signal.SIGTERM: object()}
        self.original_handlers = dict(self.handlers)
        self.handler_changes = []
        self.lock_closed = False
        self.on_spawn = None

        def close_lock():
            self.lock_closed = True

        def set_handler(signum, handler):
            self.handler_changes.append((signum, handler))
            self.handlers[signum] = handler

        def create_thread(**kwargs):
            result = thread_class(**kwargs)
            self.threads.append(result)
            return result

        def popen(command, **kwargs):
            assert kwargs["cwd"] == supervisor.PROJECT_DIR
            assert kwargs["creationflags"] == supervisor.NO_WINDOW
            self.commands.append(list(command))
            process = FakeProcess(command, clock=self.clock,
                                  events=self.process_events,
                                  pipes="stdout" in kwargs)
            self.processes.append(process)
            if self.on_spawn:
                self.on_spawn(process)
            return process

        monkeypatch.setattr(supervisor, "LOG", None)
        monkeypatch.setattr(supervisor, "log", self.logs.append)
        monkeypatch.setattr(supervisor, "acquire_singleton_lock",
                            lambda: SimpleNamespace(close=close_lock))
        monkeypatch.setattr(supervisor, "time",
                            SimpleNamespace(monotonic=lambda: self.clock.now))
        monkeypatch.setattr(supervisor, "signal", SimpleNamespace(
            SIGINT=signal.SIGINT, SIGTERM=signal.SIGTERM,
            getsignal=self.handlers.__getitem__, signal=set_handler))
        monkeypatch.setattr(supervisor, "threading", SimpleNamespace(
            Event=lambda: self.stop, Thread=create_thread, Lock=threading.Lock))
        monkeypatch.setattr(supervisor, "subprocess", SimpleNamespace(
            Popen=popen, TimeoutExpired=subprocess.TimeoutExpired,
            PIPE=subprocess.PIPE, CompletedProcess=subprocess.CompletedProcess))

    def send_signal(self, signum=signal.SIGTERM):
        self.handlers[signum](signum, None)

    def assert_released(self):
        assert self.lock_closed
        assert supervisor._LOCK_SOCK is None
        assert self.handlers == self.original_handlers


@pytest.mark.parametrize("argv", [
    [], ["--collect-min", "10"], ["--mode", "real"],
    ["--paper", "--mode", "practice"],
    ["--mode", "paper", "--collect-min", "0"],
    ["--mode", "paper", "--collect-min", "-1"],
])
def test_invalid_or_missing_mode_has_no_side_effects(monkeypatch, tmp_path, argv):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid invocation reached an operational side effect")

    monkeypatch.setattr(supervisor, "LOG", tmp_path / "logs")
    monkeypatch.setattr(supervisor, "log", forbidden)
    monkeypatch.setattr(supervisor, "acquire_singleton_lock", forbidden)
    monkeypatch.setattr(supervisor.subprocess, "Popen", forbidden)
    with pytest.raises(SystemExit) as exc:
        supervisor.main(argv)
    assert exc.value.code == 2
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("argv,trade", [
    (["--paper"], False), (["--mode", "paper"], False),
    (["--mode", "practice"], True),
])
def test_only_explicit_practice_mode_enables_orders(monkeypatch, argv, trade):
    harness = Harness(monkeypatch)
    supervisor.main(argv)
    assert len(harness.commands) == 1
    command = harness.commands[0]
    assert command[1] == "live_h2_runner.py"
    assert ("--trade" in command) is trade
    harness.assert_released()


def test_collection_runs_in_background_and_never_overlaps(monkeypatch):
    harness = Harness(monkeypatch)

    def stop_after_six_polls(timeout):
        if len(harness.stop.waits) == 6:
            harness.stop.set()

    harness.stop.on_wait = stop_after_six_polls
    supervisor.main(["--mode", "paper", "--collect-min", "1"])
    assert len(harness.commands) == 1  # busy collector did not stall runner
    assert len(harness.threads) == 1  # later cycles skipped, not queued
    assert harness.threads[0].started
    assert harness.threads[0].target is supervisor.collect_cycle
    assert sum("previous collection still running" in line
               for line in harness.logs) == 2
    assert harness.threads[0].join_timeout == supervisor.COLLECT_JOIN_S
    harness.assert_released()


def test_short_run_backoff_is_bounded_and_resets_after_healthy_session(monkeypatch):
    harness = Harness(monkeypatch)
    lifetimes = iter([0, 0, 0, 0, 0, 0, 90, 0])
    harness.on_spawn = lambda process: setattr(process, "lifetime", next(lifetimes))
    backoffs = []

    def track_backoff(timeout):
        if "short run - backing off" in harness.logs[-1]:
            backoffs.append(timeout)
            if len(backoffs) == 7:
                harness.send_signal()

    harness.stop.on_wait = track_backoff
    supervisor.main(["--mode", "paper"])
    assert backoffs == [30, 60, 120, 240, 300, 300, 30]
    # Shutdown during the final backoff must not launch a ninth runner.
    assert len(harness.commands) == 8
    harness.assert_released()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_runner_and_background_collector(monkeypatch, signum):
    harness = Harness(monkeypatch, thread_class=threading.Thread)
    collector_started = threading.Event()
    release_collector_read = threading.Event()

    def spawn(process):
        if process.command[1] == "collector.py":
            def collect_until_shutdown(timeout):
                collector_started.set()
                # Real thread, fake child: remain in this command until the
                # signal interrupts polling. No subprocess is ever created.
                assert release_collector_read.wait(2)
                raise subprocess.TimeoutExpired(process.command, timeout)
            process.communicate_hook = collect_until_shutdown
            process.stubborn = True  # exercise kill + wait as well

    harness.on_spawn = spawn

    def signal_after_collector_is_running(timeout):
        assert collector_started.wait(2), "collector did not run in background"
        harness.send_signal(signum)
        release_collector_read.set()

    harness.stop.on_wait = signal_after_collector_is_running
    supervisor.main(["--mode", "paper"])
    assert sorted(command[1] for command in harness.commands) == [
        "collector.py", "live_h2_runner.py"]
    assert all(process.returncode is not None for process in harness.processes)
    assert all(not thread.is_alive() for thread in harness.threads)
    collector = next(p for p in harness.processes if p.command[1] == "collector.py")
    assert (collector.pid, "terminate") in harness.process_events
    assert (collector.pid, "kill") in harness.process_events
    assert any(event[:2] == (collector.pid, "wait")
               for event in harness.process_events)
    assert collector.stdout.closed and collector.stderr.closed
    harness.assert_released()


def test_signal_during_spawn_tracks_child_and_prevents_later_spawns(monkeypatch):
    harness = Harness(monkeypatch)
    harness.on_spawn = lambda process: harness.send_signal()
    supervisor.main(["--mode", "paper"])
    assert len(harness.commands) == 1
    assert harness.processes[0].returncode == -15
    harness.assert_released()


def test_launch_error_still_restores_handlers_and_releases_lock(monkeypatch):
    harness = Harness(monkeypatch)

    def fail(*args, **kwargs):
        raise OSError("could not start child")

    monkeypatch.setattr(supervisor.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="could not start child"):
        supervisor.main(["--mode", "paper"])
    harness.assert_released()


def test_shutdown_terminates_all_before_waiting_and_reaps_killed_children(monkeypatch):
    harness = Harness(monkeypatch)
    children = supervisor.ChildProcesses(harness.stop)
    runner = children.spawn(["fake-runner"])
    collector = children.spawn(["fake-collector"])
    runner.stubborn = True
    children.shutdown()
    events = harness.process_events
    assert set(events[:2]) == {(runner.pid, "terminate"), (collector.pid, "terminate")}
    assert (runner.pid, "kill") in events
    kill_index = events.index((runner.pid, "kill"))
    assert any(event[:2] == (runner.pid, "wait") for event in events[kill_index + 1:])
    assert all(0 <= event[2] <= supervisor.TERMINATE_WAIT_S
               for event in events if event[1] == "wait")
    assert children.spawn(["must-not-launch"]) is None
    assert len(harness.commands) == 2


def test_collection_finishes_both_commands_and_releases_pipes(monkeypatch):
    harness = Harness(monkeypatch)
    children = supervisor.ChildProcesses(harness.stop)
    supervisor.collect_cycle(children)
    assert [command[2] for command in harness.commands] == ["candles", "payouts"]
    assert "collect candles exit=0" in harness.logs
    assert "collect payouts exit=0" in harness.logs
    assert all(p.stdout.closed and p.stderr.closed for p in harness.processes)
    children.shutdown()
    assert not any(event[1] == "terminate" for event in harness.process_events)


def test_one_child_termination_error_does_not_skip_other_cleanup(monkeypatch):
    harness = Harness(monkeypatch)
    children = supervisor.ChildProcesses(harness.stop)
    runner = children.spawn(["fake-runner"])
    collector = children.spawn(["fake-collector"])

    def fail_terminate():
        raise OSError("temporary termination failure")

    runner.terminate = fail_terminate
    children.shutdown()
    assert runner.returncode == -9
    assert collector.returncode == -15
    assert any("terminate failed" in line for line in harness.logs)


def test_collection_read_failure_stops_child_before_next_cycle(monkeypatch):
    harness = Harness(monkeypatch)
    children = supervisor.ChildProcesses(harness.stop)

    def spawn(process):
        def fail_read(timeout):
            raise OSError("pipe read failed")
        process.communicate_hook = fail_read

    harness.on_spawn = spawn
    supervisor.collect_cycle(children)
    assert len(harness.commands) == 1
    assert harness.processes[0].returncode == -15
    assert harness.processes[0].stdout.closed
    assert any("collect cycle error: OSError: pipe read failed" in line
               for line in harness.logs)


def test_collection_stop_does_not_start_payouts_or_lose_running_child(monkeypatch):
    harness = Harness(monkeypatch)
    children = supervisor.ChildProcesses(harness.stop)

    def spawn(process):
        def stop_during_read(timeout):
            harness.stop.set()
            raise subprocess.TimeoutExpired(process.command, timeout)
        process.communicate_hook = stop_during_read

    harness.on_spawn = spawn
    supervisor.collect_cycle(children)
    assert len(harness.commands) == 1
    assert harness.processes[0].poll() is None
    children.shutdown()
    children.close_pipes()
    assert harness.processes[0].returncode == -15
    assert harness.processes[0].stdout.closed


def test_collection_timeout_kills_stubborn_child(monkeypatch):
    harness = Harness(monkeypatch)
    children = supervisor.ChildProcesses(harness.stop)

    def spawn(process):
        process.stubborn = True

        def timeout_read(timeout):
            harness.clock.advance(timeout)
            raise subprocess.TimeoutExpired(process.command, timeout)
        process.communicate_hook = timeout_read

    harness.on_spawn = spawn
    with pytest.raises(subprocess.TimeoutExpired):
        supervisor._collect_command(children, ["fake-collector"], timeout=2)
    process = harness.processes[0]
    assert process.returncode == -9
    assert process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize("read_failure", ["error", "timeout"])
@pytest.mark.parametrize("kill_failure", ["error", "does-not-exit"])
def test_surviving_collector_blocks_replacement_but_runner_keeps_restarting(
        monkeypatch, read_failure, kill_failure):
    class FinishedCollectionThread(BackgroundThread):
        def start(self):
            self.started = True
            self.target(*self.args)
            self.alive = False

    harness = Harness(monkeypatch, thread_class=FinishedCollectionThread)

    def spawn(process):
        if process.command[1] != "collector.py":
            process.lifetime = 60
            return
        process.stubborn = True

        def survive_kill():
            process.events.append((process.pid, "kill"))
            if kill_failure == "error":
                raise OSError("cannot kill collector")

        def fail_read(timeout):
            if read_failure == "error":
                raise OSError("pipe read failed")
            harness.clock.advance(timeout)
            raise subprocess.TimeoutExpired(process.command, timeout)

        process.kill = survive_kill
        process.communicate_hook = fail_read

    harness.on_spawn = spawn
    harness.stop.on_wait = lambda timeout: (
        harness.stop.set() if len(harness.stop.waits) == 6 else None)
    supervisor.main(["--mode", "paper", "--collect-min", "1"])

    collectors = [p for p in harness.processes if p.command[1] == "collector.py"]
    runners = [p for p in harness.processes if p.command[1] == "live_h2_runner.py"]
    assert len(collectors) == 1
    assert collectors[0].poll() is None  # survives both cleanup attempts
    assert len(harness.threads) == 1  # finished worker must not be replaced
    assert len(runners) >= 2  # a stranded collector does not stall supervision
    assert any("previous collection still running" in line for line in harness.logs)
    assert any("did not exit within the kill deadline" in line for line in harness.logs)
    harness.assert_released()


def test_child_registry_refuses_overlap_until_prior_collector_exits(monkeypatch):
    harness = Harness(monkeypatch)
    children = supervisor.ChildProcesses(harness.stop)
    first = children.spawn(["fake-collector"], role="collector")
    assert children.has_running("collector")
    assert children.spawn(["overlapping-collector"], role="collector") is None
    assert children.spawn(["fake-runner"], role="runner") is not None
    first.returncode = 0
    assert not children.has_running("collector")
    assert children.spawn(["next-collector"], role="collector") is not None
    assert len(harness.commands) == 3
    children.shutdown()


def test_log_creates_nested_persistent_directory(monkeypatch, tmp_path):
    destination = tmp_path / "state" / "operational" / "logs"
    monkeypatch.setattr(supervisor, "LOG", destination)
    supervisor.log("offline test")
    assert "offline test" in (destination / "supervisor.log").read_text()
