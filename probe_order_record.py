"""Dump the broker's RAW record for a settled order - the fast path to the
project's most important measurement.

FORWARD_TEST.md: the demo trial exists to answer one question - does IQ
strike and settle at its displayed mid feed (profitable, 57-62%) or apply
an order-time haircut (fatal, sub-coin-flip)? Inferring that from win/loss
AGREEMENT needs ~100 trades (~3 weeks at ~6/day). But if IQ's own order
record carries the strike and expiry quotes, the same question is answered
DIRECTLY - compare its strike to our candle open for a handful of trades.

This script makes no trading decisions and places nothing. It reads the
closed-options history plus per-order detail and writes the raw payloads to
logs/order_records/<id>.json so the fields can be inspected once real
settled orders exist (from 2026-07-27, when the binary books reopen).

Usage:
  .venv\\Scripts\\python.exe probe_order_record.py            # newest settled
  .venv\\Scripts\\python.exe probe_order_record.py --order-id 12345
  .venv\\Scripts\\python.exe probe_order_record.py --list     # what is on file
"""

import argparse
import json
import os
import sys
from runtime_paths import LOGS_DIR

OUT_DIR = LOGS_DIR / "order_records"
SIGNAL_LOG = LOGS_DIR / "live_h2.jsonl"

# Field names worth reporting on sight: any of these would let us compare
# IQ's own strike/expiry price against our candle label directly.
PRICE_HINTS = ("value", "strike", "open_quote", "close_quote", "exp_value",
               "instrument_strike_value", "price", "quote")


def settled_order_ids() -> list[int]:
    """Order ids that our own log says were placed, newest last."""
    if not SIGNAL_LOG.exists():
        return []
    ids = []
    for line in SIGNAL_LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("order_id"):
            ids.append(int(r["order_id"]))
    return ids


def find_price_fields(obj, prefix: str = "") -> dict:
    """Every numeric-looking field whose name hints at a price, flattened.
    Pure - unit-tested."""
    found = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                found.update(find_price_fields(v, path))
            elif any(h in str(k).lower() for h in PRICE_HINTS) and \
                    isinstance(v, (int, float)):
                found[path] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5]):  # first few entries are enough
            found.update(find_price_fields(v, f"{prefix}[{i}]"))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--order-id", type=int)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--days", type=int, default=7,
                    help="how far back to search position history (default 7)")
    ap.add_argument("--list", action="store_true",
                    help="list order records already captured")
    args = ap.parse_args()

    if args.list:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(OUT_DIR.glob("*.json"))
        print(f"{len(files)} captured record(s)")
        for f in files:
            print(f"  {f.name}")
        return 0

    ids = settled_order_ids()
    order_id = args.order_id or (ids[-1] if ids else None)
    if order_id is None:
        print("no placed orders in logs/live_h2.jsonl yet - the binary books "
              "reopen 2026-07-27 01:00Z; run this after the first fill")
        return 0

    from run_once import _call, _load_env

    _load_env()
    from iqoptionapi.stable_api import IQ_Option

    client = IQ_Option(os.environ["IQ_EMAIL"], os.environ["IQ_PASSWORD"])
    ok, reason = _call(client.connect, timeout=90)
    if not ok:
        raise SystemExit(f"login failed: {reason}")

    import time

    now = int(time.time())
    start = now - args.days * 86400
    record = {"order_id": order_id, "captured_utc": now}

    # Only APIs verified to RESPOND on this account (probed 2026-07-25):
    # get_optioninfo_v2, get_position_history v1 and get_positions all time
    # out, so they are not attempted. position_history_v2 is the most likely
    # carrier of the strike/expiry quotes we actually want.
    for instrument_type in ("binary-option", "turbo-option"):
        key = f"position_history_v2:{instrument_type}"
        try:
            res = _call(client.get_position_history_v2, instrument_type,
                        args.limit, 0, start, now, timeout=60)
            record[key] = res[1] if isinstance(res, tuple) and len(res) == 2 else res
        except Exception as exc:
            record[key + "_error"] = f"{type(exc).__name__}: {exc}"
    try:
        record["optioninfo_v1"] = _call(client.get_optioninfo, args.limit,
                                        timeout=60)
    except Exception as exc:
        record["optioninfo_v1_error"] = f"{type(exc).__name__}: {exc}"
    try:
        ok_bet, bet = _call(client.get_betinfo, order_id, timeout=60)
        record["betinfo_ok"] = bool(ok_bet)
        record["betinfo"] = bet
    except Exception as exc:
        record["betinfo_error"] = f"{type(exc).__name__}: {exc}"
    # The position/order pair can carry fill detail the history summary omits.
    try:
        ok_ord, order = _call(client.get_order, order_id, timeout=60)
        record["order_ok"] = bool(ok_ord)
        record["order"] = order
    except Exception as exc:
        record["order_error"] = f"{type(exc).__name__}: {exc}"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{order_id}.json"
    out_path.write_text(json.dumps(record, indent=2, default=str),
                        encoding="utf-8")

    prices = find_price_fields(record)
    print(f"raw record -> {out_path}")
    if prices:
        print("price-like fields (candidates for the direct strike "
              "comparison):")
        for k, v in sorted(prices.items()):
            print(f"  {k} = {v}")
    else:
        print("no price-like fields found - the strike is NOT exposed, so "
              "label fidelity must be measured statistically over ~100 "
              "trades as originally planned", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
