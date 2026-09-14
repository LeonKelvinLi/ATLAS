"""Read-only collector health, suitable for a systemd timer or operator check."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas.collection.config import load_config
from atlas.collection.health import report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--quick", action="store_true", help="operational counts only; does not assert archive integrity")
    args = parser.parse_args(argv)
    try:
        result = report(load_config(args.config), verify=not args.quick)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result["status"] == "HEALTHY" else 1
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"status": "UNHEALTHY", "error": "health_configuration_or_state_refused",
                          "execution_enabled": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
