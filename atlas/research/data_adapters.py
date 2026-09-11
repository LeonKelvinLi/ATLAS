"""Bounded historical intake, separate from all execution and pilot state.

The built-in transport can only GET Alpaca IEX bars, US calendars and asset
metadata. Archive availability is unknown: observing an archive today does not
prove when its values or revisions were available in the past.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import http.client
from itertools import islice
import os
from pathlib import Path
import re
import ssl
import time
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from atlas.alpaca_paper_preflight import HTTPResponse, _credentials
from atlas.domain import decimal
from atlas.research.imports import _encoded, _external_path, _json, _read, _sha, _sync_dir
from atlas.research.market import DatasetError, quality_report, timestamp, validate_dataset


ADAPTER_VERSION = "historical-adapters-1"
DATA_ORIGIN = "https://data.alpaca.markets"
PAPER_ORIGIN = "https://paper-api.alpaca.markets"
MAX_RESPONSE_BYTES = 2_000_000
MAX_ARCHIVE_BYTES = 24_000_000
MAX_ROWS = 20_000
TIMEOUT_SECONDS = 10
INTERVALS = (60, 300, 900, 1800)
NY = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,19}\Z")


class DataAdapterError(DatasetError):
    """Fixed local error code. Never includes provider bodies or credentials."""


def parse_utc(value):
    """Explicit timezone-aware RFC3339 whole seconds; no local-time inference."""
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.0{1,9})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise DataAdapterError("timestamp_requires_explicit_timezone_whole_seconds")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp(int(parsed.timestamp()))
    except (ValueError, OverflowError, OSError):
        raise DataAdapterError("timestamp_invalid") from None


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class HistoricalBarsRequest:
    provider: str
    asset_class: str
    symbols: tuple[str, ...]
    start: int
    end: int
    interval_seconds: int = 300
    feed: str = "iex"
    adjustment: str = "raw"
    max_pages: int = 10
    page_limit: int = 1000

    def __post_init__(self):
        if self.provider not in {"alpaca", "databento", "massive"}:
            raise DataAdapterError("provider_unsupported")
        if self.asset_class not in {"equity", "crypto"}:
            raise DataAdapterError("asset_class_unsupported_no_futures_contract_mapping")
        if (not isinstance(self.symbols, tuple) or not 1 <= len(self.symbols) <= 10
                or any(not isinstance(s, str) or not _SYMBOL.fullmatch(s) for s in self.symbols)
                or len(set(self.symbols)) != len(self.symbols)):
            raise DataAdapterError("symbols_require_1_to_10_unique_explicit_tickers")
        try:
            timestamp(self.start); timestamp(self.end)
        except DatasetError:
            raise DataAdapterError("request_times_invalid") from None
        if not 0 < self.end - self.start <= 31 * 86400:
            raise DataAdapterError("request_window_requires_1_to_2678400_seconds")
        if type(self.interval_seconds) is not int or self.interval_seconds not in INTERVALS:
            raise DataAdapterError("interval_requires_60_300_900_or_1800_seconds")
        if self.start % 60 or self.end % 60:
            raise DataAdapterError("request_bounds_require_whole_minutes")
        if self.adjustment != "raw":
            raise DataAdapterError("only_raw_unadjusted_history_supported")
        if not isinstance(self.feed, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,31}", self.feed):
            raise DataAdapterError("feed_must_be_explicit_identifier")
        if self.provider == "alpaca" and (self.asset_class != "equity" or self.feed != "iex"):
            raise DataAdapterError("alpaca_adapter_requires_equity_iex")
        if (type(self.max_pages) is not int or not 1 <= self.max_pages <= 20
                or type(self.page_limit) is not int or not 1 <= self.page_limit <= 10000
                or self.max_pages * self.page_limit > MAX_ROWS):
            raise DataAdapterError("pagination_bounds_invalid_maximum_20000_rows")

    def to_dict(self):
        return {**asdict(self), "symbols": list(self.symbols)}


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    asset_classes: tuple[str, ...]
    built_in_transport: bool
    readiness: str
    historical_availability: str = "unknown"
    revision_policy: str = "latest_only"
    execution_supported: bool = False
    futures_contracts_supported: bool = False


def provider_capabilities(provider):
    if provider == "alpaca":
        return ProviderCapabilities(provider, ("equity",), True,
                                    "requires_explicit_external_paper_key_and_successful_data_read")
    if provider in {"databento", "massive"}:
        return ProviderCapabilities(provider, (), False,
                                    "unavailable_missing_credentials_and_verified_transport_contract")
    raise DataAdapterError("provider_unsupported")


@dataclass(frozen=True)
class RawCapture:
    role: str
    request_url: str
    received_at: int
    body: bytes = field(repr=False)

    def descriptor(self):
        return {"role": self.role, "request_url": self.request_url,
                "received_at": self.received_at, "sha256": _sha(self.body), "bytes": len(self.body)}


@dataclass(frozen=True)
class HistoricalBundle:
    dataset: dict
    quality: dict
    receipt: dict
    captures: tuple[RawCapture, ...] = field(repr=False)


class HistoricalDataAdapter(Protocol):
    capabilities: ProviderCapabilities

    def fetch_bars(self, request: HistoricalBarsRequest) -> HistoricalBundle: ...


def _target(url):
    """No arbitrary origins, accounts, orders, redirects, or credential queries."""
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
        if (parsed.scheme != "https" or parsed.fragment or len(url) > 8192
                or any(len(v) != 1 for v in query.values())):
            raise ValueError
        if parsed.netloc == "data.alpaca.markets" and parsed.path == "/v2/stocks/bars":
            required = {"symbols", "timeframe", "start", "end", "limit", "adjustment", "feed", "sort", "asof", "currency"}
            if (not required <= query.keys() or query.keys() - required - {"page_token"}
                    or query["feed"] != ["iex"] or query["adjustment"] != ["raw"]
                    or query["sort"] != ["asc"] or query["asof"] != ["-"] or query["currency"] != ["USD"]):
                raise ValueError
        elif parsed.netloc == "paper-api.alpaca.markets" and parsed.path == "/v2/calendar":
            if set(query) != {"start", "end", "date_type"} or query["date_type"] != ["TRADING"]:
                raise ValueError
        elif (parsed.netloc == "paper-api.alpaca.markets"
              and re.fullmatch(r"/v2/assets/[A-Z][A-Z0-9.\-]{0,19}", parsed.path)):
            if query:
                raise ValueError
        else:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DataAdapterError("historical_read_endpoint_refused") from None
    return parsed.netloc, parsed.path + ("?" + parsed.query if parsed.query else "")


def _safe_get(url, headers, *, timeout_seconds=TIMEOUT_SECONDS, max_response_bytes=MAX_RESPONSE_BYTES):
    host, target = _target(url)
    connection = http.client.HTTPSConnection(host, timeout=timeout_seconds, context=ssl.create_default_context())
    deadline = time.monotonic() + timeout_seconds
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise DataAdapterError("historical_redirect_refused")
        if response.status != 200:
            raise DataAdapterError("historical_http_" + str(response.status))
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > max_response_bytes):
            raise DataAdapterError("historical_response_size_invalid")
        chunks, total = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DataAdapterError("historical_request_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_response_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                raise DataAdapterError("historical_response_too_large")
        body = b"".join(chunks)
        if time.monotonic() > deadline or (length is not None and len(body) != int(length)):
            raise DataAdapterError("historical_response_incomplete_or_timeout")
        return HTTPResponse(response.status, body)
    finally:
        connection.close()


def _calendar_url(request):
    start = datetime.fromtimestamp(request.start, NY).date().isoformat()
    end = datetime.fromtimestamp(request.end - 1, NY).date().isoformat()
    return PAPER_ORIGIN + "/v2/calendar?" + urlencode({"start": start, "end": end, "date_type": "TRADING"})


def _bars_url(request, token=None):
    query = {"symbols": ",".join(sorted(request.symbols)), "timeframe": str(request.interval_seconds // 60) + "Min",
             "start": _utc(request.start), "end": _utc(request.end - 1), "limit": request.page_limit,
             "adjustment": "raw", "feed": "iex", "sort": "asc", "asof": "-", "currency": "USD"}
    if token is not None:
        query["page_token"] = token
    return DATA_ORIGIN + "/v2/stocks/bars?" + urlencode(query)


def _response_json(capture):
    try:
        return _json(capture.body, exact_numbers=True)
    except (DatasetError, TypeError, AttributeError):
        raise DataAdapterError("historical_response_json_invalid") from None


def _validate_captures(captures, *, now):
    if not isinstance(captures, tuple) or not 1 <= len(captures) <= 31:
        raise DataAdapterError("historical_capture_count_invalid")
    total, previous = 0, -1
    for capture in captures:
        if (not isinstance(capture, RawCapture) or not isinstance(capture.body, bytes)
                or not 0 < len(capture.body) <= MAX_RESPONSE_BYTES):
            raise DataAdapterError("historical_capture_invalid")
        try:
            timestamp(capture.received_at)
        except DatasetError:
            raise DataAdapterError("historical_capture_time_invalid") from None
        if not previous <= capture.received_at <= now:
            raise DataAdapterError("historical_capture_clock_regressed_or_future")
        if (not isinstance(capture.role, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", capture.role)
                or not isinstance(capture.request_url, str) or not 1 <= len(capture.request_url) <= 8192):
            raise DataAdapterError("historical_capture_metadata_invalid")
        previous = capture.received_at
        total += len(capture.body)
        if total > MAX_ARCHIVE_BYTES:
            raise DataAdapterError("historical_raw_archive_too_large")


def _calendar(raw, request):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 31:
        raise DataAdapterError("historical_calendar_invalid")
    sessions, previous = [], ""
    first = datetime.fromtimestamp(request.start, NY).date().isoformat()
    last = datetime.fromtimestamp(request.end - 1, NY).date().isoformat()
    for row in raw:
        if not isinstance(row, dict) or not {"date", "open", "close"} <= row.keys():
            raise DataAdapterError("historical_calendar_invalid")
        day = row["date"]
        try:
            if (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
                    or not first <= day <= last or day <= previous):
                raise ValueError
            bounds = []
            for key in ("open", "close"):
                value = row[key]
                if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}(?::00)?", value):
                    raise ValueError
                bounds.append(int(datetime.fromisoformat(day + "T" + value).replace(tzinfo=NY).timestamp()))
            opening, closing = bounds
            if row["open"] not in {"09:30", "09:30:00"} or row["close"] not in {"13:00", "13:00:00", "16:00", "16:00:00"}:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise DataAdapterError("historical_calendar_bounds_invalid") from None
        sessions.append({"session_date": day, "open_time": opening, "close_time": closing})
        previous = day
    return {"calendar_id": "US_EQUITY_REGULAR", "timezone": "America/New_York",
            "version": "alpaca-v2-" + _sha(_encoded(sessions))[:16],
            "source_id": "alpaca-paper-calendar-v2", "sessions": sessions}


def _asset(raw, symbol):
    if (not isinstance(raw, dict) or raw.get("symbol") != symbol or raw.get("class") != "us_equity"
            or raw.get("status") not in {"active", "inactive"}):
        raise DataAdapterError("historical_asset_identity_invalid")
    flags = {}
    for key in ("tradable", "fractionable"):
        value = raw.get(key)
        if value is not None and type(value) is not bool:
            raise DataAdapterError("historical_asset_flags_invalid")
        flags[key] = value
    return {"symbol": symbol, "asset_class": "equity", "status": raw["status"], **flags,
            "historical_universe_membership_verified": False,
            "instrument_role": {"GLD": "gold_exchange_traded_proxy_not_futures",
                                "USO": "oil_exchange_traded_proxy_not_futures"}.get(symbol, "us_listed_equity_or_etf")}


def _page(raw, page_limit):
    if (not isinstance(raw, dict) or not {"bars", "next_page_token"} <= raw.keys()
            or not isinstance(raw["bars"], dict)):
        raise DataAdapterError("historical_bars_page_invalid")
    token = raw["next_page_token"]
    if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 4096
                              or not token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise DataAdapterError("historical_page_token_invalid")
    if any(not isinstance(rows, list) for rows in raw["bars"].values()):
        raise DataAdapterError("historical_bars_page_invalid")
    count = sum(len(rows) for rows in raw["bars"].values())
    if count > page_limit or (not count and token is not None):
        raise DataAdapterError("historical_page_count_invalid")
    return raw["bars"], token


def normalize_alpaca_history(request, captures, *, now=None, source_kind="real"):
    """Replay exact captures offline. Receipt hashes are internal evidence only."""
    if not isinstance(request, HistoricalBarsRequest) or request.provider != "alpaca":
        raise DataAdapterError("alpaca_request_required")
    if source_kind not in {"real", "test", "synthetic", "unknown"}:
        raise DataAdapterError("historical_source_kind_invalid")
    now = int(time.time()) if now is None else timestamp(now)
    _validate_captures(captures, now=now)
    if request.end > captures[0].received_at:
        raise DataAdapterError("historical_request_end_after_capture")
    count = len(request.symbols)
    if not count + 2 <= len(captures) <= count + 1 + request.max_pages:
        raise DataAdapterError("historical_capture_roles_invalid")
    first = captures[0]
    if first.role != "calendar" or first.request_url != _calendar_url(request):
        raise DataAdapterError("historical_calendar_capture_mismatch")
    calendar = _calendar(_response_json(first), request)
    sessions = {s["session_date"]: (s["open_time"], s["close_time"]) for s in calendar["sessions"]}
    assets = []
    for symbol, capture in zip(sorted(request.symbols), captures[1:count + 1]):
        if capture.role != "asset:" + symbol or capture.request_url != PAPER_ORIGIN + "/v2/assets/" + symbol:
            raise DataAdapterError("historical_asset_capture_mismatch")
        assets.append(_asset(_response_json(capture), symbol))
    acquired = captures[-1].received_at
    source_id = "alpaca-iex-bars-v2"
    bars, excluded, token, seen_tokens = [], [], None, set()
    previous, raw_count = None, 0
    for page_index, capture in enumerate(captures[count + 1:]):
        if capture.role != "bars" or capture.request_url != _bars_url(request, token):
            raise DataAdapterError("historical_bars_capture_mismatch")
        mapping, token = _page(_response_json(capture), request.page_limit)
        if set(mapping) - set(request.symbols):
            raise DataAdapterError("historical_unrequested_symbol")
        for symbol, rows in sorted(mapping.items()):
            for row_index, row in enumerate(rows):
                raw_count += 1
                if raw_count > MAX_ROWS:
                    raise DataAdapterError("historical_rows_exceed_bound")
                if not isinstance(row, dict) or not {"t", "o", "h", "l", "c", "v"} <= row.keys():
                    raise DataAdapterError("historical_bar_shape_invalid")
                opening = parse_utc(row["t"])
                identity = (symbol, opening)
                if previous is not None and identity <= previous:
                    raise DataAdapterError("historical_duplicate_or_nonascending_bars")
                previous = identity
                prices = {}
                for old, new in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")):
                    value = row[old]
                    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
                        raise DataAdapterError("historical_bar_value_must_be_json_number")
                    try:
                        prices[new] = format(decimal(value, nonnegative=True, positive=old != "v"), "f")
                    except (ValueError, ArithmeticError):
                        raise DataAdapterError("historical_bar_value_invalid") from None
                if (decimal(prices["low"]) > min(decimal(prices[k]) for k in ("open", "high", "close"))
                        or decimal(prices["high"]) < max(decimal(prices[k]) for k in ("open", "low", "close"))):
                    raise DataAdapterError("historical_bar_ohlc_invalid")
                event = opening + request.interval_seconds
                day = datetime.fromtimestamp(opening, NY).date().isoformat()
                bounds = sessions.get(day)
                reasons = []
                if opening < request.start or event > request.end:
                    reasons.append("outside_complete_requested_window")
                if bounds is None or not bounds[0] <= opening < event <= bounds[1]:
                    reasons.append("outside_explicit_regular_session")
                if reasons:
                    excluded.append({"page_index": page_index, "symbol": symbol, "row_index": row_index,
                                     "opening_time": opening, "reasons": reasons})
                    continue
                if (event - bounds[0]) % request.interval_seconds:
                    raise DataAdapterError("historical_bar_off_session_grid")
                bars.append({"symbol": symbol, "asset_class": "equity", "venue": "alpaca-iex",
                             "event_time": event, "interval_seconds": request.interval_seconds,
                             "information_available_time": acquired, "ingestion_time": acquired,
                             "timezone": "UTC", "session": "regular", "session_date": day,
                             **prices, "revision": 0, "source_id": source_id})
        if token is not None:
            if token in seen_tokens:
                raise DataAdapterError("historical_pagination_token_repeated")
            seen_tokens.add(token)
        if token is None and page_index != len(captures) - count - 2:
            raise DataAdapterError("historical_pages_after_terminal_page")
    if token is not None:
        raise DataAdapterError("historical_pagination_incomplete")
    if not bars or {b["symbol"] for b in bars} != set(request.symbols):
        raise DataAdapterError("historical_requested_symbol_has_no_complete_regular_bars")
    bars.sort(key=lambda b: (b["event_time"], b["symbol"]))
    expected = {event for opening, closing in sessions.values()
                for event in range(opening + request.interval_seconds, closing + 1, request.interval_seconds)
                if request.start <= event - request.interval_seconds and event <= request.end}
    coverage = {}
    for symbol in sorted(request.symbols):
        observed = {bar["event_time"] for bar in bars if bar["symbol"] == symbol}
        coverage[symbol] = {"expected_complete_regular_buckets": len(expected), "observed_buckets": len(observed),
                            "missing_buckets": len(expected - observed),
                            "all_expected_buckets_present": expected == observed,
                            "missing_bucket_reason": "unknown_no_trade_or_missing_data_not_inferred"}
    manifest = [c.descriptor() for c in captures]
    raw_hash = _sha(_encoded(manifest))
    payload = {"schema_version": "1.0", "dataset_id": "alpaca-iex-" + raw_hash[:24],
               "source": {"source_id": source_id, "kind": source_kind,
                          "description": "Alpaca IEX raw historical archive; capture does not establish historical availability",
                          "url": "https://docs.alpaca.markets/us/reference/stockbars", "content_sha256": raw_hash},
               "asset_class": "equity", "symbols": sorted(request.symbols), "venue": "alpaca-iex",
               "price_adjustment": "unadjusted", "volume_unit": "shares", "currency": "USD",
               "universe": {"selection_method": "explicit present-day operator ticker selection; GLD/USO are exchange-traded proxies, not futures",
                            "survivorship_bias": "known"},
               "availability_basis": "unknown", "revision_policy": "latest_only", "calendar": calendar, "bars": bars}
    quality = quality_report(payload, now=now)
    if not quality["valid"]:
        raise DataAdapterError("historical_canonical_validation_failed")
    receipt = {"adapter_version": ADAPTER_VERSION, "provider": "alpaca", "request": request.to_dict(),
               "source_kind": source_kind, "raw_manifest_sha256": raw_hash, "captures": manifest,
               "captured_at": acquired, "capture_time_basis": "local_response_receipt_clock_not_provider_publication_time",
               "information_available_time_policy": "batch_acquisition_time_placeholder_with_availability_basis_unknown",
               "revision_zero_meaning": "first imported archive value, not proof of originally published value",
               "volume_policy": "unadjusted_equity_bar_volume_shares_no_quote_size_conversion_or_fill_capacity_proof",
               "feed": "iex", "consolidated_market_coverage": False, "symbol_mapping": "disabled_asof_dash",
               "assets_current_only": assets, "raw_row_count": raw_count, "retained_row_count": len(bars),
               "requested_window_coverage": coverage,
               "excluded_rows": excluded, "pagination_complete": True,
               "transformations": ["opening_timestamp_plus_interval_to_bar_close", "filter_complete_regular_session_bars",
                                   "sort_validated_symbol_major_bars_by_event_time_then_symbol", "exact_json_numbers_to_decimal_strings"],
               "dataset_hash": quality["dataset_hash"], "evidence_class": quality["evidence_class"],
               "provider_authenticity_verified": False, "alpha_validation_eligible": False,
               "actual_execution_costs": None, "orders_submitted": 0, "execution_enabled": False,
               "limitations": ["IEX is one venue; archive values and latest revisions are not point-in-time evidence.",
                               "Current asset status is not historical universe membership.",
                               "Corporate action cash flows, execution quotes, depth and actual trading costs are not supplied.",
                               "GLD and USO proxy exposure does not implement gold or oil futures contracts."]}
    return HistoricalBundle(payload, quality, receipt, captures)


class AlpacaHistoricalAdapter:
    capabilities = provider_capabilities("alpaca")

    def __init__(self, key_file, *, get=None, clock=None, source_kind="real"):
        if source_kind not in {"real", "test", "synthetic", "unknown"}:
            raise DataAdapterError("historical_source_kind_invalid")
        self._key_file = key_file
        self._get = _safe_get if get is None else get
        self._clock = time.time if clock is None else clock
        self._source_kind = source_kind

    def fetch_bars(self, request):
        if not isinstance(request, HistoricalBarsRequest) or request.provider != "alpaca":
            raise DataAdapterError("alpaca_request_required")
        started = int(self._clock())
        if request.end > started:
            raise DataAdapterError("historical_request_end_is_future")
        try:
            _external_path(self._key_file)
            credential = _credentials(self._key_file)
        except Exception:
            raise DataAdapterError("historical_credential_file_invalid") from None
        headers = {"APCA-API-KEY-ID": credential.api_key, "APCA-API-SECRET-KEY": credential.api_secret,
                   "Accept": "application/json", "User-Agent": "ATLAS-read-only-historical/1"}
        captures, total = [], 0

        def capture(role, url):
            nonlocal total
            _target(url)
            try:
                response = self._get(url, headers, timeout_seconds=TIMEOUT_SECONDS, max_response_bytes=MAX_RESPONSE_BYTES)
            except DataAdapterError:
                raise
            except Exception:
                raise DataAdapterError("historical_request_failed") from None
            if type(response.status) is not int or response.status != 200:
                status = response.status if type(response.status) is int and 100 <= response.status <= 599 else "invalid"
                raise DataAdapterError("historical_http_" + str(status))
            if not isinstance(response.body, bytes) or not 0 < len(response.body) <= MAX_RESPONSE_BYTES:
                raise DataAdapterError("historical_response_size_invalid")
            received = int(self._clock())
            if received < (captures[-1].received_at if captures else started) or received - started > 300:
                raise DataAdapterError("historical_batch_clock_regressed_or_timeout")
            total += len(response.body)
            if total > MAX_ARCHIVE_BYTES:
                raise DataAdapterError("historical_raw_archive_too_large")
            result = RawCapture(role, url, received, response.body)
            captures.append(result)
            return _response_json(result)

        _calendar(capture("calendar", _calendar_url(request)), request)
        for symbol in sorted(request.symbols):
            _asset(capture("asset:" + symbol, PAPER_ORIGIN + "/v2/assets/" + symbol), symbol)
        token, seen = None, set()
        for _ in range(request.max_pages):
            _, token = _page(capture("bars", _bars_url(request, token)), request.page_limit)
            if token is None:
                break
            if token in seen:
                raise DataAdapterError("historical_pagination_token_repeated")
            seen.add(token)
        else:
            raise DataAdapterError("historical_pagination_limit_reached_no_partial_dataset")
        return normalize_alpaca_history(request, tuple(captures), now=int(self._clock()), source_kind=self._source_kind)


class ExternalHistoricalAdapter:
    """Injection contract only; no credential discovery, paid calls or SDK setup.

An explicit caller may supply a bounded fetcher and provider-specific normalizer
for offline fixtures/exports. This does not establish a live provider integration.
"""

    def __init__(self, provider, *, fetcher: Callable | None = None, normalizer: Callable | None = None, clock=None):
        if provider not in {"databento", "massive"}:
            raise DataAdapterError("external_provider_unsupported")
        self.capabilities = provider_capabilities(provider)
        self._fetcher, self._normalizer = fetcher, normalizer
        self._clock = time.time if clock is None else clock

    def fetch_bars(self, request):
        if not isinstance(request, HistoricalBarsRequest) or request.provider != self.capabilities.provider:
            raise DataAdapterError("historical_provider_request_mismatch")
        if self._fetcher is None or self._normalizer is None:
            raise DataAdapterError("provider_unavailable_missing_credentials_and_verified_transport_contract")
        captures = tuple(islice(self._fetcher(request), 32))
        now = int(self._clock())
        _validate_captures(captures, now=now)
        payload = self._normalizer(request, captures)
        result = validate_dataset(payload, now=now)
        acquired = captures[-1].received_at
        if (payload["availability_basis"] != "unknown" or payload["revision_policy"] != "latest_only"
                or payload["price_adjustment"] != "unadjusted" or payload["asset_class"] != request.asset_class
                or set(payload["symbols"]) != set(request.symbols)
                or not payload["source"]["source_id"].startswith(request.provider + "-")
                or any(row["information_available_time"] != acquired or row["ingestion_time"] != acquired
                       or row["interval_seconds"] != request.interval_seconds
                       or not request.start <= row["event_time"] - request.interval_seconds < row["event_time"] <= request.end
                       for row in payload["bars"])):
            raise DataAdapterError("external_normalizer_contract_mismatch")
        manifest = [c.descriptor() for c in captures]
        receipt = {"adapter_version": ADAPTER_VERSION, "provider": request.provider,
                   "request": request.to_dict(), "captures": manifest,
                   "raw_manifest_sha256": _sha(_encoded(manifest)), "captured_at": acquired,
                   "injected_contract_only": True, "built_in_transport": False,
                   "evidence_class": result["evidence_class"], "provider_authenticity_verified": False,
                   "alpha_validation_eligible": False, "actual_execution_costs": None,
                   "orders_submitted": 0, "execution_enabled": False}
        return HistoricalBundle(payload, quality_report(payload, now=now), receipt, captures)


class DatabentoHistoricalAdapter(ExternalHistoricalAdapter):
    def __init__(self, **kwargs):
        super().__init__("databento", **kwargs)


class MassiveHistoricalAdapter(ExternalHistoricalAdapter):
    def __init__(self, **kwargs):
        super().__init__("massive", **kwargs)


def archive_historical_bundle(bundle, output_dir):
    """New private external archive, raw bytes retained; manifest written last."""
    if not isinstance(bundle, HistoricalBundle) or bundle.receipt.get("provider") != "alpaca":
        raise DataAdapterError("replayable_alpaca_bundle_required_for_archive")
    destination = _external_path(output_dir)
    if destination.exists() or not destination.parent.is_dir():
        raise DataAdapterError("historical_archive_requires_new_external_destination")
    request = HistoricalBarsRequest(**{**bundle.receipt["request"], "symbols": tuple(bundle.receipt["request"]["symbols"])})
    replayed = normalize_alpaca_history(request, bundle.captures, now=bundle.receipt["captured_at"],
                                       source_kind=bundle.receipt["source_kind"])
    if (bundle.dataset, bundle.quality, bundle.receipt) != (replayed.dataset, replayed.quality, replayed.receipt):
        raise DataAdapterError("historical_bundle_replay_mismatch")
    contents = {f"raw-{i:03d}.json": capture.body for i, capture in enumerate(bundle.captures)}
    contents.update({"dataset.json": _encoded(bundle.dataset), "quality.json": _encoded(bundle.quality)})
    receipt = {**bundle.receipt, "files": {name: {"sha256": _sha(value), "bytes": len(value)} for name, value in contents.items()}}
    contents["receipt.json"] = _encoded(receipt)
    created = []
    try:
        destination.mkdir(mode=0o700)
    except OSError:
        raise DataAdapterError("historical_archive_creation_failed") from None
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
        verified = verify_historical_archive(destination, now=bundle.receipt["captured_at"])
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        destination.rmdir()
        raise
    return {**verified, "output_dir": str(destination), "dataset_path": str(destination / "dataset.json"),
            "receipt_path": str(destination / "receipt.json"), "orders_submitted": 0, "execution_enabled": False}


def verify_historical_archive(output_dir, *, now=None):
    root = _external_path(output_dir)
    receipt = _json(_read(_external_path(root / "receipt.json"), max_bytes=MAX_ARCHIVE_BYTES))
    try:
        parameters = receipt["request"]
        request = HistoricalBarsRequest(**{**parameters, "symbols": tuple(parameters["symbols"])})
        descriptors = receipt["captures"]
        files = receipt["files"]
        if (not isinstance(descriptors, list) or not 1 <= len(descriptors) <= 31
                or not isinstance(files, dict)
                or set(files) != {"dataset.json", "quality.json"} | {f"raw-{i:03d}.json" for i in range(len(descriptors))}):
            raise ValueError
        content = {}
        for name, expected in files.items():
            limit = MAX_RESPONSE_BYTES if name.startswith("raw-") else MAX_ARCHIVE_BYTES
            raw = _read(_external_path(root / name), max_bytes=limit)
            if expected != {"sha256": _sha(raw), "bytes": len(raw)}:
                raise ValueError
            content[name] = raw
        captures = tuple(RawCapture(role=d["role"], request_url=d["request_url"], received_at=d["received_at"],
                                    body=content[f"raw-{i:03d}.json"]) for i, d in enumerate(descriptors))
        bundle = normalize_alpaca_history(request, captures, now=now, source_kind=receipt["source_kind"])
        if (receipt != {**bundle.receipt, "files": files} or content["dataset.json"] != _encoded(bundle.dataset)
                or content["quality.json"] != _encoded(bundle.quality)):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DataAdapterError("historical_archive_integrity_mismatch") from None
    return {"integrity_verified": True, "adapter_version": ADAPTER_VERSION,
            "raw_manifest_sha256": bundle.receipt["raw_manifest_sha256"], "dataset_hash": bundle.quality["dataset_hash"],
            "retained_row_count": len(bundle.dataset["bars"]), "excluded_row_count": len(bundle.receipt["excluded_rows"]),
            "evidence_class": bundle.quality["evidence_class"], "provider_authenticity_verified": False,
            "alpha_validation_eligible": False}
