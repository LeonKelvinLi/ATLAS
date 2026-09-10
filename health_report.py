"""Collection-only data-health report.

Reads DuckDB and the collector log ONLY - no broker login, no training, no
model or gate changes. Run at the end of every collection cycle (and any
time by hand) to monitor cycle exit statuses, gaps, conflicts, required
payout-key presence, per-day accumulation, and payout coverage ON COLLECTED
CANDLES.

Semantics (per reviewer, 2026-07-21):
- Coverage denominators are COLLECTED candles inside the window, never
  wall-clock minutes: 68 covered of 160 collected is reported as exactly
  that, not as coverage of a full 48-hour window.
- UTC dates merely touched by data are not independent days. An ELIGIBLE
  day needs at least MIN_DAY_CANDLES observed 1m candles AND per-day payout
  coverage on collected candles >= MIN_DAY_PAYOUT_COVERAGE.
- Everything here is a collection-level ESTIMATE. The research-review
  trigger is the prospective coverage of a frozen walk-forward run over
  eligible days - never this report alone.

Usage: python health_report.py [--current-cycle-status N]
(prints JSON, writes logs/health.json)
"""

import argparse
import bisect
import json
import re
import sys
from datetime import datetime, timezone
from runtime_paths import LOGS_DIR

LOG_PATH = LOGS_DIR / "collector.log"
HEALTH_PATH = LOGS_DIR / "health.json"

PAYOUT_MAX_AGE_S = 7200      # must match train.py --payout-max-age default
COVERAGE_WINDOW_H = 48
# Eligible-day rule: a UTC day counts only with this many observed 1m candles
# (a full forex day has ~1440) AND this payout coverage on those candles.
MIN_DAY_CANDLES = 1000
MIN_DAY_PAYOUT_COVERAGE = 0.95
STALE_CANDLE_S = 9000        # 2.5h: two consecutive missed hourly cycles


def parse_cycle_statuses(
    log_text: str, last: int = 24, current_status: int | None = None
) -> list[int]:
    """Exit statuses of recent collection cycles, oldest first.

    The log's status line for the RUNNING cycle is appended only after this
    report runs, so the caller passes the current cycle's status explicitly
    - otherwise health.json would always be one cycle behind."""
    statuses = [int(m) for m in re.findall(r"=== cycle exit status: (\d+) ===", log_text)]
    if current_status is not None:
        statuses.append(current_status)
    return statuses[-last:]


def payout_coverage_counts(
    candle_to_ts: list[int], snapshot_ts: list[int], max_age_s: int = PAYOUT_MAX_AGE_S
) -> tuple[int, int]:
    """(covered, collected) among the given candle close-times.

    Mirrors storage.latest_payout_before semantics: only snapshots at or
    before the timestamp count, and older-than-max-age is unavailable.
    The denominator is candles WE COLLECTED - never wall-clock minutes."""
    snapshot_ts = sorted(snapshot_ts)
    covered = 0
    for ts in candle_to_ts:
        idx = bisect.bisect_right(snapshot_ts, ts)
        if idx and ts - snapshot_ts[idx - 1] <= max_age_s:
            covered += 1
    return covered, len(candle_to_ts)


def day_breakdown(
    candle_to_ts: list[int], snapshot_ts: list[int], max_age_s: int = PAYOUT_MAX_AGE_S
) -> dict[str, dict]:
    """Per-UTC-date candle counts, payout coverage, and eligibility."""
    by_day: dict[str, list[int]] = {}
    for ts in candle_to_ts:
        day = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(int(ts))
    breakdown = {}
    for day, ts_list in sorted(by_day.items()):
        covered, collected = payout_coverage_counts(ts_list, snapshot_ts, max_age_s)
        coverage = covered / collected if collected else 0.0
        breakdown[day] = {
            "candles": collected,
            "payout_covered": covered,
            "payout_coverage": round(coverage, 4),
            "eligible": collected >= MIN_DAY_CANDLES and coverage >= MIN_DAY_PAYOUT_COVERAGE,
        }
    return breakdown


def build_report(
    conn, log_text: str, now_epoch: int, current_cycle_status: int | None = None
) -> dict:
    from instruments import INSTRUMENTS
    from storage import load_canonical_history

    statuses = parse_cycle_statuses(log_text, current_status=current_cycle_status)
    warnings: list[str] = []
    if not statuses:
        warnings.append("no cycle statuses parsed from collector.log")

    report: dict = {
        "generated_utc": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
        "semantics": (
            "collection-level ESTIMATE only; coverage denominators are collected "
            "candles, not wall-clock minutes; the research-review trigger is the "
            "prospective coverage of a frozen walk-forward run over eligible days"
        ),
        "eligible_day_rule": {
            "min_candles": MIN_DAY_CANDLES,
            "min_payout_coverage": MIN_DAY_PAYOUT_COVERAGE,
        },
        "cycles": {
            "recent_exit_statuses": statuses,
            "recent_failures": sum(1 for s in statuses if s != 0),
        },
        "instruments": {},
        "warnings": warnings,
        "healthy": True,
    }

    latest_batch = conn.execute("SELECT max(ts_epoch) FROM payout_snapshots").fetchone()[0]
    window_start = now_epoch - COVERAGE_WINDOW_H * 3600

    for asset, spec in INSTRUMENTS.items():
        history, hist_report = load_canonical_history(conn, spec.candle_asset, 60)
        snapshot_ts = [
            r[0]
            for r in conn.execute(
                "SELECT ts_epoch FROM payout_snapshots WHERE asset = ? AND kind = ? ORDER BY ts_epoch",
                (spec.quote_key, spec.option_kind),
            ).fetchall()
        ]
        if history.empty:
            report["instruments"][asset] = {"candles": 0, "error": "no data"}
            report["healthy"] = False
            continue

        all_to_ts = [int(t) for t in history["to_ts"]]
        recent_to_ts = [t for t in all_to_ts if t >= window_start]
        covered, collected = payout_coverage_counts(recent_to_ts, snapshot_ts)
        days = day_breakdown(all_to_ts, snapshot_ts)
        last_candle_age = now_epoch - max(all_to_ts)
        in_latest_batch = bool(
            latest_batch is not None
            and conn.execute(
                "SELECT count(*) FROM payout_snapshots WHERE ts_epoch = ? AND asset = ? AND kind = ?",
                (latest_batch, spec.quote_key, spec.option_kind),
            ).fetchone()[0]
        )

        report["instruments"][asset] = {
            "candles": hist_report["candles"],
            "datasets": len(hist_report["datasets_used"]),
            "gaps": len(hist_report["gaps"]),
            "conflicts": len(hist_report["conflicts"]),
            "last_candle_age_s": last_candle_age,
            "payout_snapshots": len(snapshot_ts),
            "payout_key_in_latest_batch": in_latest_batch,
            f"collected_candles_{COVERAGE_WINDOW_H}h": collected,
            f"payout_covered_candles_{COVERAGE_WINDOW_H}h": covered,
            f"payout_coverage_on_collected_candles_{COVERAGE_WINDOW_H}h": (
                round(covered / collected, 4) if collected else None
            ),
            "utc_dates_touched": len(days),
            "candles_per_utc_day": {d: v["candles"] for d, v in days.items()},
            "day_breakdown": days,
            "eligible_days": sum(1 for v in days.values() if v["eligible"]),
        }

        # Warnings surface issues without flipping the collection exit status,
        # and healthy=true must never conceal them.
        if hist_report["gaps"]:
            warnings.append(f"{asset}: {len(hist_report['gaps'])} historical gap(s) in canonical history")
        if last_candle_age > STALE_CANDLE_S:
            warnings.append(f"{asset}: latest candle is {last_candle_age}s old (stale, > {STALE_CANDLE_S}s)")
        # Data-only instruments have no option market, so a missing payout
        # quote is expected for them and must not fail collection health.
        if hist_report["conflicts"] or (not in_latest_batch and spec.tradable):
            report["healthy"] = False

    if statuses and statuses[-1] != 0:
        report["healthy"] = False
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--current-cycle-status",
        type=int,
        default=None,
        help="exit status of the collection cycle that just ran (its log line "
        "is appended after this report, so it must be passed explicitly)",
    )
    args = parser.parse_args()

    from storage import open_db

    conn = open_db()
    log_text = LOG_PATH.read_text() if LOG_PATH.exists() else ""
    report = build_report(
        conn,
        log_text,
        int(datetime.now(timezone.utc).timestamp()),
        current_cycle_status=args.current_cycle_status,
    )
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_PATH.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
