"""Bounded runner controls: no network retries, fee provenance, stop and recovery."""

import json
from pathlib import Path

import pytest

from atlas.brokers.coinbase_public import CoinbasePublicError
from atlas.coinbase_simulation import SimulationError
from atlas.coinbase_simulation_runner import fee_evidence, run


CONFIG = {"schema": 1, "product_id": "BTC-USD", "allocation_usd": "100",
          "order_budget_usd": "10", "taker_fee_bps": "120", "slippage_bps": "5",
          "max_loss_usd": "5", "daily_loss_usd": "5", "short_window": 5,
          "long_window": 20, "entry_threshold_bps": "25", "interval_seconds": 5,
          "strategy": "sma_reference_v1"}


@pytest.fixture
def fee_report(tmp_path):
    report = {"report_kind": "coinbase_advanced_view_only_preflight", "account_read_verified": True,
              "evidence_kind": "verified_tls_coinbase_rest", "origin": "https://api.coinbase.com",
              "accounts_complete": True, "execution_enabled": False, "orders_submitted": 0,
              "account_mutations": 0, "permissions": {"can_view": True, "can_trade": False,
              "can_transfer": False}, "observed_at": 1_800_000_000,
              "fees": {"taker_fee_rate": "0.012", "product_type": "SPOT", "product_venue": "CBE"}}
    path = tmp_path / "fees.json"
    path.write_text(json.dumps(report))
    return path


class Clock:
    def __init__(self):
        self.value = 1_800_000_001.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Client:
    transport_provenance = "verified_tls"

    def __init__(self, failure=False):
        self.calls = 0
        self.failure = failure

    def snapshot(self):
        self.calls += 1
        if self.failure:
            raise CoinbasePublicError("coinbase_public_http_503")
        return {"sample": self.calls}


class Ledger:
    """Only a test double for runner sequencing, not financial accounting."""
    states = {}

    def __init__(self, path, config, *, create=True):
        if create:
            Path(path).write_text("test-ledger")
        self.state = self.states.setdefault(str(path), {"samples": 0, "halted": False})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def status(self):
        return dict(self.state)

    def step(self, _):
        self.state["samples"] += 1

    def stop(self, reason):
        self.state.update(halted=True, reason=reason)

    def check_integrity(self):
        pass


def execute(tmp_path, fee_report, **kwargs):
    clock = kwargs.pop("clock", Clock())
    return run(CONFIG, tmp_path / "paper.db", samples=kwargs.pop("samples", 3),
               fee_report=fee_report, output=kwargs.pop("output", tmp_path / "run.json"),
               client=kwargs.pop("client", Client()), clock=clock, monotonic=clock,
               sleep=clock.sleep, ledger_factory=Ledger, install_signals=False, **kwargs)


def test_bounded_run_and_restart_keep_state(tmp_path, fee_report):
    report = execute(tmp_path, fee_report)
    assert report["run_state"] == "completed"
    assert report["final_status"]["samples"] == 3
    assert report["finished_at"] - report["started_at"] == 10
    second = execute(tmp_path, fee_report, output=tmp_path / "second.json")
    assert second["initial_status"]["samples"] == 3
    assert second["final_status"]["samples"] == 6
    assert second["broker_orders_submitted"] == 0
    assert not second["strategy_profitability_established"]


def test_existing_report_refused_before_any_get(tmp_path, fee_report):
    execute(tmp_path, fee_report)
    client = Client()
    with pytest.raises(SimulationError, match="must_be_new"):
        execute(tmp_path, fee_report, client=client)
    assert client.calls == 0


def test_data_failure_halts_persistently_without_retry(tmp_path, fee_report):
    client = Client(failure=True)
    report = execute(tmp_path, fee_report, client=client)
    assert client.calls == 1
    assert report["run_state"] == "failed"
    assert report["error"] == "coinbase_public_http_503"
    assert report["final_status"]["halted"]
    assert report["final_status"]["reason"] == "run_failure"
    assert json.loads((tmp_path / "run.json").read_text()) == report


def test_stop_flag_prevents_market_read(tmp_path, fee_report):
    (tmp_path / "STOP").touch()
    client = Client()
    report = execute(tmp_path, fee_report, client=client)
    assert client.calls == 0
    assert report["run_state"] == "stopped"
    assert report["final_status"]["halted"]


def test_stop_during_fetch_prevents_step(tmp_path, fee_report):
    class StopClient(Client):
        def snapshot(self):
            (tmp_path / "STOP").touch()
            return super().snapshot()
    report = execute(tmp_path, fee_report, client=StopClient())
    assert report["completed_samples"] == 0
    assert report["final_status"]["halted"]


def test_expired_fees_prevent_reads_and_state(tmp_path, fee_report):
    clock = Clock()
    clock.value += 3601
    client = Client()
    with pytest.raises(SimulationError, match="expired"):
        execute(tmp_path, fee_report, clock=clock, client=client)
    assert client.calls == 0
    assert not (tmp_path / "run.json").exists()


@pytest.mark.parametrize("patch", [
    {"account_read_verified": False}, {"evidence_kind": "injected_test_transport"},
    {"origin": "https://example.com"}, {"accounts_complete": False},
    {"permissions": {"can_view": True, "can_trade": True, "can_transfer": False}},
    {"observed_at": True}, {"observed_at": 1_900_000_000},
    {"fees": {"taker_fee_rate": "0.006", "product_type": "SPOT", "product_venue": "CBE"}},
])
def test_fee_source_cannot_silently_change(fee_report, patch):
    source = json.loads(fee_report.read_text())
    source.update(patch)
    fee_report.write_text(json.dumps(source))
    with pytest.raises(SimulationError):
        fee_evidence(fee_report, CONFIG, now=1_800_000_001)


def test_stop_on_fee_expiry_mid_run(tmp_path, fee_report):
    clock = Clock()
    clock.value += 3598
    client = Client()
    report = execute(tmp_path, fee_report, clock=clock, client=client)
    assert client.calls == 1
    assert report["run_state"] == "failed"
    assert report["final_status"]["halted"]


def test_fee_expiry_during_get_prevents_step(tmp_path, fee_report):
    clock = Clock()
    clock.value += 3598
    class SlowClient(Client):
        def snapshot(self):
            clock.sleep(2)
            return super().snapshot()
    report = execute(tmp_path, fee_report, clock=clock, client=SlowClient())
    assert report["completed_samples"] == 0
    assert report["error"] == "simulation_fee_report_expired_or_future"


def test_missing_journal_not_recreated_on_failure(tmp_path, fee_report):
    class DeletedClient(Client):
        def snapshot(self):
            (tmp_path / "paper.db").unlink()
            raise CoinbasePublicError("coinbase_public_http_503")
    report = execute(tmp_path, fee_report, client=DeletedClient())
    assert report["run_state"] == "failed"
    assert report["halt_persistence_verified"] is False
    assert not (tmp_path / "paper.db").exists()


@pytest.mark.parametrize("samples", [0, -1, 721, True, 1.5])
def test_invalid_run_bound_rejected(tmp_path, fee_report, samples):
    with pytest.raises(SimulationError, match="samples"):
        execute(tmp_path, fee_report, samples=samples)


def test_untrusted_transport_does_not_claim_live_evidence(tmp_path, fee_report):
    client = Client()
    client.transport_provenance = "injected_test_transport"
    report = execute(tmp_path, fee_report, client=client)
    assert report["completed_samples"] == 0
    assert report["error"] == "simulation_public_tls_required"
