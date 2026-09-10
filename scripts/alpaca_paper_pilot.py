"""Explicit one-shot paper setup/smoke/status; no service or live endpoint.

The per-user state location is fixed. Changing config paths cannot silently seed
another pilot database. The account fingerprint, policy and client UUID are bound
inside that database. `smoke` may POST once and cancel that exact paper order.
"""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.alpaca_paper_pilot import PaperPilot, PilotError
from atlas.brokers.alpaca_paper import AlpacaPaperClient
from atlas.config import read_secret_file
from atlas.domain import BrokerError


def load_selection(path):
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        config = json.loads(read_secret_file(path), object_pairs_hook=unique)
        if (set(config) != {"schema", "key_file", "expected_account_fingerprint"}
                or type(config["schema"]) is not int or config["schema"] != 1
                or not isinstance(config["key_file"], str)):
            raise ValueError
        return config
    except Exception:
        raise PilotError("pilot_selection_file_invalid") from None


def run_smoke(pilot, *, sleeper=time.sleep):
    submission_error = None
    try:
        pilot.submit_once()
    except BrokerError as exc:
        # Submission may have reached Alpaca. Recovery never makes another POST.
        submission_error = str(exc)
        snapshot = pilot.status(reconcile=False)
        if snapshot.get("active_submit_attempts", snapshot["submit_attempts"]) == 0:
            raise
    try:
        result = pilot.cancel_once()
        for _ in range(5):
            if result["completed"]:
                break
            sleeper(2)
            result = pilot.cancel_once()
        result = pilot.status()
        result["submission_error"] = submission_error
        result["polling_finished"] = True
        result["account_reconciled"] = (
            result.get("open_order_count") == 0 and result.get("unmanaged_order_count") == 0
            and not result.get("original_order_present_after_correction_grant", False)
        )
        return result
    finally:
        pilot.halt()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "status", "smoke", "cancel", "halt",
                                           "corrected-smoke", "corrected-status", "corrected-cancel"))
    parser.add_argument("--config", required=True, help="external owner-only 0600 paper selection JSON")
    args = parser.parse_args(argv)
    try:
        selection = load_selection(args.config)
        client = AlpacaPaperClient(key_file=selection["key_file"])
        state_dir = Path.home() / ".config" / "atlas" / "alpaca-paper" / "pilot"
        correction = args.action.startswith("corrected-")
        pilot = PaperPilot(state_dir, client, selection["expected_account_fingerprint"], correction=correction)
        action = args.action.removeprefix("corrected-")
        if correction and action == "smoke":
            pilot.grant_correction()
        if action == "smoke":
            result = run_smoke(pilot)
        else:
            result = getattr(pilot, "cancel_once" if action == "cancel" else action)()
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        if action in {"smoke", "cancel"} and (
                not result["completed"] or not result.get("account_reconciled", True)):
            return 3
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, BrokerError) else "pilot_operation_failed"
        print(json.dumps({"error": code, "mode": "paper", "automatic_trading_enabled": False,
                          "note": "No retry is performed; inspect durable state and reconcile the same client ID."}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
