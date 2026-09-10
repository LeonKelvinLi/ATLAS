"""Bounded Coinbase Advanced Trade account reads with a view-only CDP key.

This is a live-account READ, not a paper broker or execution adapter. Credentials,
JWTs, account names and raw account identifiers never appear in reports/errors.
Only the four documented GET routes below can receive authentication.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import json
import math
import re
import secrets
import ssl
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import jwt

from atlas.config import read_secret_file

ORIGIN = "https://api.coinbase.com"
HOST = "api.coinbase.com"
PREFIX = "/api/v3/brokerage"
PERMISSIONS_PATH = PREFIX + "/key_permissions"
ACCOUNTS_PATH = PREFIX + "/accounts"
FEES_PATH = PREFIX + "/transaction_summary?product_type=SPOT&product_venue=CBE"
PRODUCT_PATH = PREFIX + "/products/BTC-USD?get_tradability_status=true"
PAGE_LIMIT = 250
MAX_PAGES = 20
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1_000_000
MAX_SNAPSHOT_SECONDS = 120
_CURRENCY = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}\Z")
_NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,30})?\Z")


class PreflightError(ValueError):
    """Fixed local diagnostic code; never includes provider text or secrets."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class _Credentials:
    name: str = field(repr=False)
    private_key: ec.EllipticCurvePrivateKey = field(repr=False)


def _json(raw, code):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PreflightError(code) from None


def _uuid(value, code):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise PreflightError(code) from None


def _fingerprint(kind, identifier):
    return hashlib.sha256(f"coinbase:{kind}:{identifier}".encode()).hexdigest()


def _credentials(key_file):
    try:
        raw = read_secret_file(key_file)
    except Exception:
        raise PreflightError("coinbase_credential_file_invalid") from None
    value = _json(raw, "coinbase_credential_file_invalid")
    if not isinstance(value, dict) or set(value) != {"name", "privateKey"}:
        raise PreflightError("coinbase_credential_fields_invalid")
    name, pem = value["name"], value["privateKey"]
    if (not isinstance(name, str) or not re.fullmatch(
            r"organizations/[A-Za-z0-9_-]{1,128}/apiKeys/[A-Za-z0-9_-]{1,128}", name)
            or not isinstance(pem, str) or len(pem) > 8192):
        raise PreflightError("coinbase_credential_fields_invalid")
    if not re.fullmatch(
            r"-----BEGIN (EC PRIVATE KEY|PRIVATE KEY)-----\r?\n"
            r"[A-Za-z0-9+/=\r\n]+-----END \1-----\r?\n?", pem):
        raise PreflightError("coinbase_es256_private_key_required")
    try:
        private_key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        if (not isinstance(private_key, ec.EllipticCurvePrivateKey)
                or not isinstance(private_key.curve, ec.SECP256R1)):
            raise ValueError
    except Exception:
        raise PreflightError("coinbase_es256_private_key_required") from None
    return _Credentials(name, private_key)


def _cursor(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or not value.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise PreflightError("coinbase_pagination_cursor_invalid")
    return value


def _url(url):
    try:
        parsed = urlsplit(url)
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        if (parsed.scheme != "https" or parsed.netloc != HOST or parsed.fragment
                or url != ORIGIN + target):
            raise ValueError
        if target in {PERMISSIONS_PATH, FEES_PATH, PRODUCT_PATH}:
            return target
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (parsed.path != ACCOUNTS_PATH or len(pairs) not in {1, 2}
                or pairs[0] != ("limit", str(PAGE_LIMIT))
                or (len(pairs) == 2 and (pairs[1][0] != "cursor" or not _cursor(pairs[1][1])))
                or urlencode(pairs) != parsed.query):
            raise ValueError
        return target
    except (ValueError, TypeError):
        raise PreflightError("coinbase_read_endpoint_required") from None


def _http_error(status):
    if type(status) is not int or not 100 <= status <= 599:
        return PreflightError("coinbase_http_status_invalid")
    return PreflightError(f"coinbase_http_{status}")


def _safe_get(url, headers, *, timeout_seconds, max_response_bytes):
    """Pinned TLS GET; no redirect, retry, request body or caller-selected host."""
    target = _url(url)
    connection = http.client.HTTPSConnection(HOST, timeout=timeout_seconds,
                                              context=ssl.create_default_context())
    deadline = time.monotonic() + timeout_seconds
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise PreflightError("coinbase_redirect_refused")
        if response.status != 200:
            raise _http_error(response.status)
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > max_response_bytes):
            raise PreflightError("coinbase_response_size_invalid")
        chunks, total = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreflightError("coinbase_request_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_response_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                raise PreflightError("coinbase_response_too_large")
        if time.monotonic() > deadline:
            raise PreflightError("coinbase_request_timeout")
        body = b"".join(chunks)
        if length is not None and len(body) != int(length):
            raise PreflightError("coinbase_response_length_mismatch")
        return HTTPResponse(response.status, body)
    finally:
        connection.close()


def _amount(value):
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        raise PreflightError("coinbase_financial_field_invalid")
    try:
        result = Decimal(value)
        if not result.is_finite() or result < 0:
            raise ValueError
        return value
    except (ValueError, InvalidOperation):
        raise PreflightError("coinbase_financial_field_invalid") from None


def _bool(record, name):
    value = record.get(name)
    if type(value) is not bool:
        raise PreflightError("coinbase_boolean_field_invalid")
    return value


def _permissions(record, portfolio):
    if not isinstance(record, dict):
        raise PreflightError("coinbase_permissions_shape_invalid")
    permissions = {name: _bool(record, name) for name in ("can_view", "can_trade", "can_transfer")}
    if permissions != {"can_view": True, "can_trade": False, "can_transfer": False}:
        raise PreflightError("coinbase_view_only_key_required")
    actual = _uuid(record.get("portfolio_uuid"), "coinbase_portfolio_identity_invalid")
    if actual != portfolio:
        raise PreflightError("coinbase_portfolio_mismatch")
    return {**permissions, "portfolio_fingerprint": _fingerprint("portfolio", actual)}


def _account(record, portfolio):
    if not isinstance(record, dict):
        raise PreflightError("coinbase_account_shape_invalid")
    identity = _uuid(record.get("uuid"), "coinbase_account_identity_invalid")
    if _uuid(record.get("retail_portfolio_id"), "coinbase_portfolio_identity_invalid") != portfolio:
        raise PreflightError("coinbase_portfolio_mismatch")
    currency = record.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        raise PreflightError("coinbase_account_currency_invalid")
    amounts = {}
    for name in ("available_balance", "hold"):
        value = record.get(name)
        if not isinstance(value, dict) or value.get("currency") != currency:
            raise PreflightError("coinbase_balance_currency_mismatch")
        amounts[name] = _amount(value.get("value"))
    return identity, {"account_fingerprint": _fingerprint("account", identity), "currency": currency,
                      **amounts, "active": _bool(record, "active"), "ready": _bool(record, "ready")}


def _fees(record):
    if not isinstance(record, dict) or not isinstance(record.get("fee_tier"), dict):
        raise PreflightError("coinbase_fee_tier_unavailable")
    tier = record["fee_tier"]
    result = {name: _amount(tier.get(name)) for name in ("maker_fee_rate", "taker_fee_rate")}
    if any(Decimal(value) >= 1 for value in result.values()):
        raise PreflightError("coinbase_fee_rate_invalid")
    cost_plus = record.get("has_cost_plus_commission")
    if cost_plus is not None and type(cost_plus) is not bool:
        raise PreflightError("coinbase_boolean_field_invalid")
    gst = record.get("goods_and_services_tax")
    if gst is not None:
        if not isinstance(gst, dict) or gst.get("type") not in {"INCLUSIVE", "EXCLUSIVE"}:
            raise PreflightError("coinbase_tax_contract_invalid")
        gst = {"type": gst["type"], "rate_reported": _amount(gst.get("rate")),
               "rate_units_verified": False}
    return {**result, "basis": "authenticated transaction_summary fee_tier; SPOT / CBE",
            "product_type": "SPOT", "product_venue": "CBE", "actual_order_fee": None,
            "has_cost_plus_commission": cost_plus, "goods_and_services_tax": gst,
            "all_in_fee_rate_verified": False}


def _product(record):
    if (not isinstance(record, dict) or record.get("product_id") != "BTC-USD"
            or record.get("product_type") != "SPOT"
            or record.get("base_currency_id") != "BTC" or record.get("quote_currency_id") != "USD"):
        raise PreflightError("coinbase_product_contract_invalid")
    flags = {name: _bool(record, name) for name in (
        "view_only", "is_disabled", "trading_disabled", "cancel_only", "limit_only", "post_only", "auction_mode")}
    sizes = {name: _amount(record.get(name)) for name in (
        "base_increment", "quote_increment", "price_increment", "base_min_size", "base_max_size", "quote_min_size", "quote_max_size")}
    if (any(Decimal(value) <= 0 for value in sizes.values())
            or Decimal(sizes["base_min_size"]) > Decimal(sizes["base_max_size"])
            or Decimal(sizes["quote_min_size"]) > Decimal(sizes["quote_max_size"])):
        raise PreflightError("coinbase_product_size_invalid")
    status = record.get("status")
    if not isinstance(status, str) or not re.fullmatch(r"[a-zA-Z_]{1,40}", status):
        raise PreflightError("coinbase_product_status_invalid")
    return {"product_id": "BTC-USD", "product_type": "SPOT", "status": status, **flags, **sizes,
            "ordinary_limit_buy_product_flags_allow": status == "online" and not any(
                flags[name] for name in ("view_only", "is_disabled", "trading_disabled", "cancel_only", "auction_mode")),
            "account_trade_permission_verified": False}


def _collect(key_file, *, expected_portfolio, permissions_only, origin, transport, clock):
    if origin != ORIGIN:
        raise PreflightError("coinbase_origin_required")
    portfolio = None if permissions_only else _uuid(expected_portfolio, "coinbase_expected_portfolio_invalid")
    credentials = _credentials(key_file)
    fetch = _safe_get if transport is None else transport
    if not callable(fetch):
        raise PreflightError("coinbase_transport_invalid")
    observations, read_routes = [], []

    def now():
        value = clock()
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise PreflightError("coinbase_observation_clock_invalid")
        return value

    started = now()

    def read(path):
        url = ORIGIN + path
        _url(url)
        at = now()
        if at < (observations[-1] if observations else started) or at - started > MAX_SNAPSHOT_SECONDS:
            raise PreflightError("coinbase_snapshot_window_invalid")
        # Coinbase signs METHOD + host + path. Query parameters are NOT part of uri.
        claims = {"sub": credentials.name, "iss": "cdp", "nbf": int(at), "exp": int(at) + 120,
                  "uri": "GET " + HOST + urlsplit(url).path}
        try:
            token = jwt.encode(claims, credentials.private_key, algorithm="ES256",
                               headers={"kid": credentials.name, "nonce": secrets.token_hex()})
            response = fetch(url, {"Authorization": "Bearer " + token, "Accept": "application/json",
                                   "User-Agent": "ATLAS-Coinbase-view-only-preflight/1"},
                             timeout_seconds=TIMEOUT_SECONDS, max_response_bytes=MAX_RESPONSE_BYTES)
        except PreflightError:
            if transport is None:
                raise
            raise PreflightError("coinbase_transport_failed") from None
        except Exception:
            raise PreflightError("coinbase_transport_failed") from None
        if not isinstance(response, HTTPResponse) or type(response.status) is not int:
            raise PreflightError("coinbase_http_response_invalid")
        if 300 <= response.status < 400:
            raise PreflightError("coinbase_redirect_refused")
        if response.status != 200:
            raise _http_error(response.status)
        if not isinstance(response.body, bytes) or not 0 < len(response.body) <= MAX_RESPONSE_BYTES:
            raise PreflightError("coinbase_response_size_invalid")
        observed = now()
        if observed < at or observed - started > MAX_SNAPSHOT_SECONDS:
            raise PreflightError("coinbase_snapshot_window_invalid")
        observations.append(observed)
        read_routes.append(urlsplit(url).path)  # Opaque pagination cursors are not report data.
        return _json(response.body, "coinbase_response_json_invalid")

    permission_record = read(PERMISSIONS_PATH)
    if permissions_only:
        if not isinstance(permission_record, dict):
            raise PreflightError("coinbase_permissions_shape_invalid")
        portfolio = _uuid(permission_record.get("portfolio_uuid"), "coinbase_portfolio_identity_invalid")
    permissions = _permissions(permission_record, portfolio)
    if permissions_only:
        return {
            "schema_version": "1", "report_kind": "coinbase_view_only_key_discovery",
            "key_permission_read_verified": transport is None,
            "evidence_kind": "verified_tls_coinbase_rest" if transport is None else "injected_test_transport",
            "origin": ORIGIN, "account_environment": "live", "read_routes": read_routes,
            "permissions": permissions, "portfolio_uuid": portfolio,
            "expected_portfolio_binding_verified": False, "account_balances_read": False,
            "execution_enabled": False, "orders_submitted": 0, "account_mutations": 0,
            "observed_at": int(observations[-1]),
            "purpose": "Discover key scope only; verify this UUID matches the selected portfolio before account reads.",
        }
    accounts, identities, cursors = [], set(), set()
    cursor = None
    for _ in range(MAX_PAGES):
        path = ACCOUNTS_PATH + "?" + urlencode(
            [("limit", str(PAGE_LIMIT))] + ([("cursor", cursor)] if cursor else []))
        page = read(path)
        if (not isinstance(page, dict) or not isinstance(page.get("accounts"), list)
                or len(page["accounts"]) > PAGE_LIMIT or type(page.get("has_next")) is not bool
                or ("size" in page and (type(page["size"]) is not int or page["size"] != len(page["accounts"])))):
            raise PreflightError("coinbase_account_page_invalid")
        for record in page["accounts"]:
            identity, account = _account(record, portfolio)
            if identity in identities:
                raise PreflightError("coinbase_duplicate_account")
            identities.add(identity)
            accounts.append(account)
        if not page["has_next"]:
            break
        if not page["accounts"]:
            raise PreflightError("coinbase_pagination_no_progress")
        cursor = _cursor(page.get("cursor"))
        if cursor in cursors:
            raise PreflightError("coinbase_pagination_cycle")
        cursors.add(cursor)
    else:
        raise PreflightError("coinbase_account_listing_incomplete")
    fees = _fees(read(FEES_PATH))
    product = _product(read(PRODUCT_PATH))
    return {
        "schema_version": "1", "report_kind": "coinbase_advanced_view_only_preflight",
        "account_read_verified": transport is None, "response_contract_valid": True,
        "evidence_kind": "verified_tls_coinbase_rest" if transport is None else "injected_test_transport",
        "origin": ORIGIN, "account_environment": "live", "read_routes": read_routes,
        "permissions": permissions, "accounts": accounts, "account_count": len(accounts),
        "accounts_complete": True, "fees": fees, "product": product,
        "equity_usd": None, "daily_realized_pnl": None, "allocation_enforced": False,
        "execution_enabled": False, "orders_submitted": 0, "account_mutations": 0,
        "observed_at": int(observations[-1]), "observation_span_seconds": observations[-1] - started,
        "freshness_basis": "client receipt times of separate GETs; not an atomic account snapshot",
        "limitations": [
            "This is a read of a real Coinbase account, not simulated funds or a paper broker.",
            "Balances are native currency amounts, not USD equity or a $100 allocation.",
            "Only view permission is accepted; this key cannot certify account trading permission.",
            "Product flags and fee tiers do not certify executable order prices, fills or strategy profitability.",
            "Fees are the reported tier, not an order fee quote; order and fill history were not read.",
            "Additional commission and tax effects are unverified; absent fields are unknown, not zero.",
        ],
    }


def collect_preflight(key_file, *, expected_portfolio, origin=ORIGIN, transport=None, clock=time.time):
    """Inspect genuine live balances/fees; require independently selected portfolio."""
    return _collect(key_file, expected_portfolio=expected_portfolio, permissions_only=False,
                    origin=origin, transport=transport, clock=clock)


def collect_permissions(key_file, *, origin=ORIGIN, transport=None, clock=time.time):
    """One GET to discover view-only key scope; never reads balances or fees.

    Unlike the ordinary report, this owner-only setup artifact contains the raw
    portfolio UUID so the owner can bind an independently selected portfolio.
    Scope discovery alone does not verify that selection.
    """
    return _collect(key_file, expected_portfolio=None, permissions_only=True,
                    origin=origin, transport=transport, clock=clock)
