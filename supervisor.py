"""Portable supervisor for explicitly selected paper or practice operation.

Run with ``--mode paper`` to collect candles and log signals, or explicitly
select ``--mode practice`` to let the runner place demo-account orders.
``--paper`` remains an alias for ``--mode paper``. No mode is selected by
default. Choosing a mode does not validate model artifacts or authorize a
second host to use an account already running elsewhere.

Collection runs in the background without overlapping cycles. The runner
restarts with a bounded backoff after short sessions. SIGTERM and SIGINT
stop both types of child process and release the local singleton lock.
"""

import argparse
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from instruments import INSTRUMENTS
from runtime_paths import LOGS_DIR, PROJECT_DIR

PYTHON = sys.executable
LOG = LOGS_DIR
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SHORT_RUN_S = 60
BACKOFF_START_S = 30
BACKOFF_MAX_S = 300
LOOP_WAIT_S = 30
COLLECT_POLL_S = 1
# The runner needs time to acknowledge a submitted order, reconcile pending
# IDs and disconnect. Its own bounded cleanup fits within this grace period.
TERMINATE_WAIT_S = 30
KILL_WAIT_S = 5
COLLECT_JOIN_S = 2

SUPERVISOR_LOCK_PORT = 47202  # runner uses 47201
_LOCK_SOCK = None


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    LOG.mkdir(parents=True, exist_ok=True)
    line = f"[{_stamp()}] {msg}"
    print(line, flush=True)
    with open(LOG / "supervisor.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _stop_processes(processes) -> None:
    """Terminate together, then kill/reap stragglers within shared deadlines."""
    errors = []
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass  # the child exited between poll and terminate
            except OSError as exc:
                errors.append(f"child pid={process.pid} terminate failed: {exc}")

    deadline = time.monotonic() + TERMINATE_WAIT_S
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        except OSError as exc:
            errors.append(f"child pid={process.pid} wait failed: {exc}")

    for process in processes:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except OSError as exc:
                errors.append(f"child pid={process.pid} kill failed: {exc}")

    deadline = time.monotonic() + KILL_WAIT_S
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            errors.append(f"child pid={process.pid} did not exit within the kill deadline")
        except OSError as exc:
            errors.append(f"child pid={process.pid} final wait failed: {exc}")
    # Cleanup of one child must not prevent attempts to stop the others.
    for error in errors:
        log(error)


class ChildProcesses:
    """Track every child, including collectors created by the worker thread."""

    def __init__(self, stop: threading.Event):
        self.stop = stop
        self._lock = threading.Lock()
        self._processes: dict[subprocess.Popen, str] = {}

    def spawn(self, command, *, role="runner", **kwargs):
        # Synchronize process creation with the shutdown snapshot. A child
        # already being created when stop arrives is tracked before shutdown
        # takes the snapshot; subsequent launches are refused.
        with self._lock:
            if self.stop.is_set():
                return None
            if role == "collector" and any(
                    child_role == "collector" and child.poll() is None
                    for child, child_role in self._processes.items()):
                # A collector can outlive its worker if OS termination/kill
                # fails. Never replace that process with an overlapping one.
                return None
            process = subprocess.Popen(
                command, cwd=PROJECT_DIR, creationflags=NO_WINDOW, **kwargs)
            self._processes[process] = role
            return process

    def has_running(self, role: str) -> bool:
        with self._lock:
            return any(child_role == role and child.poll() is None
                       for child, child_role in self._processes.items())

    def forget(self, process) -> None:
        with self._lock:
            self._processes.pop(process, None)

    def shutdown(self) -> None:
        self.stop.set()
        with self._lock:
            processes = list(self._processes)
        _stop_processes(processes)

    def close_pipes(self) -> None:
        """Called only after the collection worker has finished using pipes."""
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


def _collect_command(children: ChildProcesses, command, timeout: float):
    process = children.spawn(
        command, role="collector", stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    if process is None:
        return None
    deadline = time.monotonic() + timeout
    try:
        while not children.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(
                    timeout=min(COLLECT_POLL_S, remaining))
                return subprocess.CompletedProcess(
                    command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
        # The main thread owns shutdown of any still-running child. Keep it
        # registered until then, even though this worker is returning.
        return None
    except Exception:
        if not children.stop.is_set() and process.poll() is None:
            _stop_processes([process])
        raise
    finally:
        if process.poll() is not None:
            children.forget(process)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


def collect_cycle(children: ChildProcesses) -> None:
    """Collect candles and payouts without preventing runner supervision."""
    assets = list(INSTRUMENTS)
    try:
        result = _collect_command(
            children,
            [PYTHON, "collector.py", "candles", *assets, "--interval", "60", "--hours", "2"],
            timeout=1200,
        )
        if result is None or children.stop.is_set():
            return
        log(f"collect candles exit={result.returncode}"
            + ("" if result.returncode == 0 else f" stderr={result.stderr[-200:]}"))
        result = _collect_command(
            children, [PYTHON, "collector.py", "payouts"], timeout=300)
        if result is not None:
            log(f"collect payouts exit={result.returncode}")
    except Exception as exc:
        log(f"collect cycle error: {type(exc).__name__}: {exc}")


def acquire_singleton_lock(port: int = SUPERVISOR_LOCK_PORT):
    """Hold a localhost port until shutdown, or return None if it is held.

    This coordinates processes on this host only. A server migration also
    needs an explicit handover from any other host using the same account.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    return sock


def _positive_minutes(value: str) -> int:
    minutes = int(value)
    if minutes <= 0:
        raise argparse.ArgumentTypeError("collection interval must be positive")
    return minutes


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mode", choices=("paper", "practice"),
                      help="paper logs signals; practice enables demo orders")
    mode.add_argument("--paper", dest="mode", action="store_const", const="paper",
                      help="alias for --mode paper")
    ap.add_argument("--collect-min", type=_positive_minutes, default=60)
    args = ap.parse_args(argv)

    # Parse before even acquiring the lock or writing a log. An unconfigured
    # service must fail visibly instead of silently selecting demo trading.
    global _LOCK_SOCK
    _LOCK_SOCK = acquire_singleton_lock()
    if _LOCK_SOCK is None:
        log("another supervisor already holds the lock - exiting cleanly")
        return

    stop = threading.Event()
    children = ChildProcesses(stop)
    previous_handlers = {}
    collector_thread: threading.Thread | None = None

    def request_stop(signum, frame):
        # No logging, waiting, or process cleanup from inside a handler.
        stop.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

        log(f"supervisor start (python={PYTHON}, mode={args.mode}, "
            f"trade={'on' if args.mode == 'practice' else 'off'})")
        last_collect = None
        runner = None
        runner_cmd = [PYTHON, "live_h2_runner.py", "--minutes", "57"]
        if args.mode == "practice":
            runner_cmd.append("--trade")
        backoff = BACKOFF_START_S
        launched_at = 0.0

        while not stop.is_set():
            now = time.monotonic()
            if last_collect is None or now - last_collect >= args.collect_min * 60:
                if ((collector_thread is not None and collector_thread.is_alive())
                        or children.has_running("collector")):
                    log("previous collection still running - skipping this "
                        "cycle (catchup_gaps will heal any hole)")
                else:
                    collector_thread = threading.Thread(
                        target=collect_cycle, args=(children,), daemon=True)
                    collector_thread.start()
                last_collect = now

            if runner is None or runner.poll() is not None:
                if runner is not None:
                    children.forget(runner)
                    lived = now - launched_at
                    log(f"runner exited ({runner.returncode}) after "
                        f"{lived:.0f}s; relaunching")
                    if lived < SHORT_RUN_S:
                        log(f"  short run - backing off {backoff:.0f}s "
                            "(stray lock holder?)")
                        if stop.wait(backoff):
                            break
                        backoff = min(backoff * 2, BACKOFF_MAX_S)
                    else:
                        backoff = BACKOFF_START_S
                runner = children.spawn(runner_cmd)
                if runner is not None:
                    launched_at = time.monotonic()
                    log(f"runner launched pid={runner.pid}")
            stop.wait(LOOP_WAIT_S)
    except KeyboardInterrupt:
        # Also handle explicit KeyboardInterrupt from embedding callers.
        stop.set()
    finally:
        try:
            children.shutdown()
            if collector_thread is not None:
                collector_thread.join(timeout=COLLECT_JOIN_S)
                if collector_thread.is_alive():
                    log("collection worker did not finish within the join deadline")
                else:
                    children.close_pipes()
            log("supervisor stopped")
        finally:
            _LOCK_SOCK.close()
            _LOCK_SOCK = None
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    main()
