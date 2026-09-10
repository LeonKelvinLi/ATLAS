"""Opt-in v2 feature assembly; never used to reinterpret frozen model bundles."""

import pandas as pd

from features import CAUSAL_FEATURE_VERSION, build_features
from validation import validate_candles


def assemble_cross_asset(frames: dict[str, pd.DataFrame], *, labeled_only=False):
    """Compute contemporaneous FX baskets BEFORE any future-label filter.

    `frames` must contain the entire observed feature stream per instrument,
    including unlabeled/tied rows. Missing contemporaneous data stays absent;
    no forward filling or future availability inference is performed.
    """
    parts = []
    for asset, frame in frames.items():
        if len(asset.replace("-OTC", "")) != 6:
            raise ValueError("cross-asset currency features require FX pairs")
        if not frame["feature_version"].eq(CAUSAL_FEATURE_VERSION).all():
            raise ValueError("research assembly requires causal v2 features")
        if frame["to_ts"].duplicated().any():
            raise ValueError("duplicate timestamps in an instrument")
        parts.append(frame.assign(asset=asset))
    if not parts:
        return pd.DataFrame()
    pooled = pd.concat(parts, ignore_index=True).sort_values("to_ts", kind="stable")
    # The historical formula is retained, but its input selection is corrected.
    from research_pooled import add_cross_asset
    result = add_cross_asset(pooled)
    if labeled_only:
        result = result.dropna(subset=["label_up"])
    return result.reset_index(drop=True)


def build_research_pool(candles_by_asset, *, interval=60, horizon=5, entry_next_open=True):
    """Validate raw candles, build v2 features, then assemble baskets causally."""
    from atlas.validation import validate_chronology, validate_gap
    validate_gap(horizon)
    frames = {}
    for asset, candles in candles_by_asset.items():
        validate_chronology(candles["to_ts"].tolist())
        validated = validate_candles(candles, interval)
        import numpy as np
        if not np.isfinite(validated[["open", "high", "low", "close", "volume"]].to_numpy()).all():
            raise ValueError("research OHLCV must be finite")
        frames[asset] = build_features(
            validated, interval=interval, horizon=horizon,
            entry_next_open=entry_next_open, feature_version=CAUSAL_FEATURE_VERSION,
        )
    return assemble_cross_asset(frames)
