#!/usr/bin/env python3
"""Explicit bounded public history capture; no credentials or trading methods."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.research.coinbase_history import fetch_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="inclusive UTC date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="exclusive UTC date YYYY-MM-DD")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    parse = lambda value: int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    try:
        result = fetch_history(start=parse(args.start), end=parse(args.end), output_dir=args.output_dir,
                               progress=lambda row: print(json.dumps(row), file=sys.stderr, flush=True))
        print(json.dumps({key: value for key, value in result.items() if key != "pages"}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"complete": False, "error": "public_history_capture_failed", "orders_submitted": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
