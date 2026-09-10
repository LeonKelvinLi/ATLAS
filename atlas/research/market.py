"""Explicit historical-data contracts. Pure validation; no provider or broker I/O.

Source labels are declarations, never authentication. A historical file can be
useful for research while still being ineligible to establish point-in-time alpha.
No timestamp, price, revision, gap or corporate action is silently repaired.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from atlas.domain import decimal

MAX_DATASET_BYTES = 32 * 1024 * 1024
MAX_BARS = 100_000
SCHEMA_VERSION = "1.0"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9./-]{0,19}\Z")


class DatasetError(ValueError):
    pass


def identifier(value, name="identifier"):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise DatasetError(f"invalid {name}")
    return value


def timestamp(value, name="timestamp"):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 253402300799:
        raise DatasetError(f"{name} must be nonnegative integer UTC seconds")
    return value


def _number(value, name, *, positive=False):
    try:
        number = decimal(value, nonnegative=True)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise DatasetError(f"invalid finite {name}") from exc
    if positive and number <= 0:
        raise DatasetError(f"{name} must be positive")
    return format(number, "f")


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DatasetError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise DatasetError(f"nonfinite JSON constant: {value}")


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise DatasetError("metadata must be finite JSON data") from exc


def observation_keys(dataset):
    """Stable market-event identities for overlap/sealed-holdout detection.

    Revised values, provider/venue aliases and differing slices cannot disguise
    the same asset/symbol/event. Deliberately blocks the same instrument/time
    across venues because venue authenticity is unverified. A trusted symbol
    registry would be needed to detect arbitrary instrument renaming.
    """
    bars = dataset["bars"]
    keys = set()
    for row in bars:
        identity = [identifier(row["asset_class"],"asset_class"),identifier(row["symbol"],"symbol"),
                    timestamp(row["event_time"],"event_time")]
        keys.add(hashlib.sha256(_canonical(identity)).hexdigest())
    return sorted(keys)


def _calendar(payload, asset_class):
    calendar = payload.get("calendar")
    if asset_class == "crypto":
        if calendar is not None and calendar != {"calendar_id": "UTC_24_7", "timezone": "UTC", "version": "1"}:
            raise DatasetError("crypto supports only the explicit UTC_24_7 calendar")
        return {"calendar_id": "UTC_24_7", "timezone": "UTC", "version": "1"}, {}
    if not isinstance(calendar, dict) or set(calendar) != {"calendar_id", "timezone", "version", "source_id", "sessions"}:
        raise DatasetError("equity requires a versioned explicit regular-session calendar")
    identifier(calendar["calendar_id"], "calendar_id")
    identifier(calendar["version"], "calendar version")
    identifier(calendar["source_id"], "calendar source")
    if calendar["timezone"] != "America/New_York":
        raise DatasetError("unsupported equity session timezone; only America/New_York regular sessions supported")
    sessions = calendar["sessions"]
    if not isinstance(sessions, list) or not sessions or len(sessions) > 10_000:
        raise DatasetError("invalid explicit calendar sessions")
    index = {}
    local_zone = ZoneInfo(calendar["timezone"])
    for session in sessions:
        if not isinstance(session, dict) or set(session) != {"session_date", "open_time", "close_time"}:
            raise DatasetError("invalid session bounds")
        opening = timestamp(session["open_time"], "session open")
        closing = timestamp(session["close_time"], "session close")
        start, end = (datetime.fromtimestamp(t, local_zone) for t in (opening, closing))
        date = session["session_date"]
        if (date in index or not isinstance(date, str) or start.date().isoformat() != date
                or end.date().isoformat() != date or start.weekday() > 4
                or (start.hour, start.minute, start.second) != (9, 30, 0)
                or (end.hour, end.minute, end.second) not in {(16, 0, 0), (13, 0, 0)}
                or opening >= closing):
            raise DatasetError("invalid, duplicate or unsupported regular-session calendar bounds")
        index[date] = (opening, closing)
    # The supplied calendar's holidays/early closes have not been authenticated.
    return json.loads(_canonical(calendar)), index


def validate_dataset(payload, *, now):
    """Return canonical bars and an honest quality report, or reject bad data.

    Bars are ordered by publication time. A revision must follow its original
    observation, carry a strictly later publication time and increment revision.
    `information_available_time` describes historical public availability;
    `ingestion_time` records acquisition provenance, not a fabricated old ingest.
    """
    timestamp(now, "now")
    if not isinstance(payload, dict):
        raise DatasetError("dataset must be an object")
    required = {"schema_version", "dataset_id", "source", "asset_class", "symbols", "venue",
                "price_adjustment", "universe", "availability_basis", "revision_policy", "volume_unit", "currency", "bars"}
    if not required <= payload.keys() or payload.keys() - required - {"calendar", "description"}:
        raise DatasetError("missing or unknown dataset fields")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise DatasetError("unsupported dataset schema version")
    identifier(payload["dataset_id"], "dataset_id")
    identifier(payload["venue"], "venue")
    if not isinstance(payload["currency"],str) or not re.fullmatch(r"[A-Z]{3}",payload["currency"]):
        raise DatasetError("currency must be an explicit three-letter uppercase code")
    asset_class = payload["asset_class"]
    if asset_class not in {"equity", "crypto"}:
        raise DatasetError("only equity and crypto research are supported; legacy binary evidence is separate")
    if payload["volume_unit"] not in ({"shares","unknown"} if asset_class=="equity" else {"base_asset","unknown"}):
        raise DatasetError("explicit volume unit must be instrument quantity or unknown")
    symbols = payload["symbols"]
    if (not isinstance(symbols, list) or not symbols or len(symbols) > 100
            or any(not isinstance(s, str) or not _SYMBOL.fullmatch(s) for s in symbols)
            or len(set(symbols)) != len(symbols)):
        raise DatasetError("invalid or duplicate normalized symbols")
    source = payload["source"]
    if (not isinstance(source, dict) or not {"source_id", "kind", "description"} <= source.keys()
            or source.keys() - {"source_id", "kind", "description", "url", "license", "content_sha256"}):
        raise DatasetError("invalid explicit source metadata")
    identifier(source["source_id"], "source_id")
    if source["kind"] not in {"real", "synthetic", "test", "unknown"}:
        raise DatasetError("unknown source kind")
    if not isinstance(source["description"], str) or not source["description"].strip():
        raise DatasetError("source description is required")
    if "content_sha256" in source and not re.fullmatch(r"[0-9a-f]{64}", str(source["content_sha256"])):
        raise DatasetError("invalid declared source content hash")
    if payload["price_adjustment"] not in {"unadjusted", "point_in_time_adjusted", "retrospectively_adjusted", "unknown"}:
        raise DatasetError("explicit price adjustment convention required")
    universe = payload["universe"]
    if (not isinstance(universe, dict) or set(universe) != {"selection_method", "survivorship_bias"}
            or not isinstance(universe["selection_method"], str) or not universe["selection_method"].strip()
            or universe["survivorship_bias"] not in {"known", "unknown", "mitigated"}):
        raise DatasetError("explicit universe selection and survivorship status required")
    if payload["availability_basis"] not in {"observed", "provider_published", "assumed", "synthetic", "unknown"}:
        raise DatasetError("invalid information availability basis")
    if payload["revision_policy"] not in {"as_observed", "latest_only", "synthetic", "unknown"}:
        raise DatasetError("invalid revision policy")
    calendar, session_index = _calendar(payload, asset_class)
    bars = payload["bars"]
    if not isinstance(bars, list) or not bars or len(bars) > MAX_BARS:
        raise DatasetError(f"bars must contain 1..{MAX_BARS} observations")
    bar_fields = {"symbol", "asset_class", "venue", "event_time", "interval_seconds", "information_available_time",
                  "ingestion_time", "timezone", "session", "session_date", "open", "high", "low", "close",
                  "volume", "revision", "source_id"}
    normalized, revisions, intervals = [], {}, {}
    previous_publication = -1
    for raw in bars:
        if not isinstance(raw, dict) or not bar_fields <= raw.keys() or raw.keys() - bar_fields - {"bid", "ask", "revision_reason", "volume_unit", "currency"}:
            raise DatasetError("missing or unknown canonical bar fields")
        if "volume_unit" in raw and raw["volume_unit"] != payload["volume_unit"]:
            raise DatasetError("bar volume unit differs from dataset")
        if "currency" in raw and raw["currency"] != payload["currency"]:
            raise DatasetError("bar currency differs from dataset")
        if (raw["symbol"] not in symbols or raw["asset_class"] != asset_class or raw["venue"] != payload["venue"]
                or raw["source_id"] != source["source_id"] or raw["timezone"] != "UTC"):
            raise DatasetError("bar identity/timezone/source does not match dataset")
        event, available, ingested = (timestamp(raw[k], k) for k in
                                     ("event_time", "information_available_time", "ingestion_time"))
        if not event <= available <= ingested <= now:
            raise DatasetError("future/incomplete observation or impossible event/publication/ingestion ordering")
        if available < previous_publication:
            raise DatasetError("bars must be ordered by information availability; no silent sorting")
        previous_publication = available
        interval = raw["interval_seconds"]
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0 or interval > 86400 or event < interval:
            raise DatasetError("invalid bar interval")
        if raw["symbol"] in intervals and intervals[raw["symbol"]] != interval:
            raise DatasetError("mixed frequency for one symbol")
        intervals[raw["symbol"]] = interval
        day = raw["session_date"]
        if asset_class == "crypto":
            expected_day = datetime.fromtimestamp(event - 1, timezone.utc).date().isoformat()
            if raw["session"] != "24/7" or day != expected_day or event % interval:
                raise DatasetError("crypto bars require aligned UTC 24/7 session dates (bar-close boundary belongs to prior day)")
        else:
            if raw["session"] != "regular" or day not in session_index:
                raise DatasetError("equity bar is outside the explicit regular-session calendar")
            opening, closing = session_index[day]
            if not opening <= event - interval < event <= closing or (event - opening) % interval:
                raise DatasetError("equity bar crosses a session boundary or has invalid bar duration/alignment")
        revision = raw["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise DatasetError("revision must be a nonnegative integer")
        key = (raw["symbol"], event)
        previous = revisions.get(key)
        if previous is None:
            if revision != 0:
                raise DatasetError("original observation revision 0 is required; do not backfill later values as original")
        elif (revision != previous["revision"] + 1 or available <= previous["information_available_time"]
              or any(raw[k] != previous[k] for k in ("interval_seconds", "session", "session_date"))):
            raise DatasetError("duplicate, nonmonotonic or inconsistent revision")
        row = {k: raw[k] for k in bar_fields}
        row["volume_unit"] = payload["volume_unit"]
        row["currency"] = payload["currency"]
        for key_name in ("open", "high", "low", "close", "volume"):
            row[key_name] = _number(raw[key_name], key_name, positive=key_name != "volume")
        prices = {key_name: decimal(row[key_name]) for key_name in ("open", "high", "low", "close")}
        if prices["high"] < max(prices.values()) or prices["low"] > min(prices.values()):
            raise DatasetError("inconsistent OHLC maximum/minimum")
        if ("bid" in raw) != ("ask" in raw):
            raise DatasetError("bid and ask must be supplied together or both absent")
        if "bid" in raw:
            row["bid"], row["ask"] = (_number(raw[k], k, positive=True) for k in ("bid", "ask"))
            if decimal(row["bid"]) > decimal(row["ask"]):
                raise DatasetError("crossed bid/ask")
        if "revision_reason" in raw:
            if not isinstance(raw["revision_reason"], str) or not raw["revision_reason"].strip():
                raise DatasetError("invalid revision reason")
            row["revision_reason"] = raw["revision_reason"]
        revisions[key] = row
        normalized.append(row)
    if set(intervals) != set(symbols):
        raise DatasetError("declared symbol missing from observations")
    gaps, coverage = [], {}
    for symbol in symbols:
        originals = sorted((r for r in normalized if r["symbol"] == symbol and r["revision"] == 0), key=lambda r:r["event_time"])
        interval = intervals[symbol]
        for left, right in zip(originals, originals[1:]):
            same_session = asset_class == "crypto" or left["session_date"] == right["session_date"]
            if same_session and right["event_time"] - left["event_time"] != interval:
                delta = right["event_time"] - left["event_time"]
                if delta <= 0 or delta % interval:
                    raise DatasetError("overlapping or off-grid original bars")
                gaps.append({"symbol":symbol,"after":left["event_time"],"before":right["event_time"],"missing_bars":delta//interval-1})
        if asset_class == "equity":
            # Compare the declared schedule only inside the observed span. A
            # missing complete session cannot disappear between two good days.
            observed_by_date = {}
            for row in originals:
                observed_by_date.setdefault(row["session_date"], []).append(row)
            for day,(opening,closing) in sorted(session_index.items()):
                first = max(opening+interval, originals[0]["event_time"])
                last = min(closing, originals[-1]["event_time"])
                if first > last: continue
                # Session boundaries and observed bars are already on the grid.
                expected = (last-first)//interval+1
                actual = observed_by_date.get(day, [])
                intraday_missing = sum(g["missing_bars"] for g in gaps if g["symbol"]==symbol
                                       and opening <= g["after"] < g["before"] <= closing)
                edge_missing = expected-len(actual)-intraday_missing
                if edge_missing > 0:
                    gaps.append({"symbol":symbol,"session_date":day,"after":first-interval,"before":last+interval,
                                 "missing_bars":edge_missing,"kind":"missing_session_or_session_edges"})
        coverage[symbol] = {"first_event_time":originals[0]["event_time"],"last_event_time":originals[-1]["event_time"],
                            "observations":len(originals),"interval_seconds":interval}
    blockers = ["provider_authenticity_not_verified"]
    if source["kind"] != "real": blockers.append("no_authenticated_real_market_data")
    if payload["availability_basis"] not in {"observed", "provider_published"}: blockers.append("historical_availability_not_established")
    if payload["revision_policy"] != "as_observed": blockers.append("point_in_time_revision_history_not_established")
    if payload["price_adjustment"] in {"unknown", "retrospectively_adjusted"}: blockers.append("price_adjustment_not_point_in_time")
    if universe["survivorship_bias"] != "mitigated": blockers.append("universe_survivorship_not_mitigated")
    if asset_class == "equity": blockers.extend(["calendar_authenticity_not_verified", "corporate_action_cash_flows_not_supplied"])
    if gaps: blockers.append("missing_expected_bars")
    if payload["volume_unit"] == "unknown": blockers.append("volume_unit_unknown_no_fill_capacity_assumption")
    if payload["currency"] != "USD": blockers.append("non_usd_currency_requires_explicit_fx_accounting")
    canonical = {k:payload[k] for k in required if k != "bars"}
    canonical.update(calendar=calendar,bars=normalized)
    if "description" in payload: canonical["description"] = payload["description"]
    # Serialization also rejects unsafe/nonfinite metadata and makes an independent copy.
    content = _canonical(canonical)
    if len(content) > MAX_DATASET_BYTES:
        raise DatasetError("canonical dataset exceeds maximum size")
    canonical = json.loads(content)
    descriptor = {k:v for k,v in canonical.items() if k not in {"bars", "calendar"}}
    descriptor["calendar"] = {k:v for k,v in calendar.items() if k != "sessions"}
    evidence = {"synthetic":"SYNTHETIC", "test":"TEST"}.get(source["kind"], "HISTORICAL_UNVERIFIED")
    quality = {"valid":True,"alpha_validation_eligible":False,"provider_authenticity_verified":False,
               "blockers":blockers,"gaps":gaps,"coverage":coverage,"bar_count":len(normalized),
               "revision_count":len(normalized)-len(revisions),"spread_observations":sum("bid" in r for r in normalized),
               "warnings":["Declared source, adjustment and universe metadata are not external verification.",
                           "Unobserved revisions, delistings and corporate actions cannot be inferred from prices."]}
    observation_content = _canonical([{k:v for k,v in row.items() if k not in {"source_id","revision_reason","ingestion_time"}}
                                      for row in canonical["bars"]])
    return {**canonical,"dataset_hash":hashlib.sha256(content).hexdigest(),
            "observation_hash":hashlib.sha256(observation_content).hexdigest(),
            "observation_keys":observation_keys(canonical),"descriptor":descriptor,
            "evidence_class":evidence,"quality":quality}


def quality_report(payload, *, now):
    """A bounded, nonthrowing report for user data validation failures."""
    try:
        result = validate_dataset(payload, now=now)
        return {**result["quality"],"dataset_hash":result["dataset_hash"],"observation_hash":result["observation_hash"],"evidence_class":result["evidence_class"]}
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        return {"valid":False,"alpha_validation_eligible":False,"errors":[str(exc)],"provider_authenticity_verified":False}


def load_dataset(path, *, now):
    """Read a bounded strict-JSON dataset. Never execute or unpickle artifacts."""
    path = Path(path)
    with path.open("rb") as stream:
        content = stream.read(MAX_DATASET_BYTES + 1)
    if len(content) > MAX_DATASET_BYTES:
        raise DatasetError("dataset file exceeds maximum size")
    try:
        payload = json.loads(content, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DatasetError("invalid UTF-8 JSON dataset") from exc
    return validate_dataset(payload, now=now)
