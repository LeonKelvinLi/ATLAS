"""ATLAS health watchdog. By default, reports health without starting apps.

Windows toasts alert on CRITICAL, re-alerting at most every 2 h while the
condition persists, plus one recovery toast when health returns. Task
Scheduler recovery requires --recover; dashboard spawning independently
requires --ensure-dashboard. These options do not install scheduled jobs.

State in logs/watchdog_state.json, log in logs/watchdog.log. Never touches
trading state.
"""

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mission_control import LOGS, NO_WINDOW, build_status
from runtime_paths import PROJECT_DIR

STATE_PATH = LOGS / "watchdog_state.json"
LOG_PATH = LOGS / "watchdog.log"
REALERT_S = 2 * 3600
DASHBOARD_PORT = 8787
SUPERVISOR_TASK = "ATLAS-supervisor"
# Recovery policy. 2026-07-28: the runner wedged mid-cycle, the supervisor
# waited on a live-but-stuck child forever, and the outage ran 80 minutes
# because this watchdog could only TOAST. When recovery is explicitly enabled,
# two consecutive CRITICAL checks (~30 min) are required before acting, so a
# single transient reading never bounces a healthy trader, and at most one
# attempt per hour so a genuinely broken system is not restart-looped.
RECOVERY_AFTER_CONSECUTIVE = 2
RECOVERY_COOLDOWN_S = 3600


def should_attempt_recovery(consecutive_critical: int, now: int,
                            last_attempt_ts: int) -> bool:
    """Policy only - pure, unit-tested. Kept separate from the side effects so
    the decision can be tested without touching Task Scheduler."""
    if consecutive_critical < RECOVERY_AFTER_CONSECUTIVE:
        return False
    return (now - last_attempt_ts) >= RECOVERY_COOLDOWN_S


def restart_supervisor() -> tuple[bool, str]:
    """Stop then start the supervisor task. Returns (ok, detail).

    Deliberately does NOT kill stray processes: that needs elevation the
    watchdog does not have, and a half-killed tree is worse than a clean
    retry. If the socket lock is still held by an orphan the new runner exits
    cleanly and the next cycle tries again - visible in the log either way."""
    if sys.platform != "win32":
        return False, "unavailable: supervisor recovery requires Windows Task Scheduler"
    try:
        stop = subprocess.run(["schtasks", "/end", "/tn", SUPERVISOR_TASK],
                              capture_output=True, text=True, timeout=60,
                              creationflags=NO_WINDOW)
        time.sleep(5)
        start = subprocess.run(["schtasks", "/run", "/tn", SUPERVISOR_TASK],
                               capture_output=True, text=True, timeout=60,
                               creationflags=NO_WINDOW)
        ok = start.returncode == 0
        detail = (f"stop rc={stop.returncode} start rc={start.returncode}"
                  + ("" if ok else f" :: {(start.stderr or start.stdout).strip()[:120]}"))
        return ok, detail
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {msg}\n")


def toast(title: str, body: str) -> bool:
    """Windows toast via WinRT (no extra packages). Falls back silently -
    the watchdog log always has the event either way."""
    if sys.platform != "win32":
        return False
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = @'
<toast scenario="urgent"><visual><binding template="ToastGeneric">
<text>{title}</text><text>{body}</text>
</binding></visual></toast>
'@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$n = [Windows.UI.Notifications.ToastNotification]::new($doc)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('ATLAS').Show($n)
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
            creationflags=NO_WINDOW,   # no black flash every 15 minutes
        )
        return r.returncode == 0
    except Exception:
        return False


def port_listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def dashboard_alive(port: int = DASHBOARD_PORT) -> tuple[bool, str]:
    """(ours_and_serving, detail).

    A bare TCP accept is not proof the dashboard is up - any process that
    grabs 8787 would suppress respawn forever (audit 2026-07-24). Ask the
    API endpoint and require our own payload shape.
    """
    if not port_listening(port):
        return False, "nothing listening"
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/data", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(payload, dict) and "status" in payload:
            return True, "ok"
        return False, "foreign listener (unexpected payload)"
    except Exception as exc:
        return False, f"foreign or broken listener ({type(exc).__name__})"


def ensure_dashboard() -> None:
    """Self-heal the dashboard: if our API does not answer on its port,
    spawn it detached with pythonw (no window). At most one spawn per
    watchdog run; a crash just means another try in 15 minutes."""
    alive, detail = dashboard_alive(DASHBOARD_PORT)
    if alive:
        return
    if detail.startswith("foreign"):
        # Spawning would only lose the bind race and spam the log; the
        # operator has to free the port.
        log(f"dashboard port {DASHBOARD_PORT} held by another process "
            f"- {detail}; not respawning")
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [str(exe), str(PROJECT_DIR / "dashboard.py")],
        cwd=PROJECT_DIR, creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    log(f"dashboard not listening on {DASHBOARD_PORT} - respawned")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _run(*, recover: bool = False, start_dashboard: bool = False) -> int:
    now = int(time.time())
    status = build_status(now=now, deep=False)
    tier, reasons = status["tier"], status["reasons"]
    state = load_state()
    prev_tier = state.get("tier", "HEALTHY")
    last_alert = state.get("last_alert_ts", 0)

    if tier == "CRITICAL":
        consecutive = int(state.get("consecutive_critical", 0)) + 1
        state["consecutive_critical"] = consecutive
        if prev_tier != "CRITICAL" or now - last_alert >= REALERT_S:
            ok = toast("ATLAS CRITICAL - trader down",
                       "; ".join(reasons)[:180] or "unknown")
            log(f"alert sent (toast={'ok' if ok else 'failed'})")
            # Only a DELIVERED alert starts the 2h quiet period. Counting a
            # failed toast would buy silence per failed attempt and could
            # hide the outage indefinitely (audit 2026-07-24).
            if ok:
                state["last_alert_ts"] = now

        if recover and should_attempt_recovery(
                consecutive, now, int(state.get("last_recovery_ts", 0))):
            state["last_recovery_ts"] = now
            ok, detail = restart_supervisor()
            log(f"RECOVERY attempted after {consecutive} critical checks "
                f"({'ok' if ok else 'FAILED'}): {detail}")
            toast("ATLAS recovery attempted",
                  ("supervisor restarted" if ok else f"restart FAILED - {detail}")[:180])
    else:
        state["consecutive_critical"] = 0
        if prev_tier == "CRITICAL":
            toast("ATLAS recovered", "health back to " + tier)
            log("recovery alert sent")

    if start_dashboard:
        ensure_dashboard()

    state["tier"] = tier
    LOGS.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))
    # Logged last: an exception earlier must not be masked by a tidy line,
    # and the barrier in main() reports the failure instead.
    log(f"tier={tier}" + (f" reasons={'; '.join(reasons)}" if reasons else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Exception barrier: the watchdog is the only thing watching the
    trader, and nothing watches the watchdog. A crash here must never be
    silent (pythonw discards tracebacks), so failures are alerted and
    recorded on a best-effort basis and reported through the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recover", action="store_true",
        help="allow Windows Task Scheduler supervisor restarts after persistent "
             "critical health checks, subject to the recovery cooldown",
    )
    parser.add_argument(
        "--ensure-dashboard", action="store_true",
        help="allow starting the local dashboard if it is not already serving",
    )
    args = parser.parse_args(argv)
    try:
        return _run(recover=args.recover, start_dashboard=args.ensure_dashboard)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        try:
            toast("ATLAS watchdog error", detail[:180])
        except Exception:
            pass
        try:
            log(f"WATCHDOG ERROR {detail}")
        except Exception:
            pass  # e.g. the disk-full case that caused it
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
