"""Pandera validation for candle data.

Every dataset must pass this schema before it can be stored or used by the
feature pipeline: positive prices, OHLC consistency (high is the bar maximum,
low the minimum), exact bar duration, strictly increasing non-overlapping
timestamps, and spacing in whole bars. The initial timestamp may have any
phase. Missing bars are reported separately (collector.find_gaps) - a gap is
data to be acknowledged, not an error.
"""

import pandas as pd
import pandera.pandas as pa

from collector import find_gaps


def candle_schema(interval: int) -> pa.DataFrameSchema:
    if interval <= 0:
        raise ValueError("interval must be positive")
    return pa.DataFrameSchema(
        columns={
            "from_ts": pa.Column(int, pa.Check.ge(0)),
            "to_ts": pa.Column(int, pa.Check.ge(0)),
            "open": pa.Column(float, pa.Check.gt(0)),
            "high": pa.Column(float, pa.Check.gt(0)),
            "low": pa.Column(float, pa.Check.gt(0)),
            "close": pa.Column(float, pa.Check.gt(0)),
            "volume": pa.Column(float, pa.Check.ge(0), nullable=True),
        },
        checks=[
            pa.Check(
                lambda df: df["to_ts"] - df["from_ts"] == interval,
                error=f"bar duration must be exactly {interval}s",
            ),
            pa.Check(
                lambda df: (df["high"] >= df[["open", "close", "low"]].max(axis=1)),
                error="high must be the bar maximum",
            ),
            pa.Check(
                lambda df: (df["low"] <= df[["open", "close", "high"]].min(axis=1)),
                error="low must be the bar minimum",
            ),
            pa.Check(
                lambda df: df["from_ts"].is_monotonic_increasing
                and df["from_ts"].is_unique,
                error="timestamps must be strictly increasing and unique",
            ),
            pa.Check(
                lambda df: df["from_ts"].diff().dropna().ge(interval).all(),
                error="consecutive bars must not overlap",
            ),
            pa.Check(
                lambda df: df["from_ts"].diff().dropna().mod(interval).eq(0).all(),
                error=f"bar spacing must be whole multiples of {interval}s",
            ),
        ],
        strict=True,
        ordered=True,
    )


def validate_candles(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """Raise SchemaError for invalid candles or ValueError for interval <= 0."""
    return candle_schema(interval).validate(df, lazy=False)


def gap_report(df: pd.DataFrame, interval: int) -> list[dict]:
    """Missing-bar ranges for a validated frame (delegates to collector)."""
    return find_gaps(df.to_dict("records"), interval)
