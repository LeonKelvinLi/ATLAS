"""Tests for the review-correction round: canonical history accumulation,
gap-aware features, chronological calibration, experiment ledger."""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments import count_variants, record_experiment  # noqa: E402
from features import FEATURE_COLUMNS, build_features, split_contiguous  # noqa: E402
from storage import dataset_sha256, load_canonical_history, open_db, store_dataset  # noqa: E402
from train import ChronoCalibratedModel, chrono_calibration_splits  # noqa: E402


def make_candles(n: int, start: int = 1_000_000, interval: int = 60, close: float = 1.11) -> list[dict]:
    return [
        {
            "from_ts": start + i * interval,
            "to_ts": start + (i + 1) * interval,
            "open": 1.10,
            "high": 1.12,
            "low": 1.09,
            "close": close,
            "volume": 5.0,
        }
        for i in range(n)
    ]


# ---------------- 1. canonical history accumulation ----------------

def test_history_merges_across_datasets(tmp_path):
    db = open_db(tmp_path / "m.duckdb")
    store_dataset(db, "EURUSD", 60, make_candles(10, start=1_000_000), [])
    store_dataset(db, "EURUSD", 60, make_candles(10, start=1_000_000 + 10 * 60), [])
    history, report = load_canonical_history(db, "EURUSD", 60)
    assert len(history) == 20  # accumulated, not replaced
    assert report["datasets_used"] == [1, 2]
    assert report["gaps"] == []
    assert report["conflicts"] == []
    assert history["from_ts"].is_monotonic_increasing

def test_history_dedupes_overlap_deterministically(tmp_path):
    db = open_db(tmp_path / "m.duckdb")
    store_dataset(db, "EURUSD", 60, make_candles(10), [])
    store_dataset(db, "EURUSD", 60, make_candles(10, start=1_000_000 + 5 * 60), [])  # 5 overlap
    history, report = load_canonical_history(db, "EURUSD", 60)
    assert len(history) == 15
    assert report["conflicts"] == []  # identical values: overlap is not conflict

def test_history_flags_conflicting_observations(tmp_path):
    db = open_db(tmp_path / "m.duckdb")
    store_dataset(db, "EURUSD", 60, make_candles(5, close=1.11), [])
    store_dataset(db, "EURUSD", 60, make_candles(5, close=1.99), [])  # same bars, different close
    history, report = load_canonical_history(db, "EURUSD", 60)
    assert len(report["conflicts"]) == 5
    assert report["conflicts"][0]["kept_dataset"] == 1  # earliest wins, deterministically
    assert (history["close"] == 1.11).all()

def test_history_reports_gaps(tmp_path):
    db = open_db(tmp_path / "m.duckdb")
    store_dataset(db, "EURUSD", 60, make_candles(5), [])
    store_dataset(db, "EURUSD", 60, make_candles(5, start=1_000_000 + 8 * 60), [])
    _, report = load_canonical_history(db, "EURUSD", 60)
    assert len(report["gaps"]) == 1
    assert report["gaps"][0]["missing"] == 3

def test_content_hash_deterministic_and_order_independent():
    candles = make_candles(8)
    shuffled = [candles[3], candles[0], candles[7], *candles[1:3], *candles[4:7]]
    assert dataset_sha256(candles) == dataset_sha256(shuffled)
    changed = [dict(c) for c in candles]
    changed[0]["close"] += 1e-6
    assert dataset_sha256(candles) != dataset_sha256(changed)

def test_transactional_write_rolls_back_on_failure(tmp_path):
    db = open_db(tmp_path / "m.duckdb")
    bad = make_candles(5)
    bad[2] = {**bad[2], "volume": object()}  # unserializable -> insert fails
    with pytest.raises(Exception):
        store_dataset(db, "EURUSD", 60, bad, [])
    assert db.execute("SELECT count(*) FROM datasets").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM candles").fetchone()[0] == 0


# ---------------- 1b. gap-aware features ----------------

def _wavy(n, start=1_000_000):
    candles = make_candles(n, start=start)
    for i, c in enumerate(candles):
        c["close"] = 1.10 + 0.002 * np.sin(i / 25) + 0.0004 * np.sin(i / 7)
        c["high"] = c["close"] + 0.001
        c["low"] = c["close"] - 0.001
    return candles

def test_split_contiguous_finds_segments():
    frame = pd.DataFrame(_wavy(10) + _wavy(10, start=1_000_000 + 20 * 60))
    segments = split_contiguous(frame, 60)
    assert [len(s) for s in segments] == [10, 10]

def test_features_never_span_gaps():
    seg_a = _wavy(400)
    seg_b = _wavy(400, start=1_000_000 + 500 * 60)  # 100-bar gap
    with_gap = build_features(pd.DataFrame(seg_a + seg_b), horizon=5)
    only_b = build_features(pd.DataFrame(seg_b), horizon=5)
    # Rows in segment B must be identical whether or not segment A exists:
    # nothing (windows, EMA state, labels) crossed the gap.
    b_rows = with_gap[with_gap["from_ts"] >= seg_b[0]["from_ts"]].reset_index(drop=True)
    pd.testing.assert_frame_equal(b_rows, only_b)

def test_short_segments_are_dropped_entirely():
    frame = pd.DataFrame(_wavy(30))  # far below indicator warmup
    assert build_features(frame, horizon=5).empty


# ---------------- 2. chronological calibration ----------------

def test_calibration_training_strictly_precedes_validation():
    for n in (300, 1000, 5000):
        splits = chrono_calibration_splits(n, n_folds=3, gap=5)
        assert splits, n
        for train_idx, val_idx in splits:
            assert train_idx.max() < val_idx.min() - 5 + 1  # purge gap respected
            assert train_idx.min() == 0
            assert (np.diff(val_idx) == 1).all()

def test_chrono_model_calibrates_and_predicts():
    rng = np.random.default_rng(3)
    X = pd.DataFrame(rng.normal(size=(800, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS)
    prob = 1 / (1 + np.exp(-2.5 * X["rsi"]))
    y = pd.Series((rng.random(800) < prob).astype(float))
    model = ChronoCalibratedModel(n_folds=3, gap=5).fit(X, y)
    p = model.predict_proba_up(X)
    assert p.shape == (800,)
    assert ((p >= 0) & (p <= 1)).all()
    assert np.corrcoef(p, y)[0, 1] > 0.2  # learned the planted signal

def test_chrono_model_handles_single_class_training():
    X = pd.DataFrame(np.random.default_rng(0).normal(size=(200, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS)
    y = pd.Series(np.ones(200))
    p = ChronoCalibratedModel().fit(X, y).predict_proba_up(X)
    assert (p == 1.0).all()


# ---------------- 2b. multi-seed noise abstention vs prior baseline ----------------

def test_noise_brier_never_beats_prior_baseline_materially():
    from sklearn.metrics import brier_score_loss

    from train import walk_forward

    for seed in (1, 2, 3):
        rng = np.random.default_rng(seed)
        n = 1200
        frame = pd.DataFrame(rng.normal(size=(n, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS)
        frame.insert(0, "from_ts", 1_000_000 + np.arange(n) * 60)
        frame.insert(1, "to_ts", frame["from_ts"] + 60)
        frame["label_up"] = (rng.random(n) < 0.5).astype(float)
        frame["feature_version"] = "1.3.0"
        result = walk_forward(frame, payout=0.85, n_splits=3)
        for fold in result["folds"]:
            prior_brier = fold["base_rate_up"] * (1 - fold["base_rate_up"])
            # On noise the model must not look meaningfully better than the
            # constant-prior predictor.
            assert fold["brier"] > prior_brier - 0.02, (seed, fold)


# ---------------- 6. experiment ledger ----------------

def test_ledger_appends_and_counts(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    first = record_experiment(
        campaign="test-campaign",
        dataset_content_sha256="abc", parameters={"p": 1}, fold_ranges=[],
        payout_source="assumed", outcome="completed", ledger_path=ledger,
    )
    second = record_experiment(
        campaign="test-campaign",
        dataset_content_sha256="abc", parameters={"p": 2}, fold_ranges=[],
        payout_source="assumed", outcome="rejected", ledger_path=ledger,
    )
    other = record_experiment(
        campaign="test-campaign",
        dataset_content_sha256="zzz", parameters={}, fold_ranges=[],
        payout_source="assumed", outcome="completed", ledger_path=ledger,
    )
    assert (first["id"], second["id"], other["id"]) == (1, 2, 3)
    assert count_variants("test-campaign", ledger) == 3
    lines = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert lines[1]["outcome"] == "rejected"  # rejected variants are retained
    assert all("feature_code_hash" in l for l in lines)
