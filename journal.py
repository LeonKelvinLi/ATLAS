"""SQLite trade journal.

Append-only from the strategy's point of view: the analyzer never reads the
journal, so past results cannot influence future signals. Full candle inputs
are stored so any run can be replayed through analyzer.decide() byte-for-byte.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import STATE_DIR

DB_PATH = STATE_DIR / "journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    asset TEXT NOT NULL,
    signal TEXT NOT NULL,
    reasons TEXT NOT NULL,
    metrics TEXT NOT NULL,
    payout REAL,
    market_open INTEGER,
    balance_mode TEXT,
    candles TEXT NOT NULL,
    order_id INTEGER,
    amount REAL,
    duration_minutes INTEGER,
    result TEXT,
    profit REAL,
    balance_after REAL
)
"""


def open_journal(path: Path = DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def record_run(
    conn: sqlite3.Connection,
    *,
    strategy_version: str,
    asset: str,
    signal: str,
    reasons: list,
    metrics: dict,
    payout,
    market_open: bool,
    balance_mode: str,
    candles: dict,
    order_id=None,
    amount=None,
    duration_minutes=None,
    result=None,
    profit=None,
    balance_after=None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO runs (ts_utc, strategy_version, asset, signal, reasons,
               metrics, payout, market_open, balance_mode, candles, order_id,
               amount, duration_minutes, result, profit, balance_after)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            strategy_version,
            asset,
            signal,
            json.dumps(reasons),
            json.dumps(metrics),
            payout,
            int(bool(market_open)),
            balance_mode,
            json.dumps(candles),
            order_id,
            amount,
            duration_minutes,
            result,
            profit,
            balance_after,
        ),
    )
    conn.commit()
    return cursor.lastrowid
