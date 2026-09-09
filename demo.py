"""Run ATLAS on generated candles, entirely offline.

This exercises engineering behavior, not a market hypothesis. It never loads
credentials, production databases, model bundles, or the experiment registry.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from features import FEATURE_COLUMNS, build_features
from storage import load_candles, open_db, store_dataset
from train import walk_forward
from validation import gap_report, validate_candles

SOURCE = "synthetic-offline-demo"
INTERVAL = 60
HORIZON = 5
DEFAULT_ROWS = 2400
DEFAULT_SEED = 42
OUTPUT_FILES = {"report.json", "report.md", "candles.parquet"}


def synthetic_candles(rows: int = DEFAULT_ROWS, seed: int = DEFAULT_SEED) -> pd.DataFrame:
    """Seeded random-walk OHLCV; no planted predictive signal or market data."""
    if rows < 1200:
        raise ValueError("use at least 1200 rows for indicator warmup and three folds")
    rng = np.random.default_rng(seed)
    prices = 1.1 * np.exp(np.r_[0.0, rng.normal(0.0, 0.00015, rows).cumsum()])
    opening, closing = prices[:-1], prices[1:]
    wick = rng.uniform(0.00001, 0.00015, rows)
    start = 1_735_689_600 + np.arange(rows, dtype=np.int64) * INTERVAL
    return pd.DataFrame({
        "from_ts": start,
        "to_ts": start + INTERVAL,
        "open": opening,
        "high": np.maximum(opening, closing) + wick,
        "low": np.minimum(opening, closing) - wick,
        "close": closing,
        "volume": rng.uniform(1.0, 100.0, rows),
    })


def build_report(rows: int = DEFAULT_ROWS, seed: int = DEFAULT_SEED) -> tuple[dict, pd.DataFrame]:
    """Validate, round-trip through isolated DuckDB, featurize, and predict."""
    candles = validate_candles(synthetic_candles(rows, seed), INTERVAL)
    gaps = gap_report(candles, INTERVAL)
    conn = open_db(Path(":memory:"))
    try:
        dataset_id = store_dataset(
            conn, "SYNTHETIC", INTERVAL, candles.to_dict("records"), gaps,
            source=SOURCE,
        )
        restored = load_candles(conn, dataset_id)
        metadata = conn.execute(
            "SELECT source, content_sha256 FROM datasets WHERE id = ?", [dataset_id]
        ).fetchone()
    finally:
        conn.close()

    pd.testing.assert_frame_equal(candles, restored)
    features = build_features(restored, interval=INTERVAL, horizon=HORIZON)
    result = walk_forward(
        features, payout=0.85, n_splits=3, horizon=HORIZON,
        expiry_seconds=HORIZON * INTERVAL, stake=0.0,
    )
    folds = result["folds"]
    checks = {
        "storage_round_trip": True,
        "no_missing_bars": not gaps,
        "finite_features": bool(np.isfinite(features[FEATURE_COLUMNS].to_numpy()).all()),
        "purged_fold_boundaries": all(
            f["test_start_ts"] > f["train_end_ts"] + HORIZON * INTERVAL
            for f in folds
        ),
        "prediction_per_test_row": len(result["signals"]) == sum(f["test_rows"] for f in folds),
    }
    if not all(checks.values()):
        raise RuntimeError(f"offline demo integrity failure: {checks}")

    report = {
        "schema_version": 1,
        "data_source": metadata[0],
        "purpose": "Engineering smoke test. Synthetic results are not evidence of a trading edge.",
        "seed": seed,
        "candle_rows": len(candles),
        "feature_rows": len(features),
        "feature_count": len(FEATURE_COLUMNS),
        "dataset_sha256": metadata[1],
        "checks": checks,
        "manifest": result["manifest"],
        "folds": folds,
        "simulated_decision_counts": dict(sorted(Counter(s["action"] for s in result["signals"]).items())),
    }
    return report, candles


def report_markdown(report: dict) -> str:
    lines = [
        "# ATLAS offline demo", "",
        "Generated candles → validation → in-memory DuckDB → causal features → purged walk-forward predictions.",
        "", f"**{report['purpose']}**", "",
        f"Seed {report['seed']} · {report['candle_rows']:,} candles · "
        f"{report['feature_rows']:,} feature rows · {report['feature_count']} features.", "",
        "## Integrity checks", "",
        *[f"- {'PASS' if passed else 'FAIL'}: {name.replace('_', ' ')}" for name, passed in report["checks"].items()],
        "", "## Synthetic fold diagnostics", "",
        "Brier score measures probability error (lower is better). These scores only describe the generated fixture.",
        "", "| Fold | Training rows | Test rows | Brier score | Simulated decisions |",
        "|---|---:|---:|---:|---:|",
        *[f"| {f['fold'] + 1} | {f['train_rows']} | {f['test_rows']} | {f['brier']:.4f} | {f['trades']} |" for f in report["folds"]],
        "", "The payout is assumed at 0.85. Stake is zero; no orders are submitted.",
        "", "`report.json` contains the configuration, fold timestamps, and dataset hash. "
        "`candles.parquet` contains only the generated fixture.", "",
    ]
    return "\n".join(lines)


def run_demo(out: Path, rows: int = DEFAULT_ROWS, seed: int = DEFAULT_SEED) -> dict:
    """Write demo artifacts; refuse to overwrite an unrelated directory."""
    if out.exists():
        if not out.is_dir() or out.is_symlink():
            raise ValueError("output must be a regular directory")
        if any(p.name not in OUTPUT_FILES or p.is_symlink() or not p.is_file() for p in out.iterdir()):
            raise ValueError("output contains unrelated files; choose a new directory with --out")
        if any(out.iterdir()):
            marker = out / "report.json"
            if not marker.exists() or json.loads(marker.read_text())["data_source"] != SOURCE:
                raise ValueError("output is not an ATLAS offline demo; choose a new directory with --out")
    report, candles = build_report(rows, seed)
    out.mkdir(parents=True, exist_ok=True)
    candles.to_parquet(out / "candles.parquet", index=False)
    (out / "report.md").write_text(report_markdown(report), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("output/offline-demo"))
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    try:
        report = run_demo(args.out, args.rows, args.seed)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"demo: {exc}\n")
    print(f"PASS: {len(report['checks'])} integrity checks; {report['candle_rows']} synthetic candles.")
    print("Engineering demo only — synthetic results do not establish trading performance.")
    print(f"Report: {(args.out / 'report.md').resolve()}")
    print(f"JSON:   {(args.out / 'report.json').resolve()}")


if __name__ == "__main__":
    main()
