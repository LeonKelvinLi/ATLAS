"""Explicit read-only paper-account inspection; no trading adapter or worker.

Provider contracts were reviewed in the primary Alpaca reference on 2026-09-09.
The account payload has no paper/live discriminator. Only the pinned HTTPS paper
origin establishes endpoint selection. This module never assesses strategy merit,
initial funding history, realized daily P&L, or actual order fees.
"""

from dataclasses import dataclass, field
import http.client
import json
import math
import re
import ssl
import time
from urllib.parse import urlsplit
from uuid import UUID

from atlas.config import read_secret_file
from atlas.domain import decimal
from atlas.risk import account_fingerprint

PAPER_ORIGIN = "https://paper-api.alpaca.markets"
PAPER_HOST = "paper-api.alpaca.markets"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1_000_000
MAX_SNAPSHOT_SECONDS = 30
OPEN_ORDER_LIMIT = 500
READ_PATHS = (
    "/v2/account",
    "/v2/positions",
    "/v2/orders?status=open&limit=500&direction=asc&nested=false",
)
_STATUS = frozenset({
    "INACTIVE", "PAPER_ONLY", "ONBOARDING", "SUBMISSION_FAILED", "SUBMITTED",
    "ACCOUNT_UPDATED", "APPROVAL_PENDING", "ACTIVE", "REJECTED", "ACCOUNT_CLOSED",
    "APPROVED", "ACCOUNT_CLOSED_PENDING", "ACTION_REQUIRED", "LIMITED",
})


class PreflightError(ValueError):
    """A fixed safe error code; never a provider body, header or credential."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class _Credentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)


def _origin(origin):
    if origin != PAPER_ORIGIN:
        raise PreflightError("paper_origin_required")


def _url(url):
    parsed = urlsplit(url)
    target = parsed.path + ("?" + parsed.query if parsed.query else "")
    if (parsed.scheme != "https" or parsed.netloc != PAPER_HOST or parsed.fragment
            or target not in READ_PATHS):
        raise PreflightError("paper_read_endpoint_required")
    return target


def _json(raw, code):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def finite(_):
        raise ValueError

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=finite)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise PreflightError(code) from None


def _credentials(key_file):
    try:
        raw = read_secret_file(key_file)
    except Exception:
        raise PreflightError("paper_credential_file_invalid") from None
    value = _json(raw, "paper_credential_file_invalid")
    if not isinstance(value, dict) or set(value) != {"api_key", "api_secret"}:
        raise PreflightError("paper_credential_fields_invalid")
    for secret in value.values():
        if (not isinstance(secret, str) or not 1 <= len(secret) <= 1024
                or not secret.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in secret)):
            raise PreflightError("paper_credential_fields_invalid")
    return _Credentials(**value)


def _safe_get(url, headers, *, timeout_seconds, max_response_bytes):
    """Owned HTTP transport: fixed TLS host, GET only, no redirects or retries."""
    target = _url(url)
    connection = http.client.HTTPSConnection(PAPER_HOST, timeout=timeout_seconds,
                                              context=ssl.create_default_context())
    deadline = time.monotonic() + timeout_seconds
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise PreflightError("paper_redirect_refused")
        if response.status != 200:
            raise PreflightError("paper_http_request_failed")
        length = response.getheader("Content-Length")
        if length is not None:
            try:
                if not length.isdigit() or int(length) > max_response_bytes:
                    raise ValueError
            except ValueError:
                raise PreflightError("paper_response_size_invalid") from None
        chunks, total = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreflightError("paper_request_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_response_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                raise PreflightError("paper_response_too_large")
        if time.monotonic() > deadline:
            raise PreflightError("paper_request_timeout")
        body = b"".join(chunks)
        if length is not None and len(body) != int(length):
            raise PreflightError("paper_response_length_mismatch")
        return HTTPResponse(response.status, body)
    finally:
        connection.close()


def _amount(record, name, *, optional=False, nonnegative=True):
    if name not in record or record[name] is None:
        if optional:
            return None
        raise PreflightError("paper_financial_field_unavailable")
    if not isinstance(record[name], str):
        raise PreflightError("paper_financial_field_invalid")
    try:
        return str(decimal(record[name], nonnegative=nonnegative))
    except ValueError:
        raise PreflightError("paper_financial_field_invalid") from None


def _symbol(record):
    symbol = record.get("symbol")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9./-]{0,31}", symbol):
        raise PreflightError("paper_symbol_invalid")
    return symbol


def _account(record):
    if not isinstance(record, dict):
        raise PreflightError("paper_account_shape_invalid")
    try:
        identifier = record["id"]
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise PreflightError("paper_account_identity_invalid") from None
    status, crypto_status = record.get("status"), record.get("crypto_status")
    if (not isinstance(status, str) or status not in _STATUS
            or (crypto_status is not None and (not isinstance(crypto_status, str) or crypto_status not in _STATUS))):
        raise PreflightError("paper_account_status_invalid")
    currency = record.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise PreflightError("paper_account_currency_invalid")
    flags = {}
    for name in ("account_blocked", "trading_blocked", "trade_suspended_by_user"):
        value = record.get(name)
        if value is not None and type(value) is not bool:
            raise PreflightError("paper_account_flag_invalid")
        flags[name] = value
    return {
        "account_fingerprint": account_fingerprint("alpaca_paper", identifier),
        "status": status, "crypto_status": crypto_status, "currency": currency,
        "cash": _amount(record, "cash"), "equity": _amount(record, "equity"),
        "buying_power_reported": _amount(record, "buying_power", optional=True),
        "non_marginable_buying_power": _amount(record, "non_marginable_buying_power"),
        "account_accrued_fees": _amount(record, "accrued_fees", optional=True),
        **flags,
    }


def _positions(records):
    if not isinstance(records, list) or len(records) > 1000:
        raise PreflightError("paper_positions_shape_invalid")
    result = []
    for row in records:
        if not isinstance(row, dict) or row.get("side") not in ("long", "short"):
            raise PreflightError("paper_position_contract_invalid")
        symbol = _symbol(row)
        asset_class = row.get("asset_class")
        if asset_class not in ("crypto", "us_equity", "us_option"):
            raise PreflightError("paper_position_asset_class_invalid")
        result.append({
            "symbol": symbol, "asset_class": asset_class, "side": row["side"],
            "quantity": _amount(row, "qty", nonnegative=False),
            "market_value": _amount(row, "market_value", nonnegative=False),
            "cost_basis": _amount(row, "cost_basis", nonnegative=False),
            "canonical_atlas_symbol": "BTC-USD" if asset_class == "crypto" and symbol in {"BTC/USD", "BTCUSD"} else None,
        })
    return result


def _orders(records):
    if not isinstance(records, list) or len(records) > OPEN_ORDER_LIMIT:
        raise PreflightError("paper_orders_shape_invalid")
    result = []
    for row in records:
        if not isinstance(row, dict) or row.get("side") not in ("buy", "sell"):
            raise PreflightError("paper_order_contract_invalid")
        status = row.get("status")
        if not isinstance(status, str) or not re.fullmatch(r"[a-z_]{1,40}", status):
            raise PreflightError("paper_order_status_invalid")
        quantity = _amount(row, "qty", optional=True)
        notional = _amount(row, "notional", optional=True)
        if quantity is None and notional is None:
            raise PreflightError("paper_order_size_unavailable")
        result.append({"symbol": _symbol(row), "side": row["side"], "status": status,
                       "quantity": quantity, "notional": notional,
                       "filled_quantity": _amount(row, "filled_qty"),
                       "limit_price": _amount(row, "limit_price", optional=True)})
    return result


def collect_preflight(key_file, *, origin=PAPER_ORIGIN, transport=None, clock=time.time):
    """Read one current account snapshot. Injected transports are test evidence.

    No money cap is fabricated from a larger account. Matching today's $100 cash
    and flat holdings is not proof of the initial deposit or complete history.
    This function never enables execution or returns an execution-ready flag.
    """
    _origin(origin)  # Before reading a key file or constructing authentication headers.
    credentials = _credentials(key_file)
    fetch = _safe_get if transport is None else transport
    if not callable(fetch):
        raise PreflightError("paper_transport_invalid")

    def observed_time():
        value = clock()
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise PreflightError("paper_observation_clock_invalid")
        return value

    started = observed_time()
    observed, payloads = [], []
    for path in READ_PATHS:
        url = origin + path
        _url(url)  # Check each request before attaching authentication.
        headers = {"APCA-API-KEY-ID": credentials.api_key, "APCA-API-SECRET-KEY": credentials.api_secret,
                   "Accept": "application/json", "User-Agent": "ATLAS-paper-read-only-preflight/1"}
        try:
            response = fetch(url, headers, timeout_seconds=TIMEOUT_SECONDS,
                             max_response_bytes=MAX_RESPONSE_BYTES)
        except PreflightError:
            if transport is None:
                raise
            raise PreflightError("paper_transport_failed") from None
        except Exception:
            raise PreflightError("paper_transport_failed") from None
        if not isinstance(response, HTTPResponse) or type(response.status) is not int:
            raise PreflightError("paper_http_response_invalid")
        if 300 <= response.status < 400:
            raise PreflightError("paper_redirect_refused")
        if response.status != 200:
            raise PreflightError("paper_http_request_failed")
        if not isinstance(response.body, bytes) or not 0 < len(response.body) <= MAX_RESPONSE_BYTES:
            raise PreflightError("paper_response_size_invalid")
        at = observed_time()
        if at < (observed[-1] if observed else started) or at - started > MAX_SNAPSHOT_SECONDS:
            raise PreflightError("paper_snapshot_window_invalid")
        observed.append(at)
        payloads.append(_json(response.body, "paper_response_json_invalid"))

    account, positions, orders = _account(payloads[0]), _positions(payloads[1]), _orders(payloads[2])
    current_match = (account["currency"] == "USD" and decimal(account["cash"]) == 100
                     and decimal(account["equity"]) == 100 and not positions and not orders)
    controls = (account["status"] == "ACTIVE" and account["crypto_status"] == "ACTIVE"
                and all(account[name] is False for name in
                        ("account_blocked", "trading_blocked", "trade_suspended_by_user")))
    blockers = ["execution_not_implemented", "immutable_starting_capital_unverified",
                "daily_realized_pnl_unavailable", "order_fee_estimate_unavailable"]
    if not current_match:
        blockers.append("current_state_does_not_match_flat_100_usd")
    if not controls:
        blockers.append("account_crypto_permissions_not_verified_active")
    if decimal(account["non_marginable_buying_power"]) < 100:
        blockers.append("less_than_100_usd_non_marginable_buying_power")
    if len(orders) == OPEN_ORDER_LIMIT:
        blockers.append("open_order_listing_may_be_truncated")
    return {
        "schema_version": "1", "report_kind": "alpaca_paper_read_only_preflight",
        "execution_enabled": False, "orders_submitted": 0, "account_mutations": 0,
        "paper_account_read_verified": transport is None,
        "response_contract_valid": True,
        "evidence_kind": "verified_tls_paper_rest" if transport is None else "injected_test_transport",
        "paper_origin": PAPER_ORIGIN, "read_paths": list(READ_PATHS),
        "account": account, "positions": positions, "open_orders": orders,
        "open_orders_complete": len(orders) < OPEN_ORDER_LIMIT,
        "current_state_matches_100": current_match,
        "current_account_controls_allow_crypto": controls,
        "immutable_starting_capital_verified": False,
        "daily_realized_pnl": None, "estimated_order_fee_usd": None,
        "observed_at": int(observed[-1]), "observation_span_seconds": observed[-1] - started,
        "freshness_basis": "client receipt times of three separate GETs; not atomic or provider account timestamps",
        "blockers": blockers,
        "limitations": [
            "A current $100 flat account does not prove initial funding, lifetime funding or a durable allocation limit.",
            "Buying power may include margin; crypto assessment uses cash and non-marginable buying power.",
            "Account accrued fees are not an order fee estimate or realized daily P&L.",
            "No order/fill history, quote, strategy, deposit history or immutable budget was validated.",
            "The endpoint establishes paper selection; account ID, key format and balance do not establish mode.",
        ],
    }
