"""DuckDB market-data storage.

Replaces the SQLite market_data.db. Datasets stay immutable: each collection
run inserts a new tagged dataset row (asset, timeframe, collection time,
source) whose candles are never updated afterwards. DuckDB gives us direct
Parquet export and fast analytical queries for the feature pipeline.

The trade journal (journal.db, SQLite) is unchanged - execution records and
market data are deliberately separate stores.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

DB_PATH = Path(__file__).resolve().parent / "market.duckdb"
SOURCE = "iqoptionapi-websocket"

CANDLE_FIELDS = ("from_ts", "to_ts", "open", "high", "low", "close", "volume")

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS dataset_id_seq;
CREATE TABLE IF NOT EXISTS datasets (
    id BIGINT PRIMARY KEY DEFAULT nextval('dataset_id_seq'),
    asset VARCHAR NOT NULL,
    interval_seconds INTEGER NOT NULL,
    collected_at_utc TIMESTAMPTZ NOT NULL,
    source VARCHAR NOT NULL,
    start_ts BIGINT,
    end_ts BIGINT,
    candle_count INTEGER NOT NULL,
    gap_count INTEGER NOT NULL,
    gaps VARCHAR NOT NULL,
    content_sha256 VARCHAR
);
CREATE TABLE IF NOT EXISTS candles (
    dataset_id BIGINT NOT NULL,
    from_ts BIGINT NOT NULL,
    to_ts BIGINT NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume DOUBLE,
    UNIQUE (dataset_id, from_ts)
);
CREATE SEQUENCE IF NOT EXISTS payout_id_seq;
CREATE TABLE IF NOT EXISTS payout_snapshots (
    id BIGINT PRIMARY KEY DEFAULT nextval('payout_id_seq'),
    ts_utc TIMESTAMPTZ NOT NULL,
    ts_epoch BIGINT NOT NULL,
    asset VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    payout DOUBLE NOT NULL,
    source VARCHAR NOT NULL
);
"""


def open_db(path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(path))
    conn.execute(SCHEMA)
    # Migrate pre-hash databases in place.
    columns = {r[1] for r in conn.execute("PRAGMA table_info('datasets')").fetchall()}
    if "content_sha256" not in columns:
        conn.execute("ALTER TABLE datasets ADD COLUMN content_sha256 VARCHAR")
    return conn


def dataset_sha256(candles: list[dict]) -> str:
    """Deterministic content hash: canonical JSON of sorted candle tuples."""
    canonical = [[c[f] for f in CANDLE_FIELDS] for c in sorted(candles, key=lambda c: c["from_ts"])]
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def store_dataset(
    conn: duckdb.DuckDBPyConnection,
    asset: str,
    interval: int,
    candles: list[dict],
    gaps: list[dict],
    *,
    source: str = SOURCE,
) -> int:
    """Transactionally insert an immutable dataset with its content hash."""
    conn.execute("BEGIN TRANSACTION")
    try:
        dataset_id = conn.execute(
            """INSERT INTO datasets (asset, interval_seconds, collected_at_utc, source,
                   start_ts, end_ts, candle_count, gap_count, gaps, content_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (
                asset,
                interval,
                datetime.now(timezone.utc),
                source,
                candles[0]["from_ts"] if candles else None,
                candles[-1]["to_ts"] if candles else None,
                len(candles),
                len(gaps),
                json.dumps(gaps),
                dataset_sha256(candles),
            ),
        ).fetchone()[0]
        if candles:
            frame = pd.DataFrame(candles)
            frame.insert(0, "dataset_id", dataset_id)
            conn.execute(
                """INSERT INTO candles
                   SELECT dataset_id, from_ts, to_ts, open, high, low, close, volume
                   FROM frame"""
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return dataset_id


def _fields_disagree(a, b) -> bool:
    """NaN-vs-value counts as a disagreement; NaN-vs-NaN does not. The old
    abs(a-b)>eps was always False when either side was NaN, silently merging
    datasets that genuinely disagreed (audit finding)."""
    import math

    a_nan = isinstance(a, float) and math.isnan(a)
    b_nan = isinstance(b, float) and math.isnan(b)
    if a_nan or b_nan:
        return a_nan != b_nan
    return abs(a - b) > 1e-9


def load_canonical_history(
    conn: duckdb.DuckDBPyConnection, asset: str, interval: int
) -> tuple[pd.DataFrame, dict]:
    """Merge ALL immutable datasets for an asset/interval into one canonical,
    deduplicated, ascending history.

    Deterministic resolution: for a timestamp observed by several datasets,
    the earliest-collected dataset (lowest id) wins - first observation is
    closest to the event. Later observations that DISAGREE on OHLCV are
    recorded as conflicts, never silently merged. Gaps are reported so the
    feature pipeline can invalidate windows that would span them.

    Returns (history_frame, report) where report carries datasets_used,
    conflicts, gaps and the content hash of the merged history."""
    rows = conn.execute(
        """SELECT c.dataset_id, c.from_ts, c.to_ts, c.open, c.high, c.low, c.close, c.volume
           FROM candles c JOIN datasets d ON d.id = c.dataset_id
           WHERE d.asset = ? AND d.interval_seconds = ?
           ORDER BY c.from_ts, c.dataset_id""",
        (asset, interval),
    ).df()
    if rows.empty:
        return rows, {"datasets_used": [], "candles": 0, "conflicts": [], "gaps": [], "content_sha256": None}

    conflicts = []
    winners = rows.drop_duplicates(subset="from_ts", keep="first")
    for from_ts, group in rows.groupby("from_ts"):
        if len(group) > 1:
            first = group.iloc[0]
            for _, other in group.iloc[1:].iterrows():
                if any(_fields_disagree(first[f], other[f]) for f in ("open", "high", "low", "close", "volume")):
                    conflicts.append(
                        {
                            "from_ts": int(from_ts),
                            "kept_dataset": int(first.dataset_id),
                            "conflicting_dataset": int(other.dataset_id),
                        }
                    )

    history = winners.drop(columns="dataset_id").reset_index(drop=True)
    history["from_ts"] = history["from_ts"].astype(int)
    history["to_ts"] = history["to_ts"].astype(int)

    deltas = history["from_ts"].diff().dropna()
    gaps = [
        {
            "after_ts": int(history["from_ts"].iloc[i - 1]),
            "resume_ts": int(history["from_ts"].iloc[i]),
            "missing": int((history["from_ts"].iloc[i] - history["from_ts"].iloc[i - 1]) // interval - 1),
        }
        for i in list(deltas[deltas != interval].index)
    ]

    report = {
        "datasets_used": sorted(int(i) for i in rows["dataset_id"].unique()),
        "candles": len(history),
        "conflicts": conflicts,
        "gaps": gaps,
        "content_sha256": dataset_sha256(history.to_dict("records")),
    }
    return history, report


def store_payout_snapshot(conn: duckdb.DuckDBPyConnection, profits: dict) -> int:
    now = datetime.now(timezone.utc)
    rows = [
        (now, int(now.timestamp()), asset, kind, float(payout), SOURCE)
        for asset, kinds in profits.items()
        for kind, payout in kinds.items()
        if isinstance(payout, (int, float))
    ]
    conn.executemany(
        """INSERT INTO payout_snapshots (ts_utc, ts_epoch, asset, kind, payout, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    ) if rows else None
    return len(rows)


def load_candles(conn: duckdb.DuckDBPyConnection, dataset_id: int) -> pd.DataFrame:
    return conn.execute(
        """SELECT from_ts, to_ts, open, high, low, close, volume
           FROM candles WHERE dataset_id = ? ORDER BY from_ts""",
        (dataset_id,),
    ).df()


def latest_dataset_id(
    conn: duckdb.DuckDBPyConnection, asset: str, interval: int
) -> int | None:
    row = conn.execute(
        """SELECT id FROM datasets WHERE asset = ? AND interval_seconds = ?
           ORDER BY collected_at_utc DESC, id DESC LIMIT 1""",
        (asset, interval),
    ).fetchone()
    return row[0] if row else None


def latest_payout_before(
    conn: duckdb.DuckDBPyConnection,
    quote_key: str,
    kind: str,
    ts_epoch: int,
    max_age_seconds: int,
) -> float | None:
    """The most recent payout snapshot at or BEFORE ts_epoch, or None.

    Strictly causal: a snapshot taken after the signal timestamp is never
    used, and a prior snapshot older than max_age_seconds is treated as
    unavailable rather than silently stale."""
    row = conn.execute(
        """SELECT payout, ts_epoch FROM payout_snapshots
           WHERE asset = ? AND kind = ? AND ts_epoch <= ?
           ORDER BY ts_epoch DESC LIMIT 1""",
        (quote_key, kind, int(ts_epoch)),
    ).fetchone()
    if row is None:
        return None
    payout, snapshot_ts = row
    if int(ts_epoch) - int(snapshot_ts) > max_age_seconds:
        return None
    return float(payout)


def frame_to_midas_records(frame: pd.DataFrame) -> list[dict]:
    """Convert a candle frame to MIDAS Candle JSON records (RFC3339 UTC)."""
    return [
        {
            "timestamp": datetime.fromtimestamp(int(row.from_ts), timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
        }
        for row in frame.itertuples()
    ]


def export_midas_candles(
    conn: duckdb.DuckDBPyConnection, dataset_id: int, out_path: Path
) -> Path:
    """Export a dataset as a MIDAS Candle JSON array (RFC3339 timestamps)."""
    frame = load_candles(conn, dataset_id)
    out_path.write_text(json.dumps(frame_to_midas_records(frame)))
    return out_path


def export_parquet(
    conn: duckdb.DuckDBPyConnection, dataset_id: int, out_path: Path
) -> Path:
    # COPY does not support prepared-statement parameters; inline them.
    escaped = str(out_path).replace("'", "''")
    conn.execute(
        f"""COPY (SELECT from_ts, to_ts, open, high, low, close, volume
                  FROM candles WHERE dataset_id = {int(dataset_id)} ORDER BY from_ts)
            TO '{escaped}' (FORMAT PARQUET)"""
    )
    return out_path
