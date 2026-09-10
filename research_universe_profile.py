"""Extra-universe profiler - descriptive, read-only, pre-registration dossier.

Characterizes the sidecar-collected assets (SpaceX, synthetic indices)
against reference FX spot pairs on structure alone: volatility, lag-1
autocorrelation of 1-minute returns, session shape, gaps, coverage, and
their latest payouts. NO labels, NO win rates, NO model - this is the
groundwork for the post-verdict universe-expansion hypothesis
(RESEARCH_QUEUE.md item 2), not an experiment, hence no registry entry.

The interesting question it feeds: do the broker-synthesized series carry
the OTC signature (research_otc.py measured FX-OTC at 47% WR), or do they
behave like real feeds? Structure is admissible evidence now; performance
claims wait for their own registered forward window.

Usage: python research_universe_profile.py
"""

import json
import math
from datetime import datetime, timezone
from runtime_paths import LOGS_DIR, STATE_DIR

OUT_PATH = LOGS_DIR / "universe_profile.json"

EXTRA = ["SpaceX-OTC", "SpaceX-op", "SP500-OTC", "US30-OTC",
         "USNDAQ100-OTC", "UK100-OTC"]
REFERENCE = ["EURUSD", "GBPUSD", "EURUSD-OTC"]


def series_stats(closes: list[float], to_ts: list[int]) -> dict:
    """Structure stats over a 1m close series (pure; unit-tested)."""
    n = len(closes)
    if n < 30:
        return {"candles": n, "note": "insufficient data"}
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, n) if closes[i - 1]]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    sd = math.sqrt(var)
    # lag-1 autocorrelation: OTC/synthetic feeds often show engineered
    # mean-reversion or momentum absent from real interbank mids.
    num = sum((rets[i] - m) * (rets[i - 1] - m) for i in range(1, len(rets)))
    den = sum((r - m) ** 2 for r in rets)
    ac1 = num / den if den else 0.0
    # activity by UTC hour (mean |ret| per hour bucket)
    hour_abs: dict[int, list[float]] = {}
    for i in range(1, n):
        h = datetime.fromtimestamp(int(to_ts[i]), timezone.utc).hour
        hour_abs.setdefault(h, []).append(abs(rets[i - 1]))
    hour_mean = {h: sum(v) / len(v) for h, v in hour_abs.items()}
    flat = sum(1 for r in rets if r == 0.0)
    gaps = sum(1 for i in range(1, n) if to_ts[i] - to_ts[i - 1] > 60)
    span_h = (to_ts[-1] - to_ts[0]) / 3600 if n else 0
    return {
        "candles": n,
        "span_hours": round(span_h, 1),
        "coverage": round(n / max(1.0, span_h * 60), 4),
        "ret_sd_1m": round(sd, 8),
        "lag1_autocorr": round(ac1, 4),
        "flat_bar_share": round(flat / len(rets), 4),
        "gaps_gt_1m": gaps,
        "hours_active": len(hour_mean),
        "peak_trough_activity_ratio": round(
            max(hour_mean.values()) / max(1e-12, min(hour_mean.values())), 2)
            if len(hour_mean) >= 3 else None,
    }


def main() -> int:
    import duckdb

    conn = duckdb.connect(str(STATE_DIR / "market.duckdb"), read_only=True)
    profile = {}
    try:
        for asset in EXTRA + REFERENCE:
            rows = conn.execute(
                """SELECT c.to_ts, max(c.close)
                   FROM candles c JOIN datasets d ON c.dataset_id = d.id
                   WHERE d.asset = ? AND d.interval_seconds = 60
                   GROUP BY c.to_ts ORDER BY c.to_ts""",
                [asset],
            ).fetchall()
            to_ts = [int(r[0]) for r in rows]
            closes = [float(r[1]) for r in rows]
            stats = series_stats(closes, to_ts)
            stats["group"] = "extra" if asset in EXTRA else "reference"
            stats["last_close"] = closes[-1] if closes else None
            profile[asset] = stats
        payouts = conn.execute(
            """SELECT asset, kind, payout FROM payout_snapshots
               WHERE ts_epoch = (SELECT max(ts_epoch) FROM payout_snapshots)"""
        ).fetchall()
    finally:
        conn.close()

    for a, k, p in payouts:
        if a in profile:
            profile[a].setdefault("latest_payouts", {})[k] = float(p)

    print(f"{'asset':14s} {'grp':4s} {'n':>6s} {'cov':>5s} {'sd(1m)':>10s} "
          f"{'ac1':>7s} {'flat%':>6s} {'gaps':>5s} {'p/t':>6s}")
    for asset, s in profile.items():
        if s.get("note"):
            print(f"{asset:14s} {s.get('group','?'):4s} {s['candles']:>6d}  (insufficient)")
            continue
        print(f"{asset:14s} {s['group']:4s} {s['candles']:>6d} {s['coverage']:>5.2f} "
              f"{s['ret_sd_1m']:>10.2e} {s['lag1_autocorr']:>7.3f} "
              f"{100*s['flat_bar_share']:>5.1f}% {s['gaps_gt_1m']:>5d} "
              f"{str(s['peak_trough_activity_ratio']):>6s}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "semantics": "structure-only profile; performance claims require a "
                     "registered hypothesis and forward window (queue item 2)",
        "profile": profile,
    }, indent=2))
    print(f"\nprofile -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
