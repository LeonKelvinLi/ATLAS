"""Gap-catchup collector - the Windows replacement for the Mac's catchup.sh.

The supervisor collects a fixed 2-hour window hourly, so any outage longer
than that (sleep, reboot, network loss) leaves a PERMANENT hole in
market.duckdb - the broker only serves ~60 days, and holes cannot be
refilled later. This sidecar runs every 6 h from Task Scheduler
(ATLAS-catchup, pythonw, no window): it measures the newest candle per
asset and, when the gap exceeds the supervisor's own reach, collects a
window big enough to heal it (capped at 168 h; the broker serves it).

Assets: the full frozen trading universe plus the extra-collection set.
Data-only; touches no execution or hypothesis code.
"""

import time
from datetime import datetime, timezone
from runtime_paths import LOGS_DIR, STATE_DIR

LOG = LOGS_DIR / "catchup.log"

MIN_GAP_H = 3.0      # below this the supervisor/sidecar already cover it
CAP_H = 168.0        # one week - stays well inside the broker's ~60 days
DEFAULT_H = 96.0     # for assets with no candles at all yet


def plan_catchup(latest_by_asset: dict[str, int | None], now: int,
                 min_gap_h: float = MIN_GAP_H, cap_h: float = CAP_H,
                 default_h: float = DEFAULT_H) -> tuple[list[str], float]:
    """(assets needing collection, lookback hours covering the worst gap).

    Pure - unit-tested. One collection call heals every needy asset at once
    (a single broker session), slightly over-fetching the less-needy ones;
    dataset dedupe upstream makes over-fetch harmless."""
    needy: list[str] = []
    worst = 0.0
    for asset, latest in latest_by_asset.items():
        if latest is None:
            needy.append(asset)
            worst = max(worst, default_h)
            continue
        gap_h = (now - int(latest)) / 3600
        if gap_h > min_gap_h:
            needy.append(asset)
            worst = max(worst, min(cap_h, gap_h + 1.0))
    return needy, worst


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {msg}\n")


def _run() -> int:
    import duckdb

    from extra_collect import EXTRA_ACTIVE_IDS
    from instruments import INSTRUMENTS

    assets = list(INSTRUMENTS) + list(EXTRA_ACTIVE_IDS)
    now = int(time.time())
    # The collector holds market.duckdb's single-writer lock for minutes at
    # a time, and a read_only open fails hard against it. Losing this run is
    # fine (the next is 6h away, gaps stay healable for ~60 days); dying
    # without a trace is not (audit 2026-07-24).
    try:
        conn = duckdb.connect(str(STATE_DIR / "market.duckdb"),
                              read_only=True)
    except Exception as exc:
        log(f"skipped: market.duckdb busy ({type(exc).__name__}) - "
            "collector holds the write lock; retrying next cycle")
        return 0
    try:
        rows = dict(conn.execute(
            """SELECT d.asset, max(c.to_ts)
               FROM candles c JOIN datasets d ON c.dataset_id = d.id
               WHERE d.interval_seconds = 60 GROUP BY d.asset"""
        ).fetchall())
    finally:
        conn.close()

    latest = {a: rows.get(a) for a in assets}
    needy, hours = plan_catchup(latest, now)
    if not needy:
        log("no gaps beyond supervisor reach - nothing to do")
        return 0

    # Inject the extra actives' ids (vendored constants predate them),
    # then heal everything in one broker session.
    from iqoptionapi import constants
    for key, active_id in EXTRA_ACTIVE_IDS.items():
        constants.ACTIVES.setdefault(key, active_id)
    from collector import collect_candles

    log(f"healing window={hours:.0f}h assets={len(needy)}: "
        f"{', '.join(needy)}")
    results = collect_candles(needy, 60, hours)
    stored = sum(1 for r in results if "dataset_id" in r)
    failed = [r["asset"] for r in results if "error" in r]
    log(f"healed window={hours:.0f}h assets={len(needy)} stored={stored} "
        f"failed={len(failed)} ({', '.join(failed)})")
    return 0 if stored else 1


def main() -> int:
    """Exception barrier. collect_candles' preamble is unguarded by design
    (login raises SystemExit, storage.open_db raises on the write lock), and
    under pythonw an escaping exception leaves no trace at all - a failed
    heal would be indistinguishable from a task that never fired."""
    try:
        return _run()
    except BaseException as exc:  # SystemExit from a failed login included
        if isinstance(exc, KeyboardInterrupt):
            raise
        try:
            log(f"FAILED {type(exc).__name__}: {exc}")
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
