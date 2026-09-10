"""Mission Control core - shared read-only data layer for status.py,
dashboard.py and watchdog.py on the always-on Windows host.

HARD CONSTRAINTS (see docs/superpowers/specs/2026-07-24-mission-control-design.md):
- Read-only against ALL trading state: market.duckdb is opened read_only,
  journal.db via sqlite URI mode=ro, jsonl logs are only ever read.
- Never evaluates the pre-registered forward-test criteria. Counts and
  displays only; forward_eval.py runs ONCE, by hand, after the window.
- Imports nothing from the signal path (no features/train/analyzer imports).

The candle-label computation here is the LABEL-FIDELITY TRACKER: an
approximation (close of the decision bar vs close of the bar ending nearest
entry+expiry) whose whole purpose is to be compared against the broker's
settled verdict. Disagreement is the measurement, not an error.
"""

import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime_paths import LOGS_DIR, MODELS_DIR, STATE_DIR
from execution_mode import PENDING_ORDER_FILE

LOGS = LOGS_DIR
HEARTBEAT_PATH = LOGS / "live_h2_heartbeat.jsonl"
SIGNALS_PATH = LOGS / "live_h2.jsonl"
SUPERVISOR_LOG = LOGS / "supervisor.log"
MARKET_DB = STATE_DIR / "market.duckdb"
JOURNAL_DB = STATE_DIR / "journal.db"
TASK_NAME = "ATLAS-supervisor"

EXPIRY_S = 15 * 60          # matches live_h2_runner EXPIRY_MINUTES
HORIZON_BARS = 15           # registered label horizon (FORWARD_TEST.md)
HEARTBEAT_STALE_S = 600     # runner cycles each minute; 10 min quiet = down
CANDLE_STALE_S = 9000       # mirrors health_report.STALE_CANDLE_S
CHURN_WINDOW_S = 600        # repeated runner exits inside 10 min = lock churn

# Sidecar jobs write one timestamped line per run. Nothing else observes
# them - their Task Scheduler exit codes are unread and pythonw discards
# their tracebacks - so a silently dead sidecar is invisible without this
# (audit 2026-07-24). Stale = no line in 2.5x its cadence.
SIDECARS = {
    "extra-collect": (LOGS / "extra_collect.log", 3600),
    "catchup": (LOGS / "catchup.log", 6 * 3600),
    "backup": (LOGS / "backup.log", 6 * 3600),
}
SIDECAR_STALE_FACTOR = 2.5
LOG_STAMP_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")

# A scheduled task launched with pythonw.exe has NO console, so Windows
# creates one for every console child it spawns - schtasks, powershell, git -
# and the user sees a black window flash. The watchdog runs every 15 minutes,
# so that is 96 flashes a day. CREATE_NO_WINDOW suppresses them; the flag does
# not exist off Windows, hence the getattr default of 0.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Pre-registered verdict set - DISPLAY ONLY, never evaluated here.
REGISTERED_SET = ("H2p ev0.03", "H2s ev0.04", "H3 meta0.60", "H4")


def observation_paths(mode: str | None = None) -> dict:
    """Select records without enabling execution or requiring credentials.

    Paper and practice share the historical research ledger. REAL records
    have their own directory and never contribute to that forward test.
    """
    selected = mode if mode is not None else os.environ.get("ATLAS_MONITOR_MODE", "practice")
    if selected not in {"paper", "practice", "real"}:
        raise ValueError("monitor mode must be paper, practice, or real")
    logs = LOGS_DIR / "real" if selected == "real" else LOGS_DIR
    return {"mode": selected, "log_dir": logs,
            "signals": logs / "live_h2.jsonl",
            "heartbeat": logs / "live_h2_heartbeat.jsonl",
            "supervisor": logs / "supervisor.log",
            "pending": logs / PENDING_ORDER_FILE}


def pending_submission(path: Path) -> dict:
    """A persistent marker means a submission may have reached the broker.

    Observation must not infer an outcome from the file or clear it, even
    when the contents are damaged or another order was reconciled.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return {"present": False}
    except OSError:
        return {"present": True, "note": "pending marker cannot be inspected; manual broker history reconciliation required"}
    return {"present": True, "path": str(path),
            "note": "order submission may be unresolved; manual broker history reconciliation required before restarting REAL orders"}


# ---------------------------------------------------------------- jsonl I/O

def read_jsonl(path: Path, tail: int | None = None) -> list[dict]:
    """Best-effort jsonl reader: skips torn/partial lines (the writer may be
    mid-append when we read)."""
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    if tail is not None:
        lines = lines[-tail:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def split_signals(rows: list[dict]) -> dict:
    """Partition live_h2.jsonl rows. Settled rows are appended duplicates of
    their signal row plus result/profit/settled, so totals must not double
    count them."""
    signals = [r for r in rows if not r.get("settled")]
    settled = [r for r in rows if r.get("settled")]
    placed = [r for r in signals if r.get("order_id")]
    skipped_otc = [r for r in signals if r.get("trade_skipped")]
    return {
        "signals": signals,
        "settled": settled,
        "placed": placed,
        "skipped_otc": skipped_otc,
    }


def expected_value(p_up: float, payout: float) -> float:
    """EV of the better side, mirroring train.decide_action's economics
    without importing the signal path."""
    call_ev = p_up * payout - (1.0 - p_up)
    put_ev = (1.0 - p_up) * payout - p_up
    return max(call_ev, put_ev)


_UNIVERSE_CACHE: tuple[str, float, frozenset] | None = None


def frozen_universe() -> set[str]:
    """The instruments the deployed H2 bundle was actually trained on
    (bundle meta['assets']). Signals on anything else are scored by a model
    that never saw that market, and the registered candles verdict filters
    them out - so their win rate must never be blended into the deployed
    universe's (audit 2026-07-24).

    Cached on (path, mtime): the dashboard rebuilds status every 60 s from a
    threading server, and unpickling a LightGBM bundle per request is pure
    waste for a file that is frozen by design. A rebuilt model changes mtime
    and is picked up automatically."""
    global _UNIVERSE_CACHE
    try:
        paths = sorted(MODELS_DIR.glob("h2-*.pkl"))
        if not paths:
            return set()
        path = paths[-1]
        key = (str(path), path.stat().st_mtime)
        if _UNIVERSE_CACHE and _UNIVERSE_CACHE[:2] == key:
            return set(_UNIVERSE_CACHE[2])
        import pickle
        # Project's own frozen bundle - the live runner unpickles it hourly.
        with open(path, "rb") as fh:
            bundle = pickle.load(fh)
        assets = frozenset(bundle.get("meta", {}).get("assets") or [])
        _UNIVERSE_CACHE = (key[0], key[1], assets)
        return set(assets)
    except Exception:
        return set()


def universe_split(signals: list[dict], universe: set[str] | None = None) -> dict:
    """How many signals fall inside vs outside the frozen training universe.
    Descriptive only."""
    universe = frozen_universe() if universe is None else universe
    if not universe:
        return {"universe_size": 0, "note": "frozen universe unavailable"}
    inside = [s for s in signals if s.get("asset") in universe]
    outside = [s for s in signals if s.get("asset") not in universe]
    by_asset_out = {}
    for s in outside:
        by_asset_out[s["asset"]] = by_asset_out.get(s["asset"], 0) + 1
    return {
        "universe_size": len(universe),
        "in_universe": len(inside),
        "out_of_universe": len(outside),
        "out_share": round(len(outside) / len(signals), 4) if signals else None,
        "out_by_asset": dict(sorted(by_asset_out.items(),
                                    key=lambda kv: -kv[1])),
        "note": "out-of-universe signals are paper-logged and reported, but "
                "the model has no validated edge there and the registered "
                "candles verdict excludes them",
    }


def forward_progress(signals: list[dict]) -> dict:
    """Trade COUNTS toward each pre-registered hypothesis. Display only -
    win rates for the verdict set are forward_eval.py's job, exactly once."""
    counts = dict.fromkeys(REGISTERED_SET, 0)
    for r in signals:
        p, payout = r.get("p_up"), r.get("payout")
        if p is None or payout is None:
            continue
        ev = expected_value(float(p), float(payout))
        if ev > 0.03:
            counts["H2p ev0.03"] += 1
        if ev > 0.04:
            counts["H2s ev0.04"] += 1
        if (r.get("meta_p") or 0.0) >= 0.60 and ev > 0.03:
            counts["H3 meta0.60"] += 1
        # H4 is H2's config PLUS extra_vol, i.e. its own model and therefore
        # its own EV. Counting "h4_p is present" credited H4 with H2's gate
        # decision (audit 2026-07-24); gate H4 on the H4 probability.
        h4_p = r.get("h4_p")
        if h4_p is not None and expected_value(float(h4_p), float(payout)) > 0.03:
            counts["H4"] += 1
    return counts


# ------------------------------------------------------------ process state

def scheduled_task_state(task_name: str = TASK_NAME) -> dict:
    """Task Scheduler view of the supervisor. Uses schtasks (no admin needed
    for /query)."""
    try:
        out = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/v", "/fo", "LIST"],
            capture_output=True, text=True, timeout=15,
            creationflags=NO_WINDOW,
        )
    except Exception as exc:  # schtasks missing = not Windows; report unknown
        return {"exists": None, "error": f"{type(exc).__name__}: {exc}"}
    if out.returncode != 0:
        return {"exists": False, "error": (out.stderr or out.stdout).strip()[:200]}
    def field(name):
        m = re.search(rf"^{name}:\s*(.+)$", out.stdout, re.MULTILINE)
        return m.group(1).strip() if m else None
    return {
        "exists": True,
        "status": field("Status"),
        "last_run": field("Last Run Time"),
        "last_result": field("Last Result"),
    }


def supervisor_tail(path: Path = SUPERVISOR_LOG, lines: int = 40) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]


def parse_supervisor_events(tail_lines: list[str]) -> list[tuple[int, str]]:
    """(epoch, message) for each parseable supervisor log line."""
    events = []
    for line in tail_lines:
        m = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]\s+(.*)", line)
        if not m:
            continue
        ts = int(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
                 .replace(tzinfo=timezone.utc).timestamp())
        events.append((ts, m.group(2)))
    return events


def last_log_entry(path: Path) -> tuple[int | None, str]:
    """(epoch, text) of the newest '[ISO8601Z] ...' line in a sidecar log.
    Missing file or no parseable line -> (None, '')."""
    if not path.exists():
        return None, ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, ""
    for line in reversed(lines):
        m = LOG_STAMP_RE.match(line.strip())
        if not m:
            continue
        ts = int(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
                 .replace(tzinfo=timezone.utc).timestamp())
        return ts, line.strip()
    return None, ""


def sidecar_health(now: int, sidecars: dict | None = None) -> dict:
    """Per-sidecar freshness and last-run outcome. Pure w.r.t. the clock so
    it can be unit-tested."""
    out = {}
    for name, (path, cadence_s) in (sidecars or SIDECARS).items():
        ts, line = last_log_entry(path)
        age = (now - ts) if ts is not None else None
        # 'never ran' is only a finding once the cadence has elapsed since
        # the process started caring; treat a missing log as stale so a
        # sidecar that never fires cannot hide.
        stale = age is None or age > cadence_s * SIDECAR_STALE_FACTOR
        # PARTIAL failure is normal and permanent: quoted-hours assets
        # (SpaceX-op, the real-feed indices) are shut outside their windows,
        # so every weekend cycle reports failed>=1. Flagging that trains the
        # operator to ignore the banner - exactly the alert fatigue that
        # hides a real outage. Only a TOTAL failure (stored=0) or the
        # exception barrier's explicit FAILED marker counts.
        stored = re.search(r"stored=(\d+)", line)
        out[name] = {
            "last_ts": ts,
            "age_s": age,
            "cadence_s": cadence_s,
            "stale": stale,
            "failed": ("FAILED" in line)
                      or (stored is not None and int(stored.group(1)) == 0),
            "last_line": line[-160:],
        }
    return out


def runner_churn(events: list[tuple[int, str]], now: int,
                 window_s: int = CHURN_WINDOW_S) -> int:
    """Count runner exits inside the recent window - the signature of a
    second runner losing the socket-lock race every 30 s."""
    return sum(1 for ts, msg in events
               if msg.startswith("runner exited") and now - ts <= window_s)


# ---------------------------------------------------------------- databases

def candle_freshness() -> dict:
    """Total candles + latest close-time from market.duckdb, read-only.
    The collector may hold the write lock; 'busy' is a normal answer."""
    if not MARKET_DB.exists():
        return {"exists": False}
    try:
        import duckdb
        conn = duckdb.connect(str(MARKET_DB), read_only=True)
        try:
            n, latest = conn.execute(
                "SELECT count(*), max(to_ts) FROM candles").fetchone()
            ns, snap = conn.execute(
                "SELECT count(*), max(ts_epoch) FROM payout_snapshots").fetchone()
        finally:
            conn.close()
        return {"exists": True, "candles": int(n or 0),
                "latest_to_ts": int(latest) if latest else None,
                "payout_snapshots": int(ns or 0),
                "latest_payout_ts": int(snap) if snap else None}
    except Exception as exc:
        return {"exists": True, "busy": f"{type(exc).__name__}: {exc}"}


def candle_ohlc(asset: str, ts_list: list[int]) -> dict[int, tuple[float, float]]:
    """{to_ts: (open, close)} for the requested bar close-times of one asset,
    read-only, joined through datasets like storage.load_canonical_history.

    Both fields are needed because the project's registered label uses the
    NEXT bar's OPEN as the strike and a later bar's CLOSE as the exercise
    price (features.py: entry_next_open)."""
    if not MARKET_DB.exists() or not ts_list:
        return {}
    try:
        import duckdb
        conn = duckdb.connect(str(MARKET_DB), read_only=True)
        try:
            placeholders = ",".join("?" for _ in ts_list)
            rows = conn.execute(
                f"""SELECT c.to_ts, max(c.open), max(c.close)
                    FROM candles c JOIN datasets d ON c.dataset_id = d.id
                    WHERE d.asset = ? AND d.interval_seconds = 60
                      AND c.to_ts IN ({placeholders})
                    GROUP BY c.to_ts""",
                [asset, *ts_list],
            ).fetchall()
        finally:
            conn.close()
        return {int(t): (float(o), float(c)) for t, o, c in rows}
    except Exception:
        return {}


def journal_counts() -> dict:
    """Row count from the (MCP-side) sqlite journal, opened read-only."""
    if not JOURNAL_DB.exists():
        return {"exists": False}
    try:
        conn = sqlite3.connect(f"{JOURNAL_DB.resolve().as_uri()}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        return {"exists": True, "runs": int(n)}
    except Exception as exc:
        return {"exists": True, "busy": f"{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------ label fidelity

def snapped_expiry_ts(order_ts: int, duration_min: int = 15) -> int:
    """The bar close-time the BROKER actually settles on.

    client.buy(..., 15) does not create a contract expiring 15 minutes later.
    The vendored library (iqoptionapi/expiration.py) builds candidate expiries
    at QUARTER-HOUR boundaries at least 5 minutes out and picks whichever is
    closest to the requested duration - so a 15-minute request placed at
    05:09 settles at 05:15 or 05:30, not 05:24. Measured 2026-07-28: scoring
    the demo trial on this expiry instead of bar+15 raised broker/candle
    agreement from 10/13 to 13/13.

    Minute-of-hour is timezone-invariant for whole-hour offsets, so working in
    UTC matches the library's local-time arithmetic. Pure - unit-tested."""
    now = datetime.fromtimestamp(order_ts, timezone.utc).replace(
        second=0, microsecond=0)
    candidates: list[int] = []
    probe = now
    while len(candidates) < 8:
        ts = int(probe.timestamp())
        if probe.minute % 15 == 0 and (ts - order_ts) > 300:
            candidates.append(ts)
        probe += timedelta(minutes=1)
    target = order_ts + duration_min * 60
    return min(candidates, key=lambda c: abs(c - target))


def _label_bars(bar_to_ts: int) -> tuple[int, int]:
    """(strike bar, exercise bar) close-times for a signal on the bar that
    closed at bar_to_ts, matching the project's REGISTERED label convention
    (features.py `_build_features_segment`):

        strike   = open[t+1]              -> the bar closing at t + 60
        exercise = close[t + horizon]     -> the bar closing at t + 15*60

    Getting this wrong measures our own inconsistency as broker
    disagreement. The earlier version used close[t] as the strike and
    derived the exercise bar from the ORDER timestamp (~19 s after the bar
    closed), which rounded to t + 960 - a full bar late (audit 2026-07-24).
    """
    return bar_to_ts + 60, bar_to_ts + HORIZON_BARS * 60


def label_fidelity(settled: list[dict], ohlc_fn=candle_ohlc) -> dict:
    """Broker verdict vs our candle label, per settled REAL order.

    Candle label, exactly as the forward test scores it: strike = the OPEN
    of the bar after the signal, exercise = the CLOSE 15 bars after the
    signal bar. call wins if exercise > strike, put wins if it fell, equal
    on a tie. This is the demo-trial fidelity measurement that FORWARD_TEST
    calls the project's single most important pending number - a low
    agreement rate is the FINDING, not a bug in this function.
    """
    from instruments import INSTRUMENTS  # data map only, not signal logic

    per_asset_ts: dict[str, set[int]] = {}
    for r in settled:
        asset = r.get("asset")
        spec = INSTRUMENTS.get(asset)
        if spec is None or not r.get("order_id"):
            continue
        strike_bar, exercise_bar = _label_bars(int(r["bar_to_ts"]))
        per_asset_ts.setdefault(spec.candle_asset, set()).update(
            (strike_bar, exercise_bar, snapped_expiry_ts(int(r["ts"]))))

    bars = {a: ohlc_fn(a, sorted(ts)) for a, ts in per_asset_ts.items()}

    rows, agree, disagree, undetermined = [], 0, 0, 0
    snap_agree = snap_disagree = 0
    for r in settled:
        asset = r.get("asset")
        spec = INSTRUMENTS.get(asset)
        if spec is None or not r.get("order_id"):
            continue
        strike_bar, exercise_bar = _label_bars(int(r["bar_to_ts"]))
        cmap = bars.get(spec.candle_asset, {})
        # strike is an OPEN, exercise is a CLOSE
        entry = cmap.get(strike_bar, (None, None))[0]
        settle = cmap.get(exercise_bar, (None, None))[1]
        broker = (r.get("result") or "").lower()
        if entry is None or settle is None:
            candle = None
        elif settle == entry:
            candle = "equal"
        else:
            went_up = settle > entry
            is_call = r.get("action") == "binary_call"
            candle = "win" if went_up == is_call else "loose"
        if candle is None:
            undetermined += 1
        elif broker in ("win", "loose", "equal"):
            if broker == candle:
                agree += 1
            else:
                disagree += 1

        # SECOND convention: the expiry the broker actually uses (quarter-hour
        # snapped). Measured 2026-07-28 to explain every disagreement the
        # registered convention produced. Tracked in parallel so the two can
        # never be silently conflated.
        snap_settle = cmap.get(snapped_expiry_ts(int(r["ts"])),
                               (None, None))[1]
        snap_label = None
        if entry is not None and snap_settle is not None:
            if snap_settle == entry:
                snap_label = "equal"
            else:
                snap_label = ("win"
                              if (snap_settle > entry)
                              == (r.get("action") == "binary_call")
                              else "loose")
            if broker in ("win", "loose", "equal"):
                if broker == snap_label:
                    snap_agree += 1
                else:
                    snap_disagree += 1

        rows.append({"ts": r.get("ts"), "asset": asset,
                     "action": r.get("action"), "broker": broker or None,
                     "candle": candle, "snapped": snap_label,
                     "profit": r.get("profit")})

    judged = agree + disagree
    return {
        "trades": rows,
        "settled_orders": len(rows),
        "judged": judged,
        "agree": agree,
        "disagree": disagree,
        "undetermined": undetermined,
        "agreement_rate": round(agree / judged, 4) if judged else None,
        "snapped_agree": snap_agree,
        "snapped_disagree": snap_disagree,
        "snapped_agreement_rate": (round(snap_agree / (snap_agree + snap_disagree), 4)
                                   if (snap_agree + snap_disagree) else None),
        "target_trades": 100,
        "note": "candle label uses the REGISTERED convention (strike = next "
                "bar open, exercise = close 15 bars on) on IQ's mid feed; "
                "disagreement rate IS the measurement",
    }


# -------------------------------------------------------------- health tier

def classify_health(*, now: int, heartbeat_ts: int | None, task_status: str | None,
                    churn_events: int, latest_candle_ts: int | None,
                    supervisor_seen: bool,
                    sidecars: dict | None = None) -> tuple[str, list[str]]:
    """Three-tier health. CRITICAL = trading is not happening; WARNING =
    trading continues but something needs eyes; HEALTHY otherwise."""
    reasons: list[str] = []
    tier = "HEALTHY"

    def warn(msg):
        nonlocal tier
        reasons.append(msg)
        if tier == "HEALTHY":
            tier = "WARNING"

    def critical(msg):
        nonlocal tier
        reasons.append(msg)
        tier = "CRITICAL"

    if heartbeat_ts is None:
        critical("no heartbeat file - runner has never cycled")
    elif now - heartbeat_ts > HEARTBEAT_STALE_S:
        critical(f"heartbeat stale {now - heartbeat_ts}s (> {HEARTBEAT_STALE_S}s)")

    if task_status is None:
        warn("scheduled task state unreadable")
    elif task_status.lower() != "running":
        critical(f"scheduled task not running (status={task_status})")

    if churn_events >= 3:
        warn(f"{churn_events} runner exits in last {CHURN_WINDOW_S}s - "
             "possible duplicate supervisor (socket-lock churn)")
    if not supervisor_seen:
        warn("supervisor.log missing or unparseable")
    if latest_candle_ts is not None and now - latest_candle_ts > CANDLE_STALE_S:
        warn(f"latest candle {now - latest_candle_ts}s old (> {CANDLE_STALE_S}s)")

    # Sidecars never block trading, so their trouble is a WARNING - but it
    # must be visible somewhere, which before this was nowhere at all.
    for name, s in (sidecars or {}).items():
        if s["stale"]:
            age = "never" if s["age_s"] is None else f"{s['age_s'] // 60}m"
            warn(f"sidecar '{name}' stale (last run {age}, "
                 f"cadence {s['cadence_s'] // 60}m)")
        elif s["failed"]:
            warn(f"sidecar '{name}' reported failure: {s['last_line'][:90]}")

    return tier, reasons


# ------------------------------------------------------------- full status

def build_status(now: int | None = None, deep: bool = True,
                 mode: str | None = None) -> dict:
    """Everything status.py / dashboard.py / watchdog.py need, one dict.
    deep=False skips the databases (watchdog fast path)."""
    paths = observation_paths(mode)
    real = paths["mode"] == "real"
    now = int(now if now is not None else time.time())
    heartbeats = read_jsonl(paths["heartbeat"], tail=500)
    hb_ts = int(heartbeats[-1]["ts"]) if heartbeats else None
    task = scheduled_task_state()
    events = parse_supervisor_events(supervisor_tail(paths["supervisor"]))
    churn = runner_churn(events, now)
    rows = read_jsonl(paths["signals"])
    parts = split_signals(rows)

    candles = candle_freshness() if deep else {}
    latest_candle = candles.get("latest_to_ts")
    sidecars = sidecar_health(now)  # cheap file reads: included even shallow

    tier, reasons = classify_health(
        now=now, heartbeat_ts=hb_ts, task_status=task.get("status"),
        churn_events=churn, latest_candle_ts=latest_candle,
        supervisor_seen=bool(events), sidecars=sidecars,
    )
    pending = pending_submission(paths["pending"]) if real else {"present": False}
    if pending["present"]:
        tier = "CRITICAL"
        reasons.append(pending["note"])

    status = {
        "generated_utc": datetime.fromtimestamp(now, timezone.utc)
                         .isoformat(timespec="seconds"),
        "tier": tier,
        "monitor_mode": paths["mode"],
        "balance_mode": "REAL" if real else "PRACTICE",
        "record_scope": "REAL account records" if real else "historical paper and PRACTICE records",
        "log_dir": str(paths["log_dir"]),
        "pending_submission": pending,
        "reasons": reasons,
        "task": task,
        "heartbeat": {
            "last_ts": hb_ts,
            "age_s": (now - hb_ts) if hb_ts else None,
            "last": heartbeats[-1] if heartbeats else None,
        },
        "supervisor": {
            "log_path": str(paths["supervisor"]),
            "recent_events": [f"{datetime.fromtimestamp(ts, timezone.utc):%H:%M:%S}Z {msg}"
                              for ts, msg in events[-6:]],
            "runner_churn_recent": churn,
        },
        "signals": {
            "total": len(parts["signals"]),
            "orders_placed": len(parts["placed"]),
            "settled": len(parts["settled"]),
            "otc_skipped": len(parts["skipped_otc"]),
            "last_signal": parts["signals"][-1] if parts["signals"] else None,
        },
        "forward_progress": {} if real else forward_progress(parts["signals"]),
        "universe": universe_split(parts["signals"]) if deep else {},
        "sidecars": sidecars,
        "shared_state_note": "market data and collector, catchup, and backup logs are shared across execution modes",
        "registered_note": ("REAL records are excluded from the frozen forward test" if real else
                            "counts only - verdicts belong to forward_eval.py, run once"),
    }
    if deep:
        status["market_db"] = candles
        status["journal"] = ({"selected": False, "note": "historical MCP journal is not a REAL runner ledger"}
                             if real else journal_counts())
        status["fidelity"] = label_fidelity(parts["settled"])
    return status


def tier_exit_code(tier: str) -> int:
    return {"HEALTHY": 0, "WARNING": 1, "CRITICAL": 2}.get(tier, 2)
