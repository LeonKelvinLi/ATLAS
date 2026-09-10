"""Narrow Alpaca PAPER client; no live origin, strategy, worker or retry loop.

The controller owns the durable account identity, funding baseline, order intent,
and reconciliation. This client permits only BTC/USD limit buys with GTC and a
$10 ceiling including an explicit 60 bps modeled fee reserve. Cancellation
acknowledgment never establishes a terminal order state.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import http.client
import json
import math
import re
import ssl
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

from atlas.alpaca_paper_preflight import HTTPResponse, _credentials, _account as _preflight_account
from atlas.domain import BrokerError, BrokerRejected, UnknownSubmission, decimal, financial_context

PAPER_ORIGIN = "https://paper-api.alpaca.markets"
DATA_ORIGIN = "https://data.alpaca.markets"
SYMBOL = "BTC/USD"
ASSET_PATH = "/v2/assets/BTC%2FUSD"
ORDERS_PATH = "/v2/orders?status=all&limit=500&direction=asc&nested=false"
QUOTE_PATH = "/v1beta3/crypto/us/latest/quotes?symbols=BTC%2FUSD"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1_000_000
MAX_QUOTE_AGE_SECONDS = 30
MAX_ORDER_WITH_MODELED_FEES = Decimal("10")
MODELED_FEE_BPS = Decimal("60")
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})
STATUSES = TERMINAL_STATUSES | frozenset({
    "new", "partially_filled", "done_for_day", "replaced", "pending_cancel",
    "pending_replace", "accepted", "pending_new", "accepted_for_bidding",
    "stopped", "suspended", "calculated", "held",
})
_CLEAR_REJECTIONS = frozenset({400, 401, 403, 422, 429})


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise BrokerError("paper_identifier_invalid") from None


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def reject(_):
        raise ValueError
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_constant=reject,
                          object_pairs_hook=unique)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BrokerError("paper_json_invalid") from None


def _amount(record, name, *, optional=False, positive=False, json_number=False, nonnegative=True):
    value = record.get(name)
    if value is None and optional:
        return None
    if (json_number and (isinstance(value, bool) or not isinstance(value, (int, Decimal)))) or (
            not json_number and not isinstance(value, str)):
        raise BrokerError("paper_financial_field_invalid")
    try:
        return format(decimal(value, positive=positive, nonnegative=nonnegative), "f")
    except ValueError:
        raise BrokerError("paper_financial_field_invalid") from None


@financial_context
def _terms(client_order_id, quantity, limit_price):
    client_order_id = _uuid(client_order_id)
    try:
        if any(isinstance(v, (bool, float)) or not isinstance(v, (str, int, Decimal))
               for v in (quantity, limit_price)):
            raise ValueError
        qty, price = decimal(quantity, positive=True), decimal(limit_price, positive=True)
    except ValueError:
        raise BrokerRejected("paper_order_terms_invalid") from None
    if qty * price * (1 + MODELED_FEE_BPS / 10000) > MAX_ORDER_WITH_MODELED_FEES:
        raise BrokerRejected("paper_order_exceeds_10_usd_with_modeled_fees")
    return {"symbol": SYMBOL, "side": "buy", "type": "limit", "time_in_force": "gtc",
            "qty": format(qty, "f"), "limit_price": format(price, "f"), "client_order_id": client_order_id}


def _endpoint(method, url, body):
    """Validate every URL/method/body before any authentication header is attached."""
    parsed = urlsplit(url)
    target = parsed.path + ("?" + parsed.query if parsed.query else "")
    if parsed.scheme != "https" or parsed.fragment:
        raise BrokerError("paper_endpoint_not_allowed")
    if parsed.netloc == "data.alpaca.markets":
        if method != "GET" or target != QUOTE_PATH or body:
            raise BrokerError("paper_endpoint_not_allowed")
    elif parsed.netloc == "paper-api.alpaca.markets":
        if method == "GET" and target in {"/v2/account", "/v2/positions", ORDERS_PATH, ASSET_PATH} and not body:
            pass
        elif method == "GET" and parsed.path == "/v2/account/activities" and not body:
            try:
                query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
                if (set(query) not in ({"direction", "page_size"}, {"direction", "page_size", "page_token"})
                        or query.get("direction") != ["asc"] or query.get("page_size") != ["100"]
                        or any(len(value) != 1 for value in query.values())):
                    raise ValueError
                if "page_token" in query:
                    _activity_id(query["page_token"][0])
                expected = "/v2/account/activities?" + urlencode({name: query[name][0]
                            for name in ("direction", "page_size", "page_token") if name in query})
                if target != expected:
                    raise ValueError
            except (ValueError, BrokerError):
                raise BrokerError("paper_endpoint_not_allowed") from None
        elif method == "GET" and target.startswith("/v2/orders:by_client_order_id?client_order_id=") and not body:
            _uuid(target.removeprefix("/v2/orders:by_client_order_id?client_order_id="))
        elif method == "DELETE" and target.startswith("/v2/orders/") and not body:
            _uuid(target.removeprefix("/v2/orders/"))
        elif method == "POST" and target == "/v2/orders" and isinstance(body, bytes) and len(body) <= 4096:
            value = _json(body)
            if not isinstance(value, dict) or set(value) != {
                    "symbol", "side", "type", "time_in_force", "qty", "limit_price", "client_order_id"}:
                raise BrokerError("paper_order_body_invalid")
            checked = _terms(value["client_order_id"], value["qty"], value["limit_price"])
            if value != checked:
                raise BrokerError("paper_order_body_invalid")
        else:
            raise BrokerError("paper_endpoint_not_allowed")
    else:
        raise BrokerError("paper_endpoint_not_allowed")
    return parsed.netloc, target


def _owned_http(method, url, body, headers, *, timeout_seconds, max_response_bytes):
    host, target = _endpoint(method, url, body)
    connection = http.client.HTTPSConnection(host, timeout=timeout_seconds,
                                              context=ssl.create_default_context())
    deadline = time.monotonic() + timeout_seconds
    try:
        connection.request(method, target, body=body or None, headers=headers)
        response = connection.getresponse()
        # Do not follow redirects, retry, or read unnecessary error/204 bodies.
        if response.status not in {200, 201}:
            return HTTPResponse(response.status, b"")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > max_response_bytes):
            raise BrokerError("paper_response_size_invalid")
        chunks, total = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrokerError("paper_request_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_response_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                raise BrokerError("paper_response_too_large")
        if time.monotonic() > deadline:
            raise BrokerError("paper_request_timeout")
        if length is not None and total != int(length):
            raise BrokerError("paper_response_length_mismatch")
        return HTTPResponse(response.status, b"".join(chunks))
    finally:
        connection.close()


def _timestamp(value):
    if not isinstance(value, str):
        raise BrokerError("paper_timestamp_invalid")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", value)
    if match is None:
        raise BrokerError("paper_timestamp_invalid")
    try:
        whole = int(datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        return Decimal(whole) + (Decimal("0." + match[2]) if match[2] else Decimal(0))
    except (ValueError, OverflowError):
        raise BrokerError("paper_timestamp_invalid") from None


def _activity_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value):
        raise BrokerError("paper_activity_identity_invalid")
    return value


def _canonical(value):
    """Stable JSON with numeric Decimals remaining distinct from quoted strings."""
    if isinstance(value, Decimal):
        return str(decimal(value))
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + ":" + _canonical(value[key]) for key in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _activity(row):
    if not isinstance(row, dict):
        raise BrokerError("paper_activity_shape_invalid")
    identifier, kind = _activity_id(row.get("id")), row.get("activity_type")
    if not isinstance(kind, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", kind):
        raise BrokerError("paper_activity_type_invalid")
    result = {"id": identifier, "activity_type": kind,
              "content_sha256": hashlib.sha256(_canonical(row).encode()).hexdigest()}
    for name in ("amount", "qty", "price"):
        if name in row:
            result[name] = _amount(row, name, optional=True, nonnegative=name == "price")
    if "side" in row:
        value = row["side"]
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-z_]{1,32}", value)):
            raise BrokerError("paper_activity_side_invalid")
        result["side"] = value
    if "symbol" in row:
        symbol = row["symbol"]
        if symbol is not None and (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9./-]{0,31}", symbol)):
            raise BrokerError("paper_activity_symbol_invalid")
        result["symbol"] = symbol
    if "transaction_time" in row:
        if row["transaction_time"] is not None:
            _timestamp(row["transaction_time"])
        result["transaction_time"] = row["transaction_time"]
    if "date" in row:
        value = row["date"]
        try:
            if value is not None and (not isinstance(value, str) or datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value):
                raise ValueError
        except ValueError:
            raise BrokerError("paper_activity_date_invalid") from None
        result["date"] = value
    return result


def _order(row):
    required = {"id", "client_order_id", "asset_id", "symbol", "asset_class", "side", "type", "time_in_force",
                "qty", "limit_price", "filled_qty", "filled_avg_price", "status"}
    if not isinstance(row, dict) or not required <= set(row):
        raise BrokerError("paper_order_shape_invalid")
    identifier, client_id = _uuid(row.get("id")), _uuid(row.get("client_order_id"))
    status = row.get("status")
    if (not isinstance(status, str) or status not in STATUSES or row.get("symbol") not in (SYMBOL, "BTCUSD")
            or row.get("asset_class") != "crypto" or row.get("side") not in ("buy", "sell")
            or row.get("type") != "limit" or row.get("time_in_force") != "gtc"
            or row.get("notional") is not None):
        raise BrokerError("paper_order_contract_invalid")
    quantity = _amount(row, "qty", positive=True)
    filled = _amount(row, "filled_qty")
    price = _amount(row, "limit_price", positive=True)
    average = _amount(row, "filled_avg_price", optional=True, positive=True)
    qty, done = decimal(quantity), decimal(filled)
    if (done > qty or (done > 0 and average is None)
            or (status == "filled" and done != qty)
            or (status == "partially_filled" and not 0 < done < qty)
            or (status in {"new", "accepted", "pending_new", "rejected"} and done != 0)):
        raise BrokerError("paper_order_fill_inconsistent")
    if done > 0:
        avg, limit = decimal(average), decimal(price)
        if (row["side"] == "buy" and avg > limit) or (row["side"] == "sell" and avg < limit):
            raise BrokerError("paper_fill_violates_limit")
    result = {"id": identifier, "client_order_id": client_id, "asset_id": _uuid(row["asset_id"]), "symbol": SYMBOL,
              "provider_symbol": row["symbol"], "asset_class": "crypto", "side": row["side"],
              "type": "limit", "time_in_force": "gtc", "qty": quantity, "limit_price": price,
              "filled_qty": filled, "filled_avg_price": average, "status": status,
              "terminal": status in TERMINAL_STATUSES,
              "normalized_status": "cancelled" if status == "canceled" else status
                  if status in {"filled", "expired", "rejected", "partially_filled"} else "pending",
              "fees": None, "realized_pnl": None}
    for name in ("submitted_at", "created_at", "updated_at", "filled_at", "canceled_at", "expired_at"):
        value = row.get(name)
        if value is not None:
            _timestamp(value)
        result[name] = value
    for name in ("replaced_by", "replaces"):
        result[name] = _uuid(row[name]) if row.get(name) is not None else None
    if status == "replaced" and result["replaced_by"] is None:
        raise BrokerError("paper_replacement_identity_unavailable")
    return result


class AlpacaPaperClient:
    def __init__(self, key_file, *, transport=None, clock=time.time):
        if transport is not None and not callable(transport):
            raise BrokerError("paper_transport_invalid")
        if not callable(clock):
            raise BrokerError("paper_clock_invalid")
        try:
            self._credentials = _credentials(key_file)
        except Exception:
            raise BrokerError("paper_credential_file_invalid") from None
        self._transport = _owned_http if transport is None else transport
        self._injected = transport is not None
        self._clock = clock

    def __repr__(self):
        return f"AlpacaPaperClient(transport={self.transport_provenance}, credentials=redacted)"

    @property
    def transport_provenance(self):
        return "injected_test_transport" if self._injected else "verified_tls"

    def _now(self):
        value = self._clock()
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise BrokerError("paper_clock_invalid")
        return value

    def _request(self, method, path, payload=None, *, data=False, missing=False):
        url = (DATA_ORIGIN if data else PAPER_ORIGIN) + path
        body = b"" if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        _endpoint(method, url, body)
        headers = {"APCA-API-KEY-ID": self._credentials.api_key,
                   "APCA-API-SECRET-KEY": self._credentials.api_secret,
                   "Accept": "application/json", "User-Agent": "ATLAS-bounded-paper-client/1"}
        if body:
            headers["Content-Type"] = "application/json"
        mutation = method != "GET"
        started = self._now()
        try:
            response = self._transport(method, url, body, headers, timeout_seconds=TIMEOUT_SECONDS,
                                       max_response_bytes=MAX_RESPONSE_BYTES)
        except Exception:
            raise (UnknownSubmission("paper_mutation_acknowledgment_unknown") if mutation
                   else BrokerError("paper_transport_failed")) from None
        if not isinstance(response, HTTPResponse) or type(response.status) is not int:
            raise (UnknownSubmission("paper_mutation_response_invalid") if mutation
                   else BrokerError("paper_response_invalid"))
        if method == "GET" and missing and response.status == 404:
            return None, started, self._now()
        if mutation and response.status in _CLEAR_REJECTIONS:
            raise BrokerRejected("paper_broker_rejected_request")
        success = 204 if method == "DELETE" else 200
        if response.status != success:
            raise (UnknownSubmission("paper_mutation_acknowledgment_unknown") if mutation
                   else BrokerError("paper_http_request_failed"))
        try:
            observed = self._now()
            if not 0 <= observed - started <= TIMEOUT_SECONDS:
                raise BrokerError("paper_observation_window_invalid")
            if method == "DELETE":
                return None, started, observed
            if not isinstance(response.body, bytes) or not 0 < len(response.body) <= MAX_RESPONSE_BYTES:
                raise BrokerError("paper_response_size_invalid")
            return _json(response.body), started, observed
        except Exception:
            raise (UnknownSubmission("paper_mutation_response_invalid") if mutation
                   else BrokerError("paper_response_invalid")) from None

    def account(self):
        raw, _, observed = self._request("GET", "/v2/account")
        try:
            parsed = _preflight_account(raw)
            if parsed["crypto_status"] is None or any(parsed[name] is None for name in
                    ("account_blocked", "trading_blocked", "trade_suspended_by_user")):
                raise ValueError
            return {"id": _uuid(raw["id"]), "currency": parsed["currency"], "status": parsed["status"],
                    "crypto_status": parsed["crypto_status"], "cash": parsed["cash"], "equity": parsed["equity"],
                    "non_marginable_buying_power": parsed["non_marginable_buying_power"],
                    "buying_power": parsed["buying_power_reported"], "accrued_fees": parsed["account_accrued_fees"],
                    "account_blocked": parsed["account_blocked"], "trading_blocked": parsed["trading_blocked"],
                    "trade_suspended_by_user": parsed["trade_suspended_by_user"],
                    "daily_realized_pnl": None, "observed_at": observed}
        except Exception:
            raise BrokerError("paper_account_contract_invalid") from None

    def positions(self):
        rows, _, _ = self._request("GET", "/v2/positions")
        if not isinstance(rows, list) or len(rows) > 500:
            raise BrokerError("paper_positions_invalid")
        result = []
        for row in rows:
            if (not isinstance(row, dict) or row.get("symbol") not in (SYMBOL, "BTCUSD")
                    or row.get("asset_class") != "crypto" or row.get("side") != "long"):
                raise BrokerError("paper_position_contract_invalid")
            qty = _amount(row, "qty", positive=True)
            available = _amount(row, "qty_available", optional=True)
            if available is not None and decimal(available) > decimal(qty):
                raise BrokerError("paper_position_inventory_invalid")
            result.append({"asset_id": _uuid(row.get("asset_id")), "symbol": SYMBOL,
                           "provider_symbol": row["symbol"], "asset_class": "crypto", "side": "long",
                           "qty": qty, "qty_available": available,
                           "market_value": _amount(row, "market_value"),
                           "cost_basis": _amount(row, "cost_basis"),
                           "avg_entry_price": _amount(row, "avg_entry_price", positive=True)})
        if len(result) > 1:
            raise BrokerError("paper_duplicate_position")
        return result

    def orders(self):
        rows, _, _ = self._request("GET", ORDERS_PATH)
        if not isinstance(rows, list) or len(rows) >= 500:
            raise BrokerError("paper_order_history_incomplete")
        result = [_order(row) for row in rows]
        if (len({r["id"] for r in result}) != len(result)
                or len({r["client_order_id"] for r in result}) != len(result)):
            raise BrokerError("paper_duplicate_order_identity")
        return result

    def activities(self):
        result, seen, cursor = [], set(), None
        for _ in range(20):
            query = {"direction": "asc", "page_size": "100"}
            if cursor is not None:
                query["page_token"] = cursor
            rows, _, _ = self._request("GET", "/v2/account/activities?" + urlencode(query))
            if not isinstance(rows, list) or len(rows) > 100:
                raise BrokerError("paper_activity_page_invalid")
            page = [_activity(row) for row in rows]
            for row in page:
                if row["id"] in seen:
                    raise BrokerError("paper_activity_pagination_cycle")
                seen.add(row["id"])
                result.append(row)
            if len(page) < 100:
                return result
            cursor = page[-1]["id"]
        raise BrokerError("paper_activity_history_incomplete")

    def asset(self):
        row, _, observed = self._request("GET", ASSET_PATH)
        if (not isinstance(row, dict) or row.get("symbol") not in (SYMBOL, "BTCUSD")
                or row.get("class") != "crypto" or row.get("status") not in ("active", "inactive")):
            raise BrokerError("paper_asset_contract_invalid")
        flags = {}
        for name in ("tradable", "marginable", "shortable", "fractionable"):
            if type(row.get(name)) is not bool:
                raise BrokerError("paper_asset_permission_invalid")
            flags[name] = row[name]
        return {"id": _uuid(row.get("id")), "symbol": SYMBOL, "class": "crypto",
                "status": row["status"], **flags,
                "min_order_size": _amount(row, "min_order_size", positive=True),
                "min_trade_increment": _amount(row, "min_trade_increment", positive=True),
                "price_increment": _amount(row, "price_increment", positive=True), "observed_at": observed}

    def quote(self):
        raw, started, observed = self._request("GET", QUOTE_PATH, data=True)
        if (not isinstance(raw, dict) or not isinstance(raw.get("quotes"), dict)
                or set(raw["quotes"]) != {SYMBOL} or not isinstance(raw["quotes"][SYMBOL], dict)):
            raise BrokerError("paper_quote_contract_invalid")
        row = raw["quotes"][SYMBOL]
        bid, ask = _amount(row, "bp", positive=True, json_number=True), _amount(row, "ap", positive=True, json_number=True)
        bid_size = _amount(row, "bs", positive=True, json_number=True)
        ask_size = _amount(row, "as", positive=True, json_number=True)
        timestamp = _timestamp(row.get("t"))
        if decimal(ask) < decimal(bid):
            raise BrokerError("paper_crossed_quote")
        if not 0 <= Decimal(str(observed)) - timestamp <= MAX_QUOTE_AGE_SECONDS:
            raise BrokerError("paper_quote_stale_or_future")
        return {"symbol": SYMBOL, "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
                "timestamp": float(timestamp), "provider_timestamp": row["t"], "observed_at": observed,
                "request_started_at": started, "source": "alpaca_crypto_us_latest_quote", "fee_estimate": None}

    def submit_limit_buy(self, client_order_id, quantity, limit_price):
        terms = _terms(client_order_id, quantity, limit_price)
        raw, _, _ = self._request("POST", "/v2/orders", terms)
        try:
            result = _order(raw)
            if any(result[name] != value for name, value in terms.items()):
                raise BrokerError("paper_order_acknowledgment_mismatch")
            return result
        except Exception:
            raise UnknownSubmission("paper_order_acknowledgment_mismatch") from None

    def get_order(self, client_order_id):
        identifier = _uuid(client_order_id)
        row, _, _ = self._request("GET", "/v2/orders:by_client_order_id?client_order_id=" + identifier, missing=True)
        if row is None:
            # Absence is not proof that a preceding POST was never accepted.
            return None
        result = _order(row)
        if result["client_order_id"] != identifier:
            raise BrokerError("paper_lookup_identity_mismatch")
        return result

    def cancel_order(self, order_id):
        self._request("DELETE", "/v2/orders/" + _uuid(order_id))
        return None  # 204 acknowledges the request only; controller must reconcile.
