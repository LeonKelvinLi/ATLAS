"""Explicit entrypoints. Default invocation only prints help; it starts nothing."""

import argparse
from contextlib import contextmanager
from decimal import Decimal
import json
import os
import signal
import sqlite3
import sys
import time
from urllib.parse import quote

from atlas.approval import issue_approval
from atlas.config import ConfigError, build_service, external_path, load_config, read_secret_file
from atlas.domain import Quote, json_value
from atlas.ledger import Ledger, digest


@contextmanager
def stdio_shutdown_signals(service):
    """Wake broker waits before the stdio event loop unwinds on host shutdown."""
    previous = {}
    def stop(_signum, _frame):
        service.stop.set()
        raise KeyboardInterrupt
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def make_http_server(service, app, *, host, port):
    """Construct only; the caller must explicitly invoke run on the intended host."""
    import uvicorn
    class AtlasHTTPServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            service.stop.set()
            super().handle_exit(sig, frame)
    config = uvicorn.Config(app, host=host, port=port, access_log=False, timeout_graceful_shutdown=20)
    return AtlasHTTPServer(config)


def demo():
    """Generated fixtures, an in-memory ledger, and an in-memory paper broker."""
    from atlas.brokers.paper import PaperBroker
    from atlas.risk import RiskPolicy
    from atlas.service import AtlasService
    now = 1788969600
    bars = [{"timestamp": now - (119 - i) * 60, "open": 90 + i / 10,
             "close": 90.05 + i / 10} for i in range(120)]
    market = Quote("DEMO", "equity", Decimal("101.94"), Decimal("101.95"), now,
                   True, Decimal("100"), "generated_demo", timestamp_basis="synthetic",
                   quantity_increment=Decimal("0.001"))
    broker = PaperBroker({"DEMO": market}, cash=Decimal("10000"), fee_bps=Decimal("5"), clock=lambda: now)
    ledger = Ledger(":memory:", "paper", clock=lambda: now)
    try:
        service = AtlasService(broker, ledger, RiskPolicy(mode="paper", expected_account_id="paper-demo",
                               allowed_symbols=frozenset({"DEMO"}), max_order_notional=Decimal("250")),
                               datasets={"DEMO": {"asset_class": "equity", "bars": bars,
                                          "evidence_kind": "synthetic", "source": "generated_demo"}}, clock=lambda: now)
        analysis = service.analyze("DEMO")
        signal = next(row for row in analysis["signals"] if row["strategy_id"] == "momentum")
        proposal = service.create_trade_proposal(signal["signal_id"], "1", "generated-demo-proposal")
        execution = service.execute_trade_proposal(proposal["proposal"]["id"])
        return {"evidence_kind": "synthetic", "broker": "in_memory_paper", "real_orders": 0,
                "files_created": False, "signal": signal, "proposal": proposal,
                "simulated_execution": execution, "account": service.account_summary()}
    finally:
        ledger.close()
        broker.close()


def _read_approval_proposal(journal, proposal_id):
    path = external_path(journal)
    if not path.is_file():
        raise ConfigError("approval requires an existing external execution journal")
    connection = None
    try:
        connection = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ConfigError("approval journal integrity failed")
        scope = connection.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()
        if scope is None or scope[0] != "live":
            raise ConfigError("operator approval requires the shared live journal")
        row = connection.execute("SELECT payload,digest FROM proposals WHERE scope='live' AND id=?", (proposal_id,)).fetchone()
        if row is None:
            raise ConfigError("proposal not found")
        proposal = json.loads(row[0])
        if not isinstance(proposal, dict) or proposal.get("id") != proposal_id or digest(proposal) != row[1]:
            raise ConfigError("proposal immutable hash verification failed")
        if proposal.get("mode") != "live-assisted":
            raise ConfigError("only live-assisted proposals accept human receipts")
        if proposal.get("kind") == "order" and proposal.get("risk", {}).get("passed") is not True:
            raise ConfigError("a risk-rejected order cannot be approved")
        return proposal
    except (sqlite3.DatabaseError, ValueError, TypeError):
        raise ConfigError("approval proposal cannot be read safely") from None
    finally:
        if connection is not None:
            connection.close()


def issue_operator_receipt(*, journal, proposal_id, key_file, output_file, proposal_hash=None,
                           now=None, input_fn=input, output_fn=print):
    proposal = _read_approval_proposal(journal, proposal_id)
    expected_hash = digest(proposal)
    output_fn(json.dumps({"immutable_proposal": proposal, "proposal_hash": expected_hash}, indent=2))
    supplied = proposal_hash
    if supplied is None:
        supplied = input_fn("Type the complete proposal hash shown above to approve this exact proposal: ").strip()
    if supplied != expected_hash:
        raise ConfigError("exact proposal hash was not confirmed")
    key = read_secret_file(key_file)
    receipt = issue_approval(proposal, key, now=int(time.time()) if now is None else now)
    path = external_path(output_file)
    if not path.parent.is_dir():
        raise ConfigError("receipt destination directory must already exist")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # A partial receipt must not look complete or be silently reused.
            path.unlink(missing_ok=True)
            raise
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise ConfigError("receipt could not be written securely") from None
    return {"receipt_file": str(path), "proposal_hash": expected_hash, "trade_submitted": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="ATLAS offline research and explicitly configured server interface")
    commands = parser.add_subparsers(dest="command")
    from atlas.research.interface import add_research_parser, research_command
    add_research_parser(commands)
    commands.add_parser("demo", help="generated-data example using in-memory paper state only")
    inspect = commands.add_parser("config-check", help="validate external configuration without creating state or starting services")
    inspect.add_argument("--config", required=True)
    server = commands.add_parser("mcp", help="start only when intentionally invoked on the intended host")
    server.add_argument("--config", required=True)
    server.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    approve = commands.add_parser("approve", help="operator-only exact-proposal approval; never submits an order")
    approve.add_argument("--journal", required=True)
    approve.add_argument("--proposal-id", required=True)
    approve.add_argument("--key-file", required=True)
    approve.add_argument("--output", required=True)
    approve.add_argument("--proposal-hash", help="complete immutable hash; no generic yes/approve flag")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "demo":
            result = demo()
        elif args.command == "research":
            result = research_command(args)
        elif args.command == "approve":
            if args.proposal_hash is None and not sys.stdin.isatty():
                raise ConfigError("noninteractive approval requires the explicit full proposal hash")
            result = issue_operator_receipt(journal=args.journal, proposal_id=args.proposal_id,
                                           key_file=args.key_file, output_file=args.output,
                                           proposal_hash=args.proposal_hash)
        else:
            config = load_config(args.config)
            if args.command == "config-check":
                result = {"valid": True, "mode": config.mode, "broker": config.broker,
                          "journal_scope": config.scope, "services_started": False,
                          "real_broker_factory_available": False}
            else:
                from atlas.mcp_server import create_http_app, create_mcp
                token = None
                if args.transport == "http":
                    if config.http_bearer_file is None:
                        raise ConfigError("HTTP requires ATLAS_HTTP_BEARER_FILE")
                    token = read_secret_file(config.http_bearer_file).rstrip(b"\r\n")
                    if len(token) < 32:
                        raise ConfigError("HTTP bearer secret is too short")
                service = build_service(config)
                try:
                    if args.transport == "stdio":
                        with stdio_shutdown_signals(service):
                            create_mcp(service, execution_tools=config.execution_tools).run(transport="stdio")
                    else:
                        app = create_http_app(service, bearer_token=token, execution_tools=config.execution_tools,
                                              host=config.host, port=config.port)
                        make_http_server(service, app, host=config.host, port=config.port).run()
                finally:
                    service.stop.set()
                    service.ledger.close()
                    research_store = getattr(service, "research_store", None)
                    if research_store is not None:
                        research_store.close()
                    close = getattr(service.broker, "close", None)
                    if close:
                        close()
                return 0
        print(json.dumps(json_value(result), indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Never echo raw configuration, environment values, tokens, or broker exceptions.
        print(json.dumps({"error": type(exc).__name__, "operation_completed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
