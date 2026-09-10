"""No-network paper accounting, restart and durability failure contracts."""

from dataclasses import replace
from decimal import Decimal as D, localcontext
import os
import sqlite3

import pytest

from atlas.brokers.paper import PaperBroker
from atlas.domain import BrokerError, BrokerRejected, OrderRequest, Quote, UnknownSubmission


def quote(**changes):
    return replace(Quote("ABC", "equity", D("99"), D("100"), 100, True, D("100"), "synthetic"), **changes)


def order(client="first", *, side="buy", quantity="2", limit="100"):
    return OrderRequest("paper-demo", "ABC", "equity", side, D(quantity), D(limit), client)


def broker(**kwargs):
    return PaperBroker({"ABC": quote()}, cash=D("1000"), fee_bps=D("100"), clock=lambda: 100, **kwargs)


def test_buy_partial_sell_cost_basis_fees_realized_pnl_and_flat_account():
    b = broker()
    assert b.capabilities.network_enabled is False
    buy = b.submit_order(order())
    assert buy.fees == D("2") and buy.filled_quantity == D("2")
    account = b.get_account()
    assert account.cash == D("798") and account.equity == D("996")
    assert account.positions[0].cost_basis == D("202")
    b.quotes["ABC"] = quote(bid=D("110"), ask=D("111"))
    partial = b.submit_order(order("partial", side="sell", quantity="1", limit="110"))
    assert partial.realized_pnl == D("7.9") and partial.fees == D("1.1")
    assert b.get_account().positions[0].cost_basis == D("101")
    final = b.submit_order(order("final", side="sell", quantity="1", limit="110"))
    assert final.realized_pnl == D("7.9")
    account = b.get_account()
    assert account.cash == D("1015.8") and account.positions == ()
    assert account.daily_realized_pnl == D("15.8")
    b.close()


def test_exact_retries_are_idempotent_even_without_a_current_quote():
    b = broker()
    first = b.submit_order(order())
    b.quotes.clear()
    second = b.submit_order(order())
    assert first == second
    assert b.get_order_status(client_order_id="first") == first
    with pytest.raises(BrokerRejected, match="different_order"):
        b.submit_order(order(quantity="3"))
    b.close()


def test_resting_limit_reserves_cash_and_cancel_releases_without_a_fill():
    b = broker()
    resting = b.submit_order(order(quantity="1", limit="90"))
    assert resting.status == "accepted" and resting.filled_quantity == 0
    assert b.get_account().cash == 1000 and b.get_account().buying_power == D("909.1")
    assert len(b.get_open_orders()) == 1
    cancelled = b.cancel_order(resting.broker_order_id)
    assert cancelled.status == "cancelled"
    assert b.cancel_order(resting.broker_order_id) == cancelled
    assert b.get_account().buying_power == 1000 and b.get_open_orders() == ()
    assert b.get_account().positions == ()
    b.close()


@pytest.mark.parametrize("case", ["cash", "inventory", "liquidity", "closed", "bool_quantity", "account"])
def test_rejected_orders_preserve_account_and_never_create_a_record(case):
    b = broker()
    request = order()
    if case == "cash":
        request = order(quantity="1000")
    elif case == "inventory":
        request = order(side="sell", limit="99")
    elif case == "liquidity":
        b.quotes["ABC"] = quote(available_quantity=D("1"))
    elif case == "closed":
        b.quotes["ABC"] = quote(market_open=False)
    elif case == "bool_quantity":
        request = replace(request, quantity=True)
    else:
        request = replace(request, account_id="another")
    with pytest.raises((BrokerRejected, ValueError)):
        b.submit_order(request)
    assert b.get_account().cash == 1000 and b.get_account().positions == ()
    assert b.get_order_status(client_order_id="first") is None
    b.close()


def test_account_marking_keeps_oldest_held_quote_timestamp_and_no_closed_position_dependency():
    b = broker()
    b.submit_order(order())
    b.clock = lambda: 200
    assert b.get_account().observed_at == 100
    b.quotes["ABC"] = quote(timestamp=190)
    assert b.get_account().observed_at == 190
    b.submit_order(order("sell", side="sell", quantity="2", limit="99"))
    b.quotes.clear()
    assert b.get_account().observed_at == 200 and b.get_account().positions == ()
    b.close()


def test_valuation_timestamp_and_price_come_from_the_same_quote(monkeypatch):
    b = broker()
    b.submit_order(order())
    b.clock = lambda: 200
    calls = []
    def changing_quote(symbol, *args, **kwargs):
        calls.append(symbol)
        return quote(bid=D("99") if len(calls) == 1 else D("10"), timestamp=100 if len(calls) == 1 else 200)
    monkeypatch.setattr(b, "get_quote", changing_quote)
    account = b.get_account()
    assert account.equity == D("996") and account.observed_at == 100
    assert calls == ["ABC"]
    b.close()


def test_concurrent_persistent_account_read_uses_one_cash_position_snapshot(tmp_path):
    path = tmp_path / "paper.db"
    a, b = broker(path=path), broker(path=path)
    original = a.db
    class InterleavedRead:
        fired = False
        def __getattr__(self, name):
            return getattr(original, name)
        def execute(self, sql, *args):
            cursor = original.execute(sql, *args)
            if sql.startswith("SELECT cash") and not self.fired:
                self.fired = True
                b.submit_order(order())
            return cursor
    a.db = InterleavedRead()
    snapshot = a.get_account()
    assert snapshot.cash == D("1000") and snapshot.positions == ()
    assert b.get_account().cash == D("798")
    assert len(b.get_account().positions) == 1
    a.close()
    b.close()


def test_quote_amounts_normalize_to_decimal_and_reject_bool_prices():
    b = broker()
    b.quotes["ABC"] = quote(bid=99.0, ask=100.0, available_quantity=10.0)
    assert isinstance(b.get_quote("ABC").ask, D)
    assert b.submit_order(order()).filled_quantity == 2
    b.quotes["ABC"] = quote(bid=True)
    with pytest.raises(ValueError):
        b.get_quote("ABC")
    b.close()


def test_future_holding_mark_cannot_be_relabelled_as_a_fresh_account():
    b = broker()
    b.submit_order(order())
    b.quotes["ABC"] = quote(timestamp=101)
    with pytest.raises(BrokerError, match="timestamp_in_future"):
        b.get_account()
    b.close()


def test_paper_cash_fees_and_cost_basis_do_not_depend_on_caller_precision(tmp_path):
    path = tmp_path / "paper.db"
    b = broker(path=path)
    with localcontext() as context:
        context.prec = 2
        fill = b.submit_order(order())
        assert fill.fees == D("2")
        assert b.get_account().cash == D("798")
        assert context.prec == 2
    b.close()
    restored = broker(path=path)
    assert restored.get_account().cash == D("798")
    restored.close()


def test_fractional_average_cost_allocation_remains_readable_after_restart(tmp_path):
    path = tmp_path / "paper.db"
    b = broker(path=path)
    b.submit_order(order(quantity="1"))
    b.quotes["ABC"] = quote(bid=D("100"), ask=D("101"))
    b.submit_order(order("second", quantity="2", limit="101"))
    b.quotes["ABC"] = quote(bid=D("110"), ask=D("111"))
    b.submit_order(order("partial", side="sell", quantity="1", limit="110"))
    account = b.get_account()
    b.close()
    restored = broker(path=path)
    assert restored.get_account().cash == account.cash
    assert restored.get_account().positions[0].cost_basis == account.positions[0].cost_basis
    restored.close()


def test_restart_preserves_cash_positions_fill_ids_and_never_reseeds(tmp_path):
    path = tmp_path / "state" / "paper.db"
    b = broker(path=path)
    fill = b.submit_order(order())
    account = b.get_account()
    b.close()
    restored = PaperBroker({"ABC": quote()}, cash=D("999999"), fee_bps=D("100"), path=path, clock=lambda: 100)
    assert restored.get_account() == account
    assert restored.submit_order(order()) == fill
    assert restored.get_account().cash == D("798")
    assert os.stat(path).st_mode & 0o777 == 0o600
    restored.close()


@pytest.mark.parametrize("damage", ["zero", "junk", "missing_account", "missing_table", "extra_table", "version", "cash", "fill"])
def test_existing_damaged_state_is_refused_without_repair_or_reset(tmp_path, damage):
    path = tmp_path / "paper.db"
    b = broker(path=path)
    b.submit_order(order())
    b.close()
    if damage == "zero":
        path.write_bytes(b"")
    elif damage == "junk":
        path.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(path) as conn:
            command = {"missing_account": "DELETE FROM account", "missing_table": "DROP TABLE orders",
                       "extra_table": "CREATE TABLE unwanted (x INTEGER)", "version": "PRAGMA user_version=0",
                       "cash": "UPDATE account SET cash='NaN'",
                       "fill": "UPDATE orders SET result='{}'"}[damage]
            conn.execute(command)
    with pytest.raises(BrokerError):
        broker(path=path)
    if damage == "zero":
        assert path.read_bytes() == b""
    if damage == "missing_account":
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 0


def test_existing_account_identity_mismatch_refused(tmp_path):
    path = tmp_path / "paper.db"
    broker(path=path).close()
    with pytest.raises(BrokerError, match="identity_mismatch"):
        broker(path=path, account_id="wrong")


def test_restart_cannot_change_persisted_fee_assumptions(tmp_path):
    path = tmp_path / "paper.db"
    b = broker(path=path)
    b.submit_order(order(quantity="1", limit="90"))
    b.close()
    with pytest.raises(BrokerError, match="assumptions_changed"):
        PaperBroker({"ABC": quote()}, cash=D("1000"), fee_bps=D("0"), path=path, clock=lambda: 100)
    restored = broker(path=path)
    assert restored.get_account().buying_power == D("909.1")
    restored.close()


def test_clock_rewind_cannot_expose_future_paper_account_state(tmp_path):
    path = tmp_path / "paper.db"
    b = broker(path=path)
    b.submit_order(order())
    b.clock = lambda: 200
    b.quotes["ABC"] = quote(bid=D("110"), ask=D("111"), timestamp=200)
    b.submit_order(order("sell", side="sell", quantity="2", limit="110"))
    b.clock = lambda: 100
    with pytest.raises(BrokerError, match="clock_rewind"):
        b.get_account()
    b.close()
    with pytest.raises(BrokerError, match="clock_rewind"):
        broker(path=path)


def test_legacy_unversioned_assumptions_require_recovery_not_silent_migration(tmp_path):
    path = tmp_path / "paper.db"
    b = broker(path=path)
    b.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE metadata")
        db.execute("PRAGMA user_version=1")
    with pytest.raises(BrokerError, match="schema_mismatch"):
        broker(path=path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_initialization_requires_directory_durability_and_leaves_failed_state_for_recovery(tmp_path, monkeypatch):
    path = tmp_path / "paper.db"
    monkeypatch.setattr(PaperBroker, "_sync_directory", staticmethod(lambda path: (_ for _ in ()).throw(OSError("injected"))))
    with pytest.raises(BrokerError, match="durability"):
        broker(path=path)
    assert path.exists()  # Never silently delete/reinitialize a possibly committed account.


@pytest.mark.parametrize("mutation", ["submit", "cancel"])
def test_post_commit_sync_failure_is_unknown_and_restart_reconciles_exact_operation(tmp_path, monkeypatch, mutation):
    path = tmp_path / "paper.db"
    b = broker(path=path)
    if mutation == "cancel":
        resting = b.submit_order(order(limit="90"))
    original_sync = PaperBroker._sync_directory
    monkeypatch.setattr(b, "_sync_directory", lambda path: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(UnknownSubmission, match="commit_unconfirmed"):
        b.submit_order(order()) if mutation == "submit" else b.cancel_order(resting.broker_order_id)
    with pytest.raises(BrokerError, match="recovery_required"):
        b.get_account()
    b.close()
    monkeypatch.setattr(PaperBroker, "_sync_directory", staticmethod(original_sync))
    restored = broker(path=path)
    result = restored.get_order_status(client_order_id="first")
    assert result.status == ("filled" if mutation == "submit" else "cancelled")
    assert restored.get_account().cash == (D("798") if mutation == "submit" else D("1000"))
    restored.close()
