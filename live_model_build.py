"""Train the hypothesis-#2 pooled model on ALL stored history and freeze it.

Writes models/h2-<utcdate>.pkl containing the fitted ChronoCalibratedModel,
the exact feature column list, and provenance (feature version, row count,
data end timestamp). live_h2_runner.py loads the newest pickle; it never
trains. Retrain deliberately (e.g. weekly) by re-running this script.
"""

import argparse
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

from features import EXTRA_VOL_COLUMNS, FEATURE_COLUMNS, FEATURE_VERSION
from research_pooled import XS_COLUMNS, add_cross_asset, load_pooled
from storage import open_db
from train import ChronoCalibratedModel

HORIZON = 15  # bars; must match FORWARD_TEST.md hypothesis #2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extra-vol", action="store_true",
                        help="hypothesis #4: include the range-volatility block")
    args = parser.parse_args()

    pooled = load_pooled(
        open_db(), interval=60, horizon=HORIZON, entry_next_open=True,
        extra_vol=args.extra_vol,
    )
    pooled = add_cross_asset(pooled)
    feature_cols = list(FEATURE_COLUMNS) + XS_COLUMNS
    if args.extra_vol:
        feature_cols += EXTRA_VOL_COLUMNS

    model = ChronoCalibratedModel(
        n_folds=3, gap=HORIZON * pooled["asset"].nunique(), model_kind="lgbm"
    )
    model.fit(pooled[feature_cols], pooled["label_up"])

    meta = {
        "hypothesis": "h4" if args.extra_vol else "h2",
        "horizon_bars": HORIZON,
        "feature_version": FEATURE_VERSION,
        "feature_columns": feature_cols,
        "rows": len(pooled),
        "assets": sorted(pooled["asset"].unique()),
        "data_end_ts": int(pooled["to_ts"].max()),
        "built_utc": datetime.now(timezone.utc).isoformat(),
    }
    from runtime_paths import MODELS_DIR

    out_dir = MODELS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = out_dir / f"{meta['hypothesis']}-{stamp}.pkl"
    with open(path, "wb") as fh:
        pickle.dump({"model": model, "meta": meta}, fh)
    print(json.dumps({"path": str(path), **{k: v for k, v in meta.items() if k != "feature_columns"}}, indent=2))


if __name__ == "__main__":
    main()
