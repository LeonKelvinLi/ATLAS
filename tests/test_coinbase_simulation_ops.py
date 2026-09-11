"""Synthetic process and SQLite recovery checks; no network or broker access."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import select
import signal
import socket
import sqlite3
import subprocess
import sys

import pytest

from atlas.brokers.coinbase_public import FLAGS, ORIGIN
from atlas.coinbase_simulation import SimulationError, SimulationLedger


NOW = 1_800_000_000
CONFIG = {
    "schema": 1, "product_id": "BTC-USD", "allocation_usd": "100",
    "order_budget_usd": "10", "taker_fee_bps": "120", "slippage_bps": "5",
    "max_loss_usd": "5", "daily_loss_usd": "5", "short_window": 2,
    "long_window": 3, "entry_threshold_bps": "25", "interval_seconds": 5,
    "strategy": "round_trip_smoke_v1",
}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Recovery tests cannot contact a network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def snapshot(at=NOW, *, depth_levels=1):
    product = {
        "product_id": "BTC-USD", "product_type": "SPOT",
        "base_currency_id": "BTC", "quote_currency_id": "USD", "status": "online",
        "base_increment": "0.00000001", "quote_increment": "0.01",
        "price_increment": "0.01", "base_min_size": "0.00000001",
        "base_max_size": "3400", "quote_min_size": "1", "quote_max_size": "150000000",
        **{name: False for name in FLAGS},
    }
    return {
        "public_only": True, "source": ORIGIN,
        "transport_provenance": "injected_test_transport",
        "observed_at": str(at), "product_observed_at": str(at), "product": product,
        "pricebook": {
            "product_id": "BTC-USD",
            "time": datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z"),
            "bids": [{"price": str(77000 - i), "size": "1"} for i in range(depth_levels)],
            "asks": [{"price": str(77001 + i), "size": "1"} for i in range(depth_levels)],
        },
    }


@pytest.fixture
def journal_path(tmp_path):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    return folder / "simulation.sqlite3"


# The child blocks only after step() has returned from its durable COMMIT.
# SIGKILL skips Python context-manager cleanup and releases its OS-held lock.
_CHILD_WRITER = """
import json, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('Recovery child cannot contact a network')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.getaddrinfo = forbidden
from atlas.coinbase_simulation import SimulationLedger
payload = json.loads(sys.stdin.readline())
with SimulationLedger(payload['path'], payload['config'], clock=lambda: payload['now']) as ledger:
    ledger.step(payload['snapshot'])
    print(json.dumps(ledger.status()), flush=True)
    sys.stdin.read(1)
"""


@contextmanager
def committed_child(path):
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _CHILD_WRITER],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        payload = {"path": str(path), "config": CONFIG, "snapshot": snapshot(), "now": NOW}
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, "Child did not confirm a committed synthetic observation within five seconds"
        line = process.stdout.readline()
        assert line, "Child exited before confirming its commit"
        yield process, json.loads(line)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_sigkill_after_commit_preserves_cash_position_fees_and_duplicate_identity(journal_path):
    with committed_child(journal_path) as (child, before):
        assert Decimal(before["synthetic_cash_usd"]) < 100
        assert Decimal(before["holdings_btc"]) > 0
        assert Decimal(before["cumulative_fees_usd"]) > 0
        assert before["simulated_orders"] == 1
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL

    with SimulationLedger(journal_path, CONFIG, clock=lambda: NOW + 5) as recovered:
        assert recovered.status() == before
        replay = recovered.step(snapshot())
        assert replay["idempotent_replay"] is True
        assert recovered.status() == before
        assert recovered.check_integrity()["audit_sha256"] == before["audit_sha256"]
        recovered.step(snapshot(NOW + 5))
        after = recovered.status()
        assert after["simulated_orders"] == 2
        assert Decimal(after["holdings_btc"]) == 0
        assert Decimal(after["synthetic_cash_usd"]) < 100
        assert after["broker_orders_submitted"] == 0


def test_another_process_cannot_open_same_writer_or_change_its_committed_state(journal_path):
    with committed_child(journal_path) as (child, before):
        with pytest.raises(SimulationError, match="simulation_writer_already_active"):
            SimulationLedger(journal_path, CONFIG, clock=lambda: NOW)
        assert child.poll() is None
        child.kill()
        child.wait(timeout=5)

    with SimulationLedger(journal_path, CONFIG, clock=lambda: NOW) as recovered:
        assert recovered.status() == before
        assert recovered.check_integrity()["event_count"] == before["event_count"]


def test_sqlite_full_rolls_back_observation_and_reopens_without_new_capital(journal_path):
    with SimulationLedger(journal_path, CONFIG, clock=lambda: NOW + 5) as ledger:
        ledger.step(snapshot())
        before = ledger.status()
        connection = ledger._connection
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        assert connection.execute(f"PRAGMA max_page_count={pages}").fetchone()[0] == pages

        # A large valid book requires additional event-storage pages. This is
        # SQLite's real SQLITE_FULL failure, not a mocked ledger exception.
        with pytest.raises(SimulationError, match="simulation_atomic_commit_failed") as failure:
            ledger.step(snapshot(NOW + 5, depth_levels=100))
        assert isinstance(failure.value.__context__, sqlite3.OperationalError)
        assert failure.value.__context__.sqlite_errorcode == sqlite3.SQLITE_FULL
        # A failed commit invalidates the writer even when SQLite reports FULL;
        # only a new connection and replay can establish the durable outcome.
        with pytest.raises(SimulationError, match="simulation_journal_closed"):
            ledger.status()
        with pytest.raises(SimulationError, match="simulation_journal_closed"):
            ledger.step(snapshot(NOW + 5))

    with SimulationLedger(journal_path, CONFIG, clock=lambda: NOW + 5) as recovered:
        assert recovered.status() == before
        assert Decimal(recovered.status()["synthetic_cash_usd"]) < 100
        assert recovered.check_integrity() == {
            "ok": True, "event_count": before["event_count"],
            "audit_sha256": before["audit_sha256"],
        }
        assert recovered.step(snapshot())["idempotent_replay"] is True
        assert recovered.status() == before


def test_backup_restores_exact_accounting_config_stop_and_audit_snapshot(journal_path):
    backup_path = journal_path.with_name("verified-backup.sqlite3")
    with SimulationLedger(journal_path, CONFIG, clock=lambda: NOW + 5) as source:
        source.step(snapshot())
        source.stop("operator_stop")
        checkpoint = source.status()
        receipt = source.backup(backup_path)
        assert receipt["audit_sha256"] == checkpoint["audit_sha256"]
        assert receipt["event_count"] == checkpoint["event_count"]
        assert backup_path.stat().st_mode & 0o777 == 0o600
        source.step(snapshot(NOW + 5))
        assert source.status()["audit_sha256"] != checkpoint["audit_sha256"]

    with pytest.raises(SimulationError, match="simulation_journal_config_mismatch"):
        SimulationLedger(backup_path, {**CONFIG, "order_budget_usd": "20"}, clock=lambda: NOW)
    with SimulationLedger(backup_path, CONFIG, clock=lambda: NOW) as restored:
        assert restored.status() == checkpoint
        assert restored.status()["halt_reasons"] == ["operator_stop"]
        assert restored.check_integrity() == {
            "ok": True, "event_count": checkpoint["event_count"],
            "audit_sha256": checkpoint["audit_sha256"],
        }
        assert Decimal(restored.status()["holdings_btc"]) > 0
        assert Decimal(restored.status()["synthetic_cash_usd"]) < 100
