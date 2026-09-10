"""Evidence backup to an explicitly selected destination.

This script does not archive models or snapshot market.duckdb; the eventual
host needs a separate plan for those. Limited broker retention cannot
reconstruct all accumulated history. It copies the paper/trade log
(live_h2.jsonl - the forward test's entire evidence), its heartbeats, the
trade journal, and the ops logs. Scheduling and offsite replication are
properties of the eventual host, not of this script. Credentials are not
included; configure their storage and recovery separately.

Robustness (audit 2026-07-24): each file is copied independently, so one
locked or vanished file cannot abort the rest, and each copy lands via a
.tmp sidecar + atomic replace, so an interrupted run can never truncate
the previous good backup. journal.db uses sqlite's own backup API when
possible (a plain copy of a live sqlite file can be torn).

Usage: python backup_state.py --destination /absolute/backup/directory
       or set ATLAS_BACKUP_DIR in the process environment.
"""

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import BACKUP_DIR, LOGS_DIR, STATE_DIR

# Irreplaceable, append-only or rarely-written state.
TARGETS = [
    "logs/live_h2.jsonl",
    "logs/live_h2_heartbeat.jsonl",
    "logs/supervisor.log",
    "logs/task_run.log",
    "logs/extra_collect.log",
    "logs/catchup.log",
    "logs/watchdog.log",
    "logs/payout_landscape.json",
    "logs/universe_profile.json",
    "journal.db",
]


def _copy_atomic(src: Path, dst: Path) -> None:
    """Copy via a temp sidecar then atomically replace, so a crash or a
    locked destination never leaves a half-written backup in place."""
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _copy_sqlite(src: Path, dst: Path) -> None:
    """Consistent copy of a live sqlite file via its backup API, then the
    same atomic replace. Falls back to a plain copy if the file is not a
    readable database."""
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        source = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            target = sqlite3.connect(str(tmp))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.Error:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def run_backup(project: Path = STATE_DIR, dest: Path | None = BACKUP_DIR) -> dict:
    """Copy existing targets independently.

    Returns {copied, missing, failed, bytes}: 'missing' is benign (a log
    that does not exist yet), 'failed' is not - it means a file that IS
    there could not be backed up."""
    if dest is None:
        raise ValueError("set ATLAS_BACKUP_DIR or pass --destination explicitly")
    dest = Path(dest).expanduser()
    if not dest.is_absolute():
        raise ValueError("backup destination must be an absolute path")
    if dest.resolve() == Path(project).resolve():
        raise ValueError("backup destination must differ from the source state directory")
    dest.mkdir(parents=True, exist_ok=True)
    copied, missing, failed, total = [], [], [], 0
    for rel in TARGETS:
        src = project / rel
        try:
            if not src.exists():
                missing.append(rel)
                continue
            out = dest / rel.replace("/", "_")
            if src.suffix == ".db":
                _copy_sqlite(src, out)
            else:
                _copy_atomic(src, out)
            copied.append(rel)
            total += src.stat().st_size
        except Exception as exc:
            failed.append(f"{rel} ({type(exc).__name__})")
    return {"copied": copied, "missing": missing, "failed": failed,
            "bytes": total}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=BACKUP_DIR)
    args = parser.parse_args()
    if args.destination is None:
        parser.error("set ATLAS_BACKUP_DIR or pass --destination explicitly")
    log_path = LOGS_DIR / "backup.log"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = run_backup(dest=args.destination)
    except Exception as exc:  # destination unwritable or disk full
        line = f"[{stamp}] FAILED {type(exc).__name__}: {exc}"
        result = None
    else:
        line = (f"[{stamp}] copied={len(result['copied'])} "
                f"bytes={result['bytes']:,}"
                + (f" MISSING={','.join(result['missing'])}"
                   if result["missing"] else "")
                + (f" FAILED={','.join(result['failed'])}"
                   if result["failed"] else ""))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # losing the log line must not also lose the backup's exit code
    print(line)
    # Nonzero when the backup did not do its job, so Task Scheduler's Last
    # Result and the sidecar health check can both see it.
    if result is None or result["failed"] or not result["copied"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
