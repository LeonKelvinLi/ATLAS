"""Network-free contract tests for the public-only Coinbase surface."""

from copy import deepcopy
import json
import socket

import pytest

import atlas.brokers.coinbase_public as public
from atlas.brokers.coinbase_public import CoinbasePublicClient, CoinbasePublicError, HTTPResponse

NOW = 1789078453


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Coinbase tests cannot contact a network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def product(**changes):
    return {"product_id": "BTC-USD", "product_type": "SPOT", "base_currency_id": "BTC", "quote_currency_id": "USD",
            "status": "online", "base_increment": "0.00000001", "quote_increment": "0.01", "price_increment": "0.01",
            "base_min_size": "0.00000001", "base_max_size": "3400", "quote_min_size": "1", "quote_max_size": "150000000",
            **{name: False for name in public.FLAGS}, **changes}


def book(**changes):
    return {"pricebook": {"product_id": "BTC-USD", "time": "2026-09-10T22:14:12.794990Z",
                          "bids": [{"price": "77075.04", "size": "0.01797682"}],
                          "asks": [{"price": "77075.07", "size": "0.10379491"}], **changes}}


class Transport:
    def __init__(self, *responses):
        self.responses, self.calls = responses, []
    def __call__(self, *args, **limits):
        self.calls.append((args, limits))
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, HTTPResponse) else HTTPResponse(200, json.dumps(response).encode())


def test_snapshot_uses_exactly_two_public_gets_and_no_account_claim():
    transport = Transport(product(), book())
    client = CoinbasePublicClient(transport=transport, clock=lambda: NOW)
    result = client.snapshot()
    assert result["product"]["base_increment"] == "0.00000001"
    assert result["pricebook"]["asks"][0]["price"] == "77075.07"
    assert result["transport_provenance"] == "injected_test_transport"
    assert result["public_only"] and not result["account_permissions_verified"]
    assert [args[1] for args, _ in transport.calls] == [public.ORIGIN + public.PRODUCT_PATH, public.ORIGIN + public.BOOK_PATH]
    assert all(args[0] == "GET" and args[2] is None and args[3] == {
        "Accept": "application/json", "Cache-Control": "no-cache"} for args, _ in transport.calls)
    assert all(limits == {"timeout_seconds": 10, "max_response_bytes": 1_000_000} for _, limits in transport.calls)
    assert not any(hasattr(client, name) for name in ("account", "orders", "submit", "cancel", "transfer"))


@pytest.mark.parametrize("change", [
    {"product_id": "ETH-USD"}, {"product_type": "FUTURE"}, {"quote_currency_id": "USDC"},
    {"base_currency_id": "DOGE"}, {"status": "offline"}, {"base_min_size": "4000"},
    {"quote_min_size": "150000001"}, {"base_increment": "5000"}, {"quote_increment": "150000001"},
    *[{name: True} for name in ("is_disabled", "trading_disabled", "cancel_only", "auction_mode", "view_only")],
    *[{name: "false"} for name in public.FLAGS],
])
def test_unknown_or_restricted_product_refuses_before_book_request(change):
    t = Transport(product(**change))
    with pytest.raises(CoinbasePublicError):
        CoinbasePublicClient(transport=t, clock=lambda: NOW).snapshot()
    assert len(t.calls) == 1


@pytest.mark.parametrize("name", public.FLAGS + public.AMOUNTS)
def test_missing_product_contract_fields_refuse(name):
    value = product()
    del value[name]
    with pytest.raises(CoinbasePublicError):
        public.validate_product(value)


@pytest.mark.parametrize("bad", [None, True, 0.01, 1, "0", "-1", "NaN", "Infinity", "1e-37", "1e37", "bad"])
def test_product_amounts_are_positive_bounded_decimal_strings(bad):
    with pytest.raises(CoinbasePublicError):
        public.validate_product(product(price_increment=bad))


@pytest.mark.parametrize("change", [
    {"product_id": "BTC-USDC"}, {"time": "2026-09-10T22:13:12Z"}, {"time": "2026-09-10T22:15:12Z"},
    {"time": "bad"}, {"time": None}, {"time": "2026-09-31T22:14:12Z"}, {"bids": []}, {"asks": []},
    {"bids": [{"price": "77075.07", "size": "1"}]}, {"bids": [{"price": "80000", "size": "1"}]},
    {"asks": [{"price": "77075.07", "size": "0"}]}, {"asks": [{"price": "NaN", "size": "1"}]},
    {"asks": [{"price": "77075.07", "size": True}]}, {"bids": [None]},
    {"asks": [{"price": "77075.07", "size": "1"}, {"price": "77075.06", "size": "1"}]},
    {"asks": [{"price": "77075.07", "size": "1"}] * 2},
    {"bids": [{"price": "77075.04", "size": "1"}, {"price": "77075.05", "size": "1"}]},
    {"asks": [{"price": "77075.07", "size": "1"}] * 101},
])
def test_malformed_stale_crossed_or_unordered_books_refuse(change):
    with pytest.raises(CoinbasePublicError):
        public.validate_book(book(**change), now=NOW)


@pytest.mark.parametrize("body", [b"not-json", b'{"a":1,"a":2}', b'{"a":NaN}', b"\xff", b"[" * 10000])
def test_bad_json_never_accepted(body):
    with pytest.raises(CoinbasePublicError, match="json_invalid"):
        public.parse_json(body)


@pytest.mark.parametrize("response", [
    HTTPResponse(301, b"PRIVATE"), HTTPResponse(403, b"PRIVATE"), HTTPResponse(429, b"PRIVATE"),
    HTTPResponse(500, b"PRIVATE"), HTTPResponse(200, b""), HTTPResponse(200, b"x" * 1_000_001),
    HTTPResponse(True, b"{}"), TimeoutError("PRIVATE"), OSError("PRIVATE"),
])
def test_http_failures_do_not_retry_or_expose_response_text(response):
    t = Transport(response)
    with pytest.raises(CoinbasePublicError) as error:
        CoinbasePublicClient(transport=t, clock=lambda: NOW).snapshot()
    assert len(t.calls) == 1 and "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("times", [(NOW, NOW - 1, NOW), (NOW, NOW, NOW + 21), (NOW, NOW + 1, NOW)])
def test_clock_rollback_or_slow_snapshot_refuses(times):
    clock = iter(times)
    with pytest.raises(CoinbasePublicError, match="clock_invalid"):
        CoinbasePublicClient(transport=Transport(product(), book()), clock=lambda: next(clock)).snapshot()


@pytest.mark.parametrize("method,url,body,headers", [
    ("POST", public.ORIGIN + public.PRODUCT_PATH, None, {"Accept": "application/json", "Cache-Control": "no-cache"}),
    ("GET", "http://api.coinbase.com" + public.PRODUCT_PATH, None, {}),
    ("GET", public.ORIGIN + "/api/v3/brokerage/accounts", None, {}),
    ("GET", "https://evil.invalid" + public.PRODUCT_PATH, None, {}),
    ("GET", public.ORIGIN + public.BOOK_PATH + "&extra=true", None, {}),
    ("GET", public.ORIGIN + public.PRODUCT_PATH, b"{}", {}),
    ("GET", public.ORIGIN + public.PRODUCT_PATH, None, {"Authorization": "PRIVATE"}),
])
def test_transport_rejects_every_nonpublic_route_or_auth_before_socket(method, url, body, headers):
    with pytest.raises(CoinbasePublicError, match="endpoint_not_allowed"):
        public._owned_http(method, url, body, headers, timeout_seconds=10, max_response_bytes=1_000_000)


def test_tls_transport_does_not_follow_redirect_and_always_closes(monkeypatch):
    calls = []
    class Response:
        status = 302
        def read1(self, _):
            pytest.fail("redirect body must not be read")
    class Connection:
        def __init__(self, host, **kwargs):
            calls.append((host, kwargs))
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
        def getresponse(self):
            return Response()
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(public.http.client, "HTTPSConnection", Connection)
    response = public._owned_http("GET", public.ORIGIN + public.PRODUCT_PATH, None,
                                 {"Accept": "application/json", "Cache-Control": "no-cache"},
                                 timeout_seconds=10, max_response_bytes=1_000_000)
    assert response == HTTPResponse(302, b"") and len(calls) == 3 and calls[-1] == "closed"
    assert calls[0][0] == "api.coinbase.com" and calls[0][1]["context"].check_hostname


@pytest.mark.parametrize("length,chunks", [("1000001", []), ("bad", []), ("3", [b"{}", b""]), (None, [b"x" * 1_000_001])])
def test_bounded_tls_body_and_declared_length(monkeypatch, length, chunks):
    parts = iter(chunks)
    class Response:
        status = 200
        def getheader(self, _):
            return length
        def read1(self, _):
            return next(parts)
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs):
            pass
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(public.http.client, "HTTPSConnection", Connection)
    with pytest.raises(CoinbasePublicError):
        public._owned_http("GET", public.ORIGIN + public.PRODUCT_PATH, None,
                           {"Accept": "application/json", "Cache-Control": "no-cache"},
                           timeout_seconds=10, max_response_bytes=1_000_000)
