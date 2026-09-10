"""Offline adversarial contracts; all prices and outcomes here are synthetic."""

import copy
import json
import math

import numpy as np
import pandas as pd
import pytest

from atlas.analytics import compare_baselines, performance_report
from atlas.research_features import assemble_cross_asset, build_research_pool
from atlas.strategies import Baseline, StrategyRegistry
from atlas.validation import chronological_folds, validate_gap
from features import CAUSAL_FEATURE_VERSION, FEATURE_COLUMNS, FEATURE_VERSION, build_features
from tests.test_features import make_frame, wave


def bars(n=140):
    return [{"timestamp": (i + 1) * 60, "open": 100 + i * .1,
             "close": 100 + i * .1 + math.sin(i / 3)} for i in range(n)]


def test_v2_prefix_causal_across_future_volume_change_and_frozen_default():
    frame = make_frame(wave(800))
    frame["volume"] = 0.0
    prefix = frame.iloc[:500].copy()
    frame.loc[550, "volume"] = 1.0
    before = build_features(prefix, feature_version=CAUSAL_FEATURE_VERSION)
    after = build_features(frame, feature_version=CAUSAL_FEATURE_VERSION)
    after = after[after.to_ts <= prefix.to_ts.iloc[-1]]
    assert len(before) > 100
    pd.testing.assert_frame_equal(before[FEATURE_COLUMNS], after[FEATURE_COLUMNS])
    assert before.feature_version.eq("2.0.0").all()
    # Preserve the frozen contract, including its known defect, for old models.
    legacy = build_features(frame)
    assert legacy.feature_version.eq(FEATURE_VERSION).all()
    assert legacy[legacy.to_ts <= prefix.to_ts.iloc[-1]].empty


def test_cross_asset_current_features_do_not_depend_on_future_label_availability():
    def frame(ret, label):
        return pd.DataFrame({"to_ts": [1000], "ret_5": [ret], "label_up": [label],
                             "feature_version": [CAUSAL_FEATURE_VERSION]})
    pool = {"EURUSD": frame(.01, 1.0), "GBPUSD": frame(.2, np.nan)}
    before = assemble_cross_asset(pool, labeled_only=True)
    pool["GBPUSD"].loc[0, "label_up"] = 1.0
    after = assemble_cross_asset(pool, labeled_only=True)
    assert before.iloc[0].xs_quote_str == after.iloc[0].xs_quote_str == -.2
    pool["EURUSD"]["feature_version"] = FEATURE_VERSION
    with pytest.raises(ValueError, match="causal v2"):
        assemble_cross_asset(pool)


def test_new_pool_validates_and_preserves_unlabeled_tail():
    result = build_research_pool({"EURUSD": make_frame(wave(800))}, horizon=5)
    assert result.feature_version.eq(CAUSAL_FEATURE_VERSION).all()
    assert result.label_up.tail(5).isna().all()
    frame = make_frame(wave(800))
    frame.loc[5, "volume"] = float("inf")
    with pytest.raises(ValueError, match="finite"):
        build_research_pool({"EURUSD": frame})


@pytest.mark.parametrize("horizon,gap", [(5, 4), (0, 1), (5, -1), (True, 5), (1, True), (1.5, 2)])
def test_invalid_label_purges_rejected(horizon, gap):
    with pytest.raises(ValueError):
        validate_gap(horizon, gap)


def test_folds_purge_every_training_label_before_test():
    folds = list(chronological_folds(100, train_size=30, test_size=15, horizon=5, gap=5))
    assert len(folds) > 2
    for train, test in folds:
        assert max(train) + 5 < min(test)
    assert all(a[1].stop == b[1].start for a, b in zip(folds, folds[1:]))


@pytest.mark.parametrize("mutation", ["shuffle", "duplicate", "nonfinite", "gap"])
def test_public_walkforward_fails_before_fitting_on_invalid_contract(monkeypatch, mutation):
    import train
    from tests.test_train import synthetic_features
    frame = synthetic_features(60)
    kwargs = {"horizon": 5}
    if mutation == "shuffle":
        frame = frame.iloc[::-1]
    elif mutation == "duplicate":
        frame.loc[1, "to_ts"] = frame.to_ts.iloc[0]
    elif mutation == "nonfinite":
        frame.loc[10, FEATURE_COLUMNS[0]] = np.inf
    else:
        kwargs["gap"] = 4
    monkeypatch.setattr(train.ChronoCalibratedModel, "fit", lambda *a, **kw: pytest.fail("must reject before fit"))
    with pytest.raises(ValueError):
        train.walk_forward(frame, payout=.85, **kwargs)


def test_v2_training_requires_explicit_contract_and_labels_new_manifests(monkeypatch):
    import train
    from tests.test_train import synthetic_features
    frame = synthetic_features(60)
    frame["feature_version"] = CAUSAL_FEATURE_VERSION
    with pytest.raises(ValueError, match="explicit research contract"):
        train.walk_forward(frame, payout=.85)
    monkeypatch.setattr(train.ChronoCalibratedModel, "fit", lambda *a: None)
    monkeypatch.setattr(train.ChronoCalibratedModel, "predict_proba_up", lambda self, X: np.full(len(X), .5))
    result = train.walk_forward(frame, payout=.85, n_splits=2, feature_version=CAUSAL_FEATURE_VERSION)
    assert result["manifest"]["feature_version"] == CAUSAL_FEATURE_VERSION
    assert result["manifest"]["feature_hash"] != train.feature_hash()
    assert {s["feature_hash"] for s in result["signals"]} == {train.feature_hash(CAUSAL_FEATURE_VERSION)}


def test_independent_registry_identity_disable_and_domain_rejection():
    registry = StrategyRegistry()
    rows = bars()
    first = registry.analyze("NVDA", "equity", rows, now=9000)
    assert len(first["signals"]) == 2
    assert first == registry.analyze("NVDA", "equity", rows, now=9000)
    later = registry.analyze("NVDA", "equity", rows, now=9001)
    assert [s["signal_id"] for s in later["signals"]] == [s["signal_id"] for s in first["signals"]]
    for signal in first["signals"]:
        assert signal["confidence"] is None and signal["probability"] is None
        assert signal["live_validated"] is False and len(signal["signal_id"]) == 64
    registry.set_enabled("momentum", False)
    disabled = registry.analyze("NVDA", "equity", rows, now=9000)
    assert [s["strategy_id"] for s in disabled["signals"]] == ["mean_reversion"]
    assert registry.analyze("EURUSD", "fx_binary", rows, now=9000)["signals"] == []
    changed = copy.deepcopy(rows)
    changed[-1]["close"] += 1
    assert registry.analyze("NVDA", "equity", changed, now=9000)["signals"][0]["signal_id"] != disabled["signals"][0]["signal_id"]


@pytest.mark.parametrize("mutation", ["future", "nan", "duplicate", "out_of_order"])
def test_registry_rejects_bad_bars(mutation):
    rows = bars(40)
    if mutation == "future":
        rows[-1]["timestamp"] = 10000
    elif mutation == "nan":
        rows[-1]["close"] = float("nan")
    elif mutation == "duplicate":
        rows[-1]["timestamp"] = rows[-2]["timestamp"]
    else:
        rows.reverse()
    with pytest.raises(ValueError):
        StrategyRegistry().analyze("NVDA", "equity", rows, now=3000)


def test_rule_scores_are_price_distances_not_probabilities():
    rising = [100 + i for i in range(30)]
    assert Baseline("momentum").evaluate(rising)[0] == "buy"
    assert Baseline("mean_reversion").evaluate(rising)[0] == "sell"
    assert Baseline("momentum").evaluate([100] * 30)[0] == "hold"


def test_missing_metrics_stay_unknown_and_costs_not_assumed_zero():
    empty = performance_report([], evidence_kind="live")
    assert empty["trade_count"] == 0
    assert empty["net_pnl"] is None and empty["sharpe"] is None
    incomplete = performance_report([{"gross_pnl": 10}], initial_capital=100)
    assert incomplete["win_rate"] is None and incomplete["total_return"] is None
    assert incomplete["fees"] is None


def test_net_pnl_costs_ratios_and_bankruptcy_are_descriptive_and_exact():
    report = performance_report([
        {"gross_pnl": 20, "fees": 2, "slippage": 1},
        {"net_pnl": -10, "fees": 2, "slippage": 1}], initial_capital=100,
        equity_curve=[{"timestamp": 1, "equity": 100}, {"timestamp": 2, "equity": 117}, {"timestamp": 3, "equity": 107}])
    assert report["net_pnl"] == 7
    assert report["profit_factor"] == 1.7
    assert report["fees"] == 4
    assert report["max_drawdown"] == pytest.approx(10 / 117)
    assert report["total_return"] == pytest.approx(.07)
    assert report["sharpe"] is None
    bankrupt = performance_report([], initial_capital=100, equity_curve=[{"timestamp": 1, "equity": 100}, {"timestamp": 2, "equity": 0}])
    assert bankrupt["max_drawdown"] == 1 and bankrupt["total_return"] == -1


def test_annualized_metrics_require_sufficient_regular_observations_and_explicit_frequency():
    curve = [{"timestamp": i * 60, "equity": 100 + i + math.sin(i)} for i in range(40)]
    report = performance_report([], equity_curve=curve, periods_per_year=252)
    assert report["sharpe"] is not None and math.isfinite(report["sharpe"])
    assert report["annualized_return"] is not None
    assert performance_report([], equity_curve=curve[:10], periods_per_year=252)["sharpe"] is None
    assert performance_report([], equity_curve=curve)["sharpe"] is None
    curve[-1]["timestamp"] += 10
    assert performance_report([], equity_curve=curve, periods_per_year=252)["annualized_return"] is None


def test_baseline_same_window_costs_reconcile_and_never_invent_holdout():
    result = compare_baselines(bars(), fee_bps=5, slippage_bps=10, evidence_kind="synthetic")
    assert result == compare_baselines(bars(), fee_bps=5, slippage_bps=10, evidence_kind="synthetic")
    assert result["fitted_models"] == 0 and result["untouched_holdout"] is False
    windows = set()
    for report in result["reports"].values():
        curve = report["equity_curve"]
        windows.add((curve[0]["timestamp"], curve[-1]["timestamp"], len(curve)))
        if report["net_pnl"] is not None:
            assert curve[-1]["equity"] - 10000 == pytest.approx(report["net_pnl"])
        assert report["evidence_kind"] == "synthetic"
    assert len(windows) == 1
    assert result["reports"]["buy_and_hold"]["trade_count"] == 1
    with pytest.raises(ValueError, match="cannot be labeled"):
        compare_baselines(bars(), fee_bps=0, slippage_bps=0, evidence_kind="live")
    json.dumps(result, allow_nan=False)


def test_next_open_not_signal_close_and_future_prices_do_not_rewrite_earlier_equity():
    rows = bars(100)
    first = 61
    rows[first]["open"] = 1000
    result = compare_baselines(rows, fee_bps=0, slippage_bps=0)
    buyhold = result["reports"]["buy_and_hold"]
    expected = rows[first]["close"] / 1000 * 10000
    assert buyhold["equity_curve"][1]["equity"] == pytest.approx(expected)
    altered = copy.deepcopy(rows)
    altered[-1]["close"] *= 2
    other = compare_baselines(altered, fee_bps=0, slippage_bps=0)
    for key in result["reports"]:
        assert result["reports"][key]["equity_curve"][:-1] == other["reports"][key]["equity_curve"][:-1]
