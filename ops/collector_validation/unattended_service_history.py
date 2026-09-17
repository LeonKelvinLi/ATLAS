#!/usr/bin/env python3
"""Root-only sanitized journal exporter; pure scheduled-run attribution helper."""
import argparse
import json
import math
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

BACKUP = "atlas-collector-backup.service"
OFFSITE = "atlas-collector-offsite.service"
UNITS = (BACKUP, OFFSITE)
KINDS = {"7d4958e842da4a758f6c1cdc7b36dcc5": "start",
         "39f53479d3a045ac8e11786248231fbf": "finished",
         "be02cf6855d2428ba40df7e9d022f03d": "failed",
         "d9b373ed55a64feb8242e02dbe79a49c": "failed",
         "de5b426a63be47a7b6ac3eaac82e2f6f": "stop",
         "5eb03494b6584870a536b337290809b3": "restart",
         "0e4284a0caca4bfc81c0bb6786972673": "skipped"}
FIELDS = "__REALTIME_TIMESTAMP,_BOOT_ID,_PID,UNIT,INVOCATION_ID,_SYSTEMD_UNIT,_SYSTEMD_INVOCATION_ID,MESSAGE_ID,JOB_RESULT,RESULT,PRIORITY"


def stamp(value):
    if type(value) in (int, float):
        return value if math.isfinite(value) else 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        try:
            return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            return 0

def journal(args):
    result = subprocess.run(["journalctl", "--quiet", "--no-pager", "--output=json", "--output-fields=" + FIELDS, *args],
                            check=True, capture_output=True, text=True, timeout=30)
    if result.stderr.strip() or len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError("journal_not_complete_or_bound_exceeded")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def export(base, current):
    if os.geteuid() != 0:
        raise PermissionError("root_required_for_complete_system_journal")
    begin, end = base["captured_at"], current["captured_at"]
    boot = base["boot_id"].replace("-", "")
    if current["boot_id"].replace("-", "") != boot or not begin <= end:
        raise ValueError("snapshot_boot_or_time_mismatch")
    coverage = journal(["--boot=" + boot, "--until=@" + str(begin), "--lines=1"])
    if not coverage or int(coverage[-1]["__REALTIME_TIMESTAMP"]) / 1e6 > begin:
        raise ValueError("journal_does_not_cover_baseline")
    rows = journal(["--boot=" + boot, "--since=@" + str(begin), "--until=@" + str(end),
                    "--unit=" + BACKUP, "--unit=" + OFFSITE])
    events, invocations = [], {u: set() for u in UNITS}
    for row in rows:
        unit = row.get("UNIT") if row.get("_PID") == "1" else row.get("_SYSTEMD_UNIT")
        if unit not in UNITS:
            continue
        invocation = row.get("INVOCATION_ID") if row.get("_PID") == "1" else row.get("_SYSTEMD_INVOCATION_ID")
        if invocation:
            invocations[unit].add(invocation)
        kind = KINDS.get(row.get("MESSAGE_ID")) if row.get("_PID") == "1" else None
        if kind:
            events.append({"unit": unit, "kind": kind, "invocation": invocation,
                           "at": int(row["__REALTIME_TIMESTAMP"]) / 1e6,
                           "job_result": row.get("JOB_RESULT"), "boot_id": row["_BOOT_ID"]})
    return {"version": 1, "complete_privileged_export": True, "boot_id": boot,
            "from": begin, "through": end, "events": events,
            "invocations": {u: sorted(ids) for u, ids in invocations.items()}}


def attribution(base, current, history, trigger):
    if not history or history.get("complete_privileged_export") is not True:
        return False, [], ["complete_service_journal_missing"]
    if (history.get("boot_id") != base["boot_id"].replace("-", "")
            or history.get("from") != base["captured_at"] or history.get("through") != current["captured_at"]):
        return False, [], ["service_journal_snapshot_mismatch"]
    events = history["events"]
    bad = ["backup_or_offsite_interruption_in_history"] if any(e["kind"] in {"failed", "stop", "restart", "skipped"}
          or e["kind"] == "finished" and e["job_result"] != "done" for e in events) else []
    pairs = {}
    for unit, key in ((BACKUP, "backup_service"), (OFFSITE, "offsite_service")):
        rows = [e for e in events if e["unit"] == unit]
        starts, finishes = ([e for e in rows if e["kind"] == kind] for kind in ("start", "finished"))
        expected = current[key].get("InvocationID")
        if (len(starts) != 1 or len(finishes) != 1 or not expected
                or history["invocations"].get(unit) != [expected]
                or any(e["invocation"] != expected or e["boot_id"] != history["boot_id"] for e in rows)):
            return False, bad, ["extra_missing_or_unmatched_service_invocation"]
        pairs[unit] = (starts[0]["at"], finishes[0]["at"])
    bstart, bfinish = pairs[BACKUP]
    ostart, ofinish = pairs[OFFSITE]
    linked = (OFFSITE in current["backup_service"].get("OnSuccess", "").split()
              and OFFSITE in base["backup_service"].get("OnSuccess", "").split())
    receipt = current["offsite_receipt"] or {}
    ordered = (trigger <= bstart <= trigger + 120 and bstart <= receipt.get("backup_created_at", 0) <= bfinish
               and bfinish <= ostart <= bfinish + 5 and ostart <= ofinish <= current["captured_at"])
    return linked and ordered and not bad, bad, [] if linked and ordered else ["automatic_success_chain_not_correlated"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    args = parser.parse_args()
    try:
        result = export(json.loads(Path(args.baseline).read_text()), json.loads(Path(args.current).read_text()))
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(json.dumps({"complete_privileged_export": False, "error_type": type(exc).__name__}))
        raise SystemExit(1)
