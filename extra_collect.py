"""Sidecar collector for EXTRA instruments outside the frozen verdict
universe (RESEARCH_QUEUE.md item 2 - data-only, pre-verdict legal).

Runs hourly from Task Scheduler (task: ATLAS-extra-collect, pythonw, no
window), offset from the supervisor's collection cycle to avoid DuckDB
write-lock collisions. Deliberately does NOT touch instruments.py, the
supervisor, or the runner: these assets produce no signals and no trades
until they are registered as their own hypothesis after the current
forward window.

The vendored iqoptionapi constants predate these actives, so their
server-confirmed ids (probed live 2026-07-24 via update_ACTIVES_OPCODE)
are injected into the ACTIVES map at runtime - the vendor tree stays
pristine and collector.py stays untouched.

Failures are normal: exchange-hours assets (USSPX500, US30, ...) return
no candles while closed; the next open-hours run backfills (broker keeps
~60 days). Payouts need no sidecar - the supervisor's hourly snapshot
already captures every quoted key.
"""

import json
from datetime import datetime, timezone
from runtime_paths import LOGS_DIR

LOG = LOGS_DIR / "extra_collect.log"

# Server-confirmed active ids (the vendored constants predate some).
# NOTE 2026-07-24: the real-feed index actives (USSPX500/US30/USNDAQ100/
# UK100) are DEAD - their last candle is ~Mar 2025 (probed live; ages
# ~480 days). IQ's living index products are the OTC synthetics below.
EXTRA_ACTIVE_IDS = {
    "SpaceX-OTC": 2443,     # broker-synthetic 24/7
    "SpaceX-op": 2444,      # broker-synthetic, quoted hours
    "SP500-OTC": 1971,      # synthetic S&P
    "US30-OTC": 1973,       # synthetic Dow
    "USNDAQ100-OTC": 1972,  # synthetic Nasdaq 100
    "UK100-OTC": 2047,      # synthetic FTSE 100
}
LOOKBACK_HOURS = 4.0  # self-heals a missed cycle


def _log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)


def _run() -> int:
    from iqoptionapi import constants

    for name, active_id in EXTRA_ACTIVE_IDS.items():
        constants.ACTIVES.setdefault(name, active_id)

    from collector import collect_candles

    results = collect_candles(list(EXTRA_ACTIVE_IDS), 60, LOOKBACK_HOURS)
    stored = [r for r in results if "dataset_id" in r]
    failed = [r for r in results if "error" in r]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok_part = ", ".join(f"{r['asset']}:{r['candles']}" for r in stored)
    # Causes, not just names: print() is a no-op under pythonw, so the log
    # line is the ONLY channel that survives (audit 2026-07-24).
    bad_part = ", ".join(f"{r['asset']}[{r['error'][:60]}]" for r in failed)
    _log(f"[{stamp}] stored={len(stored)} ({ok_part}) "
         f"failed={len(failed)} ({bad_part})")
    # Partial failure is normal (quoted-hours assets are shut outside their
    # window), so it exits 0. Everything failing is not: the 24/7 synthetics
    # should always store, so a total wipe means login/lock/network trouble
    # and must show up in Task Scheduler's Last Result.
    return 0 if stored else 1


def main() -> int:
    try:
        return _run()
    except BaseException as exc:  # SystemExit from a failed login included
        if isinstance(exc, KeyboardInterrupt):
            raise
        try:
            _log(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] "
                 f"FAILED {type(exc).__name__}: {exc}")
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
