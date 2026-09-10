"""Deterministic risk gates with exact amounts and no broker sessions."""

from dataclasses import replace
from decimal import Decimal

import pytest

from atlas.domain import AccountSnapshot, Position, Quote, decimal
from atlas.risk import RiskPolicy, evaluate_risk, position_size

D = Decimal
NOW = 2_000_000_000


@pytest.fixture
def state():
    policy = RiskPolicy(mode="paper", expected_account_id="fixture-account", allowed_symbols={"XYZ"})
    account = AccountSnapshot("fixture-account", "paper", "USD", D("10000"), D("10000"),
                              D("10000"), (), NOW, D("0"))
    quote = Quote("XYZ", "equity", D("9.99"), D("10"), NOW, True, D("1000"),
                  "synthetic-test", quantity_increment=D("1"), minimum_quantity=D("1"))
    signal = {"strategy_id": "momentum", "symbol": "XYZ", "asset_class": "equity",
              "direction": "buy", "confidence": None, "live_validated": False,
              "data_timestamp": NOW}
    kwargs = dict(symbol="XYZ", asset_class="equity", side="buy", quantity="5", limit_price="10",
                  strategy=signal, now=NOW, daily_stats={"intents_count": 0}, pending_intents=False,
                  broker_open_orders=0, kill_switch=False)
    return policy, account, quote, kwargs


def check(state, **changes):
    policy, account, quote, kwargs = state
    return evaluate_risk(policy, account, quote, **{**kwargs, **changes})


def test_valid_paper_order_is_bounded_without_inventing_confidence(state):
    report = check(state)
    assert report["passed"]
    assert report["notional"] == "50"
    assert state[3]["strategy"]["confidence"] is None


@pytest.mark.parametrize("field,value,code", [
    ("pending_intents", True, "pending_reconciliation"),
    ("broker_open_orders", 1, "open_orders"),
    ("kill_switch", True, "kill_switch"),
    ("daily_stats", {"intents_count": 10}, "daily_trade_count"),
    ("daily_stats", {"intents_count": None}, "daily_trade_count"),
    ("quantity", "20", "order_notional"),
    ("side", "sell", "inventory"),
    ("reference_price", "9", "price_drift"),
    ("limit_price", "11", "price_limit"),
])
def test_each_risk_failure_blocks(state, field, value, code):
    report = check(state, **{field: value})
    assert not report["passed"]
    assert any(c["code"] == code and not c["passed"] for c in report["checks"])


@pytest.mark.parametrize("field,value", [
    ("account_id", "wrong-account"), ("mode", "real"), ("currency", "EUR"),
    ("observed_at", NOW - 31), ("observed_at", NOW + 1),
    ("daily_realized_pnl", D("-100")), ("daily_realized_pnl", None),
    ("equity", None), ("cash", None), ("buying_power", None),
    ("buying_power", D("1")), ("tradable", False),
])
def test_account_risk_data_must_be_complete_and_current(state, field, value):
    p, a, q, kwargs = state
    assert not evaluate_risk(p, replace(a, **{field: value}), q, **kwargs)["passed"]


@pytest.mark.parametrize("field,value", [
    ("timestamp", NOW - 31), ("timestamp", NOW + 1), ("market_open", None),
    ("available_quantity", None), ("available_quantity", D("1")),
    ("bid", D("11")), ("bid", D("1")), ("ask", D("NaN")),
    ("fee_per_unit", D("-1")), ("symbol", "OTHER"),
    ("asset_class", "fx_binary"), ("quantity_increment", D("2")),
    ("minimum_quantity", D("10")),
])
def test_quote_risk_data_must_be_complete_and_current(state, field, value):
    p, a, q, kwargs = state
    assert not evaluate_risk(p, a, replace(q, **{field: value}), **kwargs)["passed"]


def test_position_and_concentration_count_existing_exposure(state):
    p, a, q, kwargs = state
    a = replace(a, positions=(Position("XYZ", D("49"), D("490")),))
    report = evaluate_risk(p, a, q, **kwargs)
    assert not report["passed"]
    assert any(c["code"] == "position_size" and not c["passed"] for c in report["checks"])
    p = replace(p, max_position_notional=D("2000"), max_concentration=D("0.05"))
    report = evaluate_risk(p, a, q, **kwargs)
    assert any(c["code"] == "concentration" and not c["passed"] for c in report["checks"])


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", True, "", "-1", "0"])
def test_nonfinite_or_invalid_order_quantity_never_passes(state, bad):
    assert not check(state, quantity=bad)["passed"]


def test_live_needs_independent_enable_validation_and_confidence(state):
    p, a, q, kwargs = state
    p = replace(p, mode="live-auto")
    a = replace(a, mode="real")
    report = evaluate_risk(p, a, q, **kwargs)
    failed = {c["code"] for c in report["checks"] if not c["passed"]}
    assert {"live_enable", "auto_enable", "live_validation", "confidence"} <= failed
    p = replace(p, live_enabled=True, auto_enabled=True, live_approved_strategies={"momentum"})
    # Only a trusted provider's calibrated/validated evidence can satisfy this;
    # the shipped research registry deliberately never emits these values.
    kwargs["strategy"] = {**kwargs["strategy"], "confidence": "0.7", "live_validated": True}
    assert evaluate_risk(p, a, q, **kwargs)["passed"]
    assert not evaluate_risk(p, a, replace(q, timestamp_basis="retrieval"), **kwargs)["passed"]


def test_sizing_reports_effective_notional_after_rounding_and_liquidity(state):
    p, a, q, _ = state
    result = position_size(p, a, replace(q, available_quantity=D("2.5")))
    assert result["available"]
    assert result["maximum_quantity"] == "2"
    assert result["maximum_notional"] == "20"
    assert result["policy_budget"] == "100"
    assert not position_size(p, a, replace(q, available_quantity=None))["available"]
    assert not position_size(p, a, replace(q, quantity_increment=None))["available"]


@pytest.mark.parametrize("changes", [{"live_enabled": "false"}, {"auto_enabled": 1},
                                      {"max_daily_trades": True}, {"min_confidence": 2},
                                      {"max_concentration": 0}, {"max_order_notional": "NaN"}])
def test_policy_rejects_ambiguous_or_invalid_settings(changes):
    with pytest.raises(ValueError):
        RiskPolicy(**changes)


def test_exact_decimal_money_and_public_policy_do_not_expose_account(state):
    assert decimal("0.1") + decimal("0.2") == D("0.3")
    assert "fixture-account" not in str(state[0].public())
    assert "expected_account_id" not in state[0].public()
