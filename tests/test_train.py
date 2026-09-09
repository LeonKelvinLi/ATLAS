import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from features import FEATURE_COLUMNS  # noqa: E402
from train import (  # noqa: E402
    ChronoCalibratedModel,
    decide_action,
    feature_hash,
    make_signal,
    walk_forward,
)


def synthetic_features(n: int = 1200, predictive: bool = True, seed: int = 7) -> pd.DataFrame:
    """Feature frame where label_up depends on one feature (plus noise)."""
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        rng.normal(size=(n, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS
    )
    frame.insert(0, "from_ts", 1_000_000 + np.arange(n) * 60)
    frame.insert(1, "to_ts", frame["from_ts"] + 60)
    if predictive:
        prob = 1 / (1 + np.exp(-2.5 * frame["rsi"]))
        frame["label_up"] = (rng.random(n) < prob).astype(float)
    else:
        frame["label_up"] = (rng.random(n) < 0.5).astype(float)
    frame["feature_version"] = "test"
    return frame


# ---------------- decision policy ----------------

def test_ev_policy_thresholds():
    # p=0.5 at 0.85 payout: EV = -0.075 -> no trade.
    assert decide_action(0.50, 0.85, 0.02) == "no_trade"
    # Break-even p ~ 0.5405: still below margin -> no trade.
    assert decide_action(0.545, 0.85, 0.02) == "no_trade"
    # Comfortably above break-even + margin.
    assert decide_action(0.60, 0.85, 0.02) == "binary_call"
    assert decide_action(0.40, 0.85, 0.02) == "binary_put"

def test_ev_margin_zero_matches_break_even():
    just_above = 1 / 1.85 + 0.001
    assert decide_action(just_above, 0.85, 0.0) == "binary_call"
    assert decide_action(1 - just_above, 0.85, 0.0) == "binary_put"


# ---------------- signal schema (must match MIDAS serde) ----------------

def test_signal_matches_midas_binary_signal_schema():
    signal = make_signal(1_784_600_000, 0.61, 0.85, 0.02, 1.0, 300, fold=2)
    assert set(signal) == {
        "timestamp", "action", "stake", "expiry_seconds", "payout",
        "predicted_prob_up", "model_version", "feature_hash", "note",
    }
    assert signal["timestamp"].endswith("Z") and "T" in signal["timestamp"]
    assert signal["action"] in ("binary_call", "binary_put", "no_trade")
    assert signal["stake"] == 1.0
    assert signal["expiry_seconds"] == 300
    assert json.dumps(signal)  # serializable

def test_feature_hash_is_stable():
    assert feature_hash() == feature_hash()
    assert len(feature_hash()) == 16


# ---------------- walk-forward integrity ----------------

@pytest.mark.parametrize("initial_class", [0.0, 1.0])
def test_calibration_handles_single_class_early_fold(initial_class):
    """A one-class prefix must still yield valid calibrated probabilities."""
    frame = synthetic_features(n=400)
    X = frame[FEATURE_COLUMNS]
    y = pd.Series(np.full(400, initial_class))
    y.iloc[120:] = np.arange(280) % 2
    # The first purged fold has 95 same-class training rows, while its
    # later validation block and the full window both contain two classes.
    model = ChronoCalibratedModel().fit(X, y)
    predicted = model.predict_proba_up(X.iloc[-20:])
    assert model.calibrator_ is not None
    assert predicted.shape == (20,)
    assert np.isfinite(predicted).all()
    assert ((predicted >= 0) & (predicted <= 1)).all()


def test_folds_are_temporally_ordered_with_gap():
    result = walk_forward(synthetic_features(), payout=0.85, n_splits=4, horizon=5)
    for fold in result["folds"]:
        # Test data begins strictly after training data plus the gap.
        assert fold["test_start_ts"] > fold["train_end_ts"] + 5 * 60

def test_model_learns_planted_pattern_out_of_sample():
    result = walk_forward(synthetic_features(predictive=True), payout=0.85, n_splits=4)
    briers = [f["brier"] for f in result["folds"]]
    # Strongly predictive synthetic feature -> clearly better than coin-flip 0.25.
    assert np.mean(briers) < 0.22

def test_unpredictable_labels_yield_near_chance_brier_and_low_coverage():
    result = walk_forward(synthetic_features(predictive=False), payout=0.85, n_splits=4)
    briers = [f["brier"] for f in result["folds"]]
    assert 0.23 < np.mean(briers) < 0.30
    # With no edge, the EV policy should mostly abstain.
    total_trades = sum(f["trades"] for f in result["folds"])
    total_rows = sum(f["test_rows"] for f in result["folds"])
    assert total_trades / total_rows < 0.5

def test_signals_cover_every_test_row_including_no_trade():
    result = walk_forward(synthetic_features(), payout=0.85, n_splits=4)
    assert len(result["signals"]) == sum(f["test_rows"] for f in result["folds"])

def test_manifest_declares_assumed_payout():
    result = walk_forward(synthetic_features(), payout=0.85, n_splits=4)
    manifest = result["manifest"]
    assert manifest["payout_source"] == "assumed"
    assert manifest["assumed_payout"] == 0.85
    assert manifest["feature_hash"] == feature_hash()


def test_lgbm_tuned_walk_forward_runs():
    """--tune-trials path: Optuna search stays inside training windows and the
    run completes with tuned params recorded per fold."""
    result = walk_forward(
        synthetic_features(), payout=0.85, n_splits=2, model_kind="lgbm", tune_trials=2
    )
    assert result["manifest"]["model_version"].startswith("lgbm-")
    assert result["manifest"]["tune_trials"] == 2
    for fold in result["folds"]:
        assert fold["tuned_params"] is None or "learning_rate" in fold["tuned_params"]


def test_ensemble_averages_member_probabilities():
    """The averager must expose a single-estimator surface and return the
    mean of its members' up-probabilities in classes_ order."""
    import numpy as np

    from train import ProbabilityAverager, _base_pipeline

    class Stub:
        def __init__(self, p):
            self.p = p
            self.classes_ = np.array([0.0, 1.0])

        def fit(self, X, y):
            return self

        def predict_proba(self, X):
            return np.column_stack([np.full(len(X), 1 - self.p), np.full(len(X), self.p)])

    avg = ProbabilityAverager([Stub(0.2), Stub(0.8)])
    avg.classes_ = np.array([0.0, 1.0])
    proba = avg.predict_proba(np.zeros((3, 2)))
    assert np.allclose(proba[:, 1], 0.5)
    assert np.allclose(proba.sum(axis=1), 1.0)

    assert isinstance(_base_pipeline("ensemble"), ProbabilityAverager)


def test_ensemble_walk_forward_runs():
    result = walk_forward(
        synthetic_features(), payout=0.85, n_splits=2, model_kind="ensemble"
    )
    assert result["manifest"]["model_version"].startswith("ensemble-")
