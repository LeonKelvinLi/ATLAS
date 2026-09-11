"""Offline paper-equity mapping and refusal/reconciliation fixtures only."""

from copy import deepcopy
from datetime import datetime, timezone
import socket

import pytest

from atlas.research.equity_paper import (
    DATA_ORIGIN, PAPER_ORIGIN, EquityPaperAdapter, EquityPaperError,
    FixtureResponse, FixtureTransport, account_fingerprint, intent_hash, normalize_intent,
)


NOW = 1_800_000_000
ACCOUNT = "d6fd8cd8-05d6-484e-b4e7-c3eaf5389ad4"
CLIENT = "4d8a9443-0959-4e67-9bf8-b610f4c411ef"
RESERVATION = "cbdf1f77-1649-40f4-a544-584dad958fbc"
ORDER = "a4f1c938-ffbc-47ae-819f-709f18a3c53c"
FINGERPRINT = account_fingerprint(ACCOUNT)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Paper-equity fixtures cannot contact a network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def utc(when):
    return datetime.fromtimestamp(when, timezone.utc).isoformat().replace("+00:00", "Z")


def intent(**changes):
    result = {"schema": 1, "client_order_id": CLIENT, "account_fingerprint": FINGERPRINT,
              "symbol": "SPY", "side": "buy", "quantity": ".01", "limit_price": "500.00",
              "time_in_force": "day", "extended_hours": False, "reservation_usd": "5.10",
              "fee_buffer_usd": ".10", "risk_reservation_id": RESERVATION,
              "reviewed_at": NOW - 5, "expires_at": NOW + 60, **changes}
    result["intent_sha256"] = intent_hash(result)
    return result


def reservation(terms):
    return {"reservation_id": terms["risk_reservation_id"], "client_order_id": terms["client_order_id"],
            "intent_sha256": terms["intent_sha256"], "reserved_cash_usd": terms["reservation_usd"], "state": "committed"}


def preflight_responses():
    return [FixtureResponse(200, {"id": ACCOUNT, "status": "ACTIVE", "account_blocked": False,
                                 "trading_blocked": False, "trade_suspended_by_user": False,
                                 "multiplier": "1", "short_market_value": "0", "cash": "100",
                                 "equity": "100", "non_marginable_buying_power": "100"}),
            FixtureResponse(200, []), FixtureResponse(200, []),
            FixtureResponse(200, {"is_open": True, "timestamp": utc(NOW), "next_close": utc(NOW + 3600)}),
            FixtureResponse(200, {"symbol": "SPY", "class": "us_equity", "status": "active",
                                 "tradable": True, "fractionable": True}),
            FixtureResponse(200, {"symbol": "SPY", "quote": {"bp": 499.99, "ap": 500.01,
                                                             "bs": 100, "as": 200, "t": utc(NOW)}})]


def order(**changes):
    return {"id": ORDER, "client_order_id": CLIENT, "symbol": "SPY", "side": "buy",
            "type": "limit", "time_in_force": "day", "extended_hours": False,
            "qty": ".01", "limit_price": "500.00", "filled_qty": "0",
            "filled_avg_price": None, "status": "new", **changes}


def adapter(responses, *, attempts=None, clock=lambda: NOW):
    transport = FixtureTransport(responses)
    instance = EquityPaperAdapter(transport, expected_account_fingerprint=FINGERPRINT,
                                  attempts={} if attempts is None else attempts, clock=clock)
    return instance, transport


def test_no_network_transport_or_credential_interface_exists():
    with pytest.raises(EquityPaperError, match="offline_fixtures_only"):
        EquityPaperAdapter(lambda *_: None, expected_account_fingerprint=FINGERPRINT, attempts={})
    with pytest.raises(EquityPaperError):
        FixtureTransport([lambda: None])
    assert not hasattr(EquityPaperAdapter, "live")


def test_preflight_maps_exact_paper_paths_and_never_claims_real_broker_connectivity():
    instance, transport = adapter(preflight_responses())
    result = instance.preflight(intent())
    assert [call["url"] for call in transport.calls] == [
        PAPER_ORIGIN + "/v2/account", PAPER_ORIGIN + "/v2/positions",
        PAPER_ORIGIN + "/v2/orders?status=open&limit=500&direction=asc&nested=false",
        PAPER_ORIGIN + "/v2/clock", PAPER_ORIGIN + "/v2/assets/SPY",
        DATA_ORIGIN + "/v2/stocks/SPY/quotes/latest?feed=iex",
    ]
    assert all(call["method"] == "GET" for call in transport.calls)
    assert result["payload"]["qty"] == "0.01"
    assert result["broker_activation_blocked"]
    assert result["actual_orders_submitted"] == 0
    assert not result["live_approved"]


@pytest.mark.parametrize("changes", [
    {"symbol": "../orders"}, {"symbol": "BTC/USD"}, {"symbol": "spy"},
    {"side": "short"}, {"time_in_force": "gtc"}, {"extended_hours": True},
    {"quantity": "NaN"}, {"quantity": "0.0000000001"}, {"limit_price": "500.001"},
    {"quantity": "1"}, {"reservation_usd": "5"}, {"fee_buffer_usd": "101"},
    {"expires_at": NOW + 500}, {"reviewed_at": True}, {"client_order_id": "unsafe/path"},
])
def test_reviewed_intent_rejects_bad_contracts_and_never_sizes_a_replacement(changes):
    with pytest.raises(ValueError):
        normalize_intent(intent(**changes))


def test_modified_review_hash_is_rejected_before_any_fixture_request():
    terms = intent()
    terms["quantity"] = ".02"
    instance, transport = adapter([])
    with pytest.raises(EquityPaperError, match="hash"):
        instance.submit(terms, reservation=reservation(terms))
    assert transport.calls == []


@pytest.mark.parametrize("index,changes", [
    (0, {"id": "ad993278-1fd7-4ce3-9f83-edb0f8c1b86c"}),
    (0, {"account_blocked": True}), (0, {"multiplier": "2"}),
    (0, {"cash": "1000"}), (0, {"non_marginable_buying_power": "200"}),
    (0, {"short_market_value": "1"}), (3, {"is_open": False}),
    (3, {"timestamp": utc(NOW - 11)}), (3, {"next_close": utc(NOW)}),
    (4, {"fractionable": False}), (4, {"class": "crypto"}), (4, {"tradable": False}),
    (5, {"symbol": "QQQ"}),
])
def test_preflight_restrictions_prevent_any_post(index, changes):
    responses = preflight_responses()
    responses[index] = FixtureResponse(200, {**responses[index].body, **changes})
    instance, transport = adapter(responses)
    terms = intent()
    with pytest.raises(EquityPaperError):
        instance.submit(terms, reservation=reservation(terms))
    assert not any(c["method"] == "POST" for c in transport.calls)
    assert instance.attempts == {}


@pytest.mark.parametrize("row", [
    {"bp": True}, {"bp": 500.02}, {"as": 0}, {"t": utc(NOW - 11)},
    {"t": utc(NOW + 1)}, {"t": "2027-01-15T12:00:00-05:00"},
])
def test_unavailable_stale_or_wrong_timezone_quotes_refuse(row):
    responses = preflight_responses()
    body = deepcopy(responses[-1].body)
    body["quote"].update(row)
    responses[-1] = FixtureResponse(200, body)
    instance, transport = adapter(responses)
    with pytest.raises(EquityPaperError):
        instance.preflight(intent())
    assert all(c["method"] == "GET" for c in transport.calls)


def test_owned_inventory_is_required_for_sells_and_pending_orders_block():
    terms = intent(side="sell", reservation_usd="0")
    instance, _ = adapter(preflight_responses())
    with pytest.raises(EquityPaperError, match="short_sale"):
        instance.preflight(terms)
    responses = preflight_responses()
    responses[1] = FixtureResponse(200, [{"asset_class": "us_equity", "side": "long", "symbol": "SPY", "qty": ".02"}])
    instance, _ = adapter(responses)
    assert instance.preflight(terms)["owned_quantity"] == "0.02"
    responses = preflight_responses()
    responses[2] = FixtureResponse(200, [order()])
    instance, _ = adapter(responses)
    with pytest.raises(EquityPaperError, match="orders_unresolved"):
        instance.preflight(intent())


def test_submitting_requires_caller_committed_exact_cash_reservation():
    instance, transport = adapter([])
    terms = intent()
    receipt = reservation(terms)
    receipt["reserved_cash_usd"] = "1"
    with pytest.raises(EquityPaperError, match="committed_reservation"):
        instance.submit(terms, reservation=receipt)
    assert transport.calls == []


def test_fake_attempt_record_exists_before_post_and_exact_repeat_never_posts_again(monkeypatch):
    attempts = {}
    instance, transport = adapter(preflight_responses() + [FixtureResponse(200, order())], attempts=attempts)
    original = transport.request
    def inspect_before_request(method, url, payload=None):
        if method == "POST":
            assert attempts[CLIENT]["state"] == "unknown"
            assert attempts[CLIENT]["post_attempts"] == 1
        return original(method, url, payload)
    monkeypatch.setattr(transport, "request", inspect_before_request)
    terms = intent()
    result = instance.submit(terms, reservation=reservation(terms))
    assert result["state"] == "acknowledged"
    second, fresh_transport = adapter([], attempts=attempts)
    repeated = second.submit(terms, reservation=reservation(terms))
    assert not repeated["resubmitted"] and fresh_transport.calls == []
    assert sum(c["method"] == "POST" for c in transport.calls) == 1


def test_timeout_and_missing_lookup_keep_unknown_without_retry_or_releasing_reservation():
    instance, transport = adapter(preflight_responses() + [TimeoutError("private text"), FixtureResponse(404)])
    terms = intent()
    first = instance.submit(terms, reservation=reservation(terms))
    assert first["state"] == "unknown" and "private text" not in str(first)
    second = instance.reconcile(terms)
    assert second["state"] == "unknown"
    assert second["lookup_missing_is_not_proof_of_no_submission"]
    instance.submit(terms, reservation=reservation(terms))
    assert sum(c["method"] == "POST" for c in transport.calls) == 1


def test_known_http_refusal_is_retained_without_provider_text_or_retry():
    instance, transport = adapter(preflight_responses() + [FixtureResponse(403, {"message": "PRIVATE"})])
    terms = intent()
    result = instance.submit(terms, reservation=reservation(terms))
    assert result["http_status"] == 403 and result["state"] == "http_rejected"
    assert "PRIVATE" not in str(result)
    instance.submit(terms, reservation=reservation(terms))
    assert sum(c["method"] == "POST" for c in transport.calls) == 1


def test_partial_cancel_retains_actual_fixture_fill_and_terminal_state_is_immutable():
    partial = order(status="partially_filled", filled_qty=".004", filled_avg_price="499.50")
    canceled = order(status="canceled", filled_qty=".004", filled_avg_price="499.50")
    changed = order(status="canceled", filled_qty=".004", filled_avg_price="499.25")
    instance, _ = adapter(preflight_responses() + [FixtureResponse(200, partial), FixtureResponse(200, canceled), FixtureResponse(200, changed)])
    terms = intent()
    instance.submit(terms, reservation=reservation(terms))
    terminal = instance.reconcile(terms)
    assert terminal["terminal"] and terminal["filled_qty"] == "0.004"
    after = instance.reconcile(terms)
    assert after["diagnostic"] == "reconciliation_unavailable"
    assert after["filled_avg_price"] == terminal["filled_avg_price"]


@pytest.mark.parametrize("changes", [
    {"client_order_id": RESERVATION}, {"symbol": "QQQ"}, {"qty": ".02"},
    {"filled_qty": ".02", "filled_avg_price": "500"},
    {"status": "filled", "filled_qty": ".001", "filled_avg_price": "500"},
    {"filled_qty": ".01", "filled_avg_price": "501"},
])
def test_mismatched_or_impossible_order_ack_is_unknown_not_success(changes):
    instance, transport = adapter(preflight_responses() + [FixtureResponse(200, order(**changes))])
    terms = intent()
    result = instance.submit(terms, reservation=reservation(terms))
    assert result["state"] == "unknown" and result["broker_order_id"] is None
    assert len([c for c in transport.calls if c["method"] == "POST"]) == 1


def test_expired_review_can_reconcile_but_never_submit():
    terms = intent()
    instance, transport = adapter(preflight_responses() + [FixtureResponse(200, order()), FixtureResponse(200, order())])
    instance.submit(terms, reservation=reservation(terms))
    instance.clock = lambda: NOW + 300
    with pytest.raises(EquityPaperError, match="expired"):
        instance.submit(terms, reservation=reservation(terms))
    assert instance.reconcile(terms)["state"] == "acknowledged"
    assert sum(c["method"] == "POST" for c in transport.calls) == 1
