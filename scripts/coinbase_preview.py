"""Read public BTC-USD data and export a fee-aware $100 simulation; no accounts or orders."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.brokers.coinbase_public import CoinbasePublicClient, CoinbasePublicError
from atlas.coinbase_preview import build_preview, validate_config
from atlas.config import ConfigError, _json_file, external_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="explicit external JSON with allocation=100, budget<=100, maker/taker fee scenarios")
    parser.add_argument("--output", required=True, help="new external owner-only JSON report; never overwrites")
    args = parser.parse_args(argv)
    try:
        config = validate_config(_json_file(args.config, max_bytes=4096))
        destination = external_path(args.output)
        # Do not follow a final symlink even if it points outside the checkout.
        original = Path(args.output).expanduser()
        if original.is_symlink() or destination.exists() or not destination.parent.is_dir():
            raise CoinbasePublicError("coinbase_preview_report_destination_must_be_new")
        snapshot = CoinbasePublicClient().snapshot()
        report = build_preview(snapshot, config)
        encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        print(encoded, end="")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, CoinbasePublicError) else (
            "coinbase_preview_external_config_invalid" if isinstance(exc, ConfigError) else "coinbase_preview_failed")
        print(json.dumps({"error": code, "execution_enabled": False, "orders_submitted": 0,
                          "account_mutations": 0, "account_connected": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
