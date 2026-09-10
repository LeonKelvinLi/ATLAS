"""Score ANY strategy variant against the live cross-section - no extra trades.

THE POINT. Running strategies in parallel does not speed anything up: one
broker account permits one trader, and N simultaneous hypotheses share one
forward window, so ALPHA drops to 0.05/N and every variant needs MORE evidence
than before. Parallelism buys nothing and costs power.

What does work is recording enough per cycle that variants become derivable
afterwards. live_h2_runner writes logs/cross_section.jsonl - every asset's
probability, payout and open-state each minute - which was generated in real
time before any outcome existed. So a variant scored from it inherits the live
track's leak-immunity WITHOUT having been traded, and costs no multiplicity as
long as it is REPORTED rather than verdicted.

Scoring is outcome-based (it reads candle labels), so this is a research tool:
it must NOT be used to pick a winner and then claim a pre-registered result.
That is the p-hacking this project's whole discipline exists to prevent. Use it
to size and rank candidates for the NEXT registration.

Usage:
  .venv\\Scripts\\python.exe shadow_eval.py --grid
  .venv\\Scripts\\python.exe shadow_eval.py --ev 0.02 0.03 0.04 --frozen-only
"""

import argparse
import json
from pathlib import Path

from runtime_paths import LOGS_DIR, STATE_DIR

CROSS_PATH = LOGS_DIR / "cross_section.jsonl"
HORIZON_BARS = 15


def expected_value(p: float, payout: float) -> float:
    """EV of the better side - mirrors train.decide_action's economics."""
    return max(p * payout - (1 - p), (1 - p) * payout - p)


def action_for(p: float, payout: float, margin: float) -> str | None:
    """'call'/'put'/None under an EV gate. Pure - unit-tested."""
    call_ev = p * payout - (1 - p)
    put_ev = (1 - p) * payout - p
    if max(call_ev, put_ev) <= margin:
        return None
    return "call" if call_ev >= put_ev else "put"


def count_clusters(timestamps: list[int], purge_s: int) -> tuple[int, int]:
    """(independent, clusters) from timestamps alone - same chaining rule as
    forward_eval.cluster_stats. Pure - unit-tested."""
    if not timestamps:
        return 0, 0
    ts = sorted(timestamps)
    clusters, end = 0, None
    for t in ts:
        if end is not None and t < end:
            end = max(end, t + purge_s)
        else:
            clusters += 1
            end = t + purge_s
    return len(ts), clusters


def load_cross_section(path: Path = CROSS_PATH) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def score_variant(cycles: list[dict], labels: dict, ev_margin: float,
                  universe: set[str] | None, purge_s: int) -> dict:
    """Win rate and cluster counts for one (gate, universe) variant.

    labels: {(asset, bar_to_ts): 1.0 if price rose else 0.0}. Rows without a
    label are counted as unresolved rather than dropped silently."""
    wins = n = unresolved = 0
    stamps: list[int] = []
    payouts: list[float] = []
    for cycle in cycles:
        for row in cycle.get("rows") or []:
            asset, p, pay = row.get("a"), row.get("p"), row.get("pay")
            if not row.get("open") or p is None or not pay:
                continue
            if universe is not None and asset not in universe:
                continue
            act = action_for(float(p), float(pay), ev_margin)
            if act is None:
                continue
            lab = labels.get((asset, int(row["bar"])))
            if lab is None:
                unresolved += 1
                continue
            n += 1
            wins += int((lab == 1.0) == (act == "call"))
            stamps.append(int(row["bar"]))
            payouts.append(float(pay))
    ind, clus = count_clusters(stamps, purge_s)
    mean_pay = sum(payouts) / len(payouts) if payouts else None
    return {
        "gated": n,
        "unresolved": unresolved,
        "win_rate": round(wins / n, 4) if n else None,
        "independent": ind,
        "clusters": clus,
        "mean_payout": round(mean_pay, 4) if mean_pay else None,
        "breakeven": round(1 / (1 + mean_pay), 4) if mean_pay else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ev", type=float, nargs="+",
                    default=[0.02, 0.03, 0.04, 0.05])
    ap.add_argument("--grid", action="store_true",
                    help="also score frozen-universe-only for each gate")
    ap.add_argument("--frozen-only", action="store_true")
    ap.add_argument("--horizon", type=int, default=HORIZON_BARS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cycles = load_cross_section()
    if not cycles:
        print(f"no cross-section yet at {CROSS_PATH}\n"
              "It is written by live_h2_runner from its next relaunch onward.")
        return 0

    # Labels from collected candles: registered convention is strike = next
    # bar's OPEN, exercise = close `horizon` bars on.
    import duckdb

    from instruments import INSTRUMENTS
    conn = duckdb.connect(str(STATE_DIR / "market.duckdb"), read_only=True)
    labels: dict[tuple[str, int], float] = {}
    try:
        for asset, spec in INSTRUMENTS.items():
            rows = conn.execute(
                """SELECT c.to_ts, max(c.open), max(c.close)
                   FROM candles c JOIN datasets d ON c.dataset_id = d.id
                   WHERE d.asset = ? AND d.interval_seconds = 60
                   GROUP BY c.to_ts""", [spec.candle_asset]).fetchall()
            opens = {int(t): float(o) for t, o, _ in rows}
            closes = {int(t): float(c) for t, _, c in rows}
            for bar in opens:
                entry = opens.get(bar + 60)          # next bar's OPEN
                exit_ = closes.get(bar + args.horizon * 60)
                if entry is None or exit_ is None or exit_ == entry:
                    continue
                labels[(asset, bar)] = float(exit_ > entry)
    finally:
        conn.close()

    frozen: set[str] | None = None
    try:
        from mission_control import frozen_universe
        frozen = frozen_universe() or None
    except Exception:
        pass

    purge_s = args.horizon * 60
    report = {"cycles": len(cycles), "labels": len(labels),
              "frozen_universe": len(frozen) if frozen else 0,
              "note": "REPORTED research output - not a pre-registered "
                      "verdict; do not pick a winner here and claim it",
              "variants": {}}
    for ev in args.ev:
        if not args.frozen_only:
            report["variants"][f"ev{ev}_all"] = score_variant(
                cycles, labels, ev, None, purge_s)
        if (args.grid or args.frozen_only) and frozen:
            report["variants"][f"ev{ev}_frozen16"] = score_variant(
                cycles, labels, ev, frozen, purge_s)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"cross-section cycles: {report['cycles']:,}  "
          f"labelled bars: {report['labels']:,}  "
          f"frozen universe: {report['frozen_universe']}")
    print(f"\n{'variant':22s} {'gated':>7s} {'WR':>7s} {'ind':>6s} "
          f"{'clus':>5s} {'payout':>7s} {'BE':>7s}")
    for name, v in report["variants"].items():
        wr = f"{100 * v['win_rate']:.1f}%" if v["win_rate"] is not None else "-"
        print(f"  {name:20s} {v['gated']:>7d} {wr:>7s} {v['independent']:>6d} "
              f"{v['clusters']:>5d} {str(v['mean_payout'] or '-'):>7s} "
              f"{str(v['breakeven'] or '-'):>7s}")
    print(f"\n{report['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
