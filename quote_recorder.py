"""Sub-minute quote recorder - the missing data for settlement attribution.

WHY THIS EXISTS. External research (RESEARCH_QUEUE.md, 2026-07-25) found that
a regulated venue with a PUBLISHED rule - Nadex - settles FX not on a single
closing price but on the last TEN midpoints before expiry with the highest
three and lowest three discarded, averaging the remaining four. If IQ does
anything comparable, our label (the close of the bar 15 bars on) is wrong
INDEPENDENTLY of any markup, and a broker disagreement would be an averaging
artefact rather than evidence of an order-time haircut. Those two causes have
opposite consequences for the project, and one-minute bars cannot separate
them.

So this records the live quote at sub-minute resolution into
logs/quotes_<asset>.jsonl. Fed to probe_execution's settlement analysis it
yields BOTH candidate labels - last-quote and trimmed-average - so a
disagreement becomes attributable instead of ambiguous.

Read-only against the account: it polls prices and places nothing.

Usage:
  .venv\\Scripts\\python.exe quote_recorder.py --asset EURUSD --minutes 20
  .venv\\Scripts\\python.exe quote_recorder.py --asset EURUSD --until-ts 1784999999
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import LOGS_DIR

LOGS = LOGS_DIR

# Nadex's published rule, used as the reference averaging scheme because it is
# the only settlement methodology we can actually read. Not an assumption
# about IQ - a hypothesis to test against IQ's verdicts.
NADEX_SAMPLE_COUNT = 10
NADEX_DISCARD_EACH_SIDE = 3


def log_path(asset: str) -> Path:
    safe = asset.replace("/", "_")
    return LOGS / f"quotes_{safe}.jsonl"


def trimmed_settlement(quotes: list[float],
                       sample_count: int = NADEX_SAMPLE_COUNT,
                       discard: int = NADEX_DISCARD_EACH_SIDE) -> float | None:
    """Nadex-style settlement from a quote stream: take the LAST
    `sample_count` quotes, drop the `discard` highest and `discard` lowest,
    average what remains.

    Returns None when there are too few quotes to apply the rule honestly -
    silently averaging 2 samples would invent a settlement price. Pure -
    unit-tested."""
    if not quotes:
        return None
    window = quotes[-sample_count:]
    if len(window) < sample_count:
        return None
    if 2 * discard >= len(window):
        return None
    kept = sorted(window)[discard:len(window) - discard]
    if not kept:
        return None
    return sum(kept) / len(kept)


def read_quotes(asset: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
    """Recorded (ts, quote) pairs inside [start_ts, end_ts]. Tolerates torn
    lines - the writer may be mid-append."""
    path = log_path(asset)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            ts = int(row["ts"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if start_ts <= ts <= end_ts and isinstance(row.get("q"), (int, float)):
            out.append((ts, float(row["q"])))
    out.sort(key=lambda r: r[0])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset", default="EURUSD")
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--until-ts", type=int, default=None,
                    help="record until this epoch instead of --minutes")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between samples (default 2)")
    args = ap.parse_args()

    from instruments import get_instrument
    from run_once import _call, _load_env

    _load_env()
    from iqoptionapi.stable_api import IQ_Option

    spec = get_instrument(args.asset)
    client = IQ_Option(os.environ["IQ_EMAIL"], os.environ["IQ_PASSWORD"])
    ok, reason = _call(client.connect, timeout=90)
    if not ok:
        raise SystemExit(f"login failed: {reason}")
    _call(client.change_balance, "PRACTICE")

    deadline = args.until_ts or int(time.time() + args.minutes * 60)
    path = log_path(args.asset)
    LOGS.mkdir(parents=True, exist_ok=True)
    print(f"recording {args.asset} every {args.interval}s until "
          f"{datetime.fromtimestamp(deadline, timezone.utc):%H:%M:%S}Z -> {path}",
          flush=True)

    written, failures = 0, 0
    while time.time() < deadline:
        try:
            raw = _call(client.get_candles, spec.candle_asset, 60, 1,
                        time.time(), timeout=15)
            # The newest (still forming) bar's close IS the live quote.
            if raw:
                rec = {"ts": int(time.time()), "q": float(raw[-1]["close"]),
                       "bar_from": int(raw[-1]["from"])}
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
                written += 1
        except Exception as exc:
            failures += 1
            if failures <= 3:
                print(f"WARN sample failed: {type(exc).__name__}",
                      file=sys.stderr, flush=True)
        time.sleep(max(0.25, args.interval))

    print(f"done: {written} quotes written, {failures} failures", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
