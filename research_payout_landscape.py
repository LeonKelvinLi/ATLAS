"""Payout landscape report - descriptive, read-only, ROI lever #1.

ROI = WR * payout - (1 - WR): the payout term is a free lever - the same
signals placed where payouts are fat earn more at identical win rate.
Break-even WR = 1 / (1 + payout): 53.5% at 0.87 but 52.1% at 0.92.

This script aggregates the prospectively collected payout_snapshots per
(asset, kind) and per UTC hour, prints the landscape, and writes
logs/payout_landscape.json - the routing table RESEARCH_QUEUE.md item 5
will need post-verdict. Purely descriptive: no labels, no win rates, no
hypothesis, hence no registry entry (same class as health_report.py).

Usage: python research_payout_landscape.py [--min-snapshots 5]
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from runtime_paths import LOGS_DIR, STATE_DIR

OUT_PATH = LOGS_DIR / "payout_landscape.json"


def break_even_wr(payout: float) -> float:
    """Win rate at which a binary with this payout returns exactly zero."""
    return 1.0 / (1.0 + payout)


def summarize(rows):
    """rows: (asset, kind, ts_epoch, payout) -> per-key and per-hour stats."""
    by_key = defaultdict(list)
    by_key_hour = defaultdict(lambda: defaultdict(list))
    for asset, kind, ts, payout in rows:
        key = (asset, kind)
        by_key[key].append(float(payout))
        hour = datetime.fromtimestamp(int(ts), timezone.utc).hour
        by_key_hour[key][hour].append(float(payout))

    out = {}
    for (asset, kind), vals in by_key.items():
        vals_sorted = sorted(vals)
        n = len(vals)
        mean = sum(vals) / n
        hours = {
            str(h): round(sum(v) / len(v), 4)
            for h, v in sorted(by_key_hour[(asset, kind)].items())
        }
        best_hour = max(hours, key=hours.get) if hours else None
        out[f"{asset}|{kind}"] = {
            "snapshots": n,
            "mean": round(mean, 4),
            "p10": round(vals_sorted[max(0, int(n * .1) - 1)], 4),
            "max": round(vals_sorted[-1], 4),
            "latest": round(vals[-1], 4),
            "break_even_wr_mean": round(break_even_wr(mean), 4),
            "hours_covered": len(hours),
            "by_utc_hour": hours,
            "best_hour_utc": best_hour,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-snapshots", type=int, default=5)
    args = ap.parse_args()

    import duckdb
    from instruments import INSTRUMENTS

    conn = duckdb.connect(str(STATE_DIR / "market.duckdb"), read_only=True)
    try:
        rows = conn.execute(
            "SELECT asset, kind, ts_epoch, payout FROM payout_snapshots "
            "ORDER BY ts_epoch"
        ).fetchall()
    finally:
        conn.close()

    stats = summarize(rows)
    stats = {k: v for k, v in stats.items() if v["snapshots"] >= args.min_snapshots}

    trial_keys = {f"{s.quote_key}|binary" for s in INSTRUMENTS.values()
                  if s.tradable and not s.quote_key.endswith("-OTC")}
    trial = {k: v for k, v in stats.items() if k in trial_keys}
    ranked = sorted(trial.items(), key=lambda kv: -kv[1]["mean"])

    print(f"payout snapshots aggregated: {sum(v['snapshots'] for v in stats.values()):,} "
          f"across {len(stats)} (asset, kind) keys")
    print("\n=== demo-trial universe (spot binary), ranked by mean payout ===")
    print(f"{'key':24s} {'mean':>6s} {'p10':>6s} {'max':>6s} {'latest':>7s} "
          f"{'BE-WR':>6s} {'hrs':>4s} {'best-hr':>7s}")
    for k, v in ranked:
        print(f"  {k:22s} {v['mean']:6.3f} {v['p10']:6.3f} {v['max']:6.3f} "
              f"{v['latest']:7.3f} {v['break_even_wr_mean']:6.3f} "
              f"{v['hours_covered']:4d} {str(v['best_hour_utc']):>7s}")

    under = [k for k, v in ranked if v["mean"] < 0.80]
    top_all = sorted(stats.items(), key=lambda kv: -kv[1]["mean"])[:10]
    print("\n=== top payers, any market (incl. OTC/synthetics) ===")
    for k, v in top_all:
        print(f"  {k:30s} mean={v['mean']:.3f}  BE-WR={v['break_even_wr_mean']:.3f}")
    if under:
        print(f"\nunder-earners in trial universe (mean payout < 0.80): {', '.join(under)}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "semantics": "descriptive payout aggregation; routing decisions are a "
                     "post-verdict, registered concern (RESEARCH_QUEUE item 5)",
        "stats": stats,
    }, indent=2))
    print(f"\nfull landscape -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
