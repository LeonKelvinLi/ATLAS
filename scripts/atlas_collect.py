"""Explicit initialization or foreground data collection; no execution options."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas.collection.config import load_config
from atlas.collection.runtime import init_runtime, run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "once", "run"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        result = init_runtime(config) if args.command == "init" else run(config, once=args.command == "once")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"status": "REFUSED", "error": "collector_operation_failed",
                          "execution_enabled": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
