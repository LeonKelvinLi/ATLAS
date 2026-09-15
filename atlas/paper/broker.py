"""Bounded Alpaca PAPER equity API; network mutations require explicit opt-in.

Injected transports exercise the production request/response contract and always
produce TEST evidence. No method retries a write. The controller owns durable
single-use permits, account binding, exposure policy and reconciliation.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import http.client
import json
import math
import re
import ssl
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from atlas.alpaca_paper_preflight import HTTPResponse, _credentials
from atlas.domain import decimal, financial_context
from atlas.risk import account_fingerprint

PAPER_ORIGIN = "https://paper-api.alpaca.markets"
DATA_ORIGIN = "https://data.alpaca.markets"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1_000_000
MAX_REQUESTS_PER_MINUTE = 120
MAX_RETRY_DELAY_SECONDS = 366 * 86400
STATUSES = frozenset({
    "new", "partially_filled", "filled", "done_for_day", "canceled", "expired",
    "replaced", "pending_cancel", "pending_replace", "accepted", "pending_new",
    "accepted_for_bidding", "stopped", "rejected", "suspended", "calculated", "held",
})
ACCOUNT_STATUSES = frozenset({"INACTIVE", "PAPER_ONLY", "ONBOARDING", "SUBMISSION_FAILED", "SUBMITTED",
    "ACCOUNT_UPDATED", "APPROVAL_PENDING", "ACTIVE", "REJECTED", "ACCOUNT_CLOSED", "APPROVED",
    "ACCOUNT_CLOSED_PENDING", "ACTION_REQUIRED", "LIMITED"})


class PaperBrokerError(Exception):
    """Closed local diagnostic; never retain untrusted provider text or headers."""

    def __init__(self, code, *, retry_after=None, ambiguous=False, http_status=None):
        if not isinstance(code, str) or not re.fullmatch(r"paper_[a-z0-9_]{1,100}", code):
            raise ValueError("invalid paper broker error code")
        self.code = code
        self.retry_after = retry_after
        self.ambiguous = bool(ambiguous)
        self.http_status = http_status
        super().__init__(code)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = field(default=b"", repr=False)
    headers: dict = field(default_factory=dict, repr=False)


def _fail(code="paper_response_contract_invalid"):
    raise PaperBrokerError(code)


def _json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("paper_json_invalid")
            result[key] = value
        return result

    try:
        return json.loads(body.decode("utf-8"), parse_float=Decimal,
                          parse_constant=lambda _: _fail("paper_json_invalid"),
                          object_pairs_hook=pairs)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail("paper_json_invalid")


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", value):
        _fail("paper_equity_symbol_invalid")
    return value


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, AttributeError):
        _fail("paper_identifier_invalid")


def _client_id(value):
    # Alpaca permits caller-chosen identifiers up to 48 characters. Restrict the
    # alphabet so identifiers cannot add path/query components or log controls.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value):
        _fail("paper_client_identifier_invalid")
    return value


def _number(value, *, positive=False, nonnegative=True, json_number=False):
    if ((json_number and (isinstance(value, bool) or not isinstance(value, (int, Decimal))))
            or (not json_number and not isinstance(value, str))):
        _fail("paper_financial_field_invalid")
    try:
        return format(decimal(value, positive=positive, nonnegative=nonnegative), "f")
    except ValueError:
        _fail("paper_financial_field_invalid")


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value):
        _fail("paper_timestamp_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        if not math.isfinite(result) or not 0 < result < 32503680000:
            raise ValueError
        return result
    except (ValueError, OverflowError):
        _fail("paper_timestamp_invalid")


def _after(value):
    if value is None:
        return None
    if type(value) in {int, float} and math.isfinite(value) and 0 < value < 32503680000:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    _timestamp(value)
    return value


def _boolean(row, field_name):
    if type(row.get(field_name)) is not bool:
        _fail("paper_boolean_field_invalid")
    return row[field_name]


@financial_context
def _terms(payload):
    required = {"symbol", "side", "qty", "limit_price", "type", "time_in_force", "client_order_id"}
    if not isinstance(payload, dict) or set(payload) not in (required, required | {"extended_hours"}):
        _fail("paper_order_terms_invalid")
    if (not isinstance(payload["side"], str) or payload["side"] not in {"buy", "sell"} or payload["type"] != "limit"
            or payload["time_in_force"] != "day" or payload.get("extended_hours", False) is not False):
        _fail("paper_order_terms_invalid")
    qty = _number(payload["qty"], positive=True)
    price = _number(payload["limit_price"], positive=True)
    # Fractional quantity: at most 9 decimals; prices >= $1: cents, below: 0.0001.
    if (Decimal(qty).as_tuple().exponent < -9
            or Decimal(price).as_tuple().exponent < (-2 if Decimal(price) >= 1 else -4)
            or Decimal(qty) * Decimal(price) > 100):
        _fail("paper_order_precision_or_notional_invalid")
    return {"symbol": _symbol(payload["symbol"]), "side": payload["side"], "qty": qty,
            "limit_price": price, "type": "limit", "time_in_force": "day",
            "client_order_id": _client_id(payload["client_order_id"]), "extended_hours": False}


def _query(parsed):
    try:
        pairs = parse_qsl(parsed.query, strict_parsing=True, keep_blank_values=True)
        result = dict(pairs)
        if len(result) != len(pairs) or parsed.query != urlencode(result):
            raise ValueError
        return result
    except ValueError:
        _fail("paper_endpoint_not_allowed")


def _endpoint(method, url, payload):
    """Validate complete targets before credential loading or transport calls."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.fragment or parsed.netloc not in {
            "paper-api.alpaca.markets", "data.alpaca.markets"}:
        _fail("paper_endpoint_not_allowed")
    query = _query(parsed)
    allowed = False
    if parsed.netloc == "data.alpaca.markets":
        if method == "GET" and parsed.path == "/v2/stocks/quotes/latest" and set(query) == {"symbols", "feed"}:
            _symbol(query["symbols"])
            allowed = query["feed"] == "iex"
    elif method == "GET":
        if parsed.path in {"/v2/account", "/v2/positions", "/v2/clock"}:
            allowed = not query
        elif parsed.path == "/v2/orders":
            allowed = (set(query) in ({"status", "limit", "direction", "nested"},
                                     {"status", "limit", "direction", "nested", "after"})
                       and query.get("status") in {"open", "closed", "all"}
                       and query.get("limit") == "500" and query.get("direction") == "asc"
                       and query.get("nested") == "false")
            if "after" in query:
                _timestamp(query["after"])
        elif parsed.path == "/v2/orders:by_client_order_id" and set(query) == {"client_order_id"}:
            _client_id(query["client_order_id"])
            allowed = True
        elif parsed.path.startswith("/v2/assets/") and not query:
            _symbol(parsed.path.removeprefix("/v2/assets/"))
            allowed = True
        elif parsed.path == "/v2/account/activities":
            allowed = (set(query) <= {"direction", "page_size", "page_token", "after"}
                       and query.get("direction") == "asc" and query.get("page_size") == "100")
            if "page_token" in query:
                _activity_id(query["page_token"])
            if "after" in query:
                _timestamp(query["after"])
    elif method == "POST" and parsed.path == "/v2/orders" and not query:
        allowed = payload == _terms(payload)
    elif method == "DELETE" and parsed.path.startswith("/v2/orders/") and not query:
        _uuid(parsed.path.removeprefix("/v2/orders/"))
        allowed = payload is None
    if not allowed or (method == "GET" and payload is not None):
        _fail("paper_endpoint_not_allowed")
    return parsed.netloc, parsed.path + ("?" + parsed.query if parsed.query else "")


def _owned_http(method, url, headers, payload, *, paper_execution_enabled=False):
    host, target = _endpoint(method, url, payload)
    if method != "GET" and paper_execution_enabled is not True:
        _fail("paper_network_activation_pending_review")
    connection = http.client.HTTPSConnection(host, timeout=TIMEOUT_SECONDS,
                                             context=ssl.create_default_context())
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        body = None if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        # No redirect following. Error bytes are never needed for public errors.
        selected = {name: response.getheader(name) for name in ("Retry-After", "X-RateLimit-Reset")}
        if response.status != 200:
            return Response(response.status, headers=selected)
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            _fail("paper_response_size_invalid")
        chunks, total = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("paper_request_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                _fail("paper_response_size_invalid")
        if time.monotonic() > deadline or (length is not None and int(length) != total):
            _fail("paper_response_incomplete")
        return Response(response.status, b"".join(chunks), selected)
    finally:
        connection.close()


def _retry_after(headers, now):
    if not isinstance(headers, dict):
        _fail("paper_rate_limit_header_invalid")
    delays = [1.0]
    # Preserve every matching header, including differently-cased duplicates;
    # taking only the last value could shorten a valid earlier restriction.
    for header, value in headers.items():
        if not isinstance(header, str) or header.lower() not in {"retry-after", "x-ratelimit-reset"} or value is None:
            continue
        name = header.lower()
        if (not isinstance(value, (str, int, float)) or isinstance(value, bool)
                or (isinstance(value, str) and len(value) > 128)):
            _fail("paper_rate_limit_header_invalid")
        try:
            try:
                number = float(value)
            except ValueError:
                if name != "retry-after" or not isinstance(value, str):
                    raise
                at = parsedate_to_datetime(value)
                if at.tzinfo is None:
                    raise ValueError
                number = at.timestamp() - now
            else:
                if name == "retry-after" and number < 0:
                    raise ValueError
                number = number - now if name == "x-ratelimit-reset" else number
            if not math.isfinite(number) or number > MAX_RETRY_DELAY_SECONDS:
                raise ValueError
            delays.append(max(1.0, number))
        except (ValueError, TypeError, OverflowError):
            # An unrepresentable restriction never becomes an earlier retry.
            _fail("paper_rate_limit_header_invalid")
    return max(delays) if len(delays) > 1 else 60.0


def _order(row):
    if not isinstance(row, dict):
        _fail("paper_order_contract_invalid")
    if (row.get("asset_class") != "us_equity" or row.get("type") != "limit"
            or not isinstance(row.get("side"), str) or not isinstance(row.get("status"), str)
            or row.get("side") not in {"buy", "sell"} or row.get("time_in_force") != "day"
            or row.get("notional") is not None or row.get("status") not in STATUSES
            or row.get("extended_hours") is not False or row.get("order_class", "simple") not in ("simple", "")
            or row.get("legs") not in (None, [])):
        _fail("paper_order_contract_invalid")
    qty = _number(row.get("qty"), positive=True)
    filled = _number(row.get("filled_qty"))
    price = _number(row.get("limit_price"), positive=True)
    average = None if row.get("filled_avg_price") is None else _number(row["filled_avg_price"], positive=True)
    status = row["status"]
    if (Decimal(filled) > Decimal(qty) or (Decimal(filled) > 0 and average is None)
            or (status == "filled" and filled != qty)
            or (status == "partially_filled" and not 0 < Decimal(filled) < Decimal(qty))
            or (status in {"new", "pending_new", "accepted", "rejected"} and Decimal(filled) != 0)):
        _fail("paper_order_fill_inconsistent")
    if Decimal(filled) > 0 and ((row["side"] == "buy" and Decimal(average) > Decimal(price))
                               or (row["side"] == "sell" and Decimal(average) < Decimal(price))):
        _fail("paper_order_fill_violates_limit")
    result = {"id": _uuid(row.get("id")), "client_order_id": _client_id(row.get("client_order_id")),
              "symbol": _symbol(row.get("symbol")), "side": row["side"], "qty": qty,
              "limit_price": price, "status": status, "filled_qty": filled, "filled_avg_price": average,
              "created_at": _timestamp(row.get("created_at")), "updated_at": _timestamp(row.get("updated_at")),
              "replaces": _uuid(row["replaces"]) if row.get("replaces") is not None else None,
              "replaced_by": _uuid(row["replaced_by"]) if row.get("replaced_by") is not None else None,
              "time_in_force": "day", "extended_hours": False, "asset_class": "us_equity", "type": "limit"}
    if result["updated_at"] < result["created_at"] or (status == "replaced" and result["replaced_by"] is None):
        _fail("paper_order_chronology_or_replacement_invalid")
    return result


def _activity_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value):
        _fail("paper_activity_identifier_invalid")
    return value


def _activity(row):
    if not isinstance(row, dict) or not isinstance(row.get("activity_type"), str) or not re.fullmatch(
            r"[A-Z][A-Z0-9_]{0,63}", row["activity_type"]):
        _fail("paper_activity_contract_invalid")
    at = row.get("transaction_time")
    if at is None:
        date = row.get("date")
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            _fail("paper_activity_timestamp_missing")
        at = date + "T00:00:00Z"
    result = {"id": _activity_id(row.get("id")), "type": row["activity_type"], "at": _timestamp(at)}
    if row["activity_type"] == "FILL":
        result.update(order_id=_uuid(row.get("order_id")), symbol=_symbol(row.get("symbol")),
                      qty=_number(row.get("qty"), positive=True), price=_number(row.get("price"), positive=True))
        if not isinstance(row.get("side"), str) or row.get("side") not in {"buy", "sell"}:
            _fail("paper_activity_side_invalid")
        result["side"] = row["side"]
    # Fees are unknown unless a provider field actually supplies them.
    if row.get("fee") is not None:
        result["fee"] = _number(row["fee"])
    return result


class AlpacaPaperAPI:
    """No default credentials, automatic sessions, workers or live origin."""

    def __init__(self, credentials_path=None, *, transport=None, clock=time.time, paper_execution_enabled=False):
        if ((transport is not None and not callable(transport)) or not callable(clock)
                or type(paper_execution_enabled) is not bool):
            _fail("paper_client_configuration_invalid")
        self._credentials_path = credentials_path
        self._credentials = None
        self._transport = _owned_http if transport is None else transport
        self._injected = transport is not None
        self._paper_execution_enabled = paper_execution_enabled
        self._clock = clock
        self._gate = None
        self._lock = threading.RLock()
        self._requests = deque()
        self._last_time = None
        self._blocked_until = 0
        self._rate_limit_fault = False

    @property
    def provenance(self):
        return "TEST" if self._injected else "PAPER_API"

    @property
    def transport_provenance(self):
        return self.provenance

    @property
    def execution_enabled(self):
        return self._injected or self._paper_execution_enabled

    def __repr__(self):
        return f"AlpacaPaperAPI(provenance={self.provenance!r}, credentials=redacted)"

    def bind_gate(self, callback):
        with self._lock:
            if self._gate is not None or not callable(callback):
                _fail("paper_mutation_gate_already_bound_or_invalid")
            self._gate = callback

    def _now(self):
        value = self._clock()
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value < 32503680000:
            _fail("paper_clock_invalid")
        if self._last_time is not None and value < self._last_time:
            _fail("paper_clock_rewind")
        self._last_time = value
        return value

    def _rate_check(self, now):
        if self._rate_limit_fault:
            _fail("paper_rate_limit_header_invalid")
        if now < self._blocked_until:
            raise PaperBrokerError("paper_rate_limited", retry_after=self._blocked_until - now)
        while self._requests and self._requests[0] <= now - 60:
            self._requests.popleft()
        if len(self._requests) >= MAX_REQUESTS_PER_MINUTE:
            raise PaperBrokerError("paper_rate_limited", retry_after=max(1, self._requests[0] + 60 - now))

    def _request(self, method, path, payload=None, *, data=False, missing=False, permit=None):
        url = (DATA_ORIGIN if data else PAPER_ORIGIN) + path
        _endpoint(method, url, payload)
        mutation = method != "GET"
        with self._lock:
            now = self._now()
            self._rate_check(now)
            if mutation:
                if permit is None or self._gate is None:
                    _fail("paper_mutation_permit_required")
                if not self.execution_enabled:
                    _fail("paper_network_activation_pending_review")
                # The gate receives a separate immutable-by-convention copy; a
                # callback cannot silently alter the subsequently sent terms.
                checked = None if payload is None else dict(payload)
                try:
                    allowed = self._gate(method, path, checked, permit) is True
                except Exception:
                    allowed = False
                if not allowed or checked != payload:
                    _fail("paper_mutation_permit_rejected")
            headers = {"Accept": "application/json", "User-Agent": "ATLAS-paper-execution/1"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            if self._credentials_path is not None:
                if self._credentials is None:
                    try:
                        self._credentials = _credentials(self._credentials_path)
                    except Exception:
                        _fail("paper_credential_file_invalid")
                headers.update({"APCA-API-KEY-ID": self._credentials.api_key,
                                "APCA-API-SECRET-KEY": self._credentials.api_secret})
            elif not self._injected:
                _fail("paper_credentials_required")
            self._requests.append(now)
            try:
                response = (self._transport(method, url, headers, payload) if self._injected else
                            self._transport(method, url, headers, payload,
                                            paper_execution_enabled=self._paper_execution_enabled))
            except Exception:
                raise PaperBrokerError("paper_transport_failed", ambiguous=mutation) from None
            if not isinstance(response, (Response, HTTPResponse)) or type(response.status) is not int:
                raise PaperBrokerError("paper_response_invalid", ambiguous=mutation)
            code = response.status
            if code == 429:
                try:
                    observed = self._now()
                    delay = _retry_after(getattr(response, "headers", {}), observed)
                except PaperBrokerError:
                    self._rate_limit_fault = True
                    raise PaperBrokerError("paper_rate_limit_header_invalid", http_status=code) from None
                self._blocked_until = observed + delay
                raise PaperBrokerError("paper_rate_limited", retry_after=delay, http_status=code)
            if code in {400, 401, 403}:
                raise PaperBrokerError("paper_request_rejected", http_status=code)
            if code == 422:
                # A duplicate client ID may name an earlier accepted POST. Its
                # response is never permission to create another attempt.
                raise PaperBrokerError("paper_request_unprocessable", ambiguous=mutation, http_status=code)
            if missing and not mutation and code == 404:
                return None, self._now()
            if code != (204 if method == "DELETE" else 200):
                raise PaperBrokerError("paper_http_error", ambiguous=mutation, http_status=code)
            try:
                observed = self._now()
                if not 0 <= observed - now <= TIMEOUT_SECONDS:
                    _fail("paper_observation_window_invalid")
                if method == "DELETE":
                    return None, observed
                if not isinstance(response.body, bytes) or not 0 < len(response.body) <= MAX_RESPONSE_BYTES:
                    _fail("paper_response_size_invalid")
                return _json(response.body), observed
            except PaperBrokerError as exc:
                raise PaperBrokerError(exc.code, ambiguous=mutation, http_status=code) from None

    def account(self):
        row, observed = self._request("GET", "/v2/account")
        if (not isinstance(row, dict) or row.get("currency") != "USD" or not isinstance(row.get("status"), str)
                or row["status"] not in ACCOUNT_STATUSES):
            _fail("paper_account_contract_invalid")
        blocked = [_boolean(row, name) for name in ("account_blocked", "trading_blocked", "trade_suspended_by_user")]
        return {"fingerprint": account_fingerprint("alpaca-paper", _uuid(row.get("id"))),
                "status": row["status"], "currency": "USD", "cash": _number(row.get("cash"), nonnegative=False),
                "equity": _number(row.get("equity"), nonnegative=False),
                "buying_power": _number(row.get("non_marginable_buying_power")),
                "long_market_value": _number(row.get("long_market_value")),
                "short_market_value": _number(row.get("short_market_value"), nonnegative=False),
                "multiplier": _number(row.get("multiplier"), positive=True),
                "blocked": any(blocked) or row["status"] != "ACTIVE", "observed_at": observed}

    def positions(self):
        rows, _ = self._request("GET", "/v2/positions")
        if not isinstance(rows, list) or len(rows) > 500:
            _fail("paper_position_history_incomplete")
        result = []
        for row in rows:
            if not isinstance(row, dict) or row.get("asset_class") != "us_equity" or row.get("side") != "long":
                _fail("paper_position_contract_invalid")
            result.append({"symbol": _symbol(row.get("symbol")), "qty": _number(row.get("qty"), positive=True),
                           "market_value": _number(row.get("market_value")),
                           "current_price": _number(row.get("current_price"), positive=True),
                           "cost_basis": _number(row.get("cost_basis"))})
        if len({r["symbol"] for r in result}) != len(result):
            _fail("paper_duplicate_position")
        return result

    def orders(self, status="open", after=None):
        query = {"status": status, "limit": "500", "direction": "asc", "nested": "false"}
        if after is not None:
            query["after"] = _after(after)
        rows, _ = self._request("GET", "/v2/orders?" + urlencode(query))
        if not isinstance(rows, list) or len(rows) >= 500:
            _fail("paper_order_history_incomplete")
        result = [_order(row) for row in rows]
        if len({r["id"] for r in result}) != len(result) or len({r["client_order_id"] for r in result}) != len(result):
            _fail("paper_duplicate_order_identity")
        return result

    def order_by_client_id(self, client_order_id):
        row, _ = self._request("GET", "/v2/orders:by_client_order_id?" + urlencode(
            {"client_order_id": _client_id(client_order_id)}), missing=True)
        if row is None:
            return None  # Not found never proves a previous POST was unaccepted.
        result = _order(row)
        if result["client_order_id"] != client_order_id:
            _fail("paper_order_identity_mismatch")
        return result

    def quote(self, symbol):
        symbol = _symbol(symbol)
        raw, observed = self._request("GET", "/v2/stocks/quotes/latest?" + urlencode(
            {"symbols": symbol, "feed": "iex"}), data=True)
        if not isinstance(raw, dict) or not isinstance(raw.get("quotes"), dict) or set(raw["quotes"]) != {symbol}:
            _fail("paper_quote_contract_invalid")
        row = raw["quotes"][symbol]
        if not isinstance(row, dict):
            _fail("paper_quote_contract_invalid")
        if any(type(row.get(name)) is not int or row[name] <= 0 for name in ("bs", "as")):
            _fail("paper_quote_size_invalid")
        # Stock sizes are round lots. Without instrument lot metadata, one
        # share per reported lot is a conservative share lower bound; do not
        # assume every listed equity has a 100-share round lot.
        result = {"symbol": symbol, "bid": _number(row.get("bp"), positive=True, json_number=True),
                  "ask": _number(row.get("ap"), positive=True, json_number=True),
                  "bid_size": _number(row.get("bs"), positive=True, json_number=True),
                  "ask_size": _number(row.get("as"), positive=True, json_number=True),
                  "at": _timestamp(row.get("t")), "observed_at": observed,
                  "bid_lots": str(row["bs"]), "ask_lots": str(row["as"]),
                  "size_basis": "conservative_share_lower_bound"}
        if Decimal(result["ask"]) < Decimal(result["bid"]) or not 0 <= observed - result["at"] <= 30:
            _fail("paper_quote_crossed_stale_or_future")
        return result

    def clock_status(self):
        row, observed = self._request("GET", "/v2/clock")
        if not isinstance(row, dict):
            _fail("paper_market_clock_invalid")
        result = {"at": _timestamp(row.get("timestamp")), "is_open": _boolean(row, "is_open"),
                  "next_open": _timestamp(row.get("next_open")), "next_close": _timestamp(row.get("next_close")),
                  "observed_at": observed}
        if not 0 <= observed - result["at"] <= 30 or min(result["next_open"], result["next_close"]) <= result["at"]:
            _fail("paper_market_clock_invalid")
        return result

    def asset(self, symbol):
        row, _ = self._request("GET", "/v2/assets/" + _symbol(symbol))
        if not isinstance(row, dict) or row.get("symbol") != symbol or row.get("class") != "us_equity" or row.get("status") not in ("active", "inactive"):
            _fail("paper_asset_contract_invalid")
        return {"symbol": symbol, "asset_class": "us_equity", "status": row["status"],
                "tradable": _boolean(row, "tradable"), "fractionable": _boolean(row, "fractionable")}

    def activities(self, after=None):
        result, seen, cursor = [], set(), None
        for _ in range(20):
            query = {"direction": "asc", "page_size": "100"}
            if after is not None:
                query["after"] = _after(after)
            if cursor is not None:
                query["page_token"] = cursor
            rows, _ = self._request("GET", "/v2/account/activities?" + urlencode(query))
            if not isinstance(rows, list) or len(rows) > 100:
                _fail("paper_activity_history_incomplete")
            for raw in rows:
                row = _activity(raw)
                if row["id"] in seen:
                    _fail("paper_activity_pagination_cycle")
                seen.add(row["id"])
                result.append(row)
            if len(rows) < 100:
                return result
            cursor = result[-1]["id"]
        _fail("paper_activity_history_incomplete")

    def submit_order(self, payload, *, permit=None):
        terms = _terms(payload)
        row, _ = self._request("POST", "/v2/orders", terms, permit=permit)
        try:
            result = _order(row)
            if any(result[name] != value for name, value in terms.items()):
                _fail("paper_order_identity_mismatch")
            return result
        except PaperBrokerError as exc:
            raise PaperBrokerError(exc.code, ambiguous=True) from None

    def cancel_order(self, broker_id, *, permit=None):
        self._request("DELETE", "/v2/orders/" + _uuid(broker_id), permit=permit)
        return None  # Acknowledgement only. Lookup must establish terminal state.

    def replace_order(self, *args, **kwargs):
        _fail("paper_replace_unsupported")
