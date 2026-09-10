"""Adversarial research metric tests; synthetic paths are not alpha evidence."""

import math

import pytest

from atlas.research.diagnostics import (block_bootstrap, cost_sensitivity, drawdown_analysis,
    multiple_testing, path_metrics, return_series, strategy_scorecard)


def curve(values, times=None):
    return [{"timestamp": t, "equity": value} for t, value in zip(times or range(len(values)), values)]


@pytest.mark.parametrize("row", [
    {"timestamp": 0, "equity": 0}, {"timestamp": 0, "equity": -1},
    {"timestamp": 0, "equity": float("nan")}, {"timestamp": 0, "equity": True},
    {"timestamp": True, "equity": 100}, {"timestamp": -1, "equity": 100},
    {"timestamp": 10 ** 30, "equity": 100},
])
def test_short_paths_do_not_skip_input_validation(row):
    with pytest.raises(ValueError):
        path_metrics([row], evidence_class="TEST")


def test_empty_and_single_point_paths_are_explicitly_unavailable_for_returns():
    assert return_series([]) == []
    assert path_metrics([], evidence_class="TEST")["net_return"] is None
    report = path_metrics(curve([100]), evidence_class="TEST")
    assert report["max_drawdown"] == 0 and report["time_underwater_fraction"] is None
    assert report["annualization_available"] is False


def test_underwater_time_is_duration_weighted_and_peak_to_recovery_is_preserved():
    report = drawdown_analysis(curve([100, 90, 90, 100], [0, 10, 100, 101]), evidence_class="TEST")
    assert report["time_underwater_fraction"] == pytest.approx(91 / 101)
    assert report["underwater_observation_fraction"] == 0.5
    assert report["longest_observed_underwater_seconds"] == 101
    assert report["open_drawdown"] is False
    opened = drawdown_analysis(curve([100, 90, 80], [0, 10, 100]), evidence_class="TEST")
    assert opened["longest_observed_underwater_seconds"] == 100 and opened["open_drawdown"] is True


@pytest.mark.parametrize("values", [[1e-300, 1e300], [1e300, 1e-300]])
def test_numeric_range_failures_never_become_infinite_or_total_loss_metrics(values):
    with pytest.raises(ValueError, match="numeric range"):
        return_series(curve(values))


@pytest.mark.parametrize("settings", [
    {"seed": -1}, {"seed": True}, {"resamples": 1}, {"block_size": 0},
    {"block_size": 1.5}, {"resamples": 6000},
])
def test_bootstrap_settings_are_rejected_even_with_insufficient_data(settings):
    with pytest.raises(ValueError):
        block_bootstrap([], **settings)
    with pytest.raises(ValueError):
        multiple_testing({}, attempts=1, **settings)


def test_bootstrap_rejects_nonfinite_returns_and_bounded_workload():
    for values in ([float("nan")], [float("inf")], [-1], [True]):
        with pytest.raises(ValueError):
            block_bootstrap(values)
    with pytest.raises(ValueError, match="workload"):
        block_bootstrap([0.01] * 5000, resamples=5000)


def test_bootstrap_extreme_compounding_stays_unavailable_not_infinite():
    report = block_bootstrap([1e100] * 40, resamples=100, block_size=1)
    assert report["available"] is False and "numeric range" in report["reason"]


def test_dependent_bootstrap_is_reproducible_and_labels_conditional_results():
    values = [0.01, -0.009] * 40
    first = block_bootstrap(values, seed=10, resamples=100, block_size=5, evidence_class="TEST")
    assert first == block_bootstrap(values, seed=10, resamples=100, block_size=5, evidence_class="TEST")
    assert first["available"] and first["evidence_class"] == "TEST"
    assert all(math.isfinite(value) for value in first["return_percentiles"].values())
    assert 0 <= first["conditional_loss_fraction"] <= 1


def test_joint_diagnostic_preserves_missing_family_warning():
    report = multiple_testing({"a": [0.01, -0.005] * 40}, attempts=10, resamples=100)
    assert report["complete_family"] is False
    assert report["deflated_sharpe"] is None
    assert 0 <= report["selection_adjusted_diagnostic_pvalues"]["a"] <= 1


def report(net, *, fee=1, spread=5):
    return {"equity_curve": curve([100, 100 * (1 + net)]),
            "execution_model": {"fee_bps": str(fee), "spread_bps": str(spread), "slippage_bps": "2"}}


def test_cost_axis_is_additional_fees_with_fixed_other_execution_assumptions():
    result = cost_sensitivity({0: report(.1), 10: report(.02, fee=11), 20: report(-.01, fee=21)}, evidence_class="TEST")
    assert result["cost_axis"] == "additional_fee_bps_each_side"
    assert result["break_even_bps_bracket"] == [10, 20]
    assert result["execution_assumptions_verified"] is True
    assert result["survives_20bps"] is False
    with pytest.raises(ValueError, match="spread"):
        cost_sensitivity({0: report(.1), 10: report(.02, fee=11, spread=0)}, evidence_class="TEST")
    with pytest.raises(ValueError, match="base fee"):
        cost_sensitivity({0: report(.1), 10: report(.02, fee=10)}, evidence_class="TEST")


def test_empty_or_unavailable_cost_returns_do_not_create_break_even_claims():
    for runs in ({}, {0: {"equity_curve": []}}, {0: report(.1), 20: {"equity_curve": []}}):
        result = cost_sensitivity(runs, evidence_class="TEST")
        assert result["available"] is False and result["break_even_bps_bracket"] is None
    with pytest.raises(ValueError):
        cost_sensitivity({float("nan"): report(.1)}, evidence_class="TEST")


def card(net, baseline_excess):
    return strategy_scorecard(strategy="fixture", metrics={"net_return": net}, costs={"survives_20bps": True},
        robustness={"fragile": False}, monte_carlo={"available": True}, baseline_excess=baseline_excess,
        quality={"alpha_validation_eligible": True}, evidence_class="SYNTHETIC", attempted_variants=10,
        thesis={"hypothesis": "unverified synthetic example"})


def test_scorecard_never_promotes_cash_only_superiority_or_unknown_returns():
    result = card(.1, {"cash": .1, "buy_and_hold": -.1})
    assert result["status"] == result["research_status"] == "EXPERIMENTAL"
    assert any("every declared baseline" in item for item in result["red_team"]["findings"])
    assert result["live_approved"] is False
    assert card(None, {})["research_status"] == "EXPERIMENTAL"
    assert card(-.1, {"cash": -.1})["research_status"] == "REJECTED"
