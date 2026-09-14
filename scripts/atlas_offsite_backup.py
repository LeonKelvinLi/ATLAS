#!/usr/bin/env python3
"""Upload a verified local collector backup and verify its exact GCS generation."""

import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas.collection.offsite import upload_verified_backup, write_receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--backup")
    source.add_argument("--backup-dir")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--min-free-bytes", type=int, default=268435456)
    parser.add_argument("--receipt", help="Private status file directly inside work-dir")
    args = parser.parse_args(argv)
    try:
        result = upload_verified_backup(args.backup, args.bucket, args.work_dir,
            backup_dir=args.backup_dir, min_free_bytes=args.min_free_bytes)
    except Exception as exc:
        code = str(exc)
        if not re.fullmatch(r"(?:offsite|backup|collector)_[a-z0-9_]{1,96}", code):
            code = "offsite_operation_failed"
        result = {"ok": False, "error": code, "execution_enabled": False,
                  "downloaded_snapshot_integrity_verified": False, "restore_operation_performed": False}
    if args.receipt:
        try:
            write_receipt(args.receipt, result, args.work_dir)
        except Exception:
            result = {"ok": False, "error": "offsite_receipt_write_failed", "execution_enabled": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
