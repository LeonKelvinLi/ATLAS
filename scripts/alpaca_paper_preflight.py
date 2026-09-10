"""Inspect an owner-selected Alpaca paper account using three read-only GETs."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.alpaca_paper_preflight import PreflightError, collect_preflight
from atlas.config import external_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", required=True,
                        help="external owner-only 0600 JSON file with exactly api_key and api_secret")
    parser.add_argument("--output", help="optional new external JSON report file; never overwrites")
    args = parser.parse_args(argv)
    try:
        destination = external_path(args.output) if args.output else None
        if destination is not None and (destination.exists() or not destination.parent.is_dir()):
            raise PreflightError("paper_report_destination_must_be_new")
        report = collect_preflight(args.key_file)
        encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if destination is not None:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        print(encoded, end="")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, PreflightError) else "paper_preflight_failed"
        print(json.dumps({"error": code, "paper_account_read_verified": False,
                          "execution_enabled": False, "orders_submitted": 0, "account_mutations": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
