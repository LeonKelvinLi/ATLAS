"""One-glance ATLAS status for the Windows host (parity with status.sh on
the Mac). Read-only. Exit code: 0 HEALTHY / 1 WARNING / 2 CRITICAL, so it
can gate scripts.

Usage: .venv\\Scripts\\python.exe status.py [--json]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from mission_control import build_status, observation_paths, tier_exit_code

TIER_MARK = {"HEALTHY": "OK ", "WARNING": "WRN", "CRITICAL": "CRT"}


def fmt_age(seconds):
    if seconds is None:
        return "n/a"
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("--mode", choices=("paper", "practice", "real"),
                    help="record scope; defaults to ATLAS_MONITOR_MODE or practice")
    args = ap.parse_args(argv)
    try:
        mode = observation_paths(args.mode)["mode"]
    except ValueError as exc:
        ap.error(str(exc))

    s = build_status(mode=mode)
    if args.json:
        print(json.dumps(s, indent=2, default=str))
        return tier_exit_code(s["tier"])

    now = time.time()
    print(f"=== ATLAS {s['monitor_mode'].upper()} {s['tier']} === {s['generated_utc']}")
    print(f"  {s['record_scope']} | {s['log_dir']}")
    print(f"  {s['shared_state_note']}")
    for r in s["reasons"]:
        print(f"  [{TIER_MARK[s['tier']]}] {r}")

    t = s["task"]
    print("\n--- scheduled task ---")
    print(f"  status={t.get('status')}  last_result={t.get('last_result')}  "
          f"last_run={t.get('last_run')}")

    hb = s["heartbeat"]
    print("\n--- runner heartbeat ---")
    if hb["last"]:
        print(f"  {fmt_age(hb['age_s'])} ago: assets={hb['last'].get('assets')} "
              f"max_conf={hb['last'].get('max_conf')} signals={hb['last'].get('signals')}")
    else:
        print("  none yet")

    print("\n--- supervisor (recent) ---")
    for line in s["supervisor"]["recent_events"]:
        print(f"  {line}")

    sig = s["signals"]
    print("\n--- signals / trades ---")
    print(f"  signals={sig['total']}  orders={sig['orders_placed']}  "
          f"settled={sig['settled']}  otc_skipped={sig['otc_skipped']}")
    if sig["last_signal"]:
        ls = sig["last_signal"]
        when = datetime.fromtimestamp(ls["ts"], timezone.utc).strftime("%m-%d %H:%MZ")
        print(f"  last: {when} {ls['asset']} {ls['action']} p={ls['p_up']} "
              f"payout={ls['payout']} mode={ls['mode']}")

    print(f"\n--- {s['registered_note']} ---")
    for k, v in s["forward_progress"].items():
        print(f"  {k}: {v}")

    db = s.get("market_db", {})
    print("\n--- market.duckdb ---")
    if not db.get("exists"):
        print("  missing (collector will rebuild)")
    elif db.get("busy"):
        print(f"  busy: {db['busy'][:80]}")
    else:
        age = fmt_age(now - db["latest_to_ts"]) if db.get("latest_to_ts") else "n/a"
        page = fmt_age(now - db["latest_payout_ts"]) if db.get("latest_payout_ts") else "n/a"
        print(f"  candles={db['candles']:,} (latest {age} ago)  "
              f"payout_snapshots={db['payout_snapshots']} (latest {page} ago)")

    u = s.get("universe") or {}
    if u.get("universe_size"):
        print(f"\n--- signal universe (frozen basket = {u['universe_size']}) ---")
        print(f"  in-universe={u['in_universe']}  out-of-universe="
              f"{u['out_of_universe']}"
              + (f" ({u['out_share']:.0%})" if u.get("out_share") else ""))
        if u.get("out_by_asset"):
            print("  outside: " + ", ".join(f"{a}({n})"
                                            for a, n in u["out_by_asset"].items()))

    print("\n--- sidecars ---")
    for name, sc in s.get("sidecars", {}).items():
        mark = "STALE" if sc["stale"] else ("FAIL" if sc["failed"] else "ok  ")
        print(f"  [{mark}] {name:14s} last {fmt_age(sc['age_s'])} ago "
              f"(cadence {sc['cadence_s'] // 60}m)")

    fid = s.get("fidelity", {})
    print("\n--- label fidelity (broker verdict vs candle label) ---")
    print(f"  settled orders={fid.get('settled_orders', 0)}/"
          f"{fid.get('target_trades', 100)}  judged={fid.get('judged', 0)}  "
          f"agree={fid.get('agree', 0)}  disagree={fid.get('disagree', 0)}  "
          f"rate={fid.get('agreement_rate')}")

    return tier_exit_code(s["tier"])


if __name__ == "__main__":
    sys.exit(main())
