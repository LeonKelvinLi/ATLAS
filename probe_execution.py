"""Direct execution probe - answer the fidelity question in one session
instead of three weeks.

FORWARD_TEST.md makes this the project's most important open measurement:
does IQ strike and settle on its displayed mid feed (the MID column, 57-62%,
profitable) or apply an order-time markup (sub-coin-flip, fatal)? The demo
trial answers it STATISTICALLY, needing ~100 model-generated trades at ~6/day
- so the decisive number lands weeks after the forward verdict.

This answers it DIRECTLY. It places a few deliberate $1 PRACTICE binaries on
a liquid spot instrument, and for each one records:
  - the close of the signal bar and the OPEN of the next bar (our registered
    strike convention, features.py entry_next_open)
  - wall-clock order time and the resulting decision latency
  - the broker's own reported record after settlement, including any
    price-like field (its strike, if IQ exposes one)
  - the broker's win/loss versus our candle label
Comparing IQ's strike against our next-bar open is a direct measurement of
order-time markup. Ten trades settle that; a hundred are only needed when you
can measure nothing but the outcome.

ISOLATION - this is NOT part of the forward test:
  * writes to logs/execution_probe.jsonl, never to logs/live_h2.jsonl, so no
    verdict input is touched (forward_eval reads the live log)
  * makes no model prediction and uses no gate - direction is chosen by the
    --direction flag, because execution mechanics are direction-agnostic
  * hard PRACTICE guard, re-asserted immediately before every order
  * refuses to place anything without --confirm

Usage:
  .venv\\Scripts\\python.exe probe_execution.py --dry-run
  .venv\\Scripts\\python.exe probe_execution.py --trades 6 --confirm
  .venv\\Scripts\\python.exe probe_execution.py --settle-only   # collect verdicts
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from runtime_paths import LOGS_DIR, PROJECT_DIR

OUT_PATH = LOGS_DIR / "execution_probe.jsonl"
AMOUNT = 1.0
EXPIRY_MINUTES = 15
DEFAULT_ASSET = "EURUSD"


def append(record: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def read_probe_log() -> list[dict]:
    if not OUT_PATH.exists():
        return []
    rows = []
    for line in OUT_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def label_from_prices(strike: float, exercise: float, direction: str) -> str:
    """Our candle label under the registered convention. Pure - tested."""
    if exercise == strike:
        return "equal"
    went_up = exercise > strike
    return "win" if went_up == (direction == "call") else "loose"


def summarize(rows: list[dict]) -> dict:
    """Agreement between the broker's verdict and our candle label, plus the
    strike delta when IQ exposes its own strike. Pure - tested."""
    settled = [r for r in rows if r.get("broker_result") and r.get("candle_label")]
    agree = sum(1 for r in settled
                if r["broker_result"].lower() == r["candle_label"])
    deltas = [r["strike_delta_pips"] for r in settled
              if isinstance(r.get("strike_delta_pips"), (int, float))]
    out = {
        "placed": sum(1 for r in rows if r.get("order_id")),
        "settled": len(settled),
        "agree": agree,
        "disagree": len(settled) - agree,
        "agreement_rate": round(agree / len(settled), 4) if settled else None,
        "strike_samples": len(deltas),
    }
    if deltas:
        deltas_sorted = sorted(deltas)
        out["strike_delta_pips_median"] = deltas_sorted[len(deltas_sorted) // 2]
        out["strike_delta_pips_max_abs"] = max(abs(d) for d in deltas)
        out["verdict_hint"] = (
            "consistent with MID striking (no markup)"
            if out["strike_delta_pips_max_abs"] <= 0.10
            else "MARKUP DETECTED - strike deviates from the displayed feed")
    return out


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset", default=DEFAULT_ASSET,
                    help=f"spot instrument (default {DEFAULT_ASSET})")
    ap.add_argument("--trades", type=int, default=6)
    ap.add_argument("--spacing", type=int, default=120,
                    help="seconds between probe orders (default 120)")
    ap.add_argument("--direction", choices=("call", "put", "alternate"),
                    default="alternate")
    ap.add_argument("--confirm", action="store_true",
                    help="REQUIRED to place orders; without it nothing is sent")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen and print the current summary")
    ap.add_argument("--settle-only", action="store_true",
                    help="skip placing; fetch verdicts for existing probe orders")
    args = ap.parse_args()

    rows = read_probe_log()
    if args.dry_run:
        print(f"probe log: {OUT_PATH} ({len(rows)} record(s))")
        print(json.dumps(summarize(rows), indent=2))
        print(f"\nwould place {args.trades} x ${AMOUNT:.0f} PRACTICE "
              f"{EXPIRY_MINUTES}-minute binaries on {args.asset}, "
              f"{args.spacing}s apart, direction={args.direction}")
        print("re-run with --confirm to place them")
        return 0

    if not args.confirm and not args.settle_only:
        print("refusing to place orders without --confirm", file=sys.stderr)
        return 2

    from instruments import get_instrument
    from run_once import _call, _load_env

    _load_env()
    from iqoptionapi.stable_api import IQ_Option

    spec = get_instrument(args.asset)
    if args.asset.endswith("-OTC"):
        print("refusing: OTC instruments are broker-synthesised and cannot "
              "answer a question about the real feed", file=sys.stderr)
        return 2

    client = IQ_Option(os.environ["IQ_EMAIL"], os.environ["IQ_PASSWORD"])
    ok, reason = _call(client.connect, timeout=90)
    if not ok:
        raise SystemExit(f"login failed: {reason}")
    _call(client.change_balance, "PRACTICE")
    if _call(client.get_balance_mode) != "PRACTICE":
        raise SystemExit("refusing: balance mode is not PRACTICE")
    print(f"connected, PRACTICE balance {_call(client.get_balance)}")

    if args.settle_only:
        return _settle(client, _call, rows)

    placed = 0
    for i in range(args.trades):
        # Fire just after a bar closes, mirroring the runner's timing.
        time.sleep(max(0.0, 60 - time.time() % 60) + 2)
        direction = (args.direction if args.direction != "alternate"
                     else ("call" if i % 2 == 0 else "put"))
        try:
            raw = _call(client.get_candles, spec.candle_asset, 60, 3,
                        time.time(), timeout=30)
        except Exception as exc:
            print(f"  candle fetch failed: {type(exc).__name__}", file=sys.stderr)
            continue
        closed = [c for c in raw if int(c["to"]) <= time.time() + 1]
        if not closed:
            print("  no closed bar yet, skipping", file=sys.stderr)
            continue
        signal_bar = closed[-1]

        # PRACTICE re-asserted immediately before the order, as in the runner.
        if _call(client.get_balance_mode, timeout=30) != "PRACTICE":
            print("REFUSING: balance mode changed", file=sys.stderr)
            break

        # THE QUOTE WE ACTUALLY SAW AT ORDER TIME. Our registered strike
        # convention is the NEXT bar's open, but the order fills ~19 s into
        # that bar, so "what we saw" and "what we assume" are different prices.
        # Capturing the forming bar's close here gives the third leg of the
        # comparison - displayed quote vs our assumed strike vs IQ's actual
        # strike - which is what separates an order-time markup from a
        # convention artefact. Without it a disagreement is unattributable.
        quote_at_order = None
        try:
            live = _call(client.get_candles, spec.candle_asset, 60, 1,
                         time.time(), timeout=20)
            if live:
                quote_at_order = float(live[-1]["close"])
        except Exception:
            pass  # never block the order on the diagnostic

        order_ts = int(time.time())
        try:
            ok_buy, order_id = _call(
                client.buy, AMOUNT, spec.order_active, direction,
                EXPIRY_MINUTES, timeout=60)
        except Exception as exc:
            ok_buy, order_id = False, f"{type(exc).__name__}: {exc}"

        record = {
            "probe": True,
            "ts": order_ts,
            "asset": args.asset,
            "order_active": spec.order_active,
            "direction": direction,
            "expiry_minutes": EXPIRY_MINUTES,
            "amount": AMOUNT,
            "signal_bar_to_ts": int(signal_bar["to"]),
            "signal_bar_close": float(signal_bar["close"]),
            "quote_at_order": quote_at_order,
            "decision_latency_s": order_ts - int(signal_bar["to"]),
            "order_id": order_id if ok_buy else None,
        }
        if not ok_buy:
            record["order_error"] = str(order_id)
            print(f"  [{i+1}/{args.trades}] {direction} REJECTED: {order_id}")
        else:
            placed += 1
            print(f"  [{i+1}/{args.trades}] {direction} order {order_id} "
                  f"placed, latency {record['decision_latency_s']}s")
        append(record)
        if i < args.trades - 1:
            time.sleep(max(0, args.spacing - 5))

    print(f"\nplaced {placed}/{args.trades}. Settle after "
          f"{EXPIRY_MINUTES} min with --settle-only")
    return 0


def _settle(client, call, rows: list[dict]) -> int:
    """Fetch broker verdicts + raw records for probe orders and compute the
    strike comparison against our own candles."""
    from probe_order_record import find_price_fields

    pending = [r for r in rows if r.get("order_id") and not r.get("broker_result")]
    print(f"{len(pending)} probe order(s) awaiting settlement")
    if not pending:
        print(json.dumps(summarize(rows), indent=2))
        return 0

    now = int(time.time())
    history = {}
    for instrument_type in ("binary-option", "turbo-option"):
        try:
            res = call(client.get_position_history_v2, instrument_type,
                       200, 0, now - 86400, now, timeout=60)
            payload = res[1] if isinstance(res, tuple) and len(res) == 2 else res
            history[instrument_type] = payload
        except Exception as exc:
            print(f"WARN history {instrument_type}: {type(exc).__name__}",
                  file=sys.stderr)

    from settle_missing import index_positions
    verdicts = {}
    for payload in history.values():
        verdicts.update(index_positions(payload or {}))

    settled = 0
    for record in pending:
        oid = int(record["order_id"])
        outcome = verdicts.get(oid)
        # Our own candle label: strike = next bar's OPEN, exercise = close
        # 15 bars on (features.py entry_next_open).
        strike_bar = int(record["signal_bar_to_ts"]) + 60
        exercise_bar = int(record["signal_bar_to_ts"]) + EXPIRY_MINUTES * 60
        from mission_control import candle_ohlc
        bars = candle_ohlc(record["asset"], [strike_bar, exercise_bar])
        strike = bars.get(strike_bar, (None, None))[0]
        exercise = bars.get(exercise_bar, (None, None))[1]

        out = {**record}
        if strike is not None and exercise is not None:
            out["candle_strike"] = strike
            out["candle_exercise"] = exercise
            out["candle_label"] = label_from_prices(strike, exercise,
                                                   record["direction"])
            # Convention cost: how far our assumed strike sat from the quote
            # actually on screen when the order went in. A large value here
            # means a disagreement is OUR convention, not IQ's markup - the
            # distinction the whole measurement rests on. Pip = 1e-4, or 1e-2
            # for JPY crosses and gold.
            q = record.get("quote_at_order")
            pip = 0.01 if ("JPY" in record["asset"]
                           or record["asset"].startswith("XAU")) else 0.0001
            if isinstance(q, (int, float)):
                out["convention_gap_pips"] = round((strike - q) / pip, 3)

            # ATTRIBUTION: is a disagreement a markup, or just a different
            # settlement rule? Nadex settles on a trimmed average of the last
            # ten pre-expiry midpoints, not a single close. If quote_recorder
            # was running we can compute that label too and compare BOTH
            # against the broker - which separates an averaging artefact from
            # an order-time haircut (RESEARCH_QUEUE 2026-07-25).
            from quote_recorder import read_quotes, trimmed_settlement

            expiry_ts = int(record["signal_bar_to_ts"]) + EXPIRY_MINUTES * 60
            stream = read_quotes(record["asset"], expiry_ts - 120, expiry_ts)
            if stream:
                trimmed = trimmed_settlement([q for _, q in stream])
                out["expiry_quote_samples"] = len(stream)
                if trimmed is not None:
                    out["candle_exercise_trimmed"] = trimmed
                    out["trimmed_label"] = label_from_prices(
                        strike, trimmed, record["direction"])
                    out["settlement_rule_gap_pips"] = round(
                        (trimmed - exercise) / pip, 3)
        if outcome:
            out["broker_result"], out["broker_profit"] = outcome
            settled += 1
        # Any price-like field in the broker record lets us compare strikes.
        prices = find_price_fields(history)
        if prices:
            out["broker_price_fields"] = prices
        append(out)

    print(f"settled {settled}/{len(pending)}")
    print(json.dumps(summarize(read_probe_log()), indent=2))
    if not verdicts:
        dump = LOGS_DIR / "order_records" / "probe_history.json"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps(history, indent=2, default=str),
                        encoding="utf-8")
        print(f"no verdicts parsed - raw history written to {dump} so the "
              "field mapping can be fixed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
