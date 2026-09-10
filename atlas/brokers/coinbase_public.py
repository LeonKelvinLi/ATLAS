"""Unauthenticated, read-only BTC-USD market snapshots from Coinbase Advanced.

Only two pinned public GET routes exist. There is no account, key, order,
transfer, worker, retry or redirect interface in this module.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import http.client
import json
import re
import ssl
import time

from atlas.domain import decimal, financial_context

ORIGIN = "https://api.coinbase.com"
PRODUCT_ID = "BTC-USD"
PRODUCT_PATH = "/api/v3/brokerage/market/products/BTC-USD"
BOOK_PATH = "/api/v3/brokerage/market/product_book?product_id=BTC-USD&limit=100"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1_000_000
MAX_AGE_SECONDS = Decimal("10")
MAX_FUTURE_SECONDS = Decimal("2")
FLAGS = ("is_disabled", "trading_disabled", "cancel_only", "limit_only",
         "post_only", "auction_mode", "view_only")
AMOUNTS = ("base_increment", "quote_increment", "price_increment", "base_min_size",
           "base_max_size", "quote_min_size", "quote_max_size")


class CoinbasePublicError(ValueError):
    """Fixed diagnostic code; raw remote text is never part of the error."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes


def _endpoint(method, url, body, headers):
    if (method != "GET" or url not in {ORIGIN + PRODUCT_PATH, ORIGIN + BOOK_PATH}
            or body is not None or headers != {"Accept": "application/json", "Cache-Control": "no-cache"}):
        raise CoinbasePublicError("coinbase_public_endpoint_not_allowed")
    return "api.coinbase.com", url.removeprefix(ORIGIN)


def _owned_http(method, url, body, headers, *, timeout_seconds, max_response_bytes):
    host, target = _endpoint(method, url, body, headers)
    if timeout_seconds != TIMEOUT_SECONDS or max_response_bytes != MAX_RESPONSE_BYTES:
        raise CoinbasePublicError("coinbase_public_transport_limits_invalid")
    deadline = time.monotonic() + timeout_seconds
    connection = http.client.HTTPSConnection(host, timeout=timeout_seconds,
                                              context=ssl.create_default_context())
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            return HTTPResponse(response.status, b"")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > max_response_bytes):
            raise CoinbasePublicError("coinbase_public_response_size_invalid")
        chunks, total = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CoinbasePublicError("coinbase_public_request_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_response_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                raise CoinbasePublicError("coinbase_public_response_too_large")
        if time.monotonic() > deadline:
            raise CoinbasePublicError("coinbase_public_request_timeout")
        if length is not None and total != int(length):
            raise CoinbasePublicError("coinbase_public_response_length_mismatch")
        return HTTPResponse(response.status, b"".join(chunks))
    finally:
        try:
            connection.close()
        except Exception:
            pass


def parse_json(raw):
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
    except (ValueError, UnicodeError, AttributeError, RecursionError):
        raise CoinbasePublicError("coinbase_public_json_invalid") from None


def amount(value, *, positive=True):
    if not isinstance(value, str):
        raise CoinbasePublicError("coinbase_public_amount_invalid")
    try:
        return decimal(value, positive=positive, nonnegative=True)
    except ValueError:
        raise CoinbasePublicError("coinbase_public_amount_invalid") from None


def timestamp(value):
    if not isinstance(value, str):
        raise CoinbasePublicError("coinbase_public_timestamp_invalid")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", value)
    if match is None:
        raise CoinbasePublicError("coinbase_public_timestamp_invalid")
    try:
        whole = int(datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        return Decimal(whole) + (Decimal("0." + match[2]) if match[2] else Decimal(0))
    except (ValueError, OverflowError):
        raise CoinbasePublicError("coinbase_public_timestamp_invalid") from None


def clock_value(value):
    try:
        return decimal(value, positive=True)
    except ValueError:
        raise CoinbasePublicError("coinbase_public_clock_invalid") from None


@financial_context
def validate_product(payload):
    if not isinstance(payload, dict) or any(payload.get(key) != expected for key, expected in (
            ("product_id", PRODUCT_ID), ("product_type", "SPOT"), ("base_currency_id", "BTC"),
            ("quote_currency_id", "USD"), ("status", "online"))):
        raise CoinbasePublicError("coinbase_public_product_identity_or_status_invalid")
    if any(type(payload.get(flag)) is not bool for flag in FLAGS):
        raise CoinbasePublicError("coinbase_public_product_flags_unknown")
    if any(payload[flag] for flag in ("is_disabled", "trading_disabled", "cancel_only", "auction_mode", "view_only")):
        raise CoinbasePublicError("coinbase_public_product_restricted")
    amounts = {name: amount(payload.get(name)) for name in AMOUNTS}
    if (amounts["base_min_size"] > amounts["base_max_size"]
            or amounts["quote_min_size"] > amounts["quote_max_size"]
            or amounts["base_increment"] > amounts["base_max_size"]
            or amounts["quote_increment"] > amounts["quote_max_size"]):
        raise CoinbasePublicError("coinbase_public_product_bounds_invalid")
    return {"product_id": PRODUCT_ID, "product_type": "SPOT", "base_currency_id": "BTC",
            "quote_currency_id": "USD", "status": "online",
            **{name: format(value, "f") for name, value in amounts.items()},
            **{flag: payload[flag] for flag in FLAGS}}


@financial_context
def validate_book(payload, *, now):
    now = clock_value(now)
    if not isinstance(payload, dict) or not isinstance(payload.get("pricebook"), dict):
        raise CoinbasePublicError("coinbase_public_book_invalid")
    book = payload["pricebook"]
    if book.get("product_id") != PRODUCT_ID:
        raise CoinbasePublicError("coinbase_public_book_identity_invalid")
    stamp = timestamp(book.get("time"))
    if not -MAX_FUTURE_SECONDS <= now - stamp <= MAX_AGE_SECONDS:
        raise CoinbasePublicError("coinbase_public_book_stale_or_future")
    result = {}
    for side in ("bids", "asks"):
        rows = book.get(side)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise CoinbasePublicError("coinbase_public_book_depth_invalid")
        clean, previous = [], None
        for row in rows:
            if not isinstance(row, dict):
                raise CoinbasePublicError("coinbase_public_book_row_invalid")
            price, size = amount(row.get("price")), amount(row.get("size"))
            if previous is not None and ((side == "bids" and price >= previous)
                                         or (side == "asks" and price <= previous)):
                raise CoinbasePublicError("coinbase_public_book_unordered")
            clean.append({"price": format(price, "f"), "size": format(size, "f")})
            previous = price
        result[side] = clean
    if amount(result["bids"][0]["price"]) >= amount(result["asks"][0]["price"]):
        raise CoinbasePublicError("coinbase_public_book_crossed_or_locked")
    return {"product_id": PRODUCT_ID, "time": book["time"], **result}


class CoinbasePublicClient:
    def __init__(self, *, transport=None, clock=time.time):
        self._transport = transport if transport is not None else _owned_http
        self._clock = clock
        self.transport_provenance = "injected_test_transport" if transport is not None else "verified_tls"

    def _get(self, path):
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        _endpoint("GET", ORIGIN + path, None, headers)
        try:
            response = self._transport("GET", ORIGIN + path, None, headers,
                                       timeout_seconds=TIMEOUT_SECONDS, max_response_bytes=MAX_RESPONSE_BYTES)
        except CoinbasePublicError:
            raise
        except Exception:
            raise CoinbasePublicError("coinbase_public_request_failed") from None
        if not isinstance(response, HTTPResponse) or type(response.status) is not int:
            raise CoinbasePublicError("coinbase_public_response_invalid")
        if response.status != 200:
            raise CoinbasePublicError("coinbase_public_http_" + str(response.status)
                                      if 100 <= response.status <= 599 else "coinbase_public_http_invalid")
        if not isinstance(response.body, bytes) or not 0 < len(response.body) <= MAX_RESPONSE_BYTES:
            raise CoinbasePublicError("coinbase_public_response_size_invalid")
        return parse_json(response.body)

    @financial_context
    def snapshot(self):
        started = clock_value(self._clock())
        product = validate_product(self._get(PRODUCT_PATH))
        product_observed = clock_value(self._clock())
        raw_book = self._get(BOOK_PATH)
        observed = clock_value(self._clock())
        if not started <= product_observed <= observed or observed - started > TIMEOUT_SECONDS * 2:
            raise CoinbasePublicError("coinbase_public_snapshot_clock_invalid")
        book = validate_book(raw_book, now=observed)
        return {"product": product, "pricebook": book,
                "product_observed_at": format(product_observed, "f"), "observed_at": format(observed, "f"),
                "transport_provenance": self.transport_provenance,
                "source": ORIGIN, "public_only": True, "account_permissions_verified": False}
