"""Versioned feature/label pipeline for binary-option prediction.

Every feature at row t uses only information available at t's close - no
future rows, and higher-timeframe features only use higher-TF bars that have
fully completed by t's close. Labels look forward by construction (that is
what a label is); leakage control between train and test is the walk-forward
orchestrator's job.

The feature set is deliberately small and curated (reviewer decision: do not
auto-generate indicator zoo). Any change to the set or its parameters
requires a FEATURE_VERSION bump so old model results stay reproducible.
"""

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands

FEATURE_VERSION = "1.3.0"  # 1.3.0: + adx, bb_pctb, macd_hist_atr, ret_60
CAUSAL_FEATURE_VERSION = "2.0.0"  # opt-in research; frozen models retain 1.3.0

EMA_FAST = 8
EMA_SLOW = 21
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
BB_WINDOW = 20
VOLUME_WINDOW = 20
VOL_ESTIMATOR_WINDOW = 30  # trailing bars for range-based volatility rates
REGIME_WINDOW = 480  # trailing bars for the volatility-percentile regime
SLOPE_LAG = 3

# Optional research features (build_features(extra_vol=True)). Range-based
# volatility estimators use the whole OHLC bar instead of close-to-close, so
# they are far more efficient per observation (Garman-Klass ~7x vs
# close-to-close; Parkinson/Rogers-Satchell similar family). cs_spread is the
# Corwin-Schultz high-low bid-ask spread proxy - the only microstructure
# signal recoverable without tick or order-book data. Off by default so the
# frozen H2/H3 models keep their exact v1.3.0 input contract.
EXTRA_VOL_COLUMNS = [
    "gk_vol",
    "rs_vol",
    "park_vol",
    "cs_spread",
    "vol_of_vol",
]

# Optional higher-timeframe context (build_features(extra_mtf=True)). Real
# H1/H4 positional context, unlike mtf_align's crude +-1 sign: where price
# sits inside the trailing 1h/4h range, and its distance from the 1h/4h EMA.
# All trailing windows over 1-minute bars - causal by construction.
EXTRA_MTF_COLUMNS = [
    "h1_range_pos",
    "h4_range_pos",
    "h1_ema_dist",
    "h4_ema_dist",
]

# Optional day-of-week block (build_features(extra_dow=True)). Hour-of-day
# is a top-2 feature yet day-of-week is not encoded at all; Friday closes
# and Monday opens are structurally different regimes. Encoded as sin/cos
# over the 7-day week (weekends exist for OTC data).
EXTRA_DOW_COLUMNS = [
    "dow_sin",
    "dow_cos",
]

# Optional HAR-RV block (build_features(extra_har=True)). The heterogeneous
# autoregressive model of realized volatility is the standard workhorse for
# volatility forecasting: realized variance measured over short, medium and
# long windows, because traders operating at different horizons produce a
# term structure. The ratios matter more than the levels here - they say
# whether volatility is currently elevated or suppressed relative to its own
# recent history, which is a regime signal rather than a scale.
# Windows are 15m/1h/4h rather than the classic 1h/1d/1w: 1-minute FX data
# is fragmented into short contiguous segments (median 24 bars in histdata),
# and a 1-day window discards 85% of rows to warmup - a cost no feature can
# repay. 4h retains 75%.
EXTRA_HAR_COLUMNS = [
    "rv_15m",
    "rv_1h",
    "rv_4h",
    "rv_ratio_short",
    "rv_ratio_long",
]

FEATURE_COLUMNS = [
    "ret_1",
    "ret_5",
    "ret_15",
    "ret_60",
    "adx",
    "bb_pctb",
    "macd_hist_atr",
    "ema_spread_atr",
    "ema_fast_slope",
    "rsi",
    "atr_norm",
    "body_ratio",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "vol_rel",
    "mtf_align",
    "hour_sin",
    "hour_cos",
    "session_asia",
    "session_europe",
    "session_us",
    "vol_regime",
]


def _mtf_trend(df: pd.DataFrame, interval: int, factor: int) -> pd.Series:
    """Higher-timeframe EMA trend (+1/-1/0) mapped leak-safely onto base bars.

    A higher-TF bar's trend value becomes available only at rows whose close
    time is >= that bar's close time; incomplete bars are never used."""
    group = df["from_ts"] // (interval * factor)
    agg = (
        df.assign(_group=group)
        .groupby("_group")
        .agg(close=("close", "last"), to_ts=("to_ts", "max"), bars=("close", "size"))
        .reset_index(drop=True)
    )
    complete = agg[agg["bars"] == factor].reset_index(drop=True)
    if len(complete) < EMA_SLOW:
        return pd.Series(np.nan, index=df.index)
    fast = EMAIndicator(complete["close"], EMA_FAST).ema_indicator()
    slow = EMAIndicator(complete["close"], EMA_SLOW).ema_indicator()
    trend = pd.DataFrame(
        {"to_ts": complete["to_ts"], "trend": np.sign(fast - slow)}
    ).dropna()
    merged = pd.merge_asof(
        df[["to_ts"]].reset_index(),
        trend,
        on="to_ts",
        direction="backward",
    ).set_index("index")
    return merged["trend"]


def _range_vol_features(
    out: pd.DataFrame,
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int = VOL_ESTIMATOR_WINDOW,
) -> None:
    """Range-based volatility estimators + Corwin-Schultz spread proxy.

    Every term uses only the current bar's own OHLC and earlier bars, so row
    t stays causal. Estimators are per-bar variances, averaged over a
    trailing window, then square-rooted and divided by close to give
    dimensionless rates comparable across pairs (JPY crosses included)."""
    log_hl = np.log(high / low)
    log_co = np.log(close / open_)

    park_var = log_hl**2 / (4.0 * np.log(2.0))
    gk_var = 0.5 * log_hl**2 - (2.0 * np.log(2.0) - 1.0) * log_co**2
    rs_var = (
        np.log(high / close) * np.log(high / open_)
        + np.log(low / close) * np.log(low / open_)
    )

    def _rate(var: pd.Series) -> pd.Series:
        mean = var.rolling(window).mean().clip(lower=0.0)
        return np.sqrt(mean)

    out["park_vol"] = _rate(park_var)
    out["gk_vol"] = _rate(gk_var)
    out["rs_vol"] = _rate(rs_var)

    # Corwin-Schultz: volatility scales with sqrt(time) but spread does not,
    # so comparing one-bar and two-bar ranges separates them.
    hi2 = high.rolling(2).max()
    lo2 = low.rolling(2).min()
    beta = (log_hl**2).rolling(2).sum()
    gamma = np.log(hi2 / lo2) ** 2
    k = 3.0 - 2.0 * np.sqrt(2.0)
    alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / k - np.sqrt(gamma / k)
    spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    # Negative estimates are noise around a zero spread; the estimator's
    # standard treatment is to floor them at zero.
    out["cs_spread"] = spread.clip(lower=0.0).rolling(window).mean()

    # Volatility-of-volatility: unstable vol regimes are where short-horizon
    # direction models historically degrade.
    gk_rate = out["gk_vol"]
    out["vol_of_vol"] = gk_rate.rolling(window).std() / gk_rate.rolling(window).mean()


def _mtf_context_features(
    out: pd.DataFrame,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    interval: int,
) -> None:
    """H1/H4 positional context from trailing 1-minute windows.

    range_pos: where the close sits inside the trailing window's high-low
    range, in [0, 1] (0.5 when the range is degenerate). ema_dist: relative
    distance from the trailing-window EMA. min_periods full so warmup rows
    stay NaN instead of being computed from a handful of bars."""
    per_hour = max(int(3600 / interval), 1)
    for tag, bars in (("h1", per_hour), ("h4", per_hour * 4)):
        hi = high.rolling(bars, min_periods=bars).max()
        lo = low.rolling(bars, min_periods=bars).min()
        rng = (hi - lo)
        pos = (close - lo) / rng.replace(0.0, np.nan)
        # Degenerate zero range mid-series -> neutral 0.5; warmup rows
        # (rng NaN) stay NaN as documented, instead of fabricating 0.5.
        out[f"{tag}_range_pos"] = pos.mask(rng == 0, 0.5)
        ema = close.ewm(span=bars, min_periods=bars).mean()
        out[f"{tag}_ema_dist"] = (close - ema) / close


def _har_features(out: pd.DataFrame, close: pd.Series, interval: int) -> None:
    """HAR-RV realized-variance term structure, causal by construction.

    Realized variance is the trailing sum of squared log returns, reported as
    an annualisation-free rate (sqrt of mean squared return) so the three
    horizons are directly comparable. Windows are expressed in bars so a
    non-60s interval still means one hour / four hours / one day."""
    per_hour = max(int(3600 / interval), 1)
    log_ret = np.log(close / close.shift(1))
    sq = log_ret**2

    def _rv(bars: int) -> pd.Series:
        # min_periods=bars keeps warmup rows NaN rather than quietly
        # computing a rate from a handful of observations.
        return np.sqrt(sq.rolling(bars, min_periods=bars).mean())

    rv_15m = _rv(max(per_hour // 4, 2))
    rv_1h = _rv(per_hour)
    rv_4h = _rv(per_hour * 4)
    out["rv_15m"] = rv_15m
    out["rv_1h"] = rv_1h
    out["rv_4h"] = rv_4h
    # Term-structure ratios: >1 means short-horizon volatility is running hot
    # relative to the longer window.
    out["rv_ratio_short"] = rv_15m / rv_1h.replace(0.0, np.nan)
    out["rv_ratio_long"] = rv_1h / rv_4h.replace(0.0, np.nan)


def split_contiguous(df: pd.DataFrame, interval: int) -> list[pd.DataFrame]:
    """Split a candle frame into maximal contiguous segments at gap boundaries."""
    df = df.reset_index(drop=True)
    if df.empty:
        return []
    breaks = df.index[df["from_ts"].diff() != interval].tolist()
    breaks.append(len(df))
    return [
        df.iloc[start:end].reset_index(drop=True)
        for start, end in zip(breaks, breaks[1:])
        if end > start
    ]


def build_features(
    df: pd.DataFrame,
    interval: int = 60,
    horizon: int = 5,
    entry_next_open: bool = False,
    extra_vol: bool = False,
    extra_har: bool = False,
    extra_mtf: bool = False,
    extra_dow: bool = False,
    feature_version: str = FEATURE_VERSION,
) -> pd.DataFrame:
    """Compute the curated feature set plus the forward label, gap-aware.

    The frame is split into contiguous segments at every missing-bar boundary
    and each segment is processed independently, so no rolling window, EMA
    state, multi-timeframe trend, or forward label ever crosses a gap.
    Segments too short to survive indicator warmup contribute nothing.

    Input: a validated candle frame (validation.validate_candles), ascending.
    Output columns: from_ts, to_ts, every FEATURE_COLUMNS entry, label_up
    (1.0 price rose over `horizon` bars, 0.0 fell, NaN tie/end-of-data), and
    feature_version. Warmup rows with undefined features are dropped."""
    if feature_version not in {FEATURE_VERSION, CAUSAL_FEATURE_VERSION}:
        raise ValueError("unsupported feature version")
    segments = split_contiguous(df, interval)
    parts = [
        _build_features_segment(
            segment, interval, horizon, entry_next_open, extra_vol, extra_har,
            extra_mtf, extra_dow, feature_version,
        )
        for segment in segments
    ]
    parts = [p for p in parts if not p.empty]
    if not parts:
        extra = (
            (EXTRA_VOL_COLUMNS if extra_vol else [])
            + (EXTRA_HAR_COLUMNS if extra_har else [])
            + (EXTRA_MTF_COLUMNS if extra_mtf else [])
            + (EXTRA_DOW_COLUMNS if extra_dow else [])
        )
        return pd.DataFrame(
            columns=[
                "from_ts", "to_ts", *FEATURE_COLUMNS, *extra,
                "label_up", "feature_version",
            ]
        )
    return pd.concat(parts, ignore_index=True)


def _build_features_segment(
    df: pd.DataFrame,
    interval: int,
    horizon: int,
    entry_next_open: bool = False,
    extra_vol: bool = False,
    extra_har: bool = False,
    extra_mtf: bool = False,
    extra_dow: bool = False,
    feature_version: str = FEATURE_VERSION,
) -> pd.DataFrame:
    # Segments shorter than indicator warmup can't yield any feature row and
    # crash ta's ATR/ADX outright (ADX needs ~2x its window); they
    # contribute nothing.
    if len(df) <= max(ATR_PERIOD, RSI_PERIOD, VOLUME_WINDOW, EMA_SLOW, 2 * ADX_PERIOD):
        return pd.DataFrame()
    df = df.reset_index(drop=True)
    out = pd.DataFrame({"from_ts": df["from_ts"], "to_ts": df["to_ts"]})

    close, high, low, open_ = df["close"], df["high"], df["low"], df["open"]

    out["ret_1"] = close.pct_change(1)
    out["ret_5"] = close.pct_change(5)
    out["ret_15"] = close.pct_change(15)
    out["ret_60"] = close.pct_change(60)

    ema_fast = EMAIndicator(close, EMA_FAST).ema_indicator()
    ema_slow = EMAIndicator(close, EMA_SLOW).ema_indicator()
    atr = AverageTrueRange(high, low, close, ATR_PERIOD).average_true_range()
    atr_safe = atr.replace(0.0, np.nan)
    out["ema_spread_atr"] = (ema_fast - ema_slow) / atr_safe
    out["ema_fast_slope"] = (ema_fast - ema_fast.shift(SLOPE_LAG)) / close
    out["rsi"] = RSIIndicator(close, RSI_PERIOD).rsi()
    out["atr_norm"] = atr / close

    # Trend strength (regime discriminator: trending vs ranging), scaled to ~[0,1].
    out["adx"] = ADXIndicator(high, low, close, ADX_PERIOD).adx() / 100.0
    # Position inside the Bollinger channel (mean-reversion pressure).
    out["bb_pctb"] = BollingerBands(close, BB_WINDOW).bollinger_pband()
    # Momentum acceleration, volatility-normalized like ema_spread_atr.
    out["macd_hist_atr"] = MACD(close).macd_diff() / atr_safe

    bar_range = (high - low).replace(0.0, np.nan)
    out["body_ratio"] = ((close - open_).abs() / bar_range).fillna(0.0)
    out["upper_wick_ratio"] = ((high - np.maximum(close, open_)) / bar_range).fillna(0.0)
    out["lower_wick_ratio"] = ((np.minimum(close, open_) - low) / bar_range).fillna(0.0)

    # Some feeds (IQ Option OTC markets) report volume=0 on every bar; a 0/0
    # vol_rel would NaN out the whole segment, so fall back to a neutral 1.0.
    # A zero rolling mean on an otherwise real volume feed is also mapped to
    # NaN (dropped) rather than division-by-zero inf.
    if feature_version == CAUSAL_FEATURE_VERSION:
        # A future nonzero-volume bar must never change earlier feature rows.
        # No-volume windows are neutral; this is a new research contract.
        vol_mean = df["volume"].rolling(VOLUME_WINDOW).mean()
        out["vol_rel"] = (df["volume"] / vol_mean.replace(0.0, np.nan)).where(
            vol_mean != 0.0, 1.0
        )
    elif df["volume"].gt(0).any():
        vol_mean = df["volume"].rolling(VOLUME_WINDOW).mean().replace(0.0, np.nan)
        out["vol_rel"] = df["volume"] / vol_mean
    else:
        out["vol_rel"] = 1.0

    trend_5 = _mtf_trend(df, interval, 5)
    trend_15 = _mtf_trend(df, interval, 15)
    both = pd.concat([trend_5, trend_15], axis=1)
    out["mtf_align"] = np.where(
        both.isna().any(axis=1),
        np.nan,
        np.where(
            (trend_5 == trend_15) & (trend_5 != 0), trend_5, 0.0
        ),
    )

    seconds_of_day = (out["to_ts"] % 86400).astype(float)
    angle = 2 * np.pi * seconds_of_day / 86400.0
    out["hour_sin"] = np.sin(angle)
    out["hour_cos"] = np.cos(angle)
    hour = (seconds_of_day // 3600).astype(int)
    out["session_asia"] = ((hour >= 0) & (hour < 7)).astype(float)
    out["session_europe"] = ((hour >= 7) & (hour < 13)).astype(float)
    out["session_us"] = ((hour >= 13) & (hour < 21)).astype(float)

    out["vol_regime"] = (
        out["atr_norm"].rolling(REGIME_WINDOW, min_periods=REGIME_WINDOW // 4).rank(pct=True)
    )

    future_close = close.shift(-horizon)
    # entry_next_open: realistic-execution label. The signal fires at t's
    # close but a real order fills at the NEXT bar's open, so the option's
    # strike is open[t+1], not close[t]. Exercise stays at close[t+horizon].
    entry = open_.shift(-1) if entry_next_open else close
    out["label_up"] = np.where(
        future_close.isna() | entry.isna() | (future_close == entry),
        np.nan,
        (future_close > entry).astype(float),
    )

    out["feature_version"] = feature_version
    required = list(FEATURE_COLUMNS)
    if extra_vol:
        _range_vol_features(out, open_, high, low, close)
        required += EXTRA_VOL_COLUMNS
    if extra_har:
        _har_features(out, close, interval)
        required += EXTRA_HAR_COLUMNS
    if extra_mtf:
        _mtf_context_features(out, high, low, close, interval)
        required += EXTRA_MTF_COLUMNS
    if extra_dow:
        # Unix epoch day 0 was a Thursday; weekday = (days + 3) % 7 (Mon=0).
        dow = ((out["to_ts"] // 86400 + 3) % 7).astype(float)
        out["dow_sin"] = np.sin(2 * np.pi * dow / 7.0)
        out["dow_cos"] = np.cos(2 * np.pi * dow / 7.0)
        required += EXTRA_DOW_COLUMNS
    return out.dropna(subset=required).reset_index(drop=True)
