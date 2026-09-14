"""ATLAS Data Contract V1 registration, bounded intake and coverage; no trading."""

import argparse
import base64
from datetime import datetime
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas.research.data_archive import (
    MAX_BATCH_BYTES, acquire_once, append_backfill, archive_coverage,
    freeze_strategy, init_archive, load_contract, receipt_time, verify_archive,
)
from atlas.research.data_contract_v1 import default_contract
from atlas.research.imports import _external_path, _json, _read, _sha


def utc_seconds(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None or parsed.microsecond or parsed.timestamp() <= 0:
            raise ValueError
        return int(parsed.timestamp())
    except (ValueError, TypeError, OverflowError):
        raise argparse.ArgumentTypeError("an explicit timezone-aware whole-second timestamp is required") from None


def import_bundle(path):
    """Import operator-supplied V1 data; the archive forces historical labels."""
    bundle = _json(_read(_external_path(path), max_bytes=MAX_BATCH_BYTES))
    if (not isinstance(bundle, dict) or set(bundle) != {"records", "captures", "calendar_coverage"}
            or not isinstance(bundle["records"], list) or not isinstance(bundle["calendar_coverage"], list)
            or not isinstance(bundle["captures"], list) or not 1 <= len(bundle["captures"]) <= 31):
        raise ValueError("backfill_bundle_shape_invalid")
    captures = []
    for item in bundle["captures"]:
        if not isinstance(item, dict) or set(item) != {"role", "request_url", "received_at", "body_base64"}:
            raise ValueError("backfill_capture_shape_invalid")
        try:
            raw = base64.b64decode(item["body_base64"], validate=True)
        except (ValueError, TypeError):
            raise ValueError("backfill_capture_encoding_invalid") from None
        captures.append({**{k: v for k, v in item.items() if k != "body_base64"}, "body": raw})
    return {**bundle, "captures": captures}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create one immutable 90/90/90 generation")
    init.add_argument("--archive", required=True)
    init.add_argument("--generation", required=True)
    init.add_argument("--research-start", required=True, type=utc_seconds)
    for name in ("verify", "contract", "coverage"):
        command = commands.add_parser(name)
        command.add_argument("--archive", required=True)
    backfill = commands.add_parser("import-backfill", help="append an explicit V1 bundle; no prospective promotion")
    backfill.add_argument("--archive", required=True)
    backfill.add_argument("--input", required=True)
    collect = commands.add_parser("collect-once", help="one bounded GET-only intake; no worker or scheduler")
    collect.add_argument("--archive", required=True)
    collect.add_argument("--provider", required=True, choices=("alpaca", "coinbase"))
    collect.add_argument("--start", required=True, type=utc_seconds)
    collect.add_argument("--end", required=True, type=utc_seconds)
    collect.add_argument("--key-file", help="explicit external Alpaca data key file; forbidden for Coinbase")
    freeze = commands.add_parser("freeze-strategy", help="pin a prepared specification before holdout begins")
    freeze.add_argument("--archive", required=True)
    freeze.add_argument("--specification", required=True, help="explicit external specification file; only its digest is stored")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            now = receipt_time()
            contract = default_contract(args.generation, args.research_start, now)
            result = init_archive(args.archive, contract)
        elif args.command == "contract":
            result = load_contract(args.archive)
        elif args.command == "verify":
            result = verify_archive(args.archive)
        elif args.command == "coverage":
            result = archive_coverage(args.archive)
        elif args.command == "import-backfill":
            result = append_backfill(args.archive, **import_bundle(args.input))
        elif args.command == "freeze-strategy":
            raw = _read(_external_path(args.specification), max_bytes=1_000_000)
            if not raw.strip():
                raise ValueError("strategy_specification_empty")
            result = freeze_strategy(args.archive, strategy_sha256=_sha(raw))
        else:
            if args.provider == "coinbase" and args.key_file:
                raise ValueError("coinbase_public_intake_does_not_accept_credentials")
            result = acquire_once(args.archive, provider=args.provider, start=args.start,
                                  end=args.end, key_file=args.key_file)
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        # Report fixed diagnostics through the lower-level APIs during development;
        # CLI rejects arbitrary imported text without reflecting it to the terminal.
        parser.error("data operation refused; check contract, timestamps, private paths and archive integrity")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 1 if result.get("collection_complete") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
