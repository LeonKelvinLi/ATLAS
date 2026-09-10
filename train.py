"""Walk-forward train-freeze-predict orchestrator.

For each walk-forward fold: fit a model ONLY on the training window, freeze
it, predict the strictly later test window, and emit timestamped signals in
MIDAS BinarySignal JSON form. sklearn's TimeSeriesSplit keeps test windows
after training windows; a `gap` of at least the label horizon prevents
overlapping-label leakage across the boundary.

The decision policy is deterministic expected value, not the model:
  call_ev = p_up * payout - (1 - p_up)
  put_ev  = (1 - p_up) * payout - p_up
A trade is emitted only when EV exceeds `ev_margin`; otherwise `no_trade`.
Historical payouts are not reconstructable, so the payout used is ASSUMED and
recorded in the manifest - results must be read as conditional on it.

Model provenance: every signal carries model_version and a feature hash so
backtest results trace to the exact code that produced them.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from features import CAUSAL_FEATURE_VERSION, FEATURE_COLUMNS, FEATURE_VERSION, build_features

MODEL_VERSIONS = {
    "logreg": "logreg-1.1.0",  # 1.1.0: chronological purged calibration
    "lgbm": "lgbm-1.0.0",
    "ensemble": "ensemble-1.0.0",  # multi-seed LGBM + logreg, probability average
}
MODEL_VERSION = MODEL_VERSIONS["logreg"]  # default model


LGBM_DEFAULTS = {
    # Conservative settings for small, noisy 1-minute FX feature tables:
    # shallow trees, heavy row/column subsampling, high min_child_samples.
    "n_estimators": 300,
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_child_samples": 50,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
}


class ProbabilityAverager:
    """Average the up-probabilities of several base estimators.

    Decorrelated errors average out, so a blend of differently-seeded trees
    plus a linear model is usually better calibrated than any single member.
    Exposes just enough of the sklearn surface (classes_, fit,
    predict_proba) for ChronoCalibratedModel and _prob_up to treat it as one
    estimator."""

    def __init__(self, members: list):
        self.members = members

    def fit(self, X, y):
        for m in self.members:
            m.fit(X, y)
        self.classes_ = self.members[0].classes_
        return self

    def predict_proba(self, X) -> np.ndarray:
        up = np.mean([_prob_up(m, X) for m in self.members], axis=0)
        # Column order must match self.classes_ so _prob_up indexes correctly.
        cols = [1.0 - up if c != 1.0 else up for c in self.classes_]
        return np.column_stack(cols)


def _base_pipeline(model_kind: str = "logreg", model_params: dict | None = None):
    if model_kind == "ensemble":
        from lightgbm import LGBMClassifier

        params = {**LGBM_DEFAULTS, **(model_params or {})}
        members = [
            LGBMClassifier(**params, random_state=seed, verbosity=-1)
            for seed in (0, 1, 2)
        ]
        members.append(
            make_pipeline(
                StandardScaler(), LogisticRegression(C=0.1, max_iter=1000, random_state=0)
            )
        )
        return ProbabilityAverager(members)
    if model_kind == "lgbm":
        from lightgbm import LGBMClassifier

        return LGBMClassifier(
            **{**LGBM_DEFAULTS, **(model_params or {})}, random_state=0, verbosity=-1
        )
    return make_pipeline(
        StandardScaler(), LogisticRegression(C=0.1, max_iter=1000, random_state=0)
    )


def _prob_up(model, X) -> np.ndarray:
    classes = list(model.classes_)
    if 1.0 not in classes:
        return np.zeros(len(X))
    if 0.0 not in classes:
        return np.ones(len(X))
    return model.predict_proba(X)[:, classes.index(1.0)]


def chrono_calibration_splits(
    n_rows: int, n_folds: int = 3, gap: int = 5, min_train: int = 50
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Chronological purged calibration folds.

    Rows are assumed time-ordered. The row range is cut into n_folds + 1
    sequential blocks; fold k trains on every row strictly before block k+1
    minus a `gap` purge (overlapping-label protection) and validates on block
    k+1. Every training index therefore precedes every validation index."""
    for name, value, minimum in (("n_rows", n_rows, 0), ("n_folds", n_folds, 1),
                                 ("gap", gap, 0), ("min_train", min_train, 1)):
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    boundaries = np.linspace(0, n_rows, n_folds + 2, dtype=int)
    splits = []
    for k in range(1, n_folds + 1):
        val_start, val_end = boundaries[k], boundaries[k + 1]
        train_end = max(0, val_start - gap)
        if train_end < min_train or val_end <= val_start:
            continue
        splits.append((np.arange(0, train_end), np.arange(val_start, val_end)))
    return splits


class ChronoCalibratedModel:
    """Regularized logistic regression with chronologically calibrated output.

    Calibration pairs come only from purged, strictly-later validation blocks
    inside the training window (see chrono_calibration_splits), then a sigmoid
    map (logistic regression on the raw score) is fitted to them. The final
    base model is refit on the whole training window. The test window is
    never touched."""

    def __init__(
        self,
        n_folds: int = 3,
        gap: int = 5,
        model_kind: str = "logreg",
        model_params: dict | None = None,
    ):
        self.n_folds = n_folds
        self.gap = gap
        self.model_kind = model_kind
        self.model_params = model_params

    def fit(self, X: pd.DataFrame, y: pd.Series):
        self.constant_ = None
        if len(np.unique(y)) < 2:
            # Degenerate training window: emit the only observed class.
            self.constant_ = float(np.asarray(y)[0])
            return self
        scores, labels = [], []
        for train_idx, val_idx in chrono_calibration_splits(len(X), self.n_folds, self.gap):
            train_labels = y.iloc[train_idx]
            if train_labels.nunique() == 1:
                # An early fold may contain only one class even when the
                # full training window contains both. Use that fold's
                # observed class, just as for a wholly single-class fit;
                # borrowing later rows to fit a classifier would leak.
                fold_scores = np.full(len(val_idx), float(train_labels.iloc[0]))
            else:
                base = _base_pipeline(self.model_kind, self.model_params)
                base.fit(X.iloc[train_idx], train_labels)
                fold_scores = _prob_up(base, X.iloc[val_idx])
            scores.append(fold_scores)
            labels.append(y.iloc[val_idx].to_numpy())

        self.base_ = _base_pipeline(self.model_kind, self.model_params)
        self.base_.fit(X, y)

        self.calibrator_ = None
        if scores:
            raw = np.concatenate(scores).reshape(-1, 1)
            out = np.concatenate(labels)
            if len(np.unique(out)) == 2:
                self.calibrator_ = LogisticRegression(max_iter=1000)
                self.calibrator_.fit(raw, out)
        return self

    def predict_proba_up(self, X: pd.DataFrame) -> np.ndarray:
        if self.constant_ is not None:
            return np.full(len(X), self.constant_)
        raw = _prob_up(self.base_, X)
        if self.calibrator_ is None:
            return raw
        return _prob_up(self.calibrator_, raw.reshape(-1, 1))


def tune_lgbm(X: pd.DataFrame, y: pd.Series, gap: int, n_trials: int) -> dict:
    """Optuna search over LightGBM params, scored ONLY inside the training
    window: each trial is evaluated by mean Brier over the same chronological
    purged calibration splits the model itself uses. The walk-forward test
    window is never seen. Deterministic (seeded TPE sampler)."""
    import optuna
    from lightgbm import LGBMClassifier

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    splits = chrono_calibration_splits(len(X), n_folds=3, gap=gap)
    if not splits or len(np.unique(y)) < 2:
        return {}

    def objective(trial: "optuna.Trial") -> float:
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 100, 500, step=100),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "num_leaves": trial.suggest_int("num_leaves", 7, 63, log=True),
            "min_child_samples": trial.suggest_int("min_child_samples", 20, 200, log=True),
            "subsample": trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-2, 10.0, log=True),
        }
        briers = []
        for train_idx, val_idx in splits:
            model = LGBMClassifier(
                **params, subsample_freq=1, random_state=0, verbosity=-1
            )
            model.fit(X.iloc[train_idx], y.iloc[train_idx])
            p = _prob_up(model, X.iloc[val_idx])
            briers.append(brier_score_loss(y.iloc[val_idx].to_numpy(), p))
        return float(np.mean(briers))

    study = optuna.create_study(
        direction="minimize", sampler=optuna.samplers.TPESampler(seed=0)
    )
    study.optimize(objective, n_trials=n_trials)
    return dict(study.best_params)


def feature_hash(feature_version: str = FEATURE_VERSION) -> str:
    payload = feature_version + "|" + ",".join(FEATURE_COLUMNS)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def decide_action(p_up: float, payout: float, ev_margin: float) -> str:
    """Deterministic EV policy. Returns binary_call / binary_put / no_trade."""
    call_ev = p_up * payout - (1.0 - p_up)
    put_ev = (1.0 - p_up) * payout - p_up
    if call_ev > ev_margin and call_ev >= put_ev:
        return "binary_call"
    if put_ev > ev_margin:
        return "binary_put"
    return "no_trade"


def _iso_utc(epoch: int) -> str:
    return (
        datetime.fromtimestamp(int(epoch), timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def make_signal(
    to_ts: int,
    p_up: float,
    payout: float | None,
    ev_margin: float,
    stake: float,
    expiry_seconds: int,
    fold: int,
    model_version: str = MODEL_VERSION,
    feature_version: str = FEATURE_VERSION,
) -> dict:
    """One MIDAS-compatible BinarySignal JSON object (serde field names).

    payout=None means no causally valid payout was available at signal time:
    the decision is forced to no_trade and tagged payout_unavailable - a
    payout observed after the signal must never be backfilled."""
    if payout is None:
        action, out_payout, tag = "no_trade", 0.0, ",payout_unavailable"
    else:
        action, out_payout, tag = decide_action(p_up, payout, ev_margin), payout, ""
    return {
        "timestamp": _iso_utc(to_ts),
        "action": action,
        "stake": stake,
        "expiry_seconds": expiry_seconds,
        "payout": out_payout,
        "predicted_prob_up": round(float(p_up), 6),
        "model_version": model_version,
        "feature_hash": feature_hash(feature_version),
        "note": f"fold={fold}{tag}",
    }


def walk_forward(
    feature_frame: pd.DataFrame,
    payout,
    n_splits: int = 5,
    gap: int | None = None,
    ev_margin: float = 0.02,
    stake: float = 1.0,
    expiry_seconds: int = 300,
    horizon: int = 5,
    model_kind: str = "logreg",
    tune_trials: int = 0,
    feature_version: str = FEATURE_VERSION,
) -> dict:
    """Run the train-freeze-predict loop. Returns signals + fold metrics.

    `payout` is either a float (ASSUMED constant ratio) or a callable
    (to_ts) -> float | None that returns the latest prospective payout
    snapshot at or before the signal timestamp (None = unavailable ->
    forced no_trade)."""
    # Public research callers cannot bypass the horizon purge or supply a
    # shuffled/pooled frame to this single-series row-based splitter.
    from atlas.validation import validate_chronology, validate_gap
    if feature_version not in {FEATURE_VERSION, CAUSAL_FEATURE_VERSION}:
        raise ValueError("unsupported research feature contract")
    if not feature_frame["feature_version"].eq(feature_version).all():
        raise ValueError("feature frame version must match the explicit research contract")
    gap = validate_gap(horizon, gap)
    validate_chronology(feature_frame["to_ts"].tolist())
    labeled = feature_frame.dropna(subset=["label_up"]).reset_index(drop=True)
    X = labeled[FEATURE_COLUMNS]
    y = labeled["label_up"]
    if not np.isfinite(X.to_numpy(dtype=float)).all() or not y.isin([0.0, 1.0]).all():
        raise ValueError("features must be finite and labels binary")
    model_version = MODEL_VERSIONS[model_kind]
    prospective = callable(payout)
    payout_at = payout if prospective else (lambda ts: payout)

    splitter = TimeSeriesSplit(n_splits=n_splits, gap=gap)
    signals: list[dict] = []
    folds: list[dict] = []
    payout_missing_total = 0

    for fold, (train_idx, test_idx) in enumerate(splitter.split(X)):
        tuned_params = None
        if tune_trials and model_kind == "lgbm":
            tuned_params = tune_lgbm(
                X.iloc[train_idx], y.iloc[train_idx], gap=gap, n_trials=tune_trials
            )
        model = ChronoCalibratedModel(
            n_folds=3, gap=gap, model_kind=model_kind, model_params=tuned_params
        )
        model.fit(X.iloc[train_idx], y.iloc[train_idx])
        # Model is now frozen; only strictly-later rows are predicted.
        p_up = model.predict_proba_up(X.iloc[test_idx])

        fold_signals = [
            make_signal(
                ts, p, payout_at(int(ts)), ev_margin, stake, expiry_seconds, fold,
                model_version=model_version,
                feature_version=feature_version,
            )
            for ts, p in zip(labeled["to_ts"].iloc[test_idx], p_up)
        ]
        signals.extend(fold_signals)

        y_test = y.iloc[test_idx].to_numpy()
        trades = sum(1 for s in fold_signals if s["action"] != "no_trade")
        payout_missing = sum(1 for s in fold_signals if "payout_unavailable" in s["note"])
        payout_missing_total += payout_missing
        folds.append(
            {
                "fold": fold,
                "train_rows": len(train_idx),
                "test_rows": len(test_idx),
                "train_end_ts": int(labeled["to_ts"].iloc[train_idx].max()),
                "test_start_ts": int(labeled["to_ts"].iloc[test_idx].min()),
                "brier": float(brier_score_loss(y_test, p_up)),
                "base_rate_up": float(y_test.mean()),
                "trades": trades,
                "coverage": trades / len(test_idx),
                "payout_missing": payout_missing,
                "tuned_params": tuned_params,
            }
        )

    total_rows = sum(f["test_rows"] for f in folds)
    return {
        "signals": signals,
        "folds": folds,
        "manifest": {
            "model_version": model_version,
            "feature_version": feature_version,
            "feature_hash": feature_hash(feature_version),
            "feature_columns": FEATURE_COLUMNS,
            "payout_source": "prospective" if prospective else "assumed",
            "assumed_payout": None if prospective else payout,
            # Share of evaluated rows with a causally valid payout; MIDAS may
            # only be told --payout-prospective when this is 1.0.
            "payout_coverage": (
                (total_rows - payout_missing_total) / total_rows if total_rows else 0.0
            ),
            "ev_margin": ev_margin,
            "stake": stake,
            "expiry_seconds": expiry_seconds,
            "horizon_bars": horizon,
            "tune_trials": tune_trials,
            "n_splits": n_splits,
            "gap_bars": gap,
            "labeled_rows": len(labeled),
        },
    }


DEFAULT_MIDAS_BIN = Path(__file__).resolve().parent.parent / "MIDAS" / "target" / "release" / "binary-backtest"


def midas_binary_sha256() -> str | None:
    import os

    path = Path(os.environ.get("MIDAS_BIN", DEFAULT_MIDAS_BIN))
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def write_run_bundle(out_root: Path, asset: str, candles_records: list, result: dict) -> Path:
    """Atomically write one immutable run directory.

    Everything a replay needs travels together - candles, signals, folds and
    a manifest whose hashes bind them - so a backtest can never silently pair
    new signals with stale candles. Build in a temp dir, then rename."""
    import os

    from instruments import get_instrument

    manifest = result["manifest"]
    candles_bytes = json.dumps(candles_records).encode()
    signals_bytes = json.dumps(result["signals"], indent=1).encode()
    folds_bytes = json.dumps(result["folds"], indent=2).encode()
    manifest["candles_sha256"] = hashlib.sha256(candles_bytes).hexdigest()
    manifest["signals_sha256"] = hashlib.sha256(signals_bytes).hexdigest()
    manifest["folds_sha256"] = hashlib.sha256(folds_bytes).hexdigest()
    manifest["midas_binary_sha256"] = midas_binary_sha256()
    # The exact contract this model was backtested on. Any future executor
    # must refuse to trade a different expiry/active than what was validated
    # (instruments.verify_contract).
    spec = get_instrument(asset)
    manifest["contract"] = {
        "asset": asset,
        "order_active": spec.order_active,
        "option_kind": spec.option_kind,
        "expiry_seconds": manifest.get("expiry_seconds"),
    }

    run_id = (
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        f"-{asset}-exp{manifest['experiment_id']}"
    )
    asset_root = out_root / asset
    asset_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = asset_root / f".tmp-{run_id}"
    tmp_dir.mkdir()
    (tmp_dir / "candles.json").write_bytes(candles_bytes)
    (tmp_dir / "signals.json").write_bytes(signals_bytes)
    (tmp_dir / "folds.json").write_bytes(folds_bytes)
    (tmp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    final_dir = asset_root / run_id
    os.replace(tmp_dir, final_dir)
    return final_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="EURUSD")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--horizon", type=int, default=5, help="label horizon in bars")
    parser.add_argument("--feature-version", choices=(FEATURE_VERSION, CAUSAL_FEATURE_VERSION),
                        default=FEATURE_VERSION, help="v2 is opt-in new research; never relabel frozen bundles")
    parser.add_argument(
        "--payout-mode",
        choices=("assumed", "prospective"),
        default="assumed",
        help="assumed: constant --payout; prospective: per-signal causal join "
        "to the latest payout snapshot at or before each signal",
    )
    parser.add_argument("--payout", type=float, default=0.85, help="ASSUMED payout ratio")
    parser.add_argument(
        "--payout-max-age", type=int, default=7200,
        help="prospective mode: max snapshot age in seconds (older = unavailable)",
    )
    parser.add_argument("--model", choices=tuple(MODEL_VERSIONS), default="logreg",
                        help="base model: logreg (calibrated linear) or lgbm (LightGBM)")
    parser.add_argument("--tune-trials", type=int, default=0,
                        help="lgbm only: Optuna trials per fold, scored inside the "
                        "training window (0 = use LGBM_DEFAULTS)")
    parser.add_argument("--campaign", default=None,
                        help="research-campaign id (default: <asset>-<model>)")
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--ev-margin", type=float, default=0.02)
    parser.add_argument("--out", default="runs")
    args = parser.parse_args()

    from storage import load_canonical_history, open_db

    conn = open_db()
    # Canonical history: ALL immutable datasets merged and deduplicated, so
    # scheduled rolling collections accumulate instead of replacing.
    candles, history_report = load_canonical_history(conn, args.asset, args.interval)
    if candles.empty:
        raise SystemExit(f"no data for {args.asset}@{args.interval}s - run collector.py first")
    if history_report["conflicts"]:
        raise SystemExit(
            f"canonical history has {len(history_report['conflicts'])} candle "
            f"conflicts - resolve before training: {history_report['conflicts'][:3]}"
        )

    feature_frame = build_features(candles, interval=args.interval, horizon=args.horizon,
                                   feature_version=args.feature_version)

    if args.payout_mode == "prospective":
        from instruments import get_instrument
        from storage import latest_payout_before

        spec = get_instrument(args.asset)
        payout = lambda ts: latest_payout_before(  # noqa: E731
            conn, spec.quote_key, spec.option_kind, ts, args.payout_max_age
        )
    else:
        payout = args.payout

    result = walk_forward(
        feature_frame,
        payout=payout,
        n_splits=args.splits,
        ev_margin=args.ev_margin,
        expiry_seconds=args.horizon * args.interval,
        horizon=args.horizon,
        model_kind=args.model,
        tune_trials=args.tune_trials,
        feature_version=args.feature_version,
    )
    campaign = args.campaign or f"{args.asset.lower()}-{args.model}"
    result["manifest"]["asset"] = args.asset
    result["manifest"]["campaign"] = campaign
    result["manifest"]["dataset_content_sha256"] = history_report["content_sha256"]
    result["manifest"]["datasets_used"] = history_report["datasets_used"]
    result["manifest"]["history_gaps"] = len(history_report["gaps"])

    from experiments import count_variants, record_experiment

    entry = record_experiment(
        campaign=campaign,
        dataset_content_sha256=history_report["content_sha256"],
        parameters={k: v for k, v in result["manifest"].items() if k != "feature_columns"},
        fold_ranges=[
            {"fold": f["fold"], "train_end_ts": f["train_end_ts"], "test_start_ts": f["test_start_ts"]}
            for f in result["folds"]
        ],
        payout_source=result["manifest"]["payout_source"],
        outcome="completed",
    )
    result["manifest"]["experiment_id"] = entry["id"]
    result["manifest"]["variants_attempted"] = count_variants(campaign)

    from storage import frame_to_midas_records

    run_dir = write_run_bundle(Path(args.out), args.asset, frame_to_midas_records(candles), result)

    print(
        json.dumps(
            {"run_dir": str(run_dir), "folds": result["folds"], "manifest": result["manifest"]},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
