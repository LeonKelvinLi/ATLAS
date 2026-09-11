"""Synthetic prices only: durable Coinbase paper accounting and strategy timing."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import socket
import sqlite3

import pytest

import atlas.coinbase_simulation as simulation
from atlas.brokers.coinbase_public import FLAGS
from atlas.coinbase_simulation import SimulationError, SimulationLedger, validate_config

NOW = 1789078453


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("simulation tests must never access network")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def config(**changes):
    return {"schema": 1, "product_id": "BTC-USD", "allocation_usd": "100", "order_budget_usd": "10",
            "taker_fee_bps": "120", "slippage_bps": "5", "max_loss_usd": "5", "daily_loss_usd": "5",
            "short_window": 5, "long_window": 20, "entry_threshold_bps": "25", "interval_seconds": 5,
            "strategy": "sma_reference_v1", **changes}


def snapshot(at=NOW, *, bid="100", ask="100.01", bids=None, asks=None, provider_at=None):
    stamp = datetime.fromtimestamp(at if provider_at is None else provider_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"product": {"product_id": "BTC-USD", "product_type": "SPOT", "base_currency_id": "BTC", "quote_currency_id": "USD",
                        "status": "online", "base_increment": "0.00000001", "quote_increment": "0.01", "price_increment": "0.01",
                        "base_min_size": "0.00000001", "base_max_size": "3400", "quote_min_size": "1", "quote_max_size": "150000000",
                        **{name: False for name in FLAGS}},
            "pricebook": {"product_id": "BTC-USD", "time": stamp,
                          "bids": bids or [{"price": bid, "size": "10"}], "asks": asks or [{"price": ask, "size": "10"}]},
            "observed_at": str(at), "product_observed_at": str(at), "source": "https://api.coinbase.com",
            "public_only": True, "transport_provenance": "injected_test_transport"}


def open_ledger(tmp_path, settings=None):
    clock = [NOW]
    ledger = SimulationLedger(tmp_path / "simulation.sqlite3", settings or config(), clock=lambda: clock[0])
    return ledger, clock


def advance(ledger, clock, index, **prices):
    clock[0] = NOW + index * 5
    return ledger.step(snapshot(clock[0], **prices))


def assert_account(status):
    cash = Decimal(status["synthetic_cash_usd"])
    basis = Decimal(status["cost_basis_usd"])
    assert cash >= 0 and Decimal(status["holdings_btc"]) >= 0
    assert cash + basis == Decimal(100) + Decimal(status["realized_pnl_usd"])
    assert basis <= 100
    assert status["broker_orders_submitted"] == 0 and not status["execution_enabled"]


def test_initial_100_is_durable_and_cannot_reinitialize_or_change_config(tmp_path):
    ledger, clock = open_ledger(tmp_path)
    assert ledger.status()["synthetic_cash_usd"] == "100"
    before = ledger.status()
    ledger.close()
    with SimulationLedger(tmp_path / "simulation.sqlite3", config(), clock=lambda: NOW) as reopened:
        assert reopened.status() == before
        assert reopened.check_integrity()["event_count"] == 1
    with pytest.raises(SimulationError, match="config_mismatch"):
        SimulationLedger(tmp_path / "simulation.sqlite3", config(order_budget_usd="20"))


def test_roundtrip_smoke_accounts_for_fees_slippage_and_no_second_entry(tmp_path):
    ledger, clock = open_ledger(tmp_path, config(strategy="round_trip_smoke_v1"))
    first = advance(ledger, clock, 0)
    buy = first["step"]["order"]
    assert buy["side"] == "buy" and buy["status"] == "filled"
    assert Decimal(buy["notional_usd"]) + Decimal(buy["fee_usd"]) <= 10
    assert Decimal(buy["fills"][0]["price_usd"]) > Decimal("100.01")
    assert first["smoke_phase"] == "exit" and first["holdings_btc"] != "0"
    second = advance(ledger, clock, 1)
    sell = second["step"]["order"]
    assert sell["side"] == "sell" and sell["status"] == "filled"
    assert Decimal(sell["fills"][0]["price_usd"]) < 100
    assert Decimal(second["holdings_btc"]) == 0 and Decimal(second["cost_basis_usd"]) == 0
    assert Decimal(second["realized_pnl_usd"]) < 0
    assert second["smoke_phase"] == "done" and second["accounting_only_smoke"]
    assert not second["strategy_evidence_eligible"]
    third = advance(ledger, clock, 2)
    assert third["simulated_orders"] == 2 and third["step"]["order"] is None
    assert_account(third)
    ledger.check_integrity()
    ledger.close()


def test_partial_depth_buy_and_sell_preserve_fee_inclusive_cost_basis(tmp_path):
    ledger, clock = open_ledger(tmp_path, config(strategy="round_trip_smoke_v1", slippage_bps="0"))
    first = advance(ledger, clock, 0, asks=[{"price": "100.01", "size": "0.02"}])
    assert first["step"]["order"]["status"] == "partial_canceled"
    assert Decimal(first["holdings_btc"]) == Decimal("0.02")
    basis = Decimal(first["cost_basis_usd"])
    second = advance(ledger, clock, 1, bids=[{"price": "100", "size": "0.01"}])
    assert second["step"]["order"]["status"] == "partial_canceled"
    assert Decimal(second["holdings_btc"]) == Decimal("0.01")
    assert Decimal(second["cost_basis_usd"]) == basis / 2
    assert_account(second)
    third = advance(ledger, clock, 2)
    assert Decimal(third["holdings_btc"]) == 0 and third["smoke_phase"] == "done"
    assert_account(third)
    ledger.check_integrity()
    ledger.close()


def test_buy_depth_price_changes_cannot_overspend_cash_or_100(tmp_path):
    ledger, clock = open_ledger(tmp_path, config(strategy="round_trip_smoke_v1", order_budget_usd="100"))
    result = advance(ledger, clock, 0, asks=[{"price": "100.01", "size": "0.01"}, {"price": "200", "size": "10"}])
    order = result["step"]["order"]
    assert len(order["fills"]) == 2
    assert Decimal(order["notional_usd"]) + Decimal(order["fee_usd"]) <= 100
    assert Decimal(order["buy_limit_worst_case_all_in_usd"]) <= 100
    worst_notional = Decimal(order["filled_quantity_btc"]) * Decimal(order["worst_filled_price_usd"])
    assert worst_notional * Decimal("1.012") <= 100
    assert Decimal(result["synthetic_cash_usd"]) >= 0
    assert order["status"] == "partial_canceled"
    assert_account(result)
    ledger.close()


def test_duplicate_snapshot_is_idempotent_even_after_restart_and_staleness(tmp_path):
    settings = config(strategy="round_trip_smoke_v1")
    ledger, clock = open_ledger(tmp_path, settings)
    first = advance(ledger, clock, 0)
    journal_bytes = (tmp_path / "simulation.sqlite3").read_bytes()
    clock[0] += 1000
    duplicate = ledger.step(snapshot())
    assert duplicate["idempotent_replay"] and duplicate["step"] == first["step"]
    assert duplicate["simulated_orders"] == 1 and duplicate["event_count"] == 2
    assert journal_bytes == (tmp_path / "simulation.sqlite3").read_bytes()
    ledger.close()
    with SimulationLedger(tmp_path / "simulation.sqlite3", settings, clock=lambda: NOW + 1000) as reopened:
        assert reopened.step(snapshot())["idempotent_replay"]


@pytest.mark.parametrize("at,provider_at", [(NOW, NOW), (NOW - 5, NOW - 5), (NOW + 4, NOW + 4), (NOW + 5, NOW)])
def test_nonmonotonic_provider_time_and_early_samples_cannot_trade(tmp_path, at, provider_at):
    ledger, clock = open_ledger(tmp_path, config(strategy="round_trip_smoke_v1"))
    advance(ledger, clock, 0)
    before = ledger.status()
    clock[0] = at
    altered = snapshot(at, bid="100.02", ask="100.03", provider_at=provider_at)
    with pytest.raises(SimulationError):
        ledger.step(altered)
    assert ledger.status() == before
    ledger.close()


def test_sma_executes_only_after_prior_samples_not_current_rise(tmp_path):
    ledger, clock = open_ledger(tmp_path, config(short_window=2, long_window=3, slippage_bps="0"))
    for index in range(3):
        assert advance(ledger, clock, index)["simulated_orders"] == 0
    current_jump = advance(ledger, clock, 3, bid="110", ask="110.01")
    assert current_jump["step"]["decision"]["action"] == "hold"
    assert current_jump["simulated_orders"] == 0
    next_sample = advance(ledger, clock, 4, bid="120", ask="120.01")
    assert next_sample["step"]["decision"]["reason"] == "prior_sma_entry"
    assert Decimal(next_sample["step"]["order"]["fills"][0]["price_usd"]) == Decimal("120.01")
    assert next_sample["step"]["decision"]["signal_uses_current_sample"] is False
    ledger.close()


def test_restart_preserves_warmup_and_long_gap_invalidates_pending_entry(tmp_path):
    settings = config(short_window=2, long_window=3)
    ledger, clock = open_ledger(tmp_path, settings)
    for index in range(3):
        advance(ledger, clock, index, bid=str(100 + index), ask=str(100 + index) + ".01")
    ledger.close()
    clock[0] = NOW + 100
    with SimulationLedger(tmp_path / "simulation.sqlite3", settings, clock=lambda: clock[0]) as reopened:
        assert reopened.status()["warmup_samples"] == 3
        result = reopened.step(snapshot(clock[0], bid="110", ask="110.01"))
        assert result["warmup_samples"] == 1 and result["simulated_orders"] == 0
        assert result["step"]["decision"]["reason"] == "observation_gap_warmup_reset"


def test_entry_spread_guard_blocks_smoke_without_consuming_attempt(tmp_path):
    ledger, clock = open_ledger(tmp_path, config(strategy="round_trip_smoke_v1"))
    wide = advance(ledger, clock, 0, ask="101")
    assert wide["step"]["decision"]["reason"] == "entry_spread_exceeds_25_bps"
    assert wide["smoke_phase"] == "ready" and wide["simulated_orders"] == 0
    assert advance(ledger, clock, 1)["simulated_orders"] == 1
    ledger.close()


def test_loss_halt_sticks_across_restart_and_exit_remains_allowed(tmp_path):
    settings = config(strategy="round_trip_smoke_v1", order_budget_usd="100", slippage_bps="0")
    ledger, clock = open_ledger(tmp_path, settings)
    advance(ledger, clock, 0)
    crash = advance(ledger, clock, 1, bid="90", ask="90.01")
    assert crash["halted"] and "total_loss_limit" in crash["halt_reasons"]
    assert crash["step"]["decision"]["reason"] == "halt_exit"
    assert Decimal(crash["holdings_btc"]) == 0
    ledger.close()
    with SimulationLedger(tmp_path / "simulation.sqlite3", settings, clock=lambda: NOW + 10) as reopened:
        assert reopened.status()["halted"]
        after = reopened.step(snapshot(NOW + 10, bid="200", ask="200.01"))
        assert after["simulated_orders"] == 2 and after["halted"]


def test_manual_stop_is_idempotent_persistent_and_permits_exit_only(tmp_path):
    settings = config(strategy="round_trip_smoke_v1")
    ledger, clock = open_ledger(tmp_path, settings)
    advance(ledger, clock, 0)
    stopped = ledger.stop("manual")
    assert ledger.stop("manual") == stopped
    result = advance(ledger, clock, 1)
    assert result["step"]["decision"]["reason"] == "halt_exit"
    assert Decimal(result["holdings_btc"]) == 0 and result["halted"]
    ledger.close()
    with SimulationLedger(tmp_path / "simulation.sqlite3", settings) as reopened:
        assert reopened.status()["halt_reasons"] == ["manual"]


def test_dust_is_retained_with_basis_and_not_treated_as_cash(tmp_path):
    ledger, clock = open_ledger(tmp_path, config(strategy="round_trip_smoke_v1", slippage_bps="0"))
    first = advance(ledger, clock, 0, asks=[{"price": "100.01", "size": "0.005"}])
    assert Decimal(first["holdings_btc"]) == Decimal("0.005")
    second = advance(ledger, clock, 1)
    assert second["step"]["order"]["status"] == "unfilled_below_minimum"
    assert second["holdings_btc"] == first["holdings_btc"]
    assert second["cost_basis_usd"] == first["cost_basis_usd"]
    assert Decimal(second["dust_btc"]) > 0
    assert second["liquidation_equity_estimate_usd"] == second["synthetic_cash_usd"]
    assert_account(second)
    ledger.close()


def test_daily_mark_loss_triggers_with_open_position_and_stays_after_utc_rollover(tmp_path):
    settings = config(strategy="round_trip_smoke_v1", daily_loss_usd="0.1", max_loss_usd="5")
    ledger, clock = open_ledger(tmp_path, settings)
    first = advance(ledger, clock, 0)
    assert first["halted"] and first["halt_reasons"] == ["daily_loss_limit"]
    assert Decimal(first["realized_pnl_usd"]) == 0
    assert Decimal(first["daily_loss_usd"]) > Decimal("0.1")
    clock[0] = NOW + 86400
    later = ledger.step(snapshot(clock[0]))
    assert "daily_loss_limit" in later["halt_reasons"] and later["halted"]
    ledger.close()


def test_atomic_insert_failure_preserves_whole_step_and_allows_safe_retry(tmp_path):
    ledger, clock = open_ledger(tmp_path, config(strategy="round_trip_smoke_v1"))
    before = ledger.status()
    ledger._connection.execute("CREATE TRIGGER reject_step BEFORE INSERT ON events WHEN NEW.kind='step' BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    with pytest.raises(SimulationError, match="atomic_commit_failed"):
        advance(ledger, clock, 0)
    with pytest.raises(SimulationError, match="journal_closed"):
        ledger.status()
    raw = sqlite3.connect(tmp_path / "simulation.sqlite3")
    assert raw.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    raw.execute("DROP TRIGGER reject_step")
    raw.commit()
    raw.close()
    ledger = SimulationLedger(tmp_path / "simulation.sqlite3", config(strategy="round_trip_smoke_v1"), clock=lambda: NOW, create=False)
    assert ledger.status() == before
    assert advance(ledger, clock, 0)["simulated_orders"] == 1
    ledger.close()


def test_exclusive_writer_lock_survives_multiple_open_attempts(tmp_path):
    ledger, clock = open_ledger(tmp_path)
    with pytest.raises(SimulationError, match="writer_already_active"):
        SimulationLedger(tmp_path / "simulation.sqlite3", config())
    advance(ledger, clock, 0)
    ledger.close()
    with SimulationLedger(tmp_path / "simulation.sqlite3", config()) as reopened:
        assert reopened.status()["samples_seen"] == 1


def test_deleted_journal_presence_marker_prevents_new_100_balance(tmp_path):
    ledger, _ = open_ledger(tmp_path)
    ledger.close()
    (tmp_path / "simulation.sqlite3").unlink()
    with pytest.raises(SimulationError, match="existing_journal_required"):
        SimulationLedger(tmp_path / "simulation.sqlite3", config())
    assert not (tmp_path / "simulation.sqlite3").exists()


def test_open_existing_never_creates_state_or_lock(tmp_path):
    with pytest.raises(SimulationError, match="existing_journal_required"):
        SimulationLedger(tmp_path / "absent.sqlite3", config(), create=False)
    assert list(tmp_path.iterdir()) == []


def test_disappearing_journal_during_lock_acquisition_never_reseeds(tmp_path, monkeypatch):
    ledger, _ = open_ledger(tmp_path)
    ledger.close()
    path = tmp_path / "simulation.sqlite3"
    original_flock = simulation.fcntl.flock
    def disappear(fd, operation):
        original_flock(fd, operation)
        path.unlink()
    monkeypatch.setattr(simulation.fcntl, "flock", disappear)
    with pytest.raises(SimulationError, match="existing_journal_required"):
        SimulationLedger(path, config(), create=False)
    assert not path.exists()


def test_sqlite_connect_cannot_create_after_final_file_check(tmp_path, monkeypatch):
    ledger, _ = open_ledger(tmp_path)
    ledger.close()
    path = tmp_path / "simulation.sqlite3"
    original_connect = simulation.sqlite3.connect
    def disappear(database, *args, **kwargs):
        assert "mode=rw" in database and kwargs["uri"] is True
        path.unlink()
        return original_connect(database, *args, **kwargs)
    monkeypatch.setattr(simulation.sqlite3, "connect", disappear)
    with pytest.raises(SimulationError, match="journal_open_failed"):
        SimulationLedger(path, config(), create=False)
    assert not path.exists()


def test_commit_then_error_requires_reopen_and_preserves_exactly_one_trade(tmp_path):
    settings = config(strategy="round_trip_smoke_v1")
    ledger, clock = open_ledger(tmp_path, settings)
    connection = ledger._connection
    class CommitThenError:
        def __getattr__(self, name):
            return getattr(connection, name)
        def execute(self, query, *args):
            result = connection.execute(query, *args)
            if query == "COMMIT":
                raise OSError("commit acknowledgement lost")
            return result
    ledger._connection = CommitThenError()
    with pytest.raises(SimulationError, match="atomic_commit_failed"):
        advance(ledger, clock, 0)
    with pytest.raises(SimulationError, match="journal_closed"):
        ledger.status()
    with SimulationLedger(tmp_path / "simulation.sqlite3", settings, clock=lambda: NOW, create=False) as reopened:
        assert reopened.status()["simulated_orders"] == 1
        assert reopened.step(snapshot())["idempotent_replay"]
        assert reopened.status()["event_count"] == 2


def test_drawdown_roundtrips_and_exposure_metrics_persist(tmp_path):
    settings = config(strategy="round_trip_smoke_v1", slippage_bps="0")
    ledger, clock = open_ledger(tmp_path, settings)
    first = advance(ledger, clock, 0)
    assert Decimal(first["max_drawdown_usd"]) > 0
    second = advance(ledger, clock, 1, bid="110", ask="110.01")
    assert second["completed_round_trips"] == 1
    assert second["modeled_exposure_seconds"] == "5"
    assert Decimal(second["peak_liquidation_equity_usd"]) > 100
    assert Decimal(second["max_drawdown_usd"]) >= Decimal(first["max_drawdown_usd"])
    ledger.close()
    with SimulationLedger(tmp_path / "simulation.sqlite3", settings, create=False) as reopened:
        assert reopened.status() == {k: v for k, v in second.items() if k not in {"step", "idempotent_replay"}}


def test_gap_exposure_is_separate_from_observed_exposure(tmp_path):
    ledger, clock = open_ledger(tmp_path, config(strategy="round_trip_smoke_v1"))
    advance(ledger, clock, 0)
    clock[0] = NOW + 100
    result = ledger.step(snapshot(clock[0]))
    assert result["unobserved_exposure_gap_seconds"] == "100"
    assert result["modeled_exposure_seconds"] == "0"
    assert result["observation_gap_count"] == 1
    ledger.close()


@pytest.mark.parametrize("corruption", ["empty", "garbage", "balance", "delete_event", "audit"])
def test_corrupt_existing_journal_never_reseeds_100(tmp_path, corruption):
    path = tmp_path / "simulation.sqlite3"
    if corruption in {"empty", "garbage"}:
        path.write_bytes(b"" if corruption == "empty" else b"garbage")
        path.chmod(0o600)
    else:
        ledger, clock = open_ledger(tmp_path)
        advance(ledger, clock, 0)
        ledger.close()
        connection = sqlite3.connect(path)
        if corruption == "balance":
            connection.execute("UPDATE events SET body=replace(body,'\"cash\":\"100\"','\"cash\":\"200\"') WHERE seq=1")
        elif corruption == "delete_event":
            connection.execute("DELETE FROM events WHERE seq=0")
        else:
            connection.execute("UPDATE events SET digest='bad' WHERE seq=1")
        connection.commit()
        connection.close()
    before = path.read_bytes()
    with pytest.raises(SimulationError):
        SimulationLedger(path, config())
    assert path.read_bytes() == before


def test_consistent_backup_new_private_file_restores_identical_state(tmp_path):
    ledger, clock = open_ledger(tmp_path, config(strategy="round_trip_smoke_v1"))
    advance(ledger, clock, 0)
    backup = tmp_path / "backup.sqlite3"
    receipt = ledger.backup(backup)
    assert backup.stat().st_mode & 0o777 == 0o600
    assert receipt["audit_sha256"] == ledger.status()["audit_sha256"]
    with SimulationLedger(backup, config(strategy="round_trip_smoke_v1")) as restored:
        assert restored.status() == ledger.status()
    with pytest.raises(SimulationError, match="destination_invalid"):
        ledger.backup(backup)
    ledger.close()


@pytest.mark.parametrize("changes", [
    {"schema": True}, {"product_id": "ETH-USD"}, {"allocation_usd": "101"}, {"allocation_usd": "99"},
    {"order_budget_usd": "100.01"}, {"order_budget_usd": "0"}, {"max_loss_usd": "6"},
    {"daily_loss_usd": "0"}, {"daily_loss_usd": "5.1"}, {"taker_fee_bps": None},
    {"taker_fee_bps": "-1"}, {"taker_fee_bps": "1001"}, {"slippage_bps": "101"}, {"slippage_bps": "NaN"},
    {"short_window": 1}, {"short_window": 20}, {"long_window": 201}, {"interval_seconds": 0},
    {"interval_seconds": True}, {"strategy": "secret_model"}, {"api_key": "PRIVATE"},
])
def test_invalid_configs_refuse(changes):
    with pytest.raises(SimulationError):
        validate_config(config(**changes))


def test_ambient_decimal_context_does_not_change_accounting(tmp_path):
    settings = config(strategy="round_trip_smoke_v1")
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir(mode=0o700)
    second_dir.mkdir(mode=0o700)
    first, clock1 = open_ledger(first_dir, settings)
    expected = advance(first, clock1, 0)
    first.close()
    with localcontext() as context:
        context.prec = 4
        second, clock2 = open_ledger(second_dir, settings)
        actual = advance(second, clock2, 0)
        second.check_integrity()
        second.close()
    assert actual == expected


@pytest.mark.parametrize("mutation", ["stale", "post_only", "off_grid", "wrong_source", "restricted"])
def test_invalid_snapshot_has_no_account_or_journal_effect(tmp_path, mutation):
    ledger, clock = open_ledger(tmp_path)
    data = snapshot()
    if mutation == "stale":
        clock[0] = NOW + 11
    elif mutation == "post_only":
        data["product"]["post_only"] = True
    elif mutation == "off_grid":
        data["pricebook"]["asks"][0]["price"] = "100.011"
    elif mutation == "wrong_source":
        data["source"] = "https://evil.invalid"
    else:
        data["product"]["trading_disabled"] = True
    before = ledger.status()
    with pytest.raises(SimulationError):
        ledger.step(data)
    assert ledger.status() == before
    ledger.close()
