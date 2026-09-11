"""Bounded public BTC-USD five-minute history capture; no account or order API.

Exchange candle documentation does not explicitly specify its volume units.
This collector preserves unknown units and cannot supply fill-capacity evidence.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import time
from urllib.parse import parse_qs, urlsplit

from atlas.research.imports import (
    MAX_RAW_BYTES, PROVIDER_DOCUMENTATION, _encoded, _external_path, _request_url,
    _sync_dir, _url_time, convert_coinbase_candles,
)
from atlas.research.market import DatasetError, timestamp, validate_dataset


ORIGIN = "https://api.exchange.coinbase.com"
PATH = "/products/BTC-USD/candles"
GRANULARITY = 300
PAGE_SECONDS = 86400  # 288 buckets, allowing a provider boundary bucket below its 300 maximum.
MAX_REQUESTS = 35


@dataclass(frozen=True)
class PublicResponse:
    status: int
    body: bytes


def request_url(start, end):
    timestamp(start)
    timestamp(end)
    if start % GRANULARITY or end % GRANULARITY or not 0 < end - start <= PAGE_SECONDS:
        raise DatasetError("public candle window must be aligned and at most one day")
    return _request_url("BTC-USD", GRANULARITY, start, end, None)


def public_get(url):
    """One pinned HTTPS GET, no authentication, redirects or retry path."""
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, strict_parsing=True)
    if (parsed.scheme != "https" or parsed.netloc != "api.exchange.coinbase.com" or parsed.path != PATH
            or parsed.fragment or set(query) != {"start", "end", "granularity"}
            or any(len(v) != 1 for v in query.values()) or query["granularity"] != ["300"]):
        raise DatasetError("public candle endpoint not allowed")
    start, end = _url_time(query["start"][0]), _url_time(query["end"][0])
    if url != request_url(start, end):
        raise DatasetError("public candle URL must match the exact derived window")
    connection = http.client.HTTPSConnection("api.exchange.coinbase.com", timeout=10, context=ssl.create_default_context())
    try:
        connection.request("GET", parsed.path + "?" + parsed.query,
                           headers={"Accept": "application/json", "User-Agent": "ATLAS-public-history/1"})
        response = connection.getresponse()
        deadline = time.monotonic() + 10
        chunks, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise DatasetError("public candle response deadline exceeded")
            chunk = response.read1(min(65536, MAX_RAW_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RAW_BYTES:
                raise DatasetError("public candle response exceeds byte bound")
        return PublicResponse(response.status, b"".join(chunks))
    finally:
        connection.close()


def _write(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    _sync_dir(path.parent)


def fetch_history(*, start, end, output_dir, transport=None, clock=time.time, progress=None):
    """Capture at most 35 sequential public GETs and immutable per-page receipts.

    start/end bound candle openings in [start,end). Volume remains unknown and
    historical availability is each page's capture time. Partial failures keep
    acquired bytes and a failure receipt but never return a completed dataset.
    """
    timestamp(start)
    timestamp(end)
    if start % GRANULARITY or end % GRANULARITY or not 0 < end - start <= MAX_REQUESTS * PAGE_SECONDS:
        raise DatasetError("history range exceeds aligned 35-request bound")
    initial_clock = timestamp(int(clock()))
    if end > initial_clock:
        raise DatasetError("history range must finish before capture")
    destination = _external_path(output_dir)
    if destination.exists() or not destination.parent.is_dir():
        raise DatasetError("history output must be a new external directory")
    getter = public_get if transport is None else transport
    provenance = "verified_tls_public_coinbase_exchange" if transport is None else "injected_test_transport"
    destination.mkdir(mode=0o700)
    _sync_dir(destination.parent)
    plan = {"schema": 1, "product_id": "BTC-USD", "granularity": GRANULARITY,
            "start": start, "end_exclusive": end, "maximum_requests": MAX_REQUESTS,
            "transport_provenance": provenance, "provider_documentation": PROVIDER_DOCUMENTATION,
            "volume_unit": "unknown", "volume_unit_reason": "Exchange REST candle docs do not explicitly identify base versus quote units",
            "availability_basis": "capture_time_only_historical_publication_unknown", "orders_submitted": 0}
    _write(destination / "plan.json", _encoded(plan))
    pages, datasets, bars, calls = [], [], [], 0
    captured = initial_clock
    try:
        for window_start in range(start, end, PAGE_SECONDS):
            window_end = min(end, window_start + PAGE_SECONDS)
            url = request_url(window_start, window_end)
            calls += 1
            response = getter(url)
            after = timestamp(int(clock()))
            if after < captured or after < window_end:
                raise DatasetError("capture clock moved backwards")
            captured = after
            if type(response) is not PublicResponse or type(response.status) is not int or not isinstance(response.body, bytes) or len(response.body) > MAX_RAW_BYTES:
                raise DatasetError("public candle response contract invalid")
            stem = f"page-{calls:02d}"
            _write(destination / (stem + "-raw.json"), response.body)
            receipt = {"url": url, "method": "GET", "captured_at": captured,
                       "request_start": window_start, "request_end_exclusive": window_end,
                       "http_status": response.status, "raw_sha256": hashlib.sha256(response.body).hexdigest(),
                       "raw_bytes": len(response.body), "transport_provenance": provenance}
            _write(destination / (stem + "-http.json"), _encoded(receipt))
            if response.status != 200:
                raise DatasetError("public candle HTTP refusal; no retry")
            converted = convert_coinbase_candles(response.body, product_id="BTC-USD", granularity=GRANULARITY,
                captured_at=captured, request_start=window_start, request_end=window_end,
                volume_unit="unknown", assumed_publication_lag_seconds=None, source_kind="real" if transport is None else "test",
                request_url=url, now=captured)
            _write(destination / (stem + "-dataset.json"), _encoded(converted["dataset"]))
            _write(destination / (stem + "-import.json"), _encoded(converted["receipt"]))
            _write(destination / (stem + "-quality.json"), _encoded(converted["quality"]))
            datasets.append(converted["dataset"])
            bars.extend(converted["dataset"]["bars"])
            pages.append({**receipt, "import_receipt": stem + "-import.json",
                          "retained_buckets": len(converted["dataset"]["bars"]),
                          "missing_buckets": converted["receipt"]["requested_window_coverage"]["missing_buckets"]})
            if progress is not None:
                progress({"page": calls, "retained_buckets": len(converted["dataset"]["bars"]), "total_bars": len(bars)})
        times = [bar["event_time"] for bar in bars]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise DatasetError("captured pages overlap or reverse; no silent deduplication")
        manifest_hash = hashlib.sha256(_encoded(pages)).hexdigest()
        merged = {**datasets[0], "dataset_id": "coinbase-history-" + manifest_hash[:24], "bars": bars,
                  "description": "Merged nonoverlapping public BTC-USD five-minute windows; volume unit unknown and information availability remains capture time.",
                  "source": {"source_id": "coinbase-exchange-candles-v1", "kind": "real" if transport is None else "test",
                             "description": "Public REST capture; historical publication and revision evidence unavailable",
                             "url": ORIGIN + PATH, "content_sha256": manifest_hash}}
        validated = validate_dataset(merged, now=captured)
        _write(destination / "dataset.json", _encoded(merged))
        _write(destination / "quality.json", _encoded(validated["quality"]))
        summary = {**plan, "complete": True, "captured_at": captured, "requests_performed": calls,
                   "bar_count": len(bars), "expected_bars": (end - start) // GRANULARITY,
                   "missing_bars": (end - start) // GRANULARITY - len(bars),
                   "first_event_time": times[0], "last_event_time": times[-1],
                   "dataset_path": str(destination / "dataset.json"),
                   "dataset_hash": validated["dataset_hash"], "evidence_class": validated["evidence_class"],
                   "volume_strategies_available": False, "alpha_validation_eligible": False,
                   "pages": pages}
        _write(destination / "manifest.json", _encoded(summary))
        return summary
    except Exception:
        _write(destination / "failure.json", _encoded({**plan, "complete": False, "requests_performed": calls,
            "completed_pages": len(pages), "completed_bars": len(bars), "diagnostic": "public_history_capture_incomplete",
            "preserved_pages": pages, "automatic_retry": False}))
        raise
