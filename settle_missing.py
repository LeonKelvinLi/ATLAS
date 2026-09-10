"""Recover broker verdicts for orphaned orders - run by hand when needed.

If the runner dies between placing an order and settling it (reboot, task
kill, crash), live_h2.jsonl keeps the signal row with an order_id but never
receives its settled duplicate - and the label-fidelity measurement quietly
loses a trade.

WHICH BROKER API (probed live 2026-07-25, because two plausible ones do not
work):
- check_win_v4 CANNOT settle an orphan: it blocks on
  api.socket_option_closed[id], populated only by the close event of an
  option bought in the SAME session, so the key never appears for an order
  from a dead process.
- get_optioninfo_v2 TIMES OUT on this account (busy-waits for a websocket
  reply that never arrives).
- get_position_history and get_positions also TIME OUT.
- WORKING: get_position_history_v2(instrument_type, limit, offset, start,
  end) returns {"positions": [...]}, and get_optioninfo(limit) v1 returns
  msg.result.closed_options. Both are used here, history_v2 first.

Because no settled order has existed yet, the exact field names inside a
position entry are still unverified. Parsing is therefore defensive and,
when it cannot match an orphan, the RAW payload is written to
logs/order_records/ so the real shape is learned instead of silently lost.

Run while the runner is idle if possible (both append to the same file).

Usage: .venv\\Scripts\\python.exe settle_missing.py [--dry-run] [--limit 200]
"""

import argparse
import json
import sys
from runtime_paths import LOGS_DIR

LOG_PATH = LOGS_DIR / "live_h2.jsonl"


def find_unsettled(rows: list[dict]) -> list[dict]:
    """Signal rows whose order_id never received a settled duplicate.
    Pure - unit-tested."""
    settled_ids = {r.get("order_id") for r in rows if r.get("settled")}
    return [r for r in rows
            if r.get("order_id") and not r.get("settled")
            and r["order_id"] not in settled_ids]


def _closed_option_entries(payload) -> list:
    """closed_options list from either get_optioninfo (v1: msg.result.
    closed_options) or a v2-shaped payload (msg.closed_options)."""
    if not isinstance(payload, dict):
        return []
    msg = payload.get("msg")
    if not isinstance(msg, dict):
        return []
    result = msg.get("result")
    if isinstance(result, dict) and isinstance(result.get("closed_options"), list):
        return result["closed_options"]
    return msg.get("closed_options") if isinstance(
        msg.get("closed_options"), list) else []


def index_closed_options(payload) -> dict[int, tuple[str, float]]:
    """{order_id: (win, profit)} from a get_optioninfo response.

    The broker returns each closed option with 'id' as a LIST (see the
    vendored check_win_v3), so every id in it maps to the same outcome.
    Profit mirrors the runner's own convention: equal -> 0.0, otherwise
    win_amount - amount (a loss has win_amount 0, giving -amount).
    Pure - unit-tested."""
    out: dict[int, tuple[str, float]] = {}
    for opt in _closed_option_entries(payload):
        if not isinstance(opt, dict):
            continue
        try:
            win = opt["win"]
            amount = float(opt.get("amount") or 0.0)
            win_amount = float(opt.get("win_amount") or 0.0)
            profit = 0.0 if win == "equal" else win_amount - amount
            ids = opt.get("id")
            ids = ids if isinstance(ids, (list, tuple)) else [ids]
            for oid in ids:
                if oid is not None:
                    out[int(oid)] = (win, profit)
        except (KeyError, TypeError, ValueError):
            continue
    return out


# Field names a position entry might use for the outcome and the money.
_WIN_KEYS = ("close_reason", "win", "status")
# VERIFIED against real settled positions 2026-07-28: for a $1 binary at 0.88
# payout, pnl / pnl_net / pnl_realized all read 0.88 on a win and -1 on a loss,
# which matches the runner's own convention. close_profit is the GROSS 1.88
# (stake returned plus profit) and must never be read as profit - it is listed
# last only as a desperate fallback and would need halving-style correction.
_PNL_KEYS = ("pnl", "pnl_net", "pnl_realized", "profit")
_ID_KEYS = ("external_id", "order_ids", "raw_event_id", "id")


def index_positions(payload) -> dict[int, tuple[str, float]]:
    """{order_id: (win, profit)} from get_position_history_v2's positions.

    Field names are unverified until a real settled position exists, so this
    probes the plausible spellings and skips anything it cannot read rather
    than guessing. Pure - unit-tested."""
    out: dict[int, tuple[str, float]] = {}
    positions = payload.get("positions") if isinstance(payload, dict) else None
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        win = next((str(pos[k]) for k in _WIN_KEYS
                    if isinstance(pos.get(k), str)), None)
        pnl = next((float(pos[k]) for k in _PNL_KEYS
                    if isinstance(pos.get(k), (int, float))), None)
        if win is None or pnl is None:
            continue
        # Normalise the broker's vocabulary onto the runner's.
        win = {"won": "win", "win": "win", "lose": "loose", "loose": "loose",
               "equal": "equal", "tie": "equal"}.get(win.lower(), win.lower())
        ids: list = []
        for key in _ID_KEYS:
            v = pos.get(key)
            if isinstance(v, (list, tuple)):
                ids.extend(v)
            elif v is not None:
                ids.append(v)
        for oid in ids:
            try:
                out[int(oid)] = (win, pnl)
            except (TypeError, ValueError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=200,
                    help="history entries to page from the broker (default 200)")
    ap.add_argument("--days", type=int, default=7,
                    help="how far back to search position history (default 7)")
    args = ap.parse_args()

    rows = []
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    orphans = find_unsettled(rows)
    print(f"{len(orphans)} unsettled order(s)")
    if not orphans or args.dry_run:
        return 0

    import os

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
    verdicts: dict[int, tuple[str, float]] = {}
    raw: dict = {}

    # Primary: position history v2 (the only history call that responds).
    for instrument_type in ("binary-option", "turbo-option"):
        try:
            res = _call(client.get_position_history_v2, instrument_type,
                        args.limit, 0, start, now, timeout=60)
            payload = res[1] if isinstance(res, tuple) and len(res) == 2 else res
            raw[f"position_history_v2:{instrument_type}"] = payload
            found = index_positions(payload or {})
            verdicts.update(found)
            print(f"position_history_v2 {instrument_type}: "
                  f"{len(found)} settled id(s)")
        except Exception as exc:
            print(f"WARN position_history_v2 {instrument_type}: "
                  f"{type(exc).__name__}", file=sys.stderr)

    # Secondary: the v1 option info, used ONLY to fill gaps.
    #
    # Precedence matters and got this wrong once (2026-07-28): v1's fields were
    # allowed to overwrite position_history_v2 and recorded a $1 win as +1.76
    # instead of +0.88. position_history_v2 is the VERIFIED source - its pnl,
    # pnl_net and pnl_realized all read 0.88 for a $1 win at 0.88 payout, while
    # close_profit is the gross 1.88 including the returned stake. So v1 may
    # only supply ids v2 did not return.
    try:
        payload = _call(client.get_optioninfo, args.limit, timeout=60)
        raw["optioninfo_v1"] = payload
        found = index_closed_options(payload)
        added = 0
        for oid, outcome in found.items():
            if oid not in verdicts:
                verdicts[oid] = outcome
                added += 1
        print(f"optioninfo v1: {len(found)} closed option id(s), "
              f"{added} used to fill gaps")
    except Exception as exc:
        print(f"WARN optioninfo v1: {type(exc).__name__}", file=sys.stderr)

    print(f"broker history: {len(verdicts)} settled id(s) total")

    recovered, unmatched = 0, []
    for record in orphans:
        oid = record["order_id"]
        found = verdicts.get(int(oid))
        if found is None:
            unmatched.append(oid)
            continue
        result, profit = found
        outcome = {**record, "result": result, "profit": profit,
                   "settled": True, "recovered_by": "settle_missing"}
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(outcome) + "\n")
        recovered += 1
        print(f"  recovered {oid}: {result} {profit}")

    print(f"recovered {recovered}/{len(orphans)}")
    if unmatched:
        # Field names inside a position entry are unverified until a real
        # settled order exists. Dump the raw payloads so the true shape is
        # LEARNED rather than lost to a silent miss.
        dump = LOGS_DIR / "order_records" / "unmatched_history.json"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps({"unmatched": unmatched, "raw": raw},
                                   indent=2, default=str), encoding="utf-8")
        print(f"  unmatched: {', '.join(str(o) for o in unmatched)}\n"
              f"  raw broker payloads written to {dump} - inspect it to fix "
              f"the field mapping, or retry with a larger --limit/--days",
              file=sys.stderr)
    # Nonzero when the job did not fully succeed, so a scripted caller can
    # tell "nothing to do" (0 orphans, exit 0) from "could not recover".
    return 0 if not unmatched else 1


if __name__ == "__main__":
    raise SystemExit(main())
