"""Run or inspect a finite Coinbase public-data simulation; never sends broker orders."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.coinbase_simulation import SimulationError, SimulationLedger, validate_config
from atlas.coinbase_simulation_runner import run
from atlas.config import _json_file, external_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--journal", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run", help="bounded foreground public GET collection and local simulated fills")
    start.add_argument("--samples", type=int, required=True, help="1 to 720; per-invocation bound")
    start.add_argument("--fee-report", required=True, help="authenticated View-only preflight, maximum age 1 hour")
    start.add_argument("--output", required=True, help="new external JSON receipt")
    commands.add_parser("status", help="inspect existing idle journal without contacting Coinbase")
    commands.add_parser("stop", help="persist an entry halt in an existing idle journal")
    backup = commands.add_parser("backup", help="consistent backup of existing idle journal")
    backup.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        config = validate_config(_json_file(args.config, max_bytes=8192))
        if args.command == "run":
            def progress(item):
                status = item["status"]
                print(json.dumps({"completed_samples": item["completed_samples"],
                                  "synthetic_cash_usd": status["synthetic_cash_usd"],
                                  "holdings_btc": status["holdings_btc"],
                                  "simulated_fills": status["simulated_fills"],
                                  "halted": status["halted"], "broker_orders_submitted": 0}), flush=True)
            report = run(config, args.journal, samples=args.samples, fee_report=args.fee_report,
                         output=args.output, progress=progress)
            print(json.dumps(report, sort_keys=True))
            return 0 if report["run_state"] == "completed" else 2
        if not external_path(args.journal).is_file():
            raise SimulationError("simulation_existing_journal_required")
        with SimulationLedger(args.journal, config, create=False) as ledger:
            ledger.check_integrity()
            if args.command == "stop":
                ledger.stop("manual")
            if args.command == "backup":
                ledger.backup(args.output)
            print(json.dumps(ledger.status(), sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, SimulationError) else "simulation_command_failed",
                          "execution_enabled": False, "broker_orders_submitted": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
