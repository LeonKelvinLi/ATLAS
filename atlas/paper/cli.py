"""Explicit bounded PAPER commands; status, halt and config-check are offline."""

import argparse
from dataclasses import fields
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

from atlas.ledger import digest
from atlas.paper.broker import AlpacaPaperAPI, PaperBrokerError
from atlas.paper.engine import PaperEngine
from atlas.paper.journal import PaperJournal, _directory
from atlas.paper.models import PaperError, amount, decision, sha256
from atlas.paper.preflight import collect_preflight
from atlas.paper.risk import PaperRiskPolicy
from atlas.paper.simulation import SyntheticAlpaca


class PaperCLIError(ValueError):
    """A fixed public error code, without supplied text or private paths."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PaperCLIError("paper_cli_arguments_invalid")


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 32 for char in value)):
        raise PaperCLIError("paper_cli_path_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise PaperCLIError("paper_cli_path_invalid")
    return path


def _paper_key_path(value):
    path = _path(value)
    # The earlier halted pilot and collector may share these credentials. A
    # separate equity experiment must not accidentally reuse that selection.
    if path.is_relative_to(Path.home() / ".config/atlas/alpaca-paper"):
        raise PaperCLIError("paper_cli_legacy_pilot_credentials_protected")
    return path


def _private_json(value, *, maximum=65536):
    path = _path(value)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or not 0 < info.st_size <= maximum):
                raise PaperCLIError("paper_cli_private_input_required")
            raw = handle.read(maximum + 1)
            if len(raw) != info.st_size or len(raw) > maximum:
                raise PaperCLIError("paper_cli_input_changed_or_oversized")
    except OSError:
        raise PaperCLIError("paper_cli_input_unavailable") from None

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise PaperCLIError("paper_cli_json_invalid")
            result[key] = item
        return result

    def nonfinite(_):
        raise PaperCLIError("paper_cli_json_invalid")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)
    except (UnicodeError, ValueError, RecursionError):
        raise PaperCLIError("paper_cli_json_invalid") from None


def load_config(path):
    """Read one private config without opening credentials, creating state or contacting a broker."""
    config = _private_json(path)
    required = {"schema_version", "mode", "state_dir", "credentials_file", "account_fingerprint",
                "strategy_sha256", "allocated_principal", "risk"}
    if (type(config) is not dict or set(config) not in (required, required | {"paper_execution_enabled"})
            or type(config["schema_version"]) is not int or config["schema_version"] != 1
            or config["mode"] != "PAPER" or type(config.get("paper_execution_enabled", False)) is not bool
            or type(config["risk"]) is not dict
            or set(config["risk"]) - {field.name for field in fields(PaperRiskPolicy)}):
        raise PaperCLIError("paper_cli_config_schema_invalid")
    try:
        policy = PaperRiskPolicy(**config["risk"])
        principal = amount(config["allocated_principal"], positive=True)
        if not principal <= policy.allocated_principal_limit <= 100:
            raise ValueError
        binding = {"mode": "PAPER", "account_fingerprint": sha256(config["account_fingerprint"]),
                   "strategy_sha256": sha256(config["strategy_sha256"]),
                   "allocated_principal": str(principal), "policy_sha256": policy.fingerprint()}
    except (TypeError, ValueError, ArithmeticError):
        raise PaperCLIError("paper_cli_policy_or_binding_invalid") from None
    state, credentials = _path(config["state_dir"]), _paper_key_path(config["credentials_file"])
    if credentials == _path(path) or credentials == state or credentials.is_relative_to(state) or _path(path).is_relative_to(state):
        raise PaperCLIError("paper_cli_state_and_inputs_must_be_separate")
    try:
        _directory(state, False)  # Reuse the journal's collector/pilot/repository exclusions.
    except PaperError as exc:
        if str(exc) != "paper_state_missing_explicit_initialization_required" or not state.parent.is_dir():
            raise PaperCLIError("paper_cli_state_directory_invalid") from None
    return {"state_dir": state, "credentials_file": credentials, "policy": policy, "binding": binding,
            "paper_execution_enabled": config.get("paper_execution_enabled", False), "config_sha256": digest(config)}


def config_check(config):
    return {"ok": True, "command": "config-check", "mode": "PAPER",
            "config_sha256": config["config_sha256"], "binding": config["binding"],
            "risk": config["policy"].public(), "paper_execution_enabled": config["paper_execution_enabled"],
            "network_used": False, "credential_contents_read": False, "broker_readiness_verified": False,
            "live_enabled": False}


def _execute(command, config, *, reviewed=None, client_id=None):
    if command in {"submit", "cancel", "replace"} and not config["paper_execution_enabled"]:
        raise PaperCLIError("paper_cli_execution_disabled")
    path = config["state_dir"]
    create = command == "initialize" and (not path.exists() or not any(path.iterdir()))
    # Construction is lazy: status/halt never request an account or load a key.
    api = AlpacaPaperAPI(credentials_path=str(config["credentials_file"]),
                         paper_execution_enabled=config["paper_execution_enabled"])
    with PaperJournal(path, config["binding"], create=create, now=time.time()) as journal:
        engine = PaperEngine(api, journal, config["policy"])
        if command == "initialize":
            return engine.initialize()
        if command == "status":
            return engine.status()
        if command == "halt":
            return engine.halt()
        if command == "reconcile":
            return engine.reconcile()
        if command == "submit":
            return engine.submit(reviewed)
        if command == "cancel":
            return engine.cancel(client_id)
        if command == "replace":
            return engine.replace(client_id, reviewed)
    raise PaperCLIError("paper_cli_command_invalid")


def demo():
    """An ephemeral TEST fill/cancel/restart exercise, with no persistent SIM route."""
    sim = SyntheticAlpaca()
    policy = PaperRiskPolicy(allowed_strategy_ids=("synthetic-demo",))
    api = AlpacaPaperAPI(transport=sim.request, clock=sim.clock)
    binding = {"mode": "SIMULATION", "account_fingerprint": api.account()["fingerprint"],
               "strategy_sha256": digest({"strategy": "synthetic-demo-only"}),
               "policy_sha256": policy.fingerprint(), "allocated_principal": "100"}
    with tempfile.TemporaryDirectory(prefix="atlas-paper-cli-demo-") as temporary:
        path = Path(temporary).resolve() / "state"
        with PaperJournal(path, binding, create=True, now=sim.clock()) as journal:
            engine = PaperEngine(api, journal, policy, clock=sim.clock)
            engine.initialize()
            reviewed = {"signal_id": "synthetic-demo-entry", "strategy_id": "synthetic-demo",
                        "strategy_sha256": binding["strategy_sha256"], "data_sha256": digest({"data": "invented"}),
                        "symbol": "SPY", "side": "buy", "quantity": ".5", "limit_price": "10",
                        "signal_time": sim.clock(), "expires_at": sim.clock() + 120}
            first = engine.submit(reviewed)
            if not first.get("new_submission") or first["order"]["state"] != "new":
                raise PaperCLIError("paper_cli_demo_submission_failed")
            cid = first["order"]["client_order_id"]
            sim.fill(cid, ".2", "9.99")
            if not engine.reconcile()["reconciled"]:
                raise PaperCLIError("paper_cli_demo_reconciliation_failed")
            engine.cancel(cid)
            sim.fill(cid, ".1", "9.99")  # An explicit fill races the cancel acknowledgement.
            sim.finish_cancel(cid)
            if not engine.reconcile()["reconciled"]:
                raise PaperCLIError("paper_cli_demo_reconciliation_failed")
            saved = json.loads(json.dumps(sim.export_state()))
        restored = SyntheticAlpaca.from_state(saved)
        restored.advance(1)
        api = AlpacaPaperAPI(transport=restored.request, clock=restored.clock)
        with PaperJournal(path, binding, now=restored.clock()) as journal:
            engine = PaperEngine(api, journal, policy, clock=restored.clock)
            final = engine.reconcile()
            if not final["reconciled"] or final["orders"][0]["state"] != "canceled":
                raise PaperCLIError("paper_cli_demo_restart_failed")
            result = {"ok": True, "command": "demo", "mode": "SIMULATION", "provenance": "TEST",
                      "synthetic_order_count": len(final["orders"]), "synthetic_fill_count": final["fill_count"],
                      "final_order_state": final["orders"][0]["state"], "filled_quantity": final["orders"][0]["filled_qty"],
                      "restart_reconciliation_passed": True, "actual_orders_submitted": 0,
                      "live_enabled": False, "qualification_established": False}
    return {**result, "temporary_state_removed": True}


def _operation_incomplete(command, result):
    if command == "reconcile":
        return result.get("reconciled") is not True
    if command in {"submit", "replace", "cancel"}:
        row = result.get("order", {})
        return (result.get("risk", {}).get("passed") is False
                or row.get("state") in {"unknown", "http_rejected", "not_submitted", "rejected"}
                or (row.get("cancel") or {}).get("status") in {"unknown", "http_rejected"}
                or result.get("replacement_stage") == "awaiting_terminal_reconciliation")
    return False


def main(argv=None):
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    commands.add_parser("demo", help="temporary synthetic fill/cancel/restart; no account access")
    preflight = commands.add_parser("preflight", help="bounded PAPER account/data reads; creates no journal")
    preflight.add_argument("--key-file", required=True)
    preflight.add_argument("--allocated-principal", default="100")
    preflight.add_argument("--symbol", action="append", dest="symbols")
    for name in ("config-check", "initialize", "status", "reconcile", "submit", "cancel", "replace", "halt"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", required=True)
        if name in {"submit", "replace"}:
            sub.add_argument("--decision", required=True)
        if name in {"cancel", "replace"}:
            sub.add_argument("--client-order-id", required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == "demo":
            result = demo()
        elif args.command == "preflight":
            key_path = _paper_key_path(args.key_file)
            principal = amount(args.allocated_principal, positive=True)
            if principal > 100:
                raise PaperCLIError("paper_cli_preflight_allocation_invalid")
            api = AlpacaPaperAPI(credentials_path=str(key_path))
            report = collect_preflight(api, allocated_principal=str(principal),
                symbols=tuple(args.symbols) if args.symbols is not None else ("SPY", "QQQ", "GLD", "USO"))
            result = {"ok": not report["read_errors"], "command": "preflight", "result": report,
                      "network_command": True, "journal_created": False, "live_enabled": False}
        else:
            config = load_config(args.config)
            if args.command == "config-check":
                result = config_check(config)
            else:
                reviewed = decision(_private_json(args.decision, maximum=16384)) if hasattr(args, "decision") else None
                client_id = getattr(args, "client_order_id", None)
                if client_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", client_id):
                    raise PaperCLIError("paper_cli_client_order_id_invalid")
                output = _execute(args.command, config, reviewed=reviewed, client_id=client_id)
                incomplete = _operation_incomplete(args.command, output)
                result = {"ok": not incomplete, "command": args.command, "result": output,
                          "network_command": args.command not in {"status", "halt"}, "live_enabled": False}
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if result["ok"] else 3
    except (PaperCLIError, PaperError, PaperBrokerError) as exc:
        code = exc.code if isinstance(exc, PaperBrokerError) else str(exc)
        if not re.fullmatch(r"paper_[a-z0-9_]{1,100}", code):
            code = "paper_cli_operation_failed"
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, sqlite3.Error, RecursionError):
        code = "paper_cli_operation_failed"
    print(json.dumps({"ok": False, "error": code, "live_enabled": False}, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
