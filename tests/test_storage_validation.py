import sys
from pathlib import Path

import pandas as pd
import pandera.errors
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import (  # noqa: E402
    export_parquet,
    latest_dataset_id,
    load_candles,
    open_db,
    store_dataset,
    store_payout_snapshot,
)
from validation import gap_report, validate_candles  # noqa: E402


def make_candles(n: int = 10, interval: int = 60, start: int = 1_000_000) -> list[dict]:
    return [
        {
            "from_ts": start + i * interval,
            "to_ts": start + (i + 1) * interval,
            "open": 1.10,
            "high": 1.12,
            "low": 1.09,
            "close": 1.11,
            "volume": 5.0,
        }
        for i in range(n)
    ]


@pytest.fixture
def db(tmp_path):
    return open_db(tmp_path / "test.duckdb")


# ---------------- storage ----------------

def test_store_and_load_roundtrip(db):
    candles = make_candles(10)
    dataset_id = store_dataset(db, "EURUSD", 60, candles, [])
    loaded = load_candles(db, dataset_id)
    assert len(loaded) == 10
    assert loaded["from_ts"].tolist() == [c["from_ts"] for c in candles]
    assert loaded["close"].iloc[0] == pytest.approx(1.11)

def test_datasets_are_separate_and_latest_wins(db):
    first = store_dataset(db, "EURUSD", 60, make_candles(5), [])
    second = store_dataset(db, "EURUSD", 60, make_candles(7), [])
    assert first != second
    assert latest_dataset_id(db, "EURUSD", 60) == second
    assert len(load_candles(db, first)) == 5  # first dataset untouched
    assert latest_dataset_id(db, "GBPUSD", 60) is None

def test_gap_metadata_persisted(db):
    gaps = [{"after_ts": 100, "resume_ts": 220, "missing": 1}]
    dataset_id = store_dataset(db, "EURUSD", 60, make_candles(3), gaps)
    row = db.execute("SELECT gap_count, gaps FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    assert row[0] == 1
    assert "220" in row[1]

def test_payout_snapshot_rows(db):
    count = store_payout_snapshot(db, {"EURUSD": {"turbo": 0.85, "binary": 0.80}, "GBPUSD": {"turbo": 0.7}})
    assert count == 3
    rows = db.execute("SELECT asset, kind, payout FROM payout_snapshots ORDER BY asset, kind").fetchall()
    assert ("EURUSD", "turbo", 0.85) in rows

def test_parquet_export(db, tmp_path):
    dataset_id = store_dataset(db, "EURUSD", 60, make_candles(6), [])
    out = export_parquet(db, dataset_id, tmp_path / "out.parquet")
    assert pd.read_parquet(out).shape[0] == 6


# ---------------- validation ----------------

def test_valid_frame_passes():
    frame = pd.DataFrame(make_candles(20))
    assert len(validate_candles(frame, 60)) == 20

def test_wrong_bar_duration_rejected():
    candles = make_candles(5)
    candles[2]["to_ts"] += 30
    with pytest.raises(pandera.errors.SchemaError):
        validate_candles(pd.DataFrame(candles), 60)


@pytest.mark.parametrize("interval", [0, -60])
def test_nonpositive_interval_rejected(interval):
    with pytest.raises(ValueError, match="interval must be positive"):
        validate_candles(pd.DataFrame(make_candles(2)), interval)


@pytest.mark.parametrize("spacing, error", [
    (30, "consecutive bars must not overlap"),
    (90, "bar spacing must be whole multiples"),
])
def test_overlapping_or_misaligned_bar_spacing_rejected(spacing, error):
    candles = make_candles(2)
    candles[1]["from_ts"] = candles[0]["from_ts"] + spacing
    candles[1]["to_ts"] = candles[1]["from_ts"] + 60
    with pytest.raises(pandera.errors.SchemaError, match=error):
        validate_candles(pd.DataFrame(candles), 60)


@pytest.mark.parametrize("spacing", [60, 120, 180])
def test_whole_bar_spacing_allows_any_initial_phase(spacing):
    start = 1_000_001  # Deliberately not a multiple of the 60-second interval.
    candles = make_candles(1, start=start) + make_candles(1, start=start + spacing)
    frame = validate_candles(pd.DataFrame(candles), 60)
    assert len(frame) == 2
    expected = [] if spacing == 60 else [{
        "after_ts": start,
        "resume_ts": start + spacing,
        "missing": spacing // 60 - 1,
    }]
    assert gap_report(frame, 60) == expected


def test_high_below_close_rejected():
    candles = make_candles(5)
    candles[1]["high"] = 1.00  # below open/close/low
    with pytest.raises(pandera.errors.SchemaError):
        validate_candles(pd.DataFrame(candles), 60)

def test_low_above_open_rejected():
    candles = make_candles(5)
    candles[3]["low"] = 1.20
    with pytest.raises(pandera.errors.SchemaError):
        validate_candles(pd.DataFrame(candles), 60)

def test_negative_price_rejected():
    candles = make_candles(5)
    candles[0]["open"] = -1.0
    candles[0]["low"] = -1.0
    with pytest.raises(pandera.errors.SchemaError):
        validate_candles(pd.DataFrame(candles), 60)

def test_duplicate_timestamps_rejected():
    candles = make_candles(5)
    candles[4]["from_ts"] = candles[3]["from_ts"]
    candles[4]["to_ts"] = candles[3]["to_ts"]
    with pytest.raises(pandera.errors.SchemaError):
        validate_candles(pd.DataFrame(candles), 60)

def test_gap_report_flags_missing_bars():
    candles = make_candles(3) + make_candles(3, start=1_000_000 + 6 * 60)
    frame = pd.DataFrame(candles)
    validate_candles(frame, 60)  # gaps are not schema errors
    gaps = gap_report(frame, 60)
    assert len(gaps) == 1
    assert gaps[0]["missing"] == 3
