"""One bounded offline RSI/volume observation; no I/O, orders, or strategy state.

RSI is 100 * sum(gains) / (sum(gains) + sum(losses)) over the last N
close-to-close changes, not Wilder smoothing. Volume is compared with the
arithmetic mean of N prior bars, excluding the current bar. A 200% increase
means a ratio of 3. Inputs must have closed, been published, and been ingested
by as_of. Source declarations never authenticate historical availability.
"""

from decimal import Decimal
import hashlib
import json
import re

from atlas.domain import decimal, financial_context
from atlas.research.market import DatasetError, identifier, timestamp, validate_dataset
from atlas.research.multiasset_strategies import StrategyError, StrategyUnavailable, aggregate_bars

VERSION = "causal-rsi-volume-v1"
MAX_INPUT_BARS = 10_000
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_AGGREGATION_WORK = 2_000_000
TIMEFRAMES = {60, 300, 900, 1800, 3600, 7200, 14400, 28800, 86400}
SPEC_FIELDS = {"version", "symbol", "venue", "asset_class", "currency", "timeframe_seconds",
               "rsi_length", "rsi_method", "rsi_below", "volume_lookback", "minimum_volume_ratio"}


class ScreenerError(ValueError):
    """Malformed or excessive input; never a negative market observation."""


def _encoded(value):
    try:
        chunks, size = [], 0
        for chunk in json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(value):
            data = chunk.encode()
            size += len(data)
            if size > MAX_INPUT_BYTES:
                raise ScreenerError("screener_input_size_limit")
            chunks.append(data)
        return b"".join(chunks)
    except (ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, ScreenerError):
            raise
        raise ScreenerError("screener_requires_finite_json") from exc


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def normalize_spec(spec):
    """Normalize an explicit instrument and bounded, hashable screen definition."""
    if not isinstance(spec, dict) or set(spec) - SPEC_FIELDS:
        raise ScreenerError("screener_spec_unknown_fields")
    result = {"version": VERSION, "timeframe_seconds": 900, "rsi_length": 14,
              "rsi_method": "simple_trailing_gain_share", "rsi_below": "30",
              "volume_lookback": 20, "minimum_volume_ratio": "3", **spec}
    try:
        for key in ("symbol", "venue"):
            identifier(result[key], key)
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9./-]{0,19}", result["symbol"]):
            raise ValueError
        if (result["asset_class"] not in {"crypto", "equity"}
                or not isinstance(result["currency"], str)
                or not re.fullmatch(r"[A-Z]{3}", result["currency"])
                or result["version"] != VERSION
                or result["rsi_method"] != "simple_trailing_gain_share"
                or type(result["timeframe_seconds"]) is not int
                or result["timeframe_seconds"] not in TIMEFRAMES):
            raise ValueError
        for key in ("rsi_length", "volume_lookback"):
            if type(result[key]) is not int or not 2 <= result[key] <= 256:
                raise ValueError
        for key in ("rsi_below", "minimum_volume_ratio"):
            if not isinstance(result[key], str):
                raise ValueError
            result[key] = format(decimal(result[key], positive=True), "f")
        if not 0 < decimal(result["rsi_below"]) < 100 or not 3 <= decimal(result["minimum_volume_ratio"]) <= 1000:
            raise ValueError
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise ScreenerError("screener_spec_invalid") from exc
    return result


def _unavailable(report, reason):
    return {**report, "status": "UNAVAILABLE", "unavailable_reason": reason}


def _source_evidence(checked):
    return {"evidence_class": checked["evidence_class"], "source_id": checked["source"]["source_id"],
            "source_kind": checked["source"]["kind"], "availability_basis": checked["availability_basis"],
            "revision_policy": checked["revision_policy"], "price_adjustment": checked["price_adjustment"],
            "volume_unit": checked["volume_unit"], "currency": checked["currency"],
            "provider_authenticity_verified": False, "alpha_validation_eligible": False}


def _aggregate(visible, frame, calendar):
    source_interval = visible[0]["interval_seconds"]
    if source_interval == frame:
        # Native bars preserve their actual versions and timestamps.
        return [{**row, "aggregation_start_time": row["event_time"] - source_interval,
                 "source_inputs_sha256": _hash(row)} for row in visible]
    if frame < source_interval or frame % source_interval:
        raise StrategyUnavailable("source_interval_cannot_form_requested_timeframe")
    if frame in {60, 300}:
        raise StrategyUnavailable("sub_15m_resampling_not_supported_use_native_bars")
    if len(visible) * (frame // source_interval) > MAX_AGGREGATION_WORK:
        raise StrategyUnavailable("aggregation_work_limit")
    return aggregate_bars(visible, frame, calendar=calendar)


def _boundary(previous, current, spec, sessions):
    if current["asset_class"] == "crypto":
        return None if current["event_time"] - previous["event_time"] == spec["timeframe_seconds"] else "gap"
    if spec["timeframe_seconds"] == 86400:
        days = list(sessions)
        prior = days.index(previous["session_date"])
        following = days[prior + 1] if prior + 1 < len(days) else None
        return None if current["session_date"] == following else "gap"
    if current["session_date"] != previous["session_date"]:
        return "session_boundary"
    return None if current["aggregation_start_time"] == previous["event_time"] else "gap"


@financial_context
def scan_dataset(payload, spec, *, as_of, now):
    """Return the last complete observation known by as_of, never a trade signal.

    now is the explicit validation clock and must be >= as_of. Future valid
    observations may exist in the supplied archive but cannot affect the visible
    snapshot or its hashes. An earlier result is not rewritten when a revision
    arrives later; a new as_of may reflect that revision. A dated observation is
    not a freshness, tradability, execution-cost or profitability assessment.
    """
    spec = normalize_spec(spec)
    try:
        timestamp(as_of, "as_of")
        timestamp(now, "now")
        if as_of > now:
            raise ValueError("as_of exceeds now")
    except ValueError as exc:
        raise ScreenerError("screener_clock_invalid") from exc
    if (not isinstance(payload, dict) or not isinstance(payload.get("bars"), list)
            or not 1 <= len(payload["bars"]) <= MAX_INPUT_BARS):
        raise ScreenerError("screener_requires_bounded_canonical_dataset")
    _encoded(payload)
    try:
        checked = validate_dataset(payload, now=now)
    except (DatasetError, TypeError, KeyError, ArithmeticError) as exc:
        raise ScreenerError(f"screener_dataset_invalid: {exc}") from exc
    if (spec["symbol"] not in checked["symbols"]
            or any(spec[key] != checked[key] for key in ("asset_class", "venue", "currency"))):
        raise ScreenerError("screener_instrument_contract_mismatch")
    visible = [row for row in checked["bars"] if row["symbol"] == spec["symbol"]
               and max(row["event_time"], row["information_available_time"], row["ingestion_time"]) <= as_of]
    evidence = _source_evidence(checked)
    calendar = checked["calendar"]
    if spec["asset_class"] == "equity":
        calendar = {**calendar, "sessions": sorted(
            (row for row in calendar["sessions"] if row["open_time"] <= as_of),
            key=lambda row: row["session_date"])}
    report = {"version": VERSION, "spec": spec, "spec_sha256": _hash(spec), "as_of": as_of,
              "purpose": "offline_screen_observation_only", "trading_eligible": False,
              "source_evidence": evidence,
              "visible_input_sha256": _hash({"source_evidence": evidence, "calendar": calendar, "bars": visible}),
              "visible_source_bars": len(visible), "observation": None, "matched": None,
              "rsi": None, "rsi_condition_met": None, "volume": None,
              "prior_volume_mean": None, "volume_ratio": None, "volume_condition_met": None,
              "rsi_comparison": "strictly_below", "volume_comparison": "at_least",
              "equity_intraday_continuity": "reset_each_session",
              "availability_policy": "closed_and_published_and_ingested_by_as_of",
              "feature_inputs": [], "feature_inputs_sha256": _hash([]),
              "feature_information_available_time": None, "feature_observed_time": None,
              "required_contiguous_bars": max(spec["rsi_length"], spec["volume_lookback"]) + 1,
              "contiguous_bars_available": 0, "history_boundary": "start_of_observed_history"}
    if not visible:
        return _unavailable(report, "no_closed_observed_bars_as_of")
    versions = {}
    for row in visible:
        previous = versions.get(row["event_time"], -1)
        if row["revision"] != previous + 1:
            return _unavailable(report, "revision_history_not_observed_as_of")
        versions[row["event_time"]] = row["revision"]
    try:
        aggregates = _aggregate(visible, spec["timeframe_seconds"], calendar)
    except StrategyUnavailable as exc:
        return _unavailable(report, str(exc))
    except StrategyError as exc:
        raise ScreenerError(f"screener_aggregation_invalid: {exc}") from exc
    if not aggregates:
        return _unavailable(report, "no_complete_timeframe_bars_as_of")
    known = {row["event_time"]: row for row in aggregates}
    ordered = [known[event] for event in sorted(known)]
    latest = ordered[-1]
    report["observation"] = {key: latest[key] for key in
                             ("event_time", "information_available_time", "ingestion_time", "revision", "close")}
    report["observation"]["age_seconds"] = as_of - latest["event_time"]
    report["observation"]["latest_source_event_time"] = max(row["event_time"] for row in visible)
    report["volume"] = latest["volume"]
    sessions = {row["session_date"]: row for row in calendar.get("sessions", [])}
    newest_source = max(visible, key=lambda row: row["event_time"])
    frame = spec["timeframe_seconds"]
    if spec["asset_class"] == "crypto":
        expected_end = newest_source["event_time"] // frame * frame
    else:
        session = sessions[newest_source["session_date"]]
        if frame == 86400:
            expected_end = session["close_time"] if newest_source["event_time"] == session["close_time"] else 0
        else:
            elapsed = newest_source["event_time"] - session["open_time"]
            expected_end = session["open_time"] + elapsed // frame * frame if elapsed >= frame else 0
    if expected_end > latest["event_time"]:
        return _unavailable(report, "latest_completed_bucket_has_missing_source_bars")
    segment = []
    for row in ordered[-report["required_contiguous_bars"]:]:
        boundary = _boundary(segment[-1], row, spec, sessions) if segment else None
        if boundary:
            segment = []
            report["history_boundary"] = boundary
        segment.append(row)
    report["contiguous_bars_available"] = len(segment)
    report["feature_inputs"] = [{key: row[key] for key in
                                 ("event_time", "information_available_time", "ingestion_time", "revision",
                                  "source_inputs_sha256")} for row in segment]
    report["feature_inputs_sha256"] = _hash(report["feature_inputs"])
    report["feature_information_available_time"] = max(row["information_available_time"] for row in segment)
    report["feature_observed_time"] = max(row["ingestion_time"] for row in segment)
    if checked["volume_unit"] == "unknown":
        return _unavailable(report, "volume_unit_unknown")
    if len(segment) < report["required_contiguous_bars"]:
        return _unavailable(report, "insufficient_contiguous_history")
    closes = [Decimal(row["close"]) for row in segment[-spec["rsi_length"] - 1:]]
    changes = [right - left for left, right in zip(closes, closes[1:])]
    gains = sum((max(change, Decimal(0)) for change in changes), Decimal(0))
    losses = sum((max(-change, Decimal(0)) for change in changes), Decimal(0))
    if gains + losses == 0:
        return _unavailable(report, "rsi_zero_total_price_change")
    prior_volume = sum((Decimal(row["volume"]) for row in segment[-spec["volume_lookback"] - 1:-1]), Decimal(0))
    rsi = 100 * gains / (gains + losses)
    report.update(rsi=format(rsi, "f"), rsi_condition_met=100 * gains < decimal(spec["rsi_below"]) * (gains + losses))
    if prior_volume == 0:
        return _unavailable(report, "zero_prior_volume_baseline")
    volume = Decimal(latest["volume"])
    ratio = volume * spec["volume_lookback"] / prior_volume
    volume_ok = volume * spec["volume_lookback"] >= decimal(spec["minimum_volume_ratio"]) * prior_volume
    return {**report, "status": "AVAILABLE", "unavailable_reason": None,
            "prior_volume_mean": format(prior_volume / spec["volume_lookback"], "f"),
            "volume_ratio": format(ratio, "f"), "volume_condition_met": volume_ok,
            "matched": report["rsi_condition_met"] and volume_ok}
