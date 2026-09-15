"""Pure paper risk scenarios; no accounts, credentials, network or state files."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from atlas.paper.risk import PaperRiskPolicy, evaluate


NOW = 1789560000.5


@pytest.fixture
def inputs():
    policy = PaperRiskPolicy(allowed_strategy_ids=("paper_test",))
    decision = {"signal_id": "signal-1", "strategy_id": "paper_test", "strategy_sha256": "a" * 64,
                "data_sha256": "b" * 64, "symbol": "SPY", "side": "buy", "quantity": "0.09",
                "limit_price": "100", "signal_time": NOW - 1, "expires_at": NOW + 60}
    snapshot = {"account": {"fingerprint": "c" * 64, "status": "ACTIVE", "currency": "USD",
                            "cash": "100", "equity": "100", "buying_power": "100",
                            "long_market_value": "0", "short_market_value": "0", "multiplier": "1",
                            "blocked": False, "observed_at": NOW},
                "positions": [], "open_orders": [],
                "clock": {"at": NOW, "is_open": True, "next_open": NOW + 86400,
                          "next_close": NOW + 3600, "observed_at": NOW},
                "quote": {"symbol": "SPY", "bid": "99.98", "ask": "100", "bid_size": "5",
                          "ask_size": "5", "at": NOW, "observed_at": NOW},
                "asset": {"symbol": "SPY", "asset_class": "us_equity", "status": "active",
                          "tradable": True, "fractionable": True}, "observed_at": NOW}
    state = {"account_fingerprint": "c" * 64, "strategy_sha256": "a" * 64,
             "allocated_principal": "100", "day_start_equity": "100", "peak_equity": "100",
             "daily_attempts": 0, "recent_attempt_times": [], "reservations": [],
             "kill_switch": False, "reconciliation_ok": True, "broker_failures": 0,
             "cooldown_until": 0, "unresolved_orders": 0, "entry_halt": False}
    return policy, decision, snapshot, state


def assess(inputs):
    return evaluate(*inputs, now=NOW)


def put(inputs, path, value):
    target = {"decision": inputs[1], "snapshot": inputs[2], "state": inputs[3]}
    *parents, leaf = path.split(".")
    for key in parents:
        target = target[key]
    target[leaf] = value


def holding(inputs, symbol="SPY", qty="0.2", price="100"):
    snapshot = inputs[2]
    value = Decimal(qty) * Decimal(price)
    snapshot["positions"].append({"symbol": symbol, "qty": qty, "market_value": str(value),
                                  "current_price": price, "cost_basis": str(value)})
    account = snapshot["account"]
    account["long_market_value"] = str(Decimal(account["long_market_value"]) + value)
    account["cash"] = account["buying_power"] = str(Decimal(account["cash"]) - value)


def reservation(inputs, *, side="buy", qty="0.05", cash="5.005", symbol="SPY", identifier="owned-1"):
    inputs[3]["reservations"].append({"client_order_id": identifier, "symbol": symbol, "side": side,
                                      "remaining_qty": qty, "remaining_cash": cash})
    inputs[2]["open_orders"].append({"client_order_id": identifier, "symbol": symbol, "side": side,
                                     "qty": qty, "filled_qty": "0", "limit_price": "100", "status": "accepted"})


def test_success_reserves_fees_without_mutation_and_is_decimal_context_independent(inputs):
    before = deepcopy(inputs)
    with localcontext() as context:
        context.prec = 2
        report = assess(inputs)
    assert inputs == before
    assert report["passed"] and report["reasons"] == []
    assert report["reservation_cash"] == "9.009"
    assert report["reservation_qty"] == "0" and report["estimated_fees"] == "0.009"
    assert report["reduce_only"] is False
    assert report == assess(inputs)


@pytest.mark.parametrize("path,value,reason", [
    ("decision.strategy_sha256", "d" * 64, "strategy_identity"),
    ("decision.strategy_id", "other", "strategy_identity"),
    ("decision.symbol", "TSLA", "symbol_allowed"),
    ("decision.signal_time", NOW + 0.1, "signal_fresh"),
    ("decision.signal_time", NOW - 301, "signal_fresh"),
    ("decision.expires_at", NOW, "intent_unexpired"),
    ("decision.expires_at", NOW + 301, "intent_unexpired"),
    ("decision.quantity", "0.0000000001", "order_precision"),
    ("decision.limit_price", "100.001", "order_precision"),
    ("decision.limit_price", "101", "limit_distance"),
    ("decision.quantity", "0.1", "order_notional"),
    ("decision.quantity", "0.001", "minimum_buy_notional"),
    ("snapshot.account.fingerprint", "d" * 64, "account_identity"),
    ("snapshot.account.status", "RESTRICTED", "account_permission"),
    ("snapshot.account.currency", "EUR", "account_permission"),
    ("snapshot.account.blocked", True, "account_permission"),
    ("snapshot.account.multiplier", "2", "account_permission"),
    ("snapshot.account.short_market_value", "1", "account_permission"),
    ("snapshot.account.buying_power", "101", "cash_account"),
    ("snapshot.account.observed_at", NOW - 10.01, "snapshot_fresh"),
    ("snapshot.observed_at", NOW + 0.01, "snapshot_fresh"),
    ("snapshot.quote.at", NOW - 10.01, "quote_fresh"),
    ("snapshot.quote.at", NOW + 0.01, "quote_fresh"),
    ("snapshot.quote.observed_at", NOW - 11, "snapshot_fresh"),
    ("snapshot.quote.symbol", "QQQ", "quote_valid"),
    ("snapshot.quote.bid", "101", "quote_valid"),
    ("snapshot.quote.bid", "90", "spread"),
    ("snapshot.quote.ask_size", "0.01", "quote_liquidity"),
    ("snapshot.clock.is_open", False, "session_open"),
    ("snapshot.clock.next_close", NOW, "session_open"),
    ("snapshot.clock.at", NOW + 0.01, "session_open"),
    ("snapshot.asset.fractionable", False, "asset_permission"),
    ("snapshot.asset.tradable", False, "asset_permission"),
    ("snapshot.asset.asset_class", "crypto", "asset_permission"),
    ("state.allocated_principal", "100.01", "allocation_ceiling"),
    ("state.kill_switch", True, "kill_switch_clear"),
    ("state.reconciliation_ok", False, "reconciled"),
    ("state.unresolved_orders", 1, "reconciled"),
    ("state.broker_failures", 3, "broker_circuit"),
    ("state.cooldown_until", NOW + 1, "cooldown_elapsed"),
    ("state.entry_halt", True, "entry_halt_clear"),
    ("state.daily_attempts", 30, "daily_attempt_limit"),
    ("state.recent_attempt_times", [NOW - 30] * 5, "minute_attempt_limit"),
    ("state.day_start_equity", "102", "daily_loss"),
    ("state.peak_equity", "110", "drawdown"),
    ("state.peak_equity", "99", "equity_anchors_valid"),
])
def test_named_safety_gate_refuses(inputs, path, value, reason):
    put(inputs, path, value)
    report = assess(inputs)
    assert not report["passed"] and reason in report["reasons"]
    assert report["reservation_cash"] == report["reservation_qty"] == "0"


@pytest.mark.parametrize("path,value", [
    ("decision.quantity", True), ("decision.quantity", 0.09), ("decision.quantity", "NaN"),
    ("decision.quantity", "-1"), ("decision.limit_price", "Infinity"),
    ("decision.data_sha256", "unbound"), ("decision.signal_time", True),
    ("decision.expires_at", float("inf")), ("snapshot.account.cash", None),
    ("snapshot.account.equity", "not known"), ("snapshot.quote.bid_size", "0"),
    ("snapshot.observed_at", float("nan")), ("snapshot.positions", {}),
    ("snapshot.open_orders", None), ("state.daily_attempts", True),
    ("state.allocated_principal", 100), ("state.kill_switch", "false"),
    ("state.entry_halt", 0), ("state.reservations", None),
    ("state.recent_attempt_times", [NOW + 1]),
    ("state.recent_attempt_times", [NOW - 1, NOW - 2]),
])
def test_malformed_inputs_never_authorize(inputs, path, value):
    put(inputs, path, value)
    report = assess(inputs)
    assert not report["passed"] and "complete_valid_inputs" in report["reasons"]


def test_missing_fields_and_extra_decision_authority_are_refused(inputs):
    for key in tuple(inputs[1]):
        copy = deepcopy(inputs)
        del copy[1][key]
        assert not assess(copy)["passed"]
    inputs[1]["live_enabled"] = True
    assert not assess(inputs)["passed"]


def test_existing_buy_reservation_uses_cash_and_symbol_capacity(inputs):
    holding(inputs, qty="0.2")
    reservation(inputs)
    report = assess(inputs)
    assert "position_exposure" in report["reasons"]
    inputs[2]["positions"] = []
    inputs[2]["account"].update(cash="10", buying_power="10", equity="10", long_market_value="0")
    inputs[3].update(day_start_equity="10", peak_equity="10")
    assert "cash_available" in assess(inputs)["reasons"]


@pytest.mark.parametrize("change", ["unmanaged", "wrong_symbol", "wrong_side", "wrong_qty", "underreserved", "duplicate", "terminal"])
def test_open_order_must_match_owned_reservation(inputs, change):
    reservation(inputs)
    order = inputs[2]["open_orders"][0]
    if change == "unmanaged": inputs[3]["reservations"].clear()
    if change == "wrong_symbol": order["symbol"] = "QQQ"
    if change == "wrong_side": order["side"] = "sell"
    if change == "wrong_qty": order["qty"] = "0.06"
    if change == "underreserved": inputs[3]["reservations"][0]["remaining_cash"] = "5"
    if change == "duplicate": inputs[2]["open_orders"].append(deepcopy(order))
    if change == "terminal": order["status"] = "filled"
    assert not assess(inputs)["passed"]


def test_partial_open_order_retains_remaining_reservation(inputs):
    reservation(inputs)
    inputs[2]["open_orders"][0].update(qty="0.08", filled_qty="0.03", status="partially_filled")
    assert assess(inputs)["passed"]


def test_broker_reserved_buying_power_is_not_subtracted_twice(inputs):
    reservation(inputs, qty="0.09", cash="9.009")
    inputs[2]["account"]["buying_power"] = "91"
    assert assess(inputs)["passed"]


def test_unacknowledged_local_hold_still_reduces_buying_power(inputs):
    reservation(inputs, qty="0.09", cash="9.009")
    inputs[2]["open_orders"].clear()
    inputs[2]["account"]["buying_power"] = "10"
    assert "cash_available" in assess(inputs)["reasons"]


def test_unsubmitted_reservation_remains_charged(inputs):
    reservation(inputs, qty="0.2", cash="20.02")
    inputs[2]["open_orders"].clear()
    assert "position_exposure" in assess(inputs)["reasons"]


def test_profitable_account_may_trade_without_compounding_allocation(inputs):
    inputs[2]["account"].update(cash="120", buying_power="120", equity="120")
    inputs[3].update(peak_equity="120", day_start_equity="120")
    assert assess(inputs)["passed"]
    for symbol in ("SPY", "QQQ", "GLD"):
        holding(inputs, symbol, qty="0.24")
    assert "portfolio_exposure" in assess(inputs)["reasons"]


def test_exit_reduces_inventory_despite_entry_halts(inputs):
    holding(inputs, qty="0.4")
    inputs[1].update(side="sell", quantity="0.09", limit_price="99.98")
    inputs[3].update(entry_halt=True, daily_attempts=30, recent_attempt_times=[NOW - 1] * 5,
                     peak_equity="120", day_start_equity="110")
    report = assess(inputs)
    assert report["passed"] and report["reduce_only"]
    assert report["reservation_qty"] == "0.09" and report["reservation_cash"] == "0"
    assert Decimal(report["estimated_fees"]) > 0


def test_exit_still_obeys_hard_per_order_cap(inputs):
    holding(inputs, qty="0.4")
    inputs[1].update(side="sell", quantity="0.2", limit_price="99.98")
    report = assess(inputs)
    assert report["reduce_only"] and "order_notional" in report["reasons"]


@pytest.mark.parametrize("path,value,reason", [
    ("state.kill_switch", True, "kill_switch_clear"),
    ("state.reconciliation_ok", False, "reconciled"),
    ("state.unresolved_orders", 1, "reconciled"),
    ("snapshot.clock.is_open", False, "session_open"),
    ("snapshot.quote.at", NOW - 11, "quote_fresh"),
    ("snapshot.account.fingerprint", "d" * 64, "account_identity"),
])
def test_exits_cannot_bypass_shared_safety_gates(inputs, path, value, reason):
    holding(inputs)
    inputs[1].update(side="sell", limit_price="99.98")
    put(inputs, path, value)
    assert reason in assess(inputs)["reasons"]


def test_reserved_sell_shares_cannot_be_sold_twice(inputs):
    holding(inputs)
    reservation(inputs, side="sell", qty="0.15", cash="0")
    inputs[1].update(side="sell", limit_price="99.98")
    assert "inventory" in assess(inputs)["reasons"]
    inputs[1]["quantity"] = "0.05"
    assert assess(inputs)["passed"]


def test_exact_limits_are_conservative(inputs):
    inputs[3]["recent_attempt_times"] = [NOW - 60] * 5
    assert assess(inputs)["passed"]
    inputs[3]["peak_equity"] = "100"
    inputs[2]["account"].update(cash="95", equity="95", buying_power="95")
    assert "drawdown" in assess(inputs)["reasons"]


@pytest.mark.parametrize("changes", [
    {"allocated_principal_limit": "101"}, {"max_order_notional": "26"},
    {"max_portfolio_notional": "101"}, {"max_daily_loss": "101"},
    {"max_drawdown_fraction": "1.01"}, {"max_drawdown_fraction": "NaN"},
    {"fee_buffer_bps": "0"}, {"fee_buffer_bps": 10}, {"max_positions": True},
    {"max_quote_age_seconds": 31}, {"max_intent_ttl_seconds": 301},
    {"allowed_symbols": "SPY"}, {"allowed_symbols": ("SPY", "SPY")},
    {"allowed_strategy_ids": ("bad strategy",)},
])
def test_policy_rejects_ambiguous_or_oversized_limits(changes):
    with pytest.raises(ValueError):
        PaperRiskPolicy(**changes)


def test_policy_requires_strategy_selection_and_hashes_canonical_limits(inputs):
    assert not evaluate(PaperRiskPolicy(), *inputs[1:], now=NOW)["passed"]
    policy = inputs[0]
    assert policy.public()["allocated_principal_limit"] == "100"
    assert policy.fingerprint() == PaperRiskPolicy(**policy.public()).fingerprint()
    assert policy.fingerprint() != replace(policy, max_order_notional="9").fingerprint()
    with pytest.raises(FrozenInstanceError):
        policy.max_positions = 4


@pytest.mark.parametrize("now", [True, float("nan"), float("inf"), -1, "1789560000"])
def test_invalid_clock_never_authorizes(inputs, now):
    assert not evaluate(*inputs, now=now)["passed"]
