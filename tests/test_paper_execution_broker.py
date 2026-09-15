"""Real request/normalization paths, injected I/O; never broker credentials."""

from datetime import datetime, timezone
from email.utils import format_datetime
import json
import socket
from types import SimpleNamespace
from uuid import UUID

import pytest

from atlas.alpaca_paper_preflight import HTTPResponse
from atlas.paper.broker import AlpacaPaperAPI, PaperBrokerError, Response
from atlas.risk import account_fingerprint

NOW = 1789484400.0  # 2026-09-15 15:00 UTC, during regular US market hours.
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace("+00:00", "Z")
ORDER_ID = str(UUID(int=11))
ACCOUNT_ID = str(UUID(int=12))
CLIENT_ID = "atlas-paper-0123456789"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("tests must never access broker or network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def order(**changes):
    return {"id": ORDER_ID, "client_order_id": CLIENT_ID, "symbol": "SPY", "asset_class": "us_equity",
            "side": "buy", "qty": "0.1", "limit_price": "500", "filled_qty": "0", "filled_avg_price": None,
            "type": "limit", "time_in_force": "day", "extended_hours": False, "status": "new",
            "created_at": STAMP, "updated_at": STAMP, "replaces": None, "replaced_by": None, **changes}


def payload(**changes):
    return {"symbol": "SPY", "side": "buy", "qty": "0.1", "limit_price": "500", "type": "limit",
            "time_in_force": "day", "extended_hours": False, "client_order_id": CLIENT_ID, **changes}


def account(**changes):
    return {"id": ACCOUNT_ID, "status": "ACTIVE", "currency": "USD", "cash": "100", "equity": "100",
            "non_marginable_buying_power": "100", "buying_power": "200", "long_market_value": "0",
            "short_market_value": "0", "multiplier": "1", "account_blocked": False,
            "trading_blocked": False, "trade_suspended_by_user": False, **changes}


def quote(**changes):
    return {"quotes": {"SPY": {"bp": 499, "ap": 500, "bs": 2, "as": 3, "t": STAMP, **changes}}}


def activity(i=0, **changes):
    return {"id": f"fill-{i}", "activity_type": "FILL", "transaction_time": STAMP, "order_id": ORDER_ID,
            "symbol": "SPY", "side": "buy", "qty": "0.01", "price": "499", **changes}


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, (Response, HTTPResponse)) else Response(200, json.dumps(item).encode())


def client(*responses):
    transport = Transport(*responses)
    api = AlpacaPaperAPI(transport=transport, clock=lambda: NOW)
    return api, transport


def gate(api):
    used = set()
    def permit(method, path, terms, value):
        if value != "one-use" or value in used:
            return False
        used.add(value)
        return True
    api.bind_gate(permit)


def test_account_position_quote_asset_clock_normalization():
    api, transport = client(account(), [{"symbol": "SPY", "asset_class": "us_equity", "side": "long",
        "qty": "0.1", "market_value": "50", "current_price": "500", "cost_basis": "49"}], quote(),
        {"symbol": "SPY", "class": "us_equity", "status": "active", "tradable": True, "fractionable": True},
        {"timestamp": STAMP, "is_open": True, "next_open": "2026-09-16T09:30:00-04:00",
         "next_close": "2026-09-15T16:00:00-04:00"})
    got = api.account()
    assert got["fingerprint"] == account_fingerprint("alpaca-paper", ACCOUNT_ID)
    assert got["buying_power"] == "100" and got["multiplier"] == "1" and not got["blocked"]
    assert "id" not in got and "account_id" not in got
    assert api.positions() == [{"symbol": "SPY", "qty": "0.1", "market_value": "50", "current_price": "500", "cost_basis": "49"}]
    assert api.quote("SPY")["ask"] == "500"
    assert transport.calls[-1][1] == "https://data.alpaca.markets/v2/stocks/quotes/latest?symbols=SPY&feed=iex"
    assert api.asset("SPY")["asset_class"] == "us_equity"
    assert api.clock_status()["is_open"] is True
    assert api.provenance == api.transport_provenance == "TEST"
    assert all("APCA-API-KEY-ID" not in call[2] for call in transport.calls)


def test_submit_exact_terms_single_use_gate_and_cancel_ack_only():
    api, transport = client(order(), Response(204))
    gate(api)
    result = api.submit_order(payload(qty="0.1000", limit_price="500.00"), permit="one-use")
    assert result["id"] == ORDER_ID and result["qty"] == "0.1"
    assert transport.calls[0][3] == payload()
    with pytest.raises(PaperBrokerError, match="paper_mutation_permit_rejected"):
        api.submit_order(payload(), permit="one-use")
    with pytest.raises(PaperBrokerError, match="paper_mutation_gate_already_bound_or_invalid"):
        api.bind_gate(lambda *args: True)
    api2, transport2 = client(Response(204))
    gate(api2)
    assert api2.cancel_order(ORDER_ID, permit="one-use") is None
    assert transport2.calls[0][0] == "DELETE"
    assert len(transport.calls) == 1


def test_direct_mutations_and_owned_network_activation_are_blocked_before_credentials(monkeypatch):
    api, transport = client(order())
    with pytest.raises(PaperBrokerError, match="paper_mutation_permit_required"):
        api.submit_order(payload())
    with pytest.raises(PaperBrokerError, match="paper_mutation_permit_required"):
        api.cancel_order(ORDER_ID)
    assert not transport.calls
    def forbidden(_):
        raise AssertionError("blocked mutation must not read credentials")
    monkeypatch.setattr("atlas.paper.broker._credentials", forbidden)
    owned = AlpacaPaperAPI("/intentionally/not/a/key.json", clock=lambda: NOW)
    gate(owned)
    with pytest.raises(PaperBrokerError, match="paper_network_activation_pending_review"):
        owned.submit_order(payload(), permit="one-use")
    with pytest.raises(PaperBrokerError, match="paper_network_activation_pending_review"):
        owned.cancel_order(ORDER_ID, permit="one-use")
    with pytest.raises(PaperBrokerError, match="paper_replace_unsupported"):
        owned.replace_order(ORDER_ID, payload(), permit="one-use")


@pytest.mark.parametrize("change", [
    {"qty": 0.1}, {"qty": "0.0000000001"}, {"qty": "1"}, {"qty": "0"}, {"qty": "NaN"},
    {"limit_price": "500.001"}, {"limit_price": "0.00001"}, {"type": "market"}, {"side": "short"},
    {"time_in_force": "gtc"}, {"extended_hours": True}, {"extended_hours": 0}, {"symbol": "BTC/USD"},
    {"client_order_id": "x?y=1"}, {"notional": "100"},
])
def test_invalid_terms_refuse_before_gate_and_io(change):
    api, transport = client()
    calls = []
    api.bind_gate(lambda *args: calls.append(args) or True)
    with pytest.raises(PaperBrokerError):
        api.submit_order(payload(**change), permit="anything")
    assert not calls and not transport.calls


@pytest.mark.parametrize("response,ambiguous,http_status", [
    (Response(400), False, 400), (Response(401), False, 401), (Response(403), False, 403),
    (Response(422, b'{"message":"client_order_id must be unique"}'), True, 422),
    (Response(500), True, 500), (Response(302), True, 302), (Response(404), True, 404),
    (Response(200, b"malformed"), True, 200), (TimeoutError("secret provider response"), True, None),
])
def test_mutation_error_semantics_never_retry_or_leak(response, ambiguous, http_status):
    api, transport = client(response)
    gate(api)
    with pytest.raises(PaperBrokerError) as captured:
        api.submit_order(payload(), permit="one-use")
    error = captured.value
    assert error.ambiguous is ambiguous and error.http_status == http_status
    assert "secret" not in str(error) and "client_order_id must" not in str(error)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status,filled,average", [
    ("partially_filled", "0.04", "499"), ("pending_cancel", "0.04", "499"),
    ("canceled", "0.04", "499"), ("expired", "0.04", "499"), ("filled", "0.1", "499"),
])
def test_status_preserves_cumulative_fills_and_native_terminality(status, filled, average):
    api, _ = client(order(status=status, filled_qty=filled, filled_avg_price=average))
    result = api.order_by_client_id(CLIENT_ID)
    assert result["status"] == status and result["filled_qty"] == filled and result["filled_avg_price"] == average


@pytest.mark.parametrize("changes", [
    {"status": "partially_filled", "filled_qty": "0"}, {"status": "filled", "filled_qty": "0"},
    {"filled_qty": "0.1", "filled_avg_price": "499"},
    {"status": "canceled", "filled_qty": "0.04", "filled_avg_price": None},
    {"status": "filled", "filled_qty": "0.1", "filled_avg_price": "501"},
    {"status": "replaced", "replaced_by": None}, {"asset_class": "crypto"},
    {"extended_hours": True}, {"qty": "0.1", "side": "sell_short"},
])
def test_invalid_order_responses_fail_closed(changes):
    api, _ = client(order(**changes))
    with pytest.raises(PaperBrokerError):
        api.order_by_client_id(CLIENT_ID)


def test_lookup_absence_never_submits_and_wrong_correlation_is_ambiguous():
    api, transport = client(Response(404))
    assert api.order_by_client_id(CLIENT_ID) is None
    assert [call[0] for call in transport.calls] == ["GET"]
    api, _ = client(order(client_order_id="foreign"))
    gate(api)
    with pytest.raises(PaperBrokerError) as captured:
        api.submit_order(payload(), permit="one-use")
    assert captured.value.ambiguous


def test_order_history_full_page_or_duplicate_refuses():
    for rows in ([order()] * 500, [order(), order()]):
        api, _ = client(rows)
        with pytest.raises(PaperBrokerError):
            api.orders(status="all", after=NOW - 86400)


def test_activities_pagination_preserves_order_ids_and_nonfill_events_unknown_fees():
    api, transport = client([activity(i) for i in range(100)], [
        {"id": "deposit-1", "activity_type": "CSD", "date": "2026-09-15", "net_amount": "100"},
        activity(101, fee="0.01")])
    rows = api.activities(after=NOW - 86400)
    assert len(rows) == 102 and rows[0]["order_id"] == ORDER_ID and "fee" not in rows[0]
    assert rows[100]["type"] == "CSD" and rows[100]["at"] > 0 and rows[101]["fee"] == "0.01"
    assert "page_token=fill-99" in transport.calls[-1][1]


def test_activities_cycle_or_saturation_does_not_claim_complete():
    api, _ = client([activity(i) for i in range(100)], [activity(0)])
    with pytest.raises(PaperBrokerError, match="paper_activity_pagination_cycle"):
        api.activities()
    api, _ = client(*[[activity(page * 100 + i) for i in range(100)] for page in range(20)])
    with pytest.raises(PaperBrokerError, match="paper_activity_history_incomplete"):
        api.activities()


@pytest.mark.parametrize("changes", [
    {"bp": 501}, {"ap": 0}, {"as": 0}, {"t": "2026-01-01T00:00:00Z"},
    {"t": "2099-01-01T00:00:00Z"}, {"bp": "499"},
])
def test_quote_data_fresh_finite_and_nonzero(changes):
    api, _ = client(quote(**changes))
    with pytest.raises(PaperBrokerError):
        api.quote("SPY")


@pytest.mark.parametrize("headers,expected", [
    ({"Retry-After": "12"}, 12), ({"Retry-After": "900"}, 900),
    ({"X-RateLimit-Reset": str(NOW + 90)}, 90),
    ({"X-RateLimit-Reset": str(NOW + 86400)}, 86400),
    ({"Retry-After": format_datetime(datetime.fromtimestamp(NOW + 900, timezone.utc), usegmt=True)}, 900),
    ({"Retry-After": "900", "retry-after": "12"}, 900), ({}, 60),
])
def test_429_defers_future_io_and_never_retries(headers, expected):
    api, transport = client(Response(429, headers=headers), account())
    with pytest.raises(PaperBrokerError) as captured:
        api.account()
    assert captured.value.retry_after == expected and captured.value.http_status == 429
    with pytest.raises(PaperBrokerError, match="paper_rate_limited"):
        api.account()
    assert len(transport.calls) == 1
    api._clock = lambda: NOW + expected
    assert api.account()["cash"] == "100"


def test_long_provider_cooldown_has_no_request_at_300_seconds():
    api, transport = client(Response(429, headers={"Retry-After": "900"}), account())
    with pytest.raises(PaperBrokerError):
        api.account()
    api._clock = lambda: NOW + 300
    with pytest.raises(PaperBrokerError) as captured:
        api.account()
    assert captured.value.retry_after == 600 and len(transport.calls) == 1
    api._clock = lambda: NOW + 899
    with pytest.raises(PaperBrokerError) as captured:
        api.account()
    assert captured.value.retry_after == 1 and len(transport.calls) == 1
    api._clock = lambda: NOW + 900
    assert api.account()["cash"] == "100" and len(transport.calls) == 2


@pytest.mark.parametrize("value", ["inf", "nan", "99999999", "-900", "invalid", "9" * 129, True, {}])
def test_invalid_provider_retry_boundary_latches_fail_closed(value):
    api, transport = client(Response(429, headers={"Retry-After": value}), account())
    with pytest.raises(PaperBrokerError) as captured:
        api.account()
    assert captured.value.code == "paper_rate_limit_header_invalid"
    assert captured.value.http_status == 429 and captured.value.retry_after is None
    api._clock = lambda: NOW + 86400
    with pytest.raises(PaperBrokerError, match="paper_rate_limit_header_invalid"):
        api.account()
    assert len(transport.calls) == 1


def test_retry_after_is_measured_from_response_not_request_start():
    times = iter([NOW, NOW + 9, NOW + 900])
    api, transport = client(Response(429, headers={"Retry-After": "900"}))
    api._clock = lambda: next(times)
    with pytest.raises(PaperBrokerError):
        api.account()
    with pytest.raises(PaperBrokerError) as captured:
        api.account()
    assert captured.value.retry_after == 9 and len(transport.calls) == 1


def test_execution_opt_in_is_explicit_and_owned_transport_still_requires_permit(monkeypatch):
    sent = []
    def owned(method, url, headers, data, *, paper_execution_enabled=False):
        sent.append((method, url, data, paper_execution_enabled))
        return Response(204) if method == "DELETE" else Response(200, json.dumps(order()).encode())
    monkeypatch.setattr("atlas.paper.broker._owned_http", owned)
    monkeypatch.setattr("atlas.paper.broker._credentials", lambda _: SimpleNamespace(api_key="fixture-key", api_secret="fixture-secret"))
    disabled = AlpacaPaperAPI("/fixture/no-real-credential", clock=lambda: NOW)
    assert disabled.provenance == "PAPER_API" and disabled.execution_enabled is False
    api = AlpacaPaperAPI("/fixture/no-real-credential", clock=lambda: NOW, paper_execution_enabled=True)
    assert api.execution_enabled and api.provenance == "PAPER_API"
    with pytest.raises(PaperBrokerError, match="paper_mutation_permit_required"):
        api.submit_order(payload())
    with pytest.raises(PaperBrokerError, match="paper_mutation_permit_required"):
        api.submit_order(payload(), permit="unbound")
    assert not sent
    permits = set()
    api.bind_gate(lambda method, path, data, permit: not (permit in permits or permits.add(permit)))
    assert api.submit_order(payload(), permit="submit")["id"] == ORDER_ID
    assert api.cancel_order(ORDER_ID, permit="cancel") is None
    assert sent == [("POST", "https://paper-api.alpaca.markets/v2/orders", payload(), True),
                    ("DELETE", "https://paper-api.alpaca.markets/v2/orders/" + ORDER_ID, None, True)]
    with pytest.raises(PaperBrokerError, match="paper_mutation_permit_rejected"):
        api.submit_order(payload(), permit="submit")
    assert len(sent) == 2
    for invalid in (1, "true", None):
        with pytest.raises(PaperBrokerError, match="paper_client_configuration_invalid"):
            AlpacaPaperAPI(paper_execution_enabled=invalid)
    injected, _ = client()
    assert injected.execution_enabled


def test_owned_http_opt_in_can_only_write_valid_paper_targets(monkeypatch):
    from atlas.paper.broker import _owned_http
    sent = []
    class WireResponse:
        status = 200
        def __init__(self, body):
            self.body = body
        def getheader(self, name):
            return str(len(self.body)) if name == "Content-Length" else None
        def read1(self, count):
            data, self.body = self.body, b""
            return data
    class Connection:
        sock = None
        def __init__(self, host, **kwargs):
            assert host == "paper-api.alpaca.markets" and kwargs["context"].check_hostname
        def request(self, method, path, *, body, headers):
            sent.append((method, path, body))
        def getresponse(self):
            return WireResponse(json.dumps(order()).encode())
        def close(self):
            pass
    monkeypatch.setattr("atlas.paper.broker.http.client.HTTPSConnection", Connection)
    with pytest.raises(PaperBrokerError, match="paper_network_activation_pending_review"):
        _owned_http("POST", "https://paper-api.alpaca.markets/v2/orders", {}, payload())
    for origin in ("https://api.alpaca.markets", "https://data.alpaca.markets", "http://paper-api.alpaca.markets"):
        with pytest.raises(PaperBrokerError, match="paper_endpoint_not_allowed"):
            _owned_http("POST", origin + "/v2/orders", {}, payload(), paper_execution_enabled=True)
    assert sent == []
    result = _owned_http("POST", "https://paper-api.alpaca.markets/v2/orders", {}, payload(), paper_execution_enabled=True)
    assert result.status == 200 and json.loads(sent[0][2]) == payload()


def test_conservative_local_request_budget_and_clock_rewind(monkeypatch):
    monkeypatch.setattr("atlas.paper.broker.MAX_REQUESTS_PER_MINUTE", 2)
    api, transport = client(account(), account())
    api.account()
    api.account()
    with pytest.raises(PaperBrokerError, match="paper_rate_limited"):
        api.account()
    assert len(transport.calls) == 2
    api._clock = lambda: NOW - 1
    with pytest.raises(PaperBrokerError, match="paper_clock_rewind"):
        api.account()


@pytest.mark.parametrize("method,path,data", [
    ("GET", "//api.alpaca.markets/v2/account", False),
    ("GET", "/v2/account?unused=1", False), ("GET", "/v2/account#fragment", False),
    ("GET", "/v2/orders?status=all&limit=500&direction=asc&nested=false&status=open", False),
    ("DELETE", "/v2/orders", False), ("PATCH", "/v2/orders/" + ORDER_ID, False),
    ("GET", "/v2/stocks/quotes/latest?symbols=SPY&feed=sip", True),
])
def test_endpoint_allowlist_before_credentials_or_transport(monkeypatch, method, path, data):
    api, transport = client()
    api._credentials_path = "/not/a/key"
    monkeypatch.setattr("atlas.paper.broker._credentials", lambda _: pytest.fail("credentials loaded"))
    with pytest.raises(PaperBrokerError):
        api._request(method, path, data=data)
    assert not transport.calls


def test_malformed_json_or_duplicate_financial_keys_fail():
    for body in (b'{"cash":"100","cash":"200"}', b'{"value":NaN}', b"x" * 1_000_001):
        api, _ = client(Response(200, body))
        with pytest.raises(PaperBrokerError):
            api.account()


def test_gate_cannot_mutate_permitted_request():
    api, transport = client()
    def mutating(method, path, terms, permit):
        terms["qty"] = "2"
        return True
    api.bind_gate(mutating)
    with pytest.raises(PaperBrokerError, match="paper_mutation_permit_rejected"):
        api.submit_order(payload(), permit="unused")
    assert not transport.calls


def test_httpresponse_compatibility_and_safe_repr():
    api, _ = client(HTTPResponse(200, json.dumps(account()).encode()))
    assert api.account()["cash"] == "100"
    assert "credentials=redacted" in repr(api)


def test_owned_http_tls_body_bounds_redirect_refusal(monkeypatch):
    from atlas.paper.broker import _owned_http
    calls = []
    class WireResponse:
        status = 200
        def getheader(self, name):
            return "2" if name == "Content-Length" else None
        def read1(self, count):
            if not hasattr(self, "done"):
                self.done = True
                return b"{}"
            return b""
    response = WireResponse()
    class Connection:
        sock = None
        def __init__(self, host, **kwargs):
            assert host == "paper-api.alpaca.markets"
            assert kwargs["context"].check_hostname
            calls.append("connect")
        def request(self, method, path, **kwargs):
            calls.append((method, path))
        def getresponse(self):
            return response
        def close(self):
            calls.append("close")
    monkeypatch.setattr("atlas.paper.broker.http.client.HTTPSConnection", Connection)
    assert _owned_http("GET", "https://paper-api.alpaca.markets/v2/account", {}, None).body == b"{}"
    assert calls[-1] == "close"
    response.status = 302
    assert _owned_http("GET", "https://paper-api.alpaca.markets/v2/account", {}, None).status == 302
    assert calls.count("connect") == 2
    response.status = 200
    response.getheader = lambda name: "1000001" if name == "Content-Length" else None
    with pytest.raises(PaperBrokerError, match="paper_response_size_invalid"):
        _owned_http("GET", "https://paper-api.alpaca.markets/v2/account", {}, None)
    assert calls[-1] == "close"
