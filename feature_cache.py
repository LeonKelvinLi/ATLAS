"""Content-addressed feature cache.

Rebuilding features for 39 instruments takes minutes (indicator warmup,
per-segment splitting, cross-asset pivot), and the POWER RULE means the
preflight count wants running repeatedly. build_features is deterministic
given its inputs, so the result is cacheable.

SAFETY IS THE WHOLE DESIGN. A research pipeline silently reading stale
features would be a correctness disaster far worse than the time it saves, so
the key is a SHA-256 over the actual candle values plus every parameter that
changes the output - including FEATURE_VERSION. Nothing time-based, nothing
heuristic: if a single candle differs by one tick, the key differs and the
frame is rebuilt. A stale hit is therefore not possible, only a miss.

Cache lives in cache/features/*.parquet (gitignored) and is disposable: delete
it any time, the only cost is one slow run.
"""

import hashlib
import shutil

import pandas as pd

from runtime_paths import STATE_DIR

CACHE_DIR = STATE_DIR / "cache" / "features"


def signature(candles: pd.DataFrame, asset: str, interval: int, horizon: int,
              **flags) -> str:
    """SHA-256 over the candle CONTENT plus every output-affecting parameter.

    Uses pandas' row hashing over the OHLC columns, so the digest changes if
    any value changes - not merely if the row count or latest timestamp does.
    Pure - unit-tested."""
    from features import FEATURE_VERSION

    cols = [c for c in ("from_ts", "to_ts", "open", "high", "low", "close",
                        "volume") if c in candles.columns]
    if candles.empty:
        content = b"empty"
    else:
        row_hashes = pd.util.hash_pandas_object(candles[cols], index=False)
        content = row_hashes.values.tobytes()

    h = hashlib.sha256()
    h.update(content)
    # Parameters, canonically ordered so the digest is stable across runs.
    params = [f"asset={asset}", f"interval={interval}", f"horizon={horizon}",
              f"feature_version={FEATURE_VERSION}", f"rows={len(candles)}",
              f"cols={','.join(cols)}"]
    params += [f"{k}={flags[k]}" for k in sorted(flags)]
    h.update("|".join(params).encode())
    return h.hexdigest()


def cached_build(candles: pd.DataFrame, asset: str, interval: int,
                 horizon: int, use_cache: bool = True, **flags) -> pd.DataFrame:
    """build_features(...) with a content-addressed cache.

    On a miss the frame is written atomically (temp + replace) so an
    interrupted run cannot leave a truncated parquet that a later run would
    read as valid."""
    from features import build_features

    if not use_cache:
        return build_features(candles, interval=interval, horizon=horizon,
                              **flags)

    key = signature(candles, asset, interval, horizon, **flags)
    path = CACHE_DIR / f"{key}.parquet"
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception:
            # Unreadable cache file is treated as a miss, never as an error.
            try:
                path.unlink()
            except OSError:
                pass

    frame = build_features(candles, interval=interval, horizon=horizon,
                           **flags)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        frame.to_parquet(tmp, index=False)
        tmp.replace(path)
    except Exception:
        pass  # caching is an optimisation; never fail the caller over it
    return frame


def cache_stats() -> dict:
    if not CACHE_DIR.exists():
        return {"files": 0, "bytes": 0}
    files = list(CACHE_DIR.glob("*.parquet"))
    return {"files": len(files),
            "bytes": sum(f.stat().st_size for f in files),
            "path": str(CACHE_DIR)}


def clear_cache() -> int:
    """Delete the cache. Returns files removed. Safe: it is pure derived data."""
    if not CACHE_DIR.exists():
        return 0
    n = len(list(CACHE_DIR.glob("*.parquet")))
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    return n


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()
    if args.clear:
        print(f"removed {clear_cache()} cached frame(s)")
    else:
        print(json.dumps(cache_stats(), indent=2))
