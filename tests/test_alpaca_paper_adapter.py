"""Synthetic HTTP fixtures for the narrow paper-only Alpaca client."""

from copy import deepcopy
import json
import socket
import ssl
from uuid import UUID

import pytest

import atlas.brokers.alpaca_paper as adapter
from atlas.brokers.alpaca_paper import AlpacaPaperClient, AlpacaPaperRejected, HTTPResponse
from atlas.domain import BrokerError, BrokerRejected, UnknownSubmission

NOW = 1789053228
CLIENT = "0728916d-d078-4ee3-9f3b-13f22c2c03a4"
ORDER = "a36ee0f4-a9c4-4449-b379-3ccf699382db"
ASSET = "ea1143e5-8061-46f1-8cb6-5a82a1605322"
ACCOUNT = "a9d91a9e-d0f8-4a34-92c0-574c0d28145f"
KEY, SECRET = "fake-paper-key", "fake-paper-secret-never-log"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("paper adapter tests cannot contact a network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "paper-key-fixture.json"
    path.write_text(json.dumps({"api_key": KEY, "api_secret": SECRET}))
    path.chmod(0o600)
    return path


def account(**changes):
    return {"id": ACCOUNT, "status": "ACTIVE", "crypto_status": "ACTIVE", "currency": "USD",
            "cash": "100", "equity": "100", "buying_power": "200", "non_marginable_buying_power": "100",
            "account_blocked": False, "trading_blocked": False, "trade_suspended_by_user": False,
            "account_number": "PRIVATE-ACCOUNT-NUMBER", **changes}


def order(**changes):
    return {"id": ORDER, "client_order_id": CLIENT, "asset_id": ASSET, "symbol": "BTC/USD", "asset_class": "crypto",
            "side": "buy", "type": "limit", "time_in_force": "gtc", "qty": "0.00001",
            "limit_price": "100000", "filled_qty": "0", "filled_avg_price": None,
            "notional": None, "status": "new", "submitted_at": "2026-09-10T15:13:48.123456789Z", **changes}


def asset(**changes):
    return {"id": ASSET, "symbol": "BTC/USD", "class": "crypto", "status": "active", "tradable": True,
            "marginable": False, "shortable": False, "fractionable": True, "min_order_size": "0.00001",
            "min_trade_increment": "0.000000001", "price_increment": "0.000000001", **changes}


def quote(**changes):
    return {"quotes": {"BTC/USD": {"bp": 99999, "ap": 100000, "bs": 0.01, "as": 0.002,
                                   "t": "2026-09-10T15:13:47.123456789Z", **changes}}}


class Transport:
    def __init__(self, *responses):
        self.responses, self.calls = responses, []
    def __call__(self, method, url, body, headers, **limits):
        self.calls.append((method, url, body, headers, limits))
        value = self.responses[len(self.calls) - 1]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, HTTPResponse) else HTTPResponse(200, json.dumps(value).encode())


def client(key_file, *responses):
    transport = Transport(*responses)
    return AlpacaPaperClient(key_file, transport=transport, clock=lambda: NOW), transport


def test_account_assets_quote_and_empty_history_preserve_actual_fields(key_file):
    c, t = client(key_file, account(cash="100000", equity="100000"), [], [], asset(), quote())
    assert c.transport_provenance == "injected_test_transport"
    snapshot = c.account()
    assert snapshot["id"] == ACCOUNT and snapshot["cash"] == "100000"
    assert snapshot["non_marginable_buying_power"] == "100"
    assert snapshot["daily_realized_pnl"] is snapshot["accrued_fees"] is None
    assert "account_number" not in snapshot
    assert c.positions() == c.orders() == []
    pair = c.asset()
    assert pair["symbol"] == "BTC/USD" and pair["price_increment"] == "0.000000001"
    market = c.quote()
    assert market["bid"] == "99999" and market["ask_size"] == "0.002"
    assert market["timestamp"] == pytest.approx(NOW - 0.876543211)
    assert market["fee_estimate"] is None
    assert [call[0] for call in t.calls] == ["GET"] * 5
    assert t.calls[-1][1] == adapter.DATA_ORIGIN + adapter.QUOTE_PATH
    assert all(call[4] == {"timeout_seconds": 10, "max_response_bytes": 1_000_000} for call in t.calls)
    assert KEY not in repr(c) and SECRET not in repr(c)


def test_limit_buy_is_exact_correlated_and_cancel_acknowledges_only_request(key_file):
    c, t = client(key_file, order(), HTTPResponse(204, b""), order(status="canceled"))
    placed = c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert placed["status"] == "new" and not placed["terminal"]
    assert placed["asset_id"] == ASSET
    assert placed["fees"] is placed["realized_pnl"] is None
    assert json.loads(t.calls[0][2]) == {
        "symbol": "BTC/USD", "side": "buy", "type": "limit", "time_in_force": "gtc",
        "qty": "0.00001", "limit_price": "100000", "client_order_id": CLIENT}
    assert c.cancel_order(ORDER) is None
    assert t.calls[1][0:2] == ("DELETE", adapter.PAPER_ORIGIN + "/v2/orders/" + ORDER)
    assert c.get_order(CLIENT)["terminal"] is True
    assert t.calls[2][0] == "GET"


@pytest.mark.parametrize("qty,price", [("0.0001", "100000"), ("1", "100"), (True, "1"), (0.1, "1"), ("NaN", "1"), ("1", "Infinity")])
def test_local_terms_and_fee_inclusive_10_cap_refuse_before_http(key_file, qty, price):
    c, t = client(key_file)
    with pytest.raises(BrokerRejected):
        c.submit_limit_buy(CLIENT, qty, price)
    assert t.calls == []


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429])
def test_known_broker_rejection_never_retries_or_exposes_body(key_file, status):
    c, t = client(key_file, HTTPResponse(status, SECRET.encode()))
    with pytest.raises(BrokerRejected):
        c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert len(t.calls) == 1


@pytest.mark.parametrize("response", [
    HTTPResponse(302, SECRET.encode()), HTTPResponse(409, SECRET.encode()),
    HTTPResponse(500, SECRET.encode()), HTTPResponse(201, b"{}"),
    HTTPResponse(200, b"bad-json"), HTTPResponse(200, b"x" * 1_000_001),
    TimeoutError(SECRET), BrokerError(SECRET), None,
])
def test_uncertain_mutation_is_unknown_without_retry(key_file, response):
    c, t = client(key_file, response)
    with pytest.raises(UnknownSubmission) as error:
        c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert len(t.calls) == 1 and SECRET not in str(error.value)


@pytest.mark.parametrize("change", [
    {"client_order_id": str(UUID(int=7))}, {"qty": "0.00002"}, {"limit_price": "100001"},
    {"side": "sell"}, {"symbol": "ETH/USD"}, {"time_in_force": "ioc"}, {"status": "invented"},
])
def test_mismatched_success_ack_remains_unknown(key_file, change):
    c, t = client(key_file, order(**change))
    with pytest.raises(UnknownSubmission):
        c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert len(t.calls) == 1


@pytest.mark.parametrize("status,filled,average,terminal", [
    ("partially_filled", "0.000005", "99999", False),
    ("pending_cancel", "0.000005", "99999", False),
    ("canceled", "0.000005", "99999", True),
    ("expired", "0.000005", "99999", True),
    ("filled", "0.00001", "100000", True),
    ("done_for_day", "0", None, False),
    ("calculated", "0", None, False),
])
def test_pending_and_terminal_states_retain_partial_fills(key_file, status, filled, average, terminal):
    c, _ = client(key_file, order(status=status, filled_qty=filled, filled_avg_price=average))
    value = c.get_order(CLIENT)
    assert value["status"] == status and value["filled_qty"] == filled and value["terminal"] is terminal


@pytest.mark.parametrize("change", [
    {"filled_qty": "0.00002", "filled_avg_price": "100000"},
    {"status": "filled", "filled_qty": "0"}, {"filled_qty": "0.000005", "filled_avg_price": None},
    {"status": "filled", "filled_qty": "0.00001", "filled_avg_price": "100001"},
    {"status": "replaced", "replaced_by": None},
])
def test_inconsistent_fills_or_replacement_fail_closed(key_file, change):
    c, _ = client(key_file, order(**change))
    with pytest.raises(BrokerError):
        c.get_order(CLIENT)


def test_lookup_404_does_not_retry_post_and_cancel_404_is_uncertain(key_file):
    c, t = client(key_file, HTTPResponse(404, b""), HTTPResponse(404, b""))
    assert c.get_order(CLIENT) is None
    with pytest.raises(UnknownSubmission):
        c.cancel_order(ORDER)
    assert [call[0] for call in t.calls] == ["GET", "DELETE"]


def test_all_history_refuses_full_page_duplicates_and_unrelated_orders(key_file):
    for rows in ([order()] * 500, [order(), order()], [order(symbol="ETH/USD")]):
        c, _ = client(key_file, rows)
        with pytest.raises(BrokerError):
            c.orders()


@pytest.mark.parametrize("change", [{"cash": "NaN"}, {"crypto_status": None}, {"account_blocked": None}, {"id": SECRET}])
def test_bad_or_incomplete_account_contract_fails_closed(key_file, change):
    c, _ = client(key_file, account(**change))
    with pytest.raises(BrokerError) as error:
        c.account()
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("change", [{"ap": 99998}, {"as": 0}, {"bp": True}, {"ap": "100000"},
                                   {"t": "2026-09-10T15:13:49Z"}, {"t": "2026-09-10T15:13:00Z"},
                                   {"t": "2026-09-10T15:13:47"}, {"bs": float("nan")}])
def test_crossed_empty_stale_future_or_bad_quotes_fail_closed(key_file, change):
    c, _ = client(key_file, quote(**change))
    with pytest.raises(BrokerError):
        c.quote()


def test_asset_minimum_and_position_inventory_unknowns_not_fabricated(key_file):
    pair = asset()
    del pair["min_order_size"]
    c, _ = client(key_file, pair)
    with pytest.raises(BrokerError):
        c.asset()
    row = {"asset_id": ASSET, "symbol": "BTCUSD", "asset_class": "crypto", "side": "long",
           "qty": "0.00001", "market_value": "1", "cost_basis": "1", "avg_entry_price": "100000"}
    c, _ = client(key_file, [row])
    assert c.positions()[0]["qty_available"] is None
    c, _ = client(key_file, [{**row, "qty_available": "0.00002"}])
    with pytest.raises(BrokerError):
        c.positions()


@pytest.mark.parametrize("method,url,body", [
    ("POST", "https://api.alpaca.markets/v2/orders", b"{}"),
    ("GET", "http://paper-api.alpaca.markets/v2/account", b""),
    ("GET", "https://paper-api.alpaca.markets.evil.example/v2/account", b""),
    ("DELETE", "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols=BTC%2FUSD", b""),
    ("GET", "https://data.alpaca.markets/v1beta3/crypto/eu-1/latest/quotes?symbols=BTC%2FUSD", b""),
    ("DELETE", adapter.PAPER_ORIGIN + "/v2/orders/all", b""),
])
def test_transport_endpoint_allowlist_refuses_before_connection(monkeypatch, method, url, body):
    def forbidden(*args, **kwargs):
        pytest.fail("unapproved URL must not construct a connection")
    monkeypatch.setattr(adapter.http.client, "HTTPSConnection", forbidden)
    with pytest.raises(BrokerError):
        adapter._owned_http(method, url, body, {"APCA-API-SECRET-KEY": SECRET},
                            timeout_seconds=10, max_response_bytes=1_000_000)


class Response:
    def __init__(self, body=b"{}", status=200, length=None):
        self.body, self.status = body, status
        self.length = str(len(body)) if length is None else length
    def getheader(self, name):
        return self.length if name == "Content-Length" else None
    def read1(self, count):
        chunk, self.body = self.body[:count], self.body[count:]
        return chunk


def own_transport(monkeypatch, responses):
    calls, connections = [], []
    class Connection:
        sock = None
        def __init__(self, host, *, timeout, context):
            assert host in {"paper-api.alpaca.markets", "data.alpaca.markets"}
            assert timeout == 10 and context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            self.host, self.closed = host, False
            connections.append(self)
        def request(self, method, target, *, body, headers):
            calls.append((self.host, method, target, body, headers))
        def getresponse(self):
            return responses[len(calls) - 1]
        def close(self):
            self.closed = True
    monkeypatch.setattr(adapter.http.client, "HTTPSConnection", Connection)
    return calls, connections


def test_owned_http_verified_tls_fixed_get_post_delete_and_all_connections_close(key_file, monkeypatch):
    responses = [Response(json.dumps(quote()).encode()), Response(json.dumps(order()).encode()), Response(status=204)]
    calls, connections = own_transport(monkeypatch, responses)
    c = AlpacaPaperClient(key_file, clock=lambda: NOW)
    assert c.transport_provenance == "verified_tls"
    c.quote()
    c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert c.cancel_order(ORDER) is None
    assert [row[0:2] for row in calls] == [("data.alpaca.markets", "GET"), ("paper-api.alpaca.markets", "POST"),
                                         ("paper-api.alpaca.markets", "DELETE")]
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("response", [Response(status=302), Response(b"{}", length="1000001"),
                                      Response(b"{}", length="3")])
def test_owned_http_redirect_size_or_truncation_never_retries_mutation(key_file, monkeypatch, response):
    calls, connections = own_transport(monkeypatch, [deepcopy(response)])
    c = AlpacaPaperClient(key_file, clock=lambda: NOW)
    with pytest.raises(UnknownSubmission):
        c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert len(calls) == 1 and connections[0].closed


def activity(i=0, **changes):
    return {"id": f"20260910151348000::{i:04d}", "activity_type": "JNLC", "amount": "100",
            "date": "2026-09-10", "description": SECRET, **changes}


def test_activities_complete_unfiltered_pagination_hashes_freeform_without_exposing_it(key_file):
    first = [activity(i) for i in range(100)]
    c, t = client(key_file, first, [activity(100, amount="-0.01")])
    rows = c.activities()
    assert len(rows) == 101 and rows[-1]["amount"] == "-0.01"
    assert all("description" not in row for row in rows)
    assert SECRET not in json.dumps(rows)
    assert "activity_types" not in t.calls[0][1]
    assert t.calls[1][1].endswith("page_token=20260910151348000%3A%3A0099")
    changed, _ = client(key_file, [activity(description="corrected content")])
    assert rows[0]["content_sha256"] != changed.activities()[0]["content_sha256"]


def test_activity_pagination_cycles_or_full_bound_fail_closed(key_file):
    first = [activity(i) for i in range(100)]
    c, _ = client(key_file, first, [first[-1]])
    with pytest.raises(BrokerError, match="pagination_cycle"):
        c.activities()
    pages = [[activity(page * 100 + i) for i in range(100)] for page in range(20)]
    c, t = client(key_file, *pages)
    with pytest.raises(BrokerError, match="history_incomplete"):
        c.activities()
    assert len(t.calls) == 20


@pytest.mark.parametrize("changes", [{"amount": "NaN"}, {"id": "../arbitrary"}, {"activity_type": {}},
                                    {"date": "not-a-date"}, {"transaction_time": "without-timezone"}])
def test_bad_activity_fields_cannot_be_a_funding_baseline(key_file, changes):
    c, _ = client(key_file, [activity(**changes)])
    with pytest.raises(BrokerError):
        c.activities()


def test_key_permissions_and_identifier_validation_precede_http(key_file):
    key_file.chmod(0o644)
    with pytest.raises(BrokerError, match="credential"):
        AlpacaPaperClient(key_file)
    key_file.chmod(0o600)
    c, t = client(key_file)
    for fn in (c.get_order, c.cancel_order):
        with pytest.raises(BrokerError):
            fn("not-a-uuid")
    assert t.calls == []


@pytest.mark.parametrize("status,code,message,reason", [
    (422, 42210000, "invalid limit_price 290.123. sub-penny increment does not fulfill minimum pricing criteria", "price_precision"),
    (422, 42210000, "qty must satisfy quantity precision and increment", "quantity_precision"),
    (422, 42210000, "order notional must be at least 1 USD", "minimum_order"),
    (403, 40310000, "account trading is blocked by account permission", "account_permission"),
    (429, 42910000, "too many requests", "rate_limit"),
    (403, 40310000, "insufficient buying power", "other"),
])
def test_rejection_keeps_http_numeric_code_and_fixed_attributed_hint_only(key_file, status, code, message, reason):
    payload = {"code": code, "message": message, "untrusted_extra": SECRET}
    c, t = client(key_file, HTTPResponse(status, json.dumps(payload).encode()))
    with pytest.raises(AlpacaPaperRejected) as error:
        c.submit_limit_buy(CLIENT, "0.00001", "100000")
    rejected = error.value
    assert isinstance(rejected, BrokerRejected)
    assert rejected.http_status == status and rejected.provider_code == code
    assert rejected.reason_code == reason
    assert str(rejected) == "paper_broker_rejected_request"
    diagnostic = rejected.diagnostic()
    assert diagnostic["provider_message_retained"] is False
    assert diagnostic["reason_basis"] == ("unavailable" if reason == "other" else "provider_message_hint")
    exposed = json.dumps(diagnostic) + str(rejected) + repr(rejected) + repr(rejected.__dict__)
    assert SECRET not in exposed and message not in exposed
    assert len(t.calls) == 1


@pytest.mark.parametrize("code", [True, "42210000", 40310000, 422100000000, {"secret": SECRET}, None])
def test_rejection_code_requires_bounded_numeric_matching_http_family(key_file, code):
    c, _ = client(key_file, HTTPResponse(422, json.dumps({"code": code, "message": SECRET}).encode()))
    with pytest.raises(AlpacaPaperRejected) as error:
        c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert error.value.http_status == 422 and error.value.provider_code is None
    assert error.value.reason_code == "other"
    assert SECRET not in json.dumps(error.value.diagnostic())


@pytest.mark.parametrize("body", [b"", b"not-json", b'{"code":NaN}', b'{"code":42210000,"code":42210001}', b"x" * 4097])
def test_bad_rejection_body_does_not_erase_known_http_status(key_file, body):
    c, t = client(key_file, HTTPResponse(422, body))
    with pytest.raises(AlpacaPaperRejected) as error:
        c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert error.value.diagnostic() == {
        "http_status": 422, "provider_code": None, "reason_code": "other",
        "reason_basis": "unavailable", "provider_message_retained": False}
    assert len(t.calls) == 1


def test_owned_http_preserves_numeric_rejection_diagnostics_without_retry(key_file, monkeypatch):
    message = "invalid limit_price 290.123. sub-penny increment does not fulfill minimum pricing criteria"
    response = Response(json.dumps({"code": 42210000, "message": message}).encode(), status=422)
    calls, connections = own_transport(monkeypatch, [response])
    c = AlpacaPaperClient(key_file, clock=lambda: NOW)
    with pytest.raises(AlpacaPaperRejected) as error:
        c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert error.value.provider_code == 42210000 and error.value.reason_code == "price_precision"
    assert len(calls) == 1 and connections[0].closed


@pytest.mark.parametrize("failure", ["timeout", "oversized", "length_mismatch"])
def test_owned_http_body_failure_keeps_observed_rejection_status(key_file, monkeypatch, failure):
    class FailingResponse(Response):
        def read1(self, count):
            if failure == "timeout":
                raise TimeoutError(SECRET)
            return super().read1(count)
    response = FailingResponse(b"{}", status=403, length="4097" if failure == "oversized" else "3")
    calls, connections = own_transport(monkeypatch, [response])
    c = AlpacaPaperClient(key_file, clock=lambda: NOW)
    with pytest.raises(AlpacaPaperRejected) as error:
        c.submit_limit_buy(CLIENT, "0.00001", "100000")
    assert error.value.http_status == 403 and error.value.provider_code is None
    assert error.value.reason_code == "other"
    assert SECRET not in repr(error.value.__dict__)
    assert len(calls) == 1 and connections[0].closed
