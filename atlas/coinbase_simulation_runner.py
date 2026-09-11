"""Finite public-data paper runs. No account credentials or broker write transport."""

import hashlib
import json
import os
from pathlib import Path
import signal
import time

from atlas.brokers.coinbase_public import CoinbasePublicClient, CoinbasePublicError
from atlas.coinbase_simulation import SimulationError, SimulationLedger, validate_config
from atlas.config import _json_file, external_path
from atlas.domain import decimal, financial_context

FEE_MAX_AGE_SECONDS = 3600


@financial_context
def fee_evidence(path, config, *, now):
    report = _json_file(path, max_bytes=1_000_000)
    if (not isinstance(report, dict)
            or report.get("report_kind") != "coinbase_advanced_view_only_preflight"
            or report.get("account_read_verified") is not True
            or report.get("evidence_kind") != "verified_tls_coinbase_rest"
            or report.get("origin") != "https://api.coinbase.com"
            or report.get("accounts_complete") is not True
            or report.get("execution_enabled") is not False
            or report.get("orders_submitted") != 0
            or report.get("account_mutations") != 0):
        raise SimulationError("simulation_verified_fee_report_required")
    permissions = report.get("permissions", {})
    if any(permissions.get(key) is not value for key, value in
           (("can_view", True), ("can_trade", False), ("can_transfer", False))):
        raise SimulationError("simulation_view_only_fee_report_required")
    observed = report.get("observed_at")
    if type(observed) is not int or not 0 <= now - observed <= FEE_MAX_AGE_SECONDS:
        raise SimulationError("simulation_fee_report_expired_or_future")
    fees = report.get("fees", {})
    if (fees.get("product_type") != "SPOT" or fees.get("product_venue") != "CBE"
            or decimal(fees.get("taker_fee_rate"), nonnegative=True) * 10000
            != decimal(config["taker_fee_bps"])):
        raise SimulationError("simulation_fee_rate_mismatch")
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"report_sha256": hashlib.sha256(encoded).hexdigest(), "observed_at": observed,
            "maximum_age_seconds": FEE_MAX_AGE_SECONDS,
            "taker_fee_bps": config["taker_fee_bps"],
            "basis": "dated_authenticated_fee_tier_applied_as_fixed_simulation_assumption",
            "actual_order_fee_known": False, "additional_commission_and_tax_verified": False}


def new_report(path):
    original = Path(path).expanduser()
    destination = external_path(original)
    if original.is_symlink() or destination.exists() or not destination.parent.is_dir():
        raise SimulationError("simulation_report_destination_must_be_new")
    return os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL
                   | getattr(os, "O_NOFOLLOW", 0), 0o600)


def _write_report(fd, report):
    encoded = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(encoded)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short report write")
        view = view[written:]
    os.fsync(fd)


def run(config, journal, *, samples, fee_report, output, client=None,
        clock=time.time, monotonic=time.monotonic, sleep=time.sleep,
        ledger_factory=SimulationLedger, progress=None, install_signals=True):
    """One foreground session; stale fees/data or a stop halt new simulated entries."""
    config = validate_config(config)
    if type(samples) is not int or not 1 <= samples <= 720:
        raise SimulationError("simulation_samples_out_of_range")
    evidence = fee_evidence(fee_report, config, now=clock())
    journal_path = external_path(journal)
    if Path(journal).expanduser().is_symlink():
        raise SimulationError("simulation_journal_symlink_refused")
    stop_file = journal_path.parent / "STOP"
    fd = new_report(output)
    started = clock()
    report = {"schema": 1, "mode": "coinbase_public_data_local_simulation",
              "run_state": "started", "started_at": started, "finished_at": None,
              "requested_samples": samples, "completed_samples": 0,
              "config": config, "fee_evidence": evidence,
              "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
              "execution_enabled": False, "broker_orders_submitted": 0, "account_mutations": 0,
              "cash_baseline_equity_usd": "100", "strategy_profitability_established": False,
              "limitations": ["Fills are local displayed-depth assumptions; no broker accepted an order.",
                              "A bounded engineering run is insufficient evidence of strategy profitability.",
                              "Fees are a dated tier; actual order fees, extra commissions and tax remain unknown.",
                              "No passive maker fills or queue priority are modeled.",
                              "No exposure-matched buy-and-hold comparison or statistical significance is established."]}
    signals, interrupted = {}, []
    ledger = None
    try:
        _write_report(fd, report)  # A crash leaves an explicit unfinished receipt.
        if install_signals:
            def request_stop(signum, _frame):
                interrupted.append(signum)
            for signum in (signal.SIGINT, signal.SIGTERM):
                signals[signum] = signal.signal(signum, request_stop)
        client = CoinbasePublicClient() if client is None else client
        with ledger_factory(journal_path, config) as ledger:
            report["initial_status"] = ledger.status()
            for index in range(samples):
                if interrupted or stop_file.exists() or stop_file.is_symlink():
                    ledger.stop("process_signal" if interrupted else "operator_stop_file")
                    report["run_state"] = "stopped"
                    break
                if not 0 <= clock() - evidence["observed_at"] <= FEE_MAX_AGE_SECONDS:
                    raise SimulationError("simulation_fee_report_expired_or_future")
                snapshot = client.snapshot()
                if interrupted or stop_file.exists() or stop_file.is_symlink():
                    ledger.stop("process_signal" if interrupted else "operator_stop_file")
                    report["run_state"] = "stopped"
                    break
                if client.transport_provenance != "verified_tls":
                    raise SimulationError("simulation_public_tls_required")
                if not 0 <= clock() - evidence["observed_at"] <= FEE_MAX_AGE_SECONDS:
                    raise SimulationError("simulation_fee_report_expired_or_future")
                ledger.step(snapshot)
                report["completed_samples"] += 1
                report["latest_status"] = ledger.status()
                if progress is not None:
                    progress({"completed_samples": report["completed_samples"],
                              "requested_samples": samples, "status": report["latest_status"]})
                if index + 1 < samples:
                    deadline = monotonic() + config["interval_seconds"]
                    # Small interruptible sleeps permit a bounded process stop.
                    while monotonic() < deadline and not interrupted:
                        if stop_file.exists() or stop_file.is_symlink():
                            break
                        sleep(min(0.25, deadline - monotonic()))
            else:
                report["run_state"] = "completed"
            ledger.check_integrity()
            report["final_status"] = ledger.status()
    except Exception as exc:
        report["run_state"] = "failed"
        report["error"] = (str(exc) if isinstance(exc, (SimulationError, CoinbasePublicError))
                           else "simulation_run_failed")
        # The context has closed the writer. Reopen only to persist the halt;
        # never repair/reseed a corrupt or inaccessible ledger automatically.
        if ledger is not None:
            try:
                if not journal_path.is_file() or journal_path.stat().st_size == 0:
                    raise SimulationError("simulation_existing_journal_required")
                with ledger_factory(journal_path, config, create=False) as recovery:
                    recovery.stop("run_failure")
                    report["final_status"] = recovery.status()
            except Exception:
                report["halt_persistence_verified"] = False
    finally:
        report["finished_at"] = clock()
        for signum, handler in signals.items():
            signal.signal(signum, handler)
        try:
            _write_report(fd, report)
        finally:
            os.close(fd)
    return report
