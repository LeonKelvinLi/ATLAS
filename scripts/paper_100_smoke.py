"""One bounded, no-network engineering smoke for the dedicated synthetic pilot.

Run with: uv run --locked python scripts/paper_100_smoke.py --output /new/external/directory
The output directory must not exist. This leaves both SQLite databases, fixtures,
receipts and an active kill-switch file. It never starts an application service.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from atlas.config import build_service, external_path, load_config
from atlas.domain import BrokerError, json_value
from atlas.service import ServiceError


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(json_value(value), stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Clock:
    now = int(datetime(2026, 9, 9, 16, tzinfo=timezone.utc).timestamp())

    def __call__(self):
        return self.now


def close_service(service):
    if service is not None:
        service.stop.set()
        require(all(event.is_set() for event in service._mutations), "unfinished broker helper call")
        service.broker.close()
        service.ledger.close()


def run(output):
    output = external_path(output)
    output.mkdir(mode=0o700, exist_ok=False)
    clock, service = Clock(), None
    initial_threads = set(threading.enumerate())
    result = {
        "evidence_kind": "synthetic_engineering_smoke",
        "purpose": "Accounting, risk and restart verification; not strategy performance evidence",
        "mode": "paper", "starting_synthetic_cash_usd": "100", "real_funds_allocated_usd": "0",
        "real_orders": 0, "network_calls": 0, "persistent_workers_started": 0,
        "listeners_started": 0, "models_loaded": False, "frozen_evaluator_run": False,
        "deterministic_clock_start": clock.now,
        "actual_run_started_utc": datetime.now(timezone.utc).isoformat(),
        "checks": {}, "receipts": {}, "completed": False,
    }

    def deny_network(*args, **kwargs):
        raise RuntimeError("network forbidden in synthetic pilot smoke")

    def refused(proposal, expected_codes):
        failed = {check["code"] for check in proposal["risk"]["checks"] if not check["passed"]}
        require(proposal["status"] == "risk_rejected" and failed == set(expected_codes),
                "risk refusal did not match its expected independent cause")
        try:
            service.execute_trade_proposal(proposal["id"], caller="offline-paper-smoke")
        except ServiceError:
            pass
        else:
            raise RuntimeError("risk-rejected proposal was executable")
        require(len(service.ledger.list_intents()) == 1, "refused proposal created a submission attempt")
        return {"passed": True, "failed_risk_checks": sorted(failed), "proposal": proposal,
                "new_submission": False}

    try:
        data = json.loads((PROJECT / "deploy/paper-100-config.example.json").read_text())
        data.update(state_dir=str(output / "state"), datasets_file=str(output / "datasets.json"),
                    quotes_file=str(output / "quotes.json"))
        write_json(output / "config.json", data)
        bars = [{"timestamp": clock.now - (119 - i) * 60,
                 "open": 88000 + i * 100, "close": 88050 + i * 100} for i in range(120)]
        write_json(output / "datasets.json", {"BTC-USD": {
            "asset_class": "crypto", "bars": bars, "evidence_kind": "synthetic",
            "source": "generated engineering fixture; not observed BTC market prices",
        }})
        write_json(output / "quotes.json", {"BTC-USD": {
            "symbol": "BTC-USD", "asset_class": "crypto", "bid": "99999", "ask": "100000",
            "timestamp": clock.now, "market_open": True, "available_quantity": "1",
            "quantity_increment": "0.000001", "minimum_quantity": "0.000001",
            "source": "generated engineering fixture; not a market observation",
            "timestamp_basis": "synthetic",
        }})
        selected = load_config(output / "config.json", environ={})
        require(selected.mode == "paper" and selected.broker == "paper"
                and selected.execution_tools is False and not selected.state_dir.exists(),
                "configuration or no-start contract changed")
        result["checks"]["config_without_startup"] = True

        with patch.object(socket.socket, "connect", deny_network), \
                patch.object(socket.socket, "connect_ex", deny_network), \
                patch.object(socket, "getaddrinfo", deny_network):
            service = build_service(selected, clock=clock)
            require(service.broker.capabilities.network_enabled is False, "non-paper broker selected")
            result["initial_account"] = service.account_summary()
            analysis = service.analyze("BTC-USD")
            signal = next(row for row in analysis["signals"] if row["strategy_id"] == "momentum")
            require(signal["direction"] == "buy" and signal["data_kind"] == "synthetic"
                    and signal["live_validated"] is False, "unexpected fixture signal")
            accepted = service.create_trade_proposal(signal["signal_id"], "0.000099", "smoke-allowed")["proposal"]
            require(accepted["risk"]["passed"] and accepted["status"] == "prepared", "allowed order failed risk")
            gross = Decimal(accepted["risk"]["notional"]) + Decimal(accepted["risk"]["estimated_fees"])
            require(gross == Decimal("9.9594") and gross < 10, "fee-inclusive order amount changed")
            execution = service.execute_trade_proposal(accepted["id"], caller="offline-paper-smoke")
            require(execution["intent"]["status"] == "filled" and execution["new_submission"],
                    "single synthetic order was not filled")
            after_fill = service.account_summary()
            require(after_fill["cash"] == "90.0406", "synthetic cash/fee accounting mismatch")
            result["receipts"]["allowed_order"] = {"proposal": accepted, "execution": execution,
                                                       "fee_inclusive_notional_usd": str(gross)}
            result["checks"]["one_allowed_order_under_10_with_fees"] = True
            repeated = service.create_trade_proposal(signal["signal_id"], "0.000099", "smoke-allowed")["proposal"]
            replay = service.execute_trade_proposal(repeated["id"], caller="offline-paper-smoke")
            require(repeated["id"] == accepted["id"] and replay["new_submission"] is False
                    and service.account_summary() == after_fill and len(service.ledger.list_intents()) == 1,
                    "duplicate request altered the account or submitted twice")
            result["checks"]["idempotent_duplicate"] = True
            close_service(service)
            service = None

            service = build_service(load_config(output / "config.json", environ={}), clock=clock)
            require(service.account_summary() == after_fill, "restart changed cash or holdings")
            replay = service.execute_trade_proposal(accepted["id"], caller="offline-paper-smoke")
            require(replay["new_submission"] is False and service.account_summary() == after_fill,
                    "restart lost order idempotency")
            result["checks"]["restart_conserves_cash_positions_and_order_identity"] = True

            reseed = deepcopy(data)
            reseed["paper"].update(cash="1000", initial_capital_limit="1000")
            write_json(output / "reseed-rejected-config.json", reseed)
            try:
                unexpected = build_service(load_config(output / "reseed-rejected-config.json", environ={}), clock=clock)
            except BrokerError as exc:
                require(str(exc) == "paper_initial_capital_binding_changed", "unexpected reseed refusal")
                result["receipts"]["reseed_refusal"] = {"code": str(exc), "requested_synthetic_cash": "1000"}
            else:
                close_service(unexpected)
                raise RuntimeError("existing pilot allowed reseeding")
            require(service.account_summary() == after_fill, "reseed refusal damaged original state")
            result["checks"]["reseed_refused_without_state_change"] = True

            signal = next(row for row in service.analyze("BTC-USD")["signals"] if row["strategy_id"] == "momentum")
            oversized = service.create_trade_proposal(signal["signal_id"], "0.00011", "smoke-oversized")["proposal"]
            result["receipts"]["oversized_order_refusal"] = refused(oversized, {"order_notional"})
            result["checks"]["over_10_order_refused"] = True

            kill = selected.state_dir / "KILL_SWITCH"
            kill.touch(exist_ok=False)
            stopped = service.create_trade_proposal(signal["signal_id"], "0.00001", "smoke-killed")["proposal"]
            result["receipts"]["kill_switch_refusal"] = refused(stopped, {"kill_switch"})
            result["checks"]["kill_switch_refuses_fresh_otherwise_valid_order"] = True
            kill.unlink()  # Only this newly created fixture marker is removed.

            clock.now += 31
            stale = service.create_trade_proposal(signal["signal_id"], "0.00001", "smoke-stale")["proposal"]
            result["receipts"]["stale_input_refusal"] = refused(stale, {"quote_freshness", "account_freshness"})
            result["checks"]["stale_inputs_refused_with_kill_switch_inactive"] = True
            kill.touch(exist_ok=False)
            result["final_account"] = service.account_summary()
            require(result["final_account"] == after_fill, "refusal checks altered account")
            result["durable_intents"] = service.ledger.list_intents()
            result["audit_events"] = service.ledger.list_events()
            result["proposal_count"] = len(service.ledger.list_proposals())
            result["simulated_order_count"] = len(service.ledger.list_intents())
            result["kill_switch_left_active"] = True
            result["checks"]["final_account_conserved_after_refusals"] = True
            close_service(service)
            service = None

        for thread in set(threading.enumerate()) - initial_threads:
            thread.join(timeout=1)
        require(not (set(threading.enumerate()) - initial_threads), "broker helper thread remained running")
        result["checks"]["all_transient_broker_helper_threads_finished"] = True
        result["completed"] = True
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        close_service(service)
        state = output / "state"
        if state.is_dir():
            (state / "KILL_SWITCH").touch(exist_ok=True)
        result["deterministic_clock_end"] = clock.now
        result["artifact_hashes"] = {
            str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob("*")) if path.is_file()
        }
        result["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        write_json(output / "evidence.json", result)
    return {"completed": result["completed"], "evidence": str(output / "evidence.json"),
            "checks_passed": sum(result["checks"].values()), "synthetic_cash_remaining": result["final_account"]["cash"],
            "simulated_orders": result["simulated_order_count"], "real_orders": 0,
            "kill_switch_left_active": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="new absolute external evidence directory")
    args = parser.parse_args()
    print(json.dumps(run(args.output), indent=2))
