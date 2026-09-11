"""Bounded, causal long/flat hypotheses; no execution, sizing or profitability claims.

Equity symbols are ETFs: GLD and USO are proxies, never futures contracts.
Input event_time is the end of a completed bar. Later published revisions
change only later feature snapshots; labels and unrecognized source fields
are never copied into features or decisions.
"""

from collections import defaultdict
from bisect import insort
from datetime import datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
import statistics
from zoneinfo import ZoneInfo

from atlas.domain import decimal, financial_context
from atlas.research.market import identifier, timestamp

VERSION = "multiasset-v1"
MAX_BARS = 200_000
FAMILIES = {
    "equity_mean_reversion": ("zscore", "vwap", "bollinger", "rsi", "ema", "atr_deviation", "opening_range"),
    "btc_breakout": ("donchian", "prior_period", "consolidation", "atr_distance"),
    "commodity_proxy_trend": ("momentum", "moving_average", "donchian", "regression", "pullback"),
}
SYMBOLS = {"equity_mean_reversion": ("SPY", "QQQ"), "btc_breakout": ("BTC-USD",),
           "commodity_proxy_trend": ("GLD", "USO")}
TIMEFRAMES = {"equity_mean_reversion": (900,), "btc_breakout": (900, 1800, 3600, 7200, 14400),
              "commodity_proxy_trend": (3600, 7200, 14400, 28800, 86400)}
SPEC_KEYS = {"strategy_id", "strategy_version", "family", "variant", "symbols", "timeframe_seconds", "lookback",
             "z_threshold", "atr_multiple", "volume_multiplier", "rsi_threshold", "opening_range_bars"}
NY = ZoneInfo("America/New_York")


class StrategyError(ValueError):
    pass


class StrategyUnavailable(StrategyError):
    """A requested timeframe cannot honestly be formed from these sessions."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def normalize_spec(spec):
    if not isinstance(spec, dict) or set(spec) - SPEC_KEYS:
        raise StrategyError("strategy_spec_unknown_fields")
    if set(spec) == {"strategy_id"}:
        found = next((row for row in strategy_catalog() if row["strategy_id"] == spec["strategy_id"]), None)
        if found is None:
            raise StrategyError("strategy_id_unknown")
        return found
    family = spec.get("family")
    if family not in FAMILIES or spec.get("variant") not in FAMILIES[family]:
        raise StrategyError("strategy_family_or_variant_unknown")
    result = {"family": family, "variant": spec["variant"], "symbols": list(SYMBOLS[family]),
              "timeframe_seconds": TIMEFRAMES[family][0], "lookback": 20, "z_threshold": "2",
              "atr_multiple": "2", "volume_multiplier": "1.2", "rsi_threshold": "30", "opening_range_bars": 2,
              "strategy_version": VERSION, **spec}
    if (result["strategy_version"] != VERSION or not isinstance(result["symbols"], list) or not result["symbols"]
            or any(not isinstance(symbol, str) for symbol in result["symbols"])
            or len(set(result["symbols"])) != len(result["symbols"]) or not set(result["symbols"]) <= set(SYMBOLS[family])
            or type(result["timeframe_seconds"]) is not int or result["timeframe_seconds"] not in TIMEFRAMES[family]
            or type(result["lookback"]) is not int or result["lookback"] not in {5, 10, 14, 20, 40, 60}
            or type(result["opening_range_bars"]) is not int or result["opening_range_bars"] not in {1, 2, 3, 4}):
        raise StrategyError("strategy_parameters_out_of_bounds")
    for name, choices in (("z_threshold", {"1.5", "2", "2.5", "3"}), ("atr_multiple", {"1", "1.5", "2", "3"}),
                          ("volume_multiplier", {"1", "1.2", "1.5", "2"}), ("rsi_threshold", {"20", "25", "30", "35"})):
        value = result[name]
        if not isinstance(value, str):
            raise StrategyError("strategy_parameters_require_decimal_strings")
        try:
            result[name] = format(decimal(value, positive=True), "f")
        except ValueError:
            raise StrategyError("strategy_parameters_out_of_bounds") from None
        if result[name] not in choices:
            raise StrategyError("strategy_parameters_out_of_bounds")
    result["symbols"] = sorted(result["symbols"])
    supplied_id = result.pop("strategy_id", None)
    stable_id = family + "/" + result["variant"] + "/" + _hash(result)[:16]
    if supplied_id is not None and supplied_id != stable_id:
        raise StrategyError("strategy_id_parameter_mismatch")
    return {"strategy_id": stable_id, **result}


def strategy_catalog():
    result = []
    for variant in FAMILIES["equity_mean_reversion"]:
        for lookback in (10, 20):
            result.append(normalize_spec({"family": "equity_mean_reversion", "variant": variant, "lookback": lookback}))
    for family in ("btc_breakout", "commodity_proxy_trend"):
        for variant in FAMILIES[family]:
            for frame in TIMEFRAMES[family]:
                result.append(normalize_spec({"family": family, "variant": variant, "timeframe_seconds": frame}))
    return result


def _calendar_index(calendar):
    if calendar is None:
        return None
    if (not isinstance(calendar, dict) or not isinstance(calendar.get("sessions"), list)
            or not 1 <= len(calendar["sessions"]) <= 10000):
        raise StrategyError("strategy_explicit_calendar_invalid")
    result, previous = {}, ""
    for session in calendar["sessions"]:
        try:
            day = session["session_date"]
            standard_open, standard_close = _session_bounds(day)
            opening, closing = timestamp(session["open_time"]), timestamp(session["close_time"])
            early_close = standard_close - 3 * 3600
            if day <= previous or opening != standard_open or closing not in {standard_close, early_close}:
                raise ValueError
            result[day] = (opening, closing)
            previous = day
        except (ValueError, TypeError, KeyError):
            raise StrategyError("strategy_explicit_calendar_invalid") from None
    return result


def _session_bounds(day, calendar_index=None):
    if calendar_index is not None:
        if day not in calendar_index:
            raise StrategyError("strategy_session_missing_from_explicit_calendar")
        return calendar_index[day]
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").date()
        if parsed.isoformat() != day or parsed.weekday() >= 5:
            raise ValueError
        opening = int(datetime.combine(parsed, time(9, 30), NY).timestamp())
        closing = int(datetime.combine(parsed, time(16), NY).timestamp())
        return opening, closing
    except (ValueError, TypeError, OverflowError):
        raise StrategyError("equity_regular_session_date_invalid") from None


@financial_context
def _bars(bars, calendar_index=None):
    if not isinstance(bars, list) or len(bars) > MAX_BARS:
        raise StrategyError("strategy_bars_require_bounded_list")
    result, identities, latest, contracts = [], set(), {}, {}
    for raw in bars:
        try:
            symbol = identifier(raw["symbol"], "symbol")
            event = timestamp(raw["event_time"])
            available = timestamp(raw["information_available_time"])
            interval = raw["interval_seconds"]
            revision = raw.get("revision", 0)
            ingested = timestamp(raw.get("ingestion_time", available))
            asset = raw["asset_class"]
            currency = raw.get("currency", "unknown")
            volume_unit = raw.get("volume_unit", "unknown")
            day = raw["session_date"]
            if (asset not in {"equity", "crypto"} or type(interval) is not int or not 1 <= interval <= 86400
                    or event < interval or available < event or ingested < available or type(revision) is not int or revision < 0):
                raise ValueError
            if (not isinstance(currency, str) or not (currency == "unknown" or re.fullmatch(r"[A-Z]{3}", currency))
                    or volume_unit not in ({"shares", "unknown"} if asset == "equity" else {"base_asset", "unknown"})):
                raise ValueError
            prices = {key: decimal(raw[key], positive=key != "volume", nonnegative=True) for key in ("open", "high", "low", "close", "volume")}
            if not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
                raise ValueError
            if asset == "equity":
                opening, closing = _session_bounds(day, calendar_index)
                if (raw.get("session", "regular") != "regular" or not opening <= event - interval < event <= closing
                        or (event - opening) % interval):
                    raise ValueError
            elif (raw.get("session", "24/7") != "24/7" or event % interval
                  or day != datetime.fromtimestamp(event - 1, timezone.utc).date().isoformat()):
                raise ValueError
            if (symbol, event, revision) in identities:
                raise ValueError
            identities.add((symbol, event, revision))
            contract = (asset, interval, identifier(raw.get("venue", "unspecified"), "venue"), currency, volume_unit)
            if symbol in contracts and contracts[symbol] != contract:
                raise ValueError
            contracts[symbol] = contract
            result.append({"symbol": symbol, "asset_class": asset, "event_time": event,
                           "information_available_time": available, "interval_seconds": interval,
                           "session_date": day, "session": "regular" if asset == "equity" else "24/7",
                           "revision": revision, "venue": contract[2],
                           "currency": currency, "volume_unit": volume_unit,
                           "source_id": identifier(raw.get("source_id", "unspecified"), "source_id"),
                           "ingestion_time": ingested,
                           **{key: format(value, "f") for key, value in prices.items()}})
        except (ValueError, TypeError, KeyError, ArithmeticError):
            raise StrategyError("strategy_bar_contract_invalid") from None
    result.sort(key=lambda row: (row["information_available_time"], row["event_time"], row["symbol"], row["revision"]))
    for row in result:
        key = (row["symbol"], row["event_time"])
        previous = latest.get(key)
        if ((previous is None and row["revision"] != 0) or (previous is not None and (
                row["revision"] != previous["revision"] + 1
                or row["information_available_time"] <= previous["information_available_time"]
                or row["session_date"] != previous["session_date"]))):
            raise StrategyError("strategy_revision_order_invalid")
        latest[key] = row
    return result


def _bucket(row, frame, calendar_index=None):
    event, interval = row["event_time"], row["interval_seconds"]
    if row["asset_class"] == "equity":
        opening, closing = _session_bounds(row["session_date"], calendar_index)
        if frame == 28800:
            raise StrategyUnavailable("equity_8h_exceeds_regular_session")
        width = closing - opening if frame == 86400 else frame
        if width > closing - opening:
            return None  # A short session has no complete bucket at this frame.
        start = opening if frame == 86400 else opening + ((event - opening - 1) // frame) * frame
        if start + width > closing:
            return None
    else:
        width = frame
        start = ((event - 1) // frame) * frame
    if width % interval or interval > width:
        raise StrategyUnavailable("source_interval_cannot_form_requested_timeframe")
    if event - interval < start or event > start + width:
        raise StrategyError("source_bar_crosses_aggregation_boundary")
    return row["symbol"], start, start + width, width


@financial_context
def aggregate_bars(bars, timeframe_seconds, *, calendar=None):
    if type(timeframe_seconds) is not int or timeframe_seconds not in {900, 1800, 3600, 7200, 14400, 28800, 86400}:
        raise StrategyError("aggregation_timeframe_unsupported")
    if not isinstance(bars, list) or len(bars) > MAX_BARS:
        raise StrategyError("strategy_bars_require_bounded_list")
    index = _calendar_index(calendar) if any(isinstance(row, dict) and row.get("asset_class") == "equity" for row in bars) else None
    rows = _bars(bars, index)
    groups, emitted, revisions, output = {}, {}, {}, []
    by_publication = defaultdict(list)
    for row in rows:
        by_publication[row["information_available_time"]].append(row)
    for publication in sorted(by_publication):
        changed = set()
        for row in by_publication[publication]:
            bucket = _bucket(row, timeframe_seconds, index)
            if bucket is not None:
                groups.setdefault(bucket, {})[row["event_time"]] = row
                changed.add(bucket)
        for bucket in sorted(changed):
            symbol, start, end, width = bucket
            inputs = sorted(groups[bucket].values(), key=lambda row: row["event_time"])
            interval = inputs[0]["interval_seconds"]
            if [row["event_time"] for row in inputs] != list(range(start + interval, end + 1, interval)):
                continue  # Partial, gapped and not-yet-complete buckets remain unavailable.
            source_inputs = [{**{key: row[key] for key in ("symbol", "event_time", "information_available_time", "revision", "source_id")},
                              "ohlcv_sha256": _hash({key: row[key] for key in ("open", "high", "low", "close", "volume", "asset_class", "venue", "currency", "volume_unit")})}
                             for row in inputs]
            fingerprint = _hash(source_inputs)
            if emitted.get(bucket) == fingerprint:
                continue
            revision = revisions.get(bucket, -1) + 1
            bar = {"symbol": symbol, "asset_class": inputs[0]["asset_class"], "venue": inputs[0]["venue"],
                   "event_time": end, "information_available_time": max(row["information_available_time"] for row in inputs),
                   "ingestion_time": max(row["ingestion_time"] for row in inputs), "interval_seconds": width,
                   "timeframe_seconds": timeframe_seconds, "session_date": inputs[-1]["session_date"],
                   "session": inputs[0]["session"], "revision": revision, "source_id": "multiasset_aggregation_v1",
                   "open": inputs[0]["open"], "high": format(max(Decimal(row["high"]) for row in inputs), "f"),
                   "low": format(min(Decimal(row["low"]) for row in inputs), "f"), "close": inputs[-1]["close"],
                   "volume": format(sum(Decimal(row["volume"]) for row in inputs), "f"),
                   "currency": inputs[0]["currency"], "volume_unit": inputs[0]["volume_unit"],
                   "source_inputs": source_inputs, "source_inputs_sha256": fingerprint,
                   "aggregation_complete": True, "aggregation_start_time": start,
                   "explicit_session_calendar": index is not None,
                   "regular_session_daily": timeframe_seconds == 86400 and inputs[0]["asset_class"] == "equity"}
            output.append(bar)
            emitted[bucket], revisions[bucket] = fingerprint, revision
    return sorted(output, key=lambda row: (row["information_available_time"], row["event_time"], row["symbol"], row["revision"]))


def _ema(values):
    value = values[0]
    alpha = 2 / (len(values) + 1)
    for current in values[1:]:
        value = alpha * current + (1 - alpha) * value
    return value


def _features(rows, lookback, opening_count, opening_rows=()):
    if len(rows) < lookback + 1:
        return None
    window = rows[-lookback - 1:]
    closes = [float(row["close"]) for row in window]
    highs = [float(row["high"]) for row in window]
    lows = [float(row["low"]) for row in window]
    volumes = [float(row["volume"]) for row in window]
    prior = closes[:-1]
    mean, std = statistics.mean(prior), statistics.pstdev(prior)
    tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(window))]
    atr = statistics.mean(tr)
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains, losses = sum(max(x, 0) for x in changes), sum(max(-x, 0) for x in changes)
    rsi = 100 * gains / (gains + losses) if gains + losses else 50.0
    path = sum(abs(x) for x in changes)
    efficiency = abs(closes[-1] - closes[0]) / path if path else 0.0
    momentum = closes[-1] / closes[0] - 1
    drawdown = closes[-1] / max(closes) - 1
    ret = closes[-1] / closes[-2] - 1
    atr_fraction = atr / closes[-1]
    regime = ("crash" if ret <= -.04 or (drawdown <= -.08 and momentum < 0) else
              "high_volatility" if atr_fraction >= .03 else "trend" if efficiency >= .55 else "range")
    volume_sum = sum(volumes[:-1])
    vwap = sum(((highs[i] + lows[i] + closes[i]) / 3) * volumes[i] for i in range(lookback)) / volume_sum if volume_sum else mean
    xs = list(range(lookback))
    xm = (lookback - 1) / 2
    slope = sum((x - xm) * (price - mean) for x, price in zip(xs, prior)) / sum((x - xm) ** 2 for x in xs)
    opening = opening_rows
    features = {"close": closes[-1], "previous_close": closes[-2], "mean": mean, "std": std,
                "zscore": (closes[-1] - mean) / std if std else 0.0, "atr": atr, "atr_fraction": atr_fraction,
                "ema": _ema(prior), "previous_ema": _ema(prior[:-1]), "rsi": rsi, "vwap": vwap,
                "prior_high": max(highs[:-1]), "prior_low": min(lows[:-1]), "previous_high": highs[-2],
                "prior_range": max(highs[:-1]) - min(lows[:-1]), "volume": volumes[-1],
                "prior_volume_mean": statistics.mean(volumes[:-1]), "momentum": momentum, "efficiency": efficiency,
                "regression_slope": slope, "drawdown": drawdown, "regime": regime,
                "opening_high": max((float(row["high"]) for row in opening), default=None),
                "opening_low": min((float(row["low"]) for row in opening), default=None),
                "opening_complete": len(opening) == opening_count and opening[-1]["event_time"] < rows[-1]["event_time"]}
    if any(not math.isfinite(value) for value in features.values() if isinstance(value, float)):
        raise StrategyError("strategy_features_nonfinite")
    return features


def _compatible(previous, current, spec, calendar_index=None):
    if current["event_time"] <= previous["event_time"]:
        return False
    if current["asset_class"] == "crypto":
        return current["event_time"] - previous["event_time"] == spec["timeframe_seconds"]
    if previous["session_date"] == current["session_date"]:
        return current["event_time"] - previous["event_time"] == spec["timeframe_seconds"]
    if spec["family"] == "equity_mean_reversion":
        return False
    if calendar_index is None:
        return False  # A calendar-free gap cannot distinguish a holiday from missing data.
    frame = spec["timeframe_seconds"]
    following = next((day for day, (opening, closing) in calendar_index.items()
                      if day > previous["session_date"] and (frame == 86400 or closing - opening >= frame)), None)
    if following != current["session_date"]:
        return False
    prior_open, prior_close = _session_bounds(previous["session_date"], calendar_index)
    opening, closing = _session_bounds(current["session_date"], calendar_index)
    if frame == 86400:
        return previous["event_time"] == prior_close and current["event_time"] == closing
    return (previous["event_time"] == prior_open + ((prior_close - prior_open) // frame) * frame
            and current["event_time"] == opening + frame)


def _target(features, spec, active, entry_level):
    f, family, variant = features, spec["family"], spec["variant"]
    if f is None:
        return False, "warmup_unavailable", None
    if f["atr"] <= 0:
        return False, "zero_atr_unavailable", None
    if f["regime"] in {"crash", "high_volatility"}:
        return False, "risk_regime_exit", None
    z, multiple = float(spec["z_threshold"]), float(spec["atr_multiple"])
    if family == "equity_mean_reversion":
        if active:
            exit_value = f["vwap"] if variant == "vwap" else f["ema"] if variant == "ema" else f["mean"]
            return (f["close"] < exit_value and f["rsi"] < 55 and f["regime"] == "range", "mean_reversion_hold_or_exit", entry_level)
        if f["regime"] != "range":
            return False, "mean_reversion_trend_filter", None
        condition = {"zscore": f["zscore"] <= -z, "bollinger": f["close"] < f["mean"] - z * f["std"],
                     "vwap": f["close"] < f["vwap"] - z * f["atr"], "rsi": f["rsi"] <= float(spec["rsi_threshold"]),
                     "ema": f["close"] < f["ema"] - z * f["atr"],
                     "atr_deviation": f["close"] < f["mean"] - z * f["atr"],
                     "opening_range": f["opening_complete"] and f["close"] < f["opening_low"] - .5 * f["atr"]}[variant]
        return condition, "mean_reversion_entry" if condition else "mean_reversion_wait", f["close"] if condition else None
    if family == "btc_breakout":
        if active:
            fakeout = entry_level is not None and f["close"] <= entry_level
            exit_now = fakeout or f["close"] < f["mean"] or f["momentum"] <= 0
            return not exit_now, "breakout_fakeout_exit" if fakeout else "breakout_hold_or_exit", entry_level
        level = f["previous_high"] if variant == "prior_period" else f["mean"] + multiple * f["atr"] if variant == "atr_distance" else f["prior_high"]
        volume_ok = f["prior_volume_mean"] > 0 and f["volume"] >= f["prior_volume_mean"] * float(spec["volume_multiplier"])
        consolidation_ok = variant != "consolidation" or f["prior_range"] <= 3 * f["atr"]
        condition = f["close"] > level and f["momentum"] > 0 and volume_ok and consolidation_ok
        return condition, "breakout_entry" if condition else "breakout_confirmation_wait", level if condition else None
    if active:
        exit_now = f["close"] < f["ema"] or f["momentum"] <= 0 or f["regime"] != "trend"
        return not exit_now, "proxy_trend_hold_or_exit", entry_level
    if f["regime"] != "trend" or f["momentum"] <= 0:
        return False, "proxy_trend_regime_filter", None
    condition = {"momentum": f["momentum"] > f["atr_fraction"], "moving_average": f["close"] > f["mean"] and f["ema"] > f["previous_ema"],
                 "donchian": f["close"] > f["prior_high"], "regression": f["regression_slope"] > 0 and f["close"] > f["mean"],
                 "pullback": f["previous_close"] <= f["ema"] and f["close"] > f["ema"] and f["close"] > f["mean"]}[variant]
    return condition, "proxy_trend_entry" if condition else "proxy_trend_wait", f["close"] if condition else None


def signals_for(bars, spec, *, calendar=None):
    spec = normalize_spec(spec)
    if not isinstance(bars, list) or len(bars) > MAX_BARS or any(not isinstance(row, dict) for row in bars):
        raise StrategyError("strategy_bars_require_bounded_list")
    selected = [row for row in bars if row.get("symbol") in spec["symbols"]]
    expected_asset = "crypto" if spec["family"] == "btc_breakout" else "equity"
    if any(row.get("asset_class") != expected_asset for row in selected):
        raise StrategyError("strategy_symbol_asset_class_mismatch")
    expected_unit = "base_asset" if expected_asset == "crypto" else "shares"
    if any(row.get("currency") != "USD" or row.get("volume_unit") != expected_unit for row in selected):
        raise StrategyUnavailable("strategy_requires_explicit_usd_and_volume_units")
    calendar_index = _calendar_index(calendar) if expected_asset == "equity" else None
    aggregates = aggregate_bars(selected, spec["timeframe_seconds"], calendar=calendar)
    known, events, active, levels, decisions = defaultdict(dict), defaultdict(list), defaultdict(bool), {}, []
    publications = defaultdict(list)
    for row in aggregates:
        publications[row["information_available_time"]].append(row)
    for now in sorted(publications):
        changed = set()
        for row in publications[now]:
            symbol = row["symbol"]
            if row["event_time"] not in known[symbol]:
                insort(events[symbol], row["event_time"])
            known[symbol][row["event_time"]] = row
            changed.add((symbol, row["event_time"]))
        for symbol in sorted({symbol for symbol, _ in changed}):
            latest = events[symbol][-1]
            if (symbol, latest) not in changed:
                continue  # Late historical revisions only affect later decisions.
            row = known[symbol][latest]
            ordered = [known[symbol][event] for event in events[symbol][-spec["lookback"] - 1:]]
            segment = []
            for item in ordered:
                if segment and not _compatible(segment[-1], item, spec, calendar_index):
                    segment = []
                segment.append(item)
            opening = []
            if spec["variant"] == "opening_range":
                start, _ = _session_bounds(row["session_date"], calendar_index)
                expected = [start + index * spec["timeframe_seconds"] for index in range(1, spec["opening_range_bars"] + 1)]
                opening = [known[symbol][event] for event in expected if event in known[symbol]]
            f = _features(segment, spec["lookback"], spec["opening_range_bars"], opening)
            target, reason, level = _target(f, spec, active[symbol], levels.get(symbol))
            active[symbol], levels[symbol] = bool(target), level
            used = segment[-spec["lookback"] - 1:]
            # Opening-range features additionally identify their first session bars.
            if opening:
                used = list({(item["event_time"], item["revision"]): item for item in opening + used}.values())
            feature_time = max(item["information_available_time"] for item in used)
            if feature_time > now:
                raise StrategyError("strategy_feature_availability_violation")
            inputs = [{key: item[key] for key in ("event_time", "information_available_time", "revision", "source_inputs_sha256")} for item in used]
            metadata = {"family": spec["family"], "variant": spec["variant"], "reason": reason,
                        "timeframe_seconds": spec["timeframe_seconds"], "direction": "long" if target else "flat",
                        "explicit_session_calendar": row["explicit_session_calendar"],
                        "equity_cross_session_continuity": "explicit_calendar_only" if expected_asset == "equity" else "not_applicable",
                        "regime": "unavailable" if f is None else f["regime"], "atr": None if f is None else format(f["atr"], ".15g"),
                        "stop_distance": None if f is None else format(f["atr"] * float(spec["atr_multiple"]), ".15g"),
                        "feature_event_time": row["event_time"], "feature_information_available_time": feature_time,
                        "feature_inputs": inputs, "feature_inputs_sha256": _hash(inputs),
                        "features": f, "position_sizing": "direction_placeholder_requires_shared_risk_layer",
                        "feature_definitions": {"vwap": "prior_lookback_volume_weighted_typical_price_not_session_anchored",
                                                "rsi": "simple_trailing_gain_share_not_Wilder_smoothing",
                                                "atr": "mean_trailing_true_range_including_previous_close",
                                                "regime": "causal_trailing_efficiency_atr_fraction_and_drawdown_rules"},
                        "same_event_fill_allowed": False, "earliest_fill_event_time_exclusive": row["event_time"],
                        "instrument_kind": "spot_crypto" if expected_asset == "crypto" else "commodity_proxy_etf" if spec["family"] == "commodity_proxy_trend" else "equity_etf",
                        "actual_futures_contract": False, "strategy_validated": False}
            identity = [spec["strategy_id"], symbol, now, row["event_time"], row["revision"], metadata["feature_inputs_sha256"]]
            decisions.append({"signal_id": "multiasset/" + _hash(identity)[:32], "strategy_id": spec["strategy_id"],
                              "strategy_version": VERSION, "symbol": symbol, "decision_time": now,
                              "expires_at": now + spec["timeframe_seconds"], "target_weight": "1" if target else "0",
                              "direction": "long" if target else "flat", "metadata": metadata})
    return sorted(decisions, key=lambda row: (row["decision_time"], row["symbol"], row["signal_id"]))
