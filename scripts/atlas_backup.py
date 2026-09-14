#!/usr/bin/env python3
"""Explicit local collector backup and restore; never starts a service."""

import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.collection.backup import BackupError, create_backup, restore_backup, verify_backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("create", help="Snapshot an explicitly selected collector state tree")
    backup.add_argument("--state-dir", required=True)
    backup.add_argument("--backup-dir", required=True)
    backup.add_argument("--retention", type=int, default=7, help="Keep this many verified completed snapshots (1..365)")
    backup.add_argument("--wait-seconds", type=int, default=0, help="Wait up to 0..300 seconds for the collector snapshot barrier")
    verify = commands.add_parser("verify", help="Verify manifest, SQLite and complete archive hashes")
    verify.add_argument("--backup", required=True)
    restore = commands.add_parser("restore", help="Restore to a new directory and record a collection gap")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_backup(args.state_dir, args.backup_dir, retention=args.retention, wait_seconds=args.wait_seconds)
        elif args.command == "verify":
            manifest = verify_backup(args.backup)
            result = {"backup_id": manifest["backup_id"], "file_count": len(manifest["files"]),
                      "integrity_verified": True, "execution_enabled": False}
        else:
            result = restore_backup(args.backup, args.destination)
    except BackupError as exc:
        code = str(exc)
        if not re.fullmatch(r"(?:backup|restore)_[a-z0-9_]{1,96}", code):
            code = "backup_or_restore_refused"
        print(json.dumps({"ok": False, "error": code, "execution_enabled": False}))
        return 1
    except (OSError, ValueError):
        # Even errors supplied by JSON/filesystem libraries may contain content.
        print(json.dumps({"ok": False, "error": "backup_or_restore_refused", "execution_enabled": False}))
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
