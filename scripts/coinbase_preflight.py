"""Inspect a selected Coinbase portfolio with an owner-only VIEW permission key."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.coinbase_preflight import PreflightError, collect_permissions, collect_preflight
from atlas.config import external_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", required=True,
                        help="external 0600 CDP JSON with exactly name and privateKey; ECDSA P-256")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--expected-portfolio",
                           help="owner-selected portfolio UUID; must match key and every returned account")
    selection.add_argument("--permissions-only", action="store_true",
                           help="single permission GET; discover portfolio UUID without reading accounts or fees")
    parser.add_argument("--output", required=True, help="new external JSON report file; never overwrites")
    args = parser.parse_args(argv)
    try:
        destination = external_path(args.output)
        if destination.exists() or not destination.parent.is_dir():
            raise PreflightError("coinbase_report_destination_must_be_new")
        report = (collect_permissions(args.key_file) if args.permissions_only
                  else collect_preflight(args.key_file, expected_portfolio=args.expected_portfolio))
        encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        print(encoded, end="")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, PreflightError) else "coinbase_preflight_failed"
        print(json.dumps({"error": code, "account_read_verified": False, "execution_enabled": False,
                          "orders_submitted": 0, "account_mutations": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
