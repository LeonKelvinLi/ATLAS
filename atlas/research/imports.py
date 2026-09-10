"""Offline, provenance-preserving imports of explicitly selected candle exports.

This module has no network, broker, experiment or strategy authority. A matching
provider-shaped response is an operator declaration, not provider authentication.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from atlas.config import external_path
from atlas.domain import decimal
from atlas.research.market import DatasetError, timestamp, validate_dataset


IMPORT_VERSION = "coinbase-exchange-candles-1"
MAX_RAW_BYTES = 1_000_000
GRANULARITIES = frozenset({60, 300, 900, 3600, 21600, 86400})
PROVIDER_DOCUMENTATION = "https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles"
_FILES = ("raw.json", "dataset.json", "quality.json")


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _reject_constant(value):
    raise DatasetError("nonfinite JSON constant: " + value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DatasetError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _json(content, *, exact_numbers=False):
    try:
        return json.loads(content.decode("utf-8"), parse_float=Decimal if exact_numbers else float,
                          parse_constant=_reject_constant, object_pairs_hook=_unique)
    except (UnicodeError, ValueError, ArithmeticError, RecursionError) as exc:
        if isinstance(exc, DatasetError):
            raise
        raise DatasetError("invalid UTF-8 JSON import") from exc


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _url_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None or parsed.microsecond:
            raise ValueError
        return timestamp(int(parsed.timestamp()), "request URL time")
    except (ValueError, TypeError, OverflowError) as exc:
        raise DatasetError("request URL dates must be explicit timezone-aware whole seconds") from exc


def _request_url(product_id, granularity, start, end, supplied):
    base = "https://api.exchange.coinbase.com/products/" + product_id + "/candles"
    query = {"granularity": str(granularity)}
    if start is not None:
        query.update(start=_utc(start), end=_utc(end))
    derived = base + "?" + urlencode(query)
    if supplied is None:
        return derived
    if not isinstance(supplied, str) or len(supplied) > 2048:
        raise DatasetError("invalid declared request URL")
    try:
        parsed = urlsplit(supplied)
        actual = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (parsed.scheme != "https" or parsed.netloc != "api.exchange.coinbase.com"
                or parsed.path != urlsplit(base).path or parsed.fragment
                or set(actual) != set(query) or any(len(v) != 1 for v in actual.values())
                or actual["granularity"][0] != str(granularity)):
            raise ValueError
        if start is not None and (_url_time(actual["start"][0]) != start or _url_time(actual["end"][0]) != end):
            raise ValueError
    except (ValueError, KeyError) as exc:
        raise DatasetError("declared request URL does not match provider, product, interval and window") from exc
    return supplied


def convert_coinbase_candles(raw: bytes, *, product_id: str, granularity: int,
                             captured_at: int, request_start: int | None = None,
                             request_end: int | None = None,
                             assumed_publication_lag_seconds: int | None = None,
                             volume_unit: str = "unknown", source_kind: str = "real",
                             request_url: str | None = None, now: int | None = None):
    """Convert one six-field Coinbase Exchange response without external I/O.

An explicit window retains only whole candles inside [start, end), using the
provider's opening timestamp. Every excluded raw row is recorded. Incomplete
retained candles refuse. Default availability is the declared capture clock,
not a guessed historical publication time. A supplied lag is an assumption.
"""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RAW_BYTES:
        raise DatasetError("raw response must be 1..1000000 bytes")
    if not isinstance(product_id, str) or not re.fullmatch(r"[A-Z0-9]{2,12}-USD", product_id) or product_id == "USD-USD":
        raise DatasetError("import requires an explicit uppercase BASE-USD product")
    if type(granularity) is not int or granularity not in GRANULARITIES:
        raise DatasetError("unsupported Coinbase candle granularity")
    now = int(time.time()) if now is None else timestamp(now, "now")
    timestamp(captured_at, "captured_at")
    if captured_at > now:
        raise DatasetError("declared capture time is in the future")
    if (request_start is None) != (request_end is None):
        raise DatasetError("request_start and request_end must be supplied together")
    if request_start is not None:
        timestamp(request_start, "request_start"); timestamp(request_end, "request_end")
        if request_start >= request_end or request_end > captured_at:
            raise DatasetError("request window must be ordered and finish by capture time")
        if request_start % granularity or request_end % granularity:
            raise DatasetError("request window boundaries must align to the declared UTC candle grid")
    lag = assumed_publication_lag_seconds
    if lag is not None and (type(lag) is not int or not 0 <= lag <= 86400):
        raise DatasetError("assumed publication lag must be 0..86400 whole seconds")
    if volume_unit not in {"unknown", "base_asset"}:
        raise DatasetError("volume_unit must be unknown or explicitly assumed base_asset")
    if source_kind not in {"real", "test", "synthetic", "unknown"}:
        raise DatasetError("source_kind must be real, test, synthetic or unknown")
    url = _request_url(product_id, granularity, request_start, request_end, request_url)
    response = _json(raw, exact_numbers=True)
    if not isinstance(response, list) or not 1 <= len(response) <= 300:
        raise DatasetError("Coinbase response must contain 1..300 candle arrays")
    parsed, openings = [], []
    for index, row in enumerate(response):
        if not isinstance(row, list) or len(row) != 6:
            raise DatasetError("each Coinbase candle must have exactly six fields")
        opening = timestamp(row[0], "provider candle opening")
        if opening % granularity:
            raise DatasetError("provider candle opening is off the declared UTC grid")
        values = []
        for column, value in zip(("low", "high", "open", "close", "volume"), row[1:]):
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
                raise DatasetError("provider candle values must be JSON numbers")
            try:
                values.append(decimal(value, nonnegative=True, positive=column != "volume"))
            except (ValueError, TypeError, ArithmeticError) as exc:
                raise DatasetError("invalid provider candle " + column) from exc
        low, high, opening_price, close, volume = values
        if low > min(opening_price, close, high) or high < max(opening_price, close, low):
            raise DatasetError("inconsistent provider candle OHLC")
        openings.append(opening)
        parsed.append((index, opening, dict(zip(("low", "high", "open", "close", "volume"),
                                                (format(v, "f") for v in values)))))
    if len(set(openings)) != len(openings):
        raise DatasetError("duplicate provider candle openings; no revision history supplied")
    ascending = all(a < b for a, b in zip(openings, openings[1:]))
    descending = all(a > b for a, b in zip(openings, openings[1:]))
    if not ascending and not descending:
        raise DatasetError("mixed provider candle order; no silent sorting")
    order = "ascending" if ascending else "descending"
    if order == "descending":
        parsed.reverse()
    bars, included, excluded = [], [], []
    source_id = "coinbase-exchange-candles-v1"
    for index, opening, prices in parsed:
        event = opening + granularity
        reasons = []
        if request_start is not None:
            if opening < request_start:
                reasons.append("opens_before_requested_start")
            if event > request_end:
                reasons.append("closes_after_requested_end")
        if reasons:
            excluded.append({"raw_row_index": index, "opening_time": opening, "reasons": reasons})
            continue
        if event > captured_at:
            raise DatasetError("incomplete candle at declared capture time")
        available = captured_at if lag is None else event + lag
        if available > captured_at:
            raise DatasetError("assumed publication time is later than capture time")
        bars.append({"symbol": product_id, "asset_class": "crypto", "venue": "coinbase-exchange",
                     "event_time": event, "interval_seconds": granularity,
                     "information_available_time": available, "ingestion_time": captured_at,
                     "timezone": "UTC", "session": "24/7",
                     "session_date": datetime.fromtimestamp(event - 1, timezone.utc).date().isoformat(),
                     **prices, "revision": 0, "source_id": source_id})
        included.append(index)
    if not bars:
        raise DatasetError("no complete candles remain inside the requested window")
    coverage = {"assessed": False, "reason": "explicit_request_window_not_supplied"}
    if request_start is not None:
        expected = (request_end - request_start) // granularity
        leading = (bars[0]["event_time"] - granularity - request_start) // granularity
        trailing = (request_end - bars[-1]["event_time"]) // granularity
        missing = expected - len(bars)
        coverage = {"assessed": True, "request_start": request_start, "request_end": request_end,
                    "expected_aligned_buckets": expected, "retained_buckets": len(bars),
                    "missing_buckets": missing, "missing_leading_buckets": leading,
                    "missing_trailing_buckets": trailing, "missing_interior_buckets": missing - leading - trailing,
                    "coverage_bounds_complete": leading == trailing == 0,
                    "all_expected_buckets_present": missing == 0,
                    "missing_bucket_cause": "unknown; no-tick intervals and omitted data are not distinguished"}
    parameters = {"product_id": product_id, "granularity": granularity, "captured_at": captured_at,
                  "request_start": request_start, "request_end": request_end,
                  "assumed_publication_lag_seconds": lag, "volume_unit": volume_unit,
                  "source_kind": source_kind, "request_url": url}
    identity = _sha(raw + _encoded(parameters))[:24]
    payload = {"schema_version": "1.0", "dataset_id": "coinbase-import-" + identity,
               "source": {"source_id": source_id, "kind": source_kind,
                          "description": "Operator-declared Coinbase Exchange response; no provider authentication or historical revision evidence",
                          "url": url, "content_sha256": _sha(raw)},
               "asset_class": "crypto", "symbols": [product_id], "venue": "coinbase-exchange",
               "volume_unit": volume_unit, "currency": "USD", "price_adjustment": "unadjusted",
               "universe": {"selection_method": "Operator-selected single USD product; imported development history",
                            "survivorship_bias": "unknown"},
               "availability_basis": "unknown" if lag is None else "assumed",
               "revision_policy": "latest_only", "bars": bars}
    if coverage["assessed"]:
        # Canonical metadata preserves this caveat when a later consumer loads
        # only dataset.json, without the richer import receipt/quality sidecars.
        payload["description"] = ("Requested UTC window " + _utc(request_start) + " through " + _utc(request_end)
                                  + "; expected " + str(coverage["expected_aligned_buckets"])
                                  + " aligned buckets, retained " + str(len(bars)) + ", absent "
                                  + str(coverage["missing_buckets"]) + ". Absence cause is unknown; no bars were filled.")
    validated = validate_dataset(payload, now=now)
    quality = {**validated["quality"], "dataset_hash": validated["dataset_hash"],
               "observation_hash": validated["observation_hash"], "evidence_class": validated["evidence_class"]}
    quality["requested_window_coverage"] = coverage
    if coverage["assessed"] and not coverage["all_expected_buckets_present"]:
        quality["blockers"].append("requested_window_incomplete")
    receipt = {"schema_version": 1, "import_version": IMPORT_VERSION, "parameters": parameters,
               "provider_documentation": PROVIDER_DOCUMENTATION, "raw_sha256": _sha(raw),
               "raw_bytes": len(raw), "raw_row_count": len(response), "retained_row_count": len(bars),
               "input_order": order, "transformations": ["bar_open_to_exclusive_close"] +
                    (["reverse_descending_rows"] if order == "descending" else []),
               "requested_window_coverage": coverage,
               "included_raw_row_indices": included,
               "excluded_rows": sorted(excluded, key=lambda r: r["raw_row_index"]),
               "capture_time_basis": "operator_declared_not_independently_verified",
               "availability_policy": "capture_time_only_historical_publication_unknown" if lag is None else "explicit_assumption_bar_close_plus_lag",
               "volume_policy": "unknown" if volume_unit == "unknown" else "operator_assumes_base_asset_not_independently_verified",
               "revision_zero_meaning": "first retained latest-only snapshot; not proof of original published values",
               "request_url_basis": "operator_declared_or_derived_not_proof_of_request",
               "license_status": "not established by importer",
               "provider_authenticity_verified": False, "alpha_validation_eligible": False,
               "limitations": ["Raw bytes and local capture time do not establish provider authenticity.",
                               "Historical publication times and earlier revisions were not recovered.",
                               "The explicit request window filters complete candles and does not fill gaps.",
                               "Missing intervals may have no ticks; their cause is not inferred.",
                               "This import does not run an experiment, submit an order or certify alpha."]}
    return {"dataset": payload, "quality": quality, "receipt": receipt}


def _external_path(value):
    original = Path(value).expanduser()
    resolved = external_path(original)
    # Check the original spelling before resolving it, including parent links.
    for part in (original, *original.parents):
        if part.is_symlink():
            raise DatasetError("import paths and their parents must not be symlinks")
    return resolved


def _read(path, *, max_bytes):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise DatasetError("import input must be a regular file")
            content = stream.read(max_bytes + 1)
    except OSError as exc:
        raise DatasetError("import file could not be read") from exc
    if len(content) > max_bytes:
        raise DatasetError("import file exceeds maximum size")
    return content


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def import_coinbase_candles(*, input_path, output_dir, **parameters):
    """Create a new external archive; receipt is written last as its manifest.

Existing destinations are never reused. No state is created until raw bytes,
parameters and canonical data all pass validation. File hashes prove internal
consistency, not the truth of the declared provider or capture clock.
"""
    source, destination = _external_path(input_path), _external_path(output_dir)
    if destination.exists():
        raise DatasetError("import destination already exists; refusing overwrite")
    if not destination.parent.is_dir():
        raise DatasetError("import destination parent must already be a directory")
    raw = _read(source, max_bytes=MAX_RAW_BYTES)
    converted = convert_coinbase_candles(raw, **parameters)
    contents = {"raw.json": raw, "dataset.json": _encoded(converted["dataset"]),
                "quality.json": _encoded(converted["quality"])}
    receipt = {**converted["receipt"], "files": {name: {"sha256": _sha(value), "bytes": len(value)}
                                                for name, value in contents.items()}}
    contents["receipt.json"] = _encoded(receipt)
    created = []
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise DatasetError("import destination could not be created exclusively") from exc
    try:
        for name, content in contents.items():
            path = destination / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            created.append(path)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        _sync_dir(destination)
        _sync_dir(destination.parent)
        verified = verify_import_archive(destination, now=parameters.get("now"))
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        destination.rmdir()
        raise
    return {**verified, "output_dir": str(destination),
            "paths": {name: str(destination / name) for name in contents},
            "quality": converted["quality"], "trade_submitted": False, "experiment_run": False}


def verify_import_archive(output_dir, *, now=None):
    """Verify file digests and repeat the exact import; performs no evaluation."""
    root = _external_path(output_dir)
    if not root.is_dir():
        raise DatasetError("import archive must be an existing directory")
    receipt = _json(_read(_external_path(root / "receipt.json"), max_bytes=MAX_RAW_BYTES))
    if not isinstance(receipt, dict) or not isinstance(receipt.get("parameters"), dict):
        raise DatasetError("invalid import receipt")
    files = receipt.get("files")
    if not isinstance(files, dict) or set(files) != set(_FILES):
        raise DatasetError("invalid archive file manifest")
    contents = {}
    for name in _FILES:
        content = _read(_external_path(root / name), max_bytes=MAX_RAW_BYTES)
        if files[name] != {"sha256": _sha(content), "bytes": len(content)}:
            raise DatasetError("archive file digest mismatch: " + name)
        contents[name] = content
    try:
        converted = convert_coinbase_candles(contents["raw.json"], now=now, **receipt["parameters"])
    except TypeError as exc:
        raise DatasetError("invalid archived import parameters") from exc
    if (contents["dataset.json"] != _encoded(converted["dataset"])
            or contents["quality.json"] != _encoded(converted["quality"])
            or receipt != {**converted["receipt"], "files": files}):
        raise DatasetError("archive conversion or receipt integrity mismatch")
    return {"integrity_verified": True, "import_version": IMPORT_VERSION,
            "raw_sha256": converted["receipt"]["raw_sha256"],
            "evidence_class": converted["quality"]["evidence_class"],
            "retained_row_count": converted["receipt"]["retained_row_count"],
            "excluded_row_count": len(converted["receipt"]["excluded_rows"]),
            "provider_authenticity_verified": False, "alpha_validation_eligible": False}
