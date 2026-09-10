"""Tail calibration - the metric that actually predicts realized win rate.

WHY THIS REPLACES A WRONG FRAMING. Global Brier skill (0.24% here) does NOT
cap the win rate at the gate. Murphy's decomposition describes the whole
probability distribution, not its extreme tail: with 1.6% selection the loose
Cauchy-Schwarz ceiling is ~69.4%. What actually determines realized win rate is

    w_S = mean(q_S) + mean(W - q)_S

where q_i = max(p_i, 1-p_i) is directional confidence on a selected trade and
W_i is its outcome. The first term is what the model PROMISED. The second is
TAIL CALIBRATION ERROR - the only part that can make the promise false. So a
selected set averaging q = 0.5600 should win 56.00% if its tail is calibrated,
regardless of how unimpressive the global score looks.

Overlapping 15-minute windows and simultaneous correlated pairs mean raw trade
counts overstate precision badly, so the interval here is DAY-CLUSTERED: it
resamples whole days, never individual trades.

Read-only. Research output, not a pre-registered verdict.

Usage: .venv\\Scripts\\python.exe tail_calibration.py [--ev 0.03] [--snapped]
"""

import argparse
import json
import random
from datetime import datetime, timezone
from runtime_paths import LOGS_DIR
# The FX daily rollover. IQ's binary books are shut here, so signals in these
# hours are scoreable but NOT executable (measured 2026-07-29: 0/214 orders
# accepted at 20-21Z versus 14/45 elsewhere).
ROLLOVER_HOURS = (20, 21)


def confidence(p: float) -> float:
    """Directional confidence q = max(p, 1-p). Pure - unit-tested."""
    return max(float(p), 1.0 - float(p))


def calibration_gap(rows: list[dict]) -> dict:
    """rows: [{'q': confidence, 'won': bool}] -> promised vs realized.

    The gap is realized minus promised: negative means the tail is
    OVERCONFIDENT, which is the failure mode that turns a positive-EV gate
    into a losing one. Pure - unit-tested."""
    if not rows:
        return {"n": 0, "promised": None, "realized": None, "gap": None}
    n = len(rows)
    promised = sum(r["q"] for r in rows) / n
    realized = sum(1 for r in rows if r["won"]) / n
    return {"n": n, "promised": round(promised, 4),
            "realized": round(realized, 4),
            "gap": round(realized - promised, 4)}


def day_clustered_ci(rows: list[dict], iters: int = 2000,
                     seed: int = 12345) -> tuple[float, float] | None:
    """95% interval for realized win rate, resampling whole DAYS.

    Trades inside a day share overlapping windows and correlated pairs, so
    resampling individual trades would understate the interval badly. Pure
    given the seed - unit-tested."""
    days: dict[str, list[dict]] = {}
    for r in rows:
        days.setdefault(r["day"], []).append(r)
    keys = list(days)
    if len(keys) < 2:
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(iters):
        picked = [days[rng.choice(keys)] for _ in keys]
        flat = [t for group in picked for t in group]
        if flat:
            stats.append(sum(1 for t in flat if t["won"]) / len(flat))
    if not stats:
        return None
    stats.sort()
    lo = stats[int(0.025 * len(stats))]
    hi = stats[int(0.975 * len(stats)) - 1]
    return round(lo, 4), round(hi, 4)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ev", type=float, default=0.03)
    ap.add_argument("--snapped", action="store_true",
                    help="label on the broker's true quarter-hour expiry "
                         "instead of the registered bar+15 convention")
    ap.add_argument("--frozen-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from instruments import INSTRUMENTS
    from mission_control import (candle_ohlc, frozen_universe, read_jsonl,
                                 snapped_expiry_ts, split_signals)

    signals = split_signals(read_jsonl(LOGS_DIR / "live_h2.jsonl"))["signals"]
    if not signals:
        print("no signals logged yet")
        return 0
    universe = frozen_universe() if args.frozen_only else None

    # Gather the bars each signal needs, per asset, in one pass.
    need: dict[str, set[int]] = {}
    for s in signals:
        spec = INSTRUMENTS.get(s.get("asset"))
        if spec is None:
            continue
        bar = int(s["bar_to_ts"])
        exercise = (snapped_expiry_ts(int(s["ts"])) if args.snapped
                    else bar + 15 * 60)
        need.setdefault(spec.candle_asset, set()).update((bar + 60, exercise))
    bars = {a: candle_ohlc(a, sorted(ts)) for a, ts in need.items()}

    rows, unresolved = [], 0
    for s in signals:
        asset = s.get("asset")
        spec = INSTRUMENTS.get(asset)
        if spec is None or (universe is not None and asset not in universe):
            continue
        p, payout = s.get("p_up"), s.get("payout")
        if p is None or payout is None:
            continue
        # Apply the same EV gate the strategy uses.
        ev = max(p * payout - (1 - p), (1 - p) * payout - p)
        if ev <= args.ev:
            continue
        bar = int(s["bar_to_ts"])
        exercise = (snapped_expiry_ts(int(s["ts"])) if args.snapped
                    else bar + 15 * 60)
        cmap = bars.get(spec.candle_asset, {})
        strike = cmap.get(bar + 60, (None, None))[0]
        settle = cmap.get(exercise, (None, None))[1]
        if strike is None or settle is None or settle == strike:
            unresolved += 1
            continue
        won = (settle > strike) == (s["action"] == "binary_call")
        rows.append({
            "q": confidence(p),
            "won": bool(won),
            "day": datetime.fromtimestamp(int(s["ts"]), timezone.utc)
                           .strftime("%Y-%m-%d"),
            "hour": datetime.fromtimestamp(int(s["ts"]), timezone.utc).hour,
        })

    # TRADEABILITY SPLIT (2026-07-29). 64% of gated signals fire in the
    # 20-21Z FX rollover, when the binary books are SHUT - they are scored
    # here but could never be executed. Reporting one blended number hid a
    # 73.4% untradeable bucket propping up a 53.3% tradeable one, which is
    # below the 53.5% break-even. Never report the blend alone again.
    roll = [r for r in rows if r["hour"] in ROLLOVER_HOURS]
    trad = [r for r in rows if r["hour"] not in ROLLOVER_HOURS]

    out = calibration_gap(rows)
    out["unresolved"] = unresolved
    out["rollover_20_21z"] = calibration_gap(roll)
    out["tradeable_hours"] = calibration_gap(trad)
    out["ev_gate"] = args.ev
    out["label"] = "snapped (broker expiry)" if args.snapped else "registered (bar+15)"
    out["universe"] = "frozen16" if args.frozen_only else "all"
    ci = day_clustered_ci(rows)
    out["realized_ci95_day_clustered"] = ci
    out["days"] = len({r["day"] for r in rows})
    if out["promised"] is not None:
        r_ = 0.87
        out["breakeven_at_0.87"] = round(1 / (1 + r_), 4)
        out["ev_per_stake_at_realized"] = round(
            (1 + r_) * out["realized"] - 1, 4)

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"=== tail calibration | gate ev>{args.ev} | {out['label']} "
          f"| universe {out['universe']} ===")
    if not out["n"]:
        print("  no resolved gated signals yet")
        return 0
    print(f"  gated signals   : {out['n']}  over {out['days']} day(s)"
          f"   (unresolved {out['unresolved']})")
    print(f"  PROMISED (mean q): {out['promised']:.4f}")
    print(f"  REALIZED win rate: {out['realized']:.4f}"
          + (f"   95% CI (day-clustered) {ci}" if ci else ""))
    print(f"  CALIBRATION GAP  : {out['gap']:+.4f}  "
          + ("(overconfident tail)" if out["gap"] < 0 else "(honest or better)"))
    print(f"  break-even @0.87 : {out['breakeven_at_0.87']}"
          f"   EV/stake at realized: {out['ev_per_stake_at_realized']:+.4f}")
    # The blended number above is misleading on its own - print the split.
    for name, key in (("rollover 20-21Z (UNTRADEABLE)", "rollover_20_21z"),
                      ("tradeable hours", "tradeable_hours")):
        b = out[key]
        if b["n"]:
            flag = ""
            if key == "tradeable_hours" and b["realized"] is not None:
                flag = ("  <-- BELOW BREAK-EVEN"
                        if b["realized"] < out["breakeven_at_0.87"] else "")
            print(f"  {name:30s} n={b['n']:4d}  promised {b['promised']:.4f}"
                  f"  realized {b['realized']:.4f}{flag}")

    print("\n  NOTE: research output, not a pre-registered verdict. The gap - "
          "not global Brier skill - is what makes the gate's promise true.\n"
          "  The blended rate is dominated by rollover-hour signals the broker "
          "will not accept; only the tradeable row is executable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
