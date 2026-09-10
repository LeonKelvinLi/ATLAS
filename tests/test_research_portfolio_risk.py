"""Trusted portfolio projections fail closed before any order authority."""

from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from atlas.risk import PortfolioRiskPolicy, evaluate_portfolio_risk, portfolio_order_fingerprint


NOW = 1_800_000_000
ACCOUNT = "sha256:test-account"


def snapshot(**changes):
    result = {"account_fingerprint": ACCOUNT, "observed_at": NOW, "equity": "1000",
              "order_mark_price": "100", "positions": [
                  {"symbol": "AAA", "asset_class": "equity", "quantity": "2", "market_value": "200"},
                  {"symbol": "BBB", "asset_class": "crypto", "quantity": "1", "market_value": "100"}],
              "global_enabled": True, "broker_enabled": True, "account_enabled": True,
              "data_quality_passed": True, "strategy_health": {"test-strategy": "HEALTHY"},
              "symbol_enabled": {"AAA": True}, "drawdown": "0.05", "turnover": "0.2",
              "turnover_equity_basis": "1000", "correlated_exposure_by_symbol": {"AAA": "300"}}
    result.update(changes)
    return result


def evaluate(policy=None, data=None, **changes):
    args = dict(now=NOW, expected_account_fingerprint=ACCOUNT, symbol="AAA", asset_class="equity",
                strategy_id="test-strategy", side="buy", quantity="1", limit_price="100", estimated_fees="0")
    args.update(changes)
    return evaluate_portfolio_risk(policy or PortfolioRiskPolicy(), snapshot() if data is None else data, **args)


def failed_codes(result):
    return {row["code"] for row in result["checks"] if not row["passed"]}


def test_exact_post_trade_caps_and_fees_have_no_rounding_escape():
    policy = PortfolioRiskPolicy(max_gross_leverage="0.4", max_net_leverage="0.4",
                                 max_asset_class_weights={"equity": "0.3"}, max_correlated_weight="0.4",
                                 max_positions=2, max_turnover="0.3", max_drawdown="0.1")
    result = evaluate(policy)
    assert result["passed"] and not result["risk_reducing"]
    assert result["projections"]["gross_exposure"] == "400"
    assert result["projections"]["turnover"] == "0.3"
    assert result["projections"]["position_count"] == 2
    charged = evaluate(policy, estimated_fees="0.000000000000000001")
    assert not charged["passed"]
    assert {"portfolio_max_gross_leverage", "portfolio_max_net_leverage", "portfolio_asset_class_equity",
            "portfolio_correlated_exposure"} <= failed_codes(charged)
    with localcontext() as context:
        context.prec = 2
        assert evaluate(policy, estimated_fees="0.000000000000000001") == charged


@pytest.mark.parametrize("changes,code", [
    ({"account_fingerprint": "different"}, "portfolio_identity"),
    ({"observed_at": NOW - 31}, "portfolio_freshness"),
    ({"observed_at": NOW + 1}, "portfolio_freshness"),
    ({"observed_at": True}, "portfolio_freshness"),
    ({"gross_exposure": "0"}, "portfolio_gross_exposure_consistent"),
    ({"net_exposure": "-1"}, "portfolio_complete_finite_data"),
    ({"positions": None}, "portfolio_complete_finite_data"),
    ({"order_mark_price": None}, "portfolio_complete_finite_data"),
    ({"equity": "NaN"}, "portfolio_complete_finite_data"),
])
def test_stale_missing_nonfinite_or_inconsistent_snapshot_is_rejected(changes, code):
    result = evaluate(data=snapshot(**changes))
    assert not result["passed"] and code in failed_codes(result)


def test_missing_snapshot_and_unknown_fees_cannot_be_assumed_zero():
    result = evaluate_portfolio_risk(PortfolioRiskPolicy(), None, now=NOW,
        expected_account_fingerprint=ACCOUNT, symbol="AAA", asset_class="equity", strategy_id="test-strategy",
        side="buy", quantity="1", limit_price="100", estimated_fees="0")
    assert failed_codes(result) == {"portfolio_snapshot"}
    assert "portfolio_complete_finite_data" in failed_codes(evaluate(estimated_fees=None))


@pytest.mark.parametrize("key", ["global_enabled", "broker_enabled", "account_enabled", "data_quality_passed"])
@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_disabled_or_unknown_authority_and_data_block_even_inventory_reduction(key, value):
    for require_health in (True, False):
        result = evaluate(PortfolioRiskPolicy(require_health=require_health), snapshot(**{key: value}), side="sell")
        assert result["risk_reducing"] and not result["passed"]
        assert "portfolio_" + key in failed_codes(result)


@pytest.mark.parametrize("health", ["DEGRADED", "DISABLED"])
def test_known_degraded_strategy_can_reduce_but_not_add_inventory(health):
    data = snapshot(strategy_health={"test-strategy": health})
    assert "portfolio_strategy_health" in failed_codes(evaluate(data=data))
    result = evaluate(data=data, side="sell")
    assert result["passed"] and result["risk_reducing"]
    assert result["projections"]["gross_exposure"] == "200"


@pytest.mark.parametrize("health", [None, "UNKNOWN", "healthy"])
def test_missing_or_unrecognized_strategy_health_blocks_exits(health):
    assert "portfolio_strategy_health" in failed_codes(evaluate(data=snapshot(
        strategy_health={"test-strategy": health}), side="sell"))


def test_explicit_symbol_disable_also_blocks_exit():
    assert "portfolio_symbol_enabled" in failed_codes(evaluate(data=snapshot(symbol_enabled={"AAA": False}), side="sell"))


def test_breached_limits_allow_bounded_reduction_but_missing_projection_does_not():
    policy = PortfolioRiskPolicy(max_drawdown="0.01", max_gross_leverage="0.1", max_net_leverage="0.1",
        max_asset_class_weights={"equity": "0.05", "crypto": "0.05"}, max_correlated_weight="0.1",
        max_positions=1, max_turnover="0.1")
    assert not evaluate(policy)["passed"]
    reduced = evaluate(policy, side="sell", quantity="0.5")
    assert reduced["passed"] and reduced["risk_reducing"]
    assert reduced["projections"]["position_count"] == 2
    assert not evaluate(policy, side="sell", quantity="2.1")["passed"]
    assert not evaluate(policy, snapshot(drawdown=None), side="sell")["passed"]


def test_full_exit_decrements_position_count_and_never_uses_unrealized_limit_gain_as_budget():
    result = evaluate(PortfolioRiskPolicy(max_positions=1), side="sell", quantity="2", limit_price="120")
    assert result["passed"] and result["projections"]["position_count"] == 1
    assert result["projections"]["equity"] == "1000"
    loss = evaluate(side="sell", quantity="1", limit_price="90", estimated_fees="1")
    assert loss["projections"]["equity"] == "989"


def test_spread_cost_reduces_buy_equity_without_overstating_marked_exposure():
    result = evaluate(limit_price="101", estimated_fees="1")
    assert result["passed"] and result["projections"]["equity"] == "998"
    assert result["projections"]["gross_exposure"] == "400"


@pytest.mark.parametrize("cap,changes", [
    ({"max_drawdown": "0.2"}, {"drawdown": None}),
    ({"max_correlated_weight": "0.5"}, {"correlated_exposure_by_symbol": None}),
    ({"max_correlated_weight": "0.5"}, {"correlated_exposure_by_symbol": {"AAA": "199"}}),
    ({"max_correlated_weight": "0.5"}, {"correlated_exposure_by_symbol": {"AAA": "301"}}),
    ({"max_turnover": "0.5"}, {"turnover_equity_basis": None}),
    ({"max_portfolio_volatility": "0.2"}, {"projected_portfolio_volatility": None}),
])
def test_configured_caps_fail_when_measure_is_unknown(cap, changes):
    result = evaluate(PortfolioRiskPolicy(**cap), snapshot(**changes))
    assert not result["passed"] and "portfolio_complete_finite_data" in failed_codes(result)


def test_drawdown_at_limit_vetoes_new_risk_and_fees_cannot_hide_a_breach():
    policy = PortfolioRiskPolicy(max_drawdown="0.1")
    assert not evaluate(policy, snapshot(drawdown="0.1"))["passed"]
    assert evaluate(policy, snapshot(drawdown="0.1"), side="sell")["passed"]
    assert not evaluate(policy, snapshot(drawdown="0.099"), estimated_fees="2")["passed"]


def test_volatility_projection_requires_exact_order_and_horizon_binding():
    policy = PortfolioRiskPolicy(max_portfolio_volatility="0.2")
    fingerprint = portfolio_order_fingerprint("AAA", "equity", "buy", "1", "100", "0")
    data = snapshot(projected_portfolio_volatility="0.2", order_fingerprint=fingerprint,
                    volatility_basis="annualized_daily_returns")
    assert evaluate(policy, data)["passed"]
    assert "portfolio_volatility_identity" in failed_codes(evaluate(policy, data, quantity="1.01"))
    assert "portfolio_volatility_identity" in failed_codes(evaluate(policy, {**data, "volatility_basis": "hourly"}))
    assert "portfolio_volatility" in failed_codes(evaluate(policy, {**data, "projected_portfolio_volatility": "0.201"}))


def test_policy_is_immutable_and_fingerprints_cover_every_limit():
    source = {"equity": "0.5"}
    policy = PortfolioRiskPolicy(max_asset_class_weights=source)
    source["equity"] = "1"
    assert policy.max_asset_class_weights["equity"] == Decimal("0.5")
    with pytest.raises(TypeError):
        policy.max_asset_class_weights["equity"] = Decimal("1")
    assert policy.fingerprint() == PortfolioRiskPolicy(max_asset_class_weights={"equity": "0.50"}).fingerprint()
    assert policy.fingerprint() != PortfolioRiskPolicy(max_asset_class_weights={"equity": "0.6"}).fingerprint()


@pytest.mark.parametrize("kwargs", [{"max_drawdown": "NaN"}, {"max_drawdown": "1.1"},
    {"max_positions": True}, {"max_snapshot_age_seconds": None}, {"max_snapshot_age_seconds": -1},
    {"max_asset_class_weights": {"unknown": "0.1"}}, {"allowed_strategy_health": {"DISABLED"}}])
def test_invalid_policy_cannot_relax_gates(kwargs):
    with pytest.raises(ValueError):
        PortfolioRiskPolicy(**kwargs)


def test_complete_positions_required_and_class_identity_must_agree():
    for update in ({"quantity": "0"}, {"quantity": "-1"}, {"asset_class": "crypto"}):
        data = deepcopy(snapshot())
        data["positions"][0].update(update)
        assert not evaluate(data=data)["passed"]
