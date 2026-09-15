"""Deterministic exchange lifecycle through the actual PAPER API contract."""

from copy import deepcopy
from decimal import Decimal
import json
import socket

import pytest

from atlas.paper.broker import AlpacaPaperAPI, PaperBrokerError
from atlas.paper.simulation import SyntheticAlpaca


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline exchange must never access network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def terms(cid="synthetic-buy-1", **changes):
    return {"symbol": "SPY", "side": "buy", "qty": "1", "limit_price": "10", "type": "limit",
            "time_in_force": "day", "extended_hours": False, "client_order_id": cid, **changes}


def api_for(sim):
    api = AlpacaPaperAPI(transport=sim.request, clock=sim.clock)
    seen = set()
    def gate(method, path, payload, permit):
        if not isinstance(permit, str) or permit in seen:
            return False
        seen.add(permit)
        return True
    api.bind_gate(gate)
    return api


def test_synthetic_funding_quotes_market_and_no_automatic_fill():
    sim = SyntheticAlpaca()
    api = api_for(sim)
    assert sim.provenance == api.provenance == "TEST"
    assert api.account()["cash"] == api.account()["equity"] == "100"
    quote = api.quote("SPY")
    assert quote["bid"] == "9.99" and quote["ask"] == "10"
    assert quote["size_basis"] == "conservative_share_lower_bound"
    assert api.clock_status()["is_open"]
    assert api.asset("SPY")["fractionable"]
    order = api.submit_order(terms(), permit="submission")
    assert order["status"] == "new" and order["filled_qty"] == "0"
    assert api.account()["cash"] == "100" and api.account()["buying_power"] == "90"
    sim.advance(5)
    assert api.order_by_client_id(order["client_order_id"])["status"] == "new"
    assert api.positions() == api.activities() == []
    assert len(api.orders()) == 1


def test_partial_fill_cancel_race_preserves_cash_inventory_and_fill_identifiers():
    sim = SyntheticAlpaca()
    api = api_for(sim)
    order = api.submit_order(terms(), permit="submit")
    sim.advance(1)
    sim.fill(order["client_order_id"], "0.3", "9.98")
    partial = api.order_by_client_id(order["client_order_id"])
    assert partial["status"] == "partially_filled" and partial["filled_qty"] == "0.3"
    sim.advance(1)
    assert api.cancel_order(order["id"], permit="cancel") is None
    assert api.order_by_client_id(order["client_order_id"])["status"] == "pending_cancel"
    sim.advance(1)
    sim.fill(order["client_order_id"], "0.2", "9.99")
    sim.finish_cancel(order["client_order_id"])
    final = api.order_by_client_id(order["client_order_id"])
    assert final["status"] == "canceled" and final["filled_qty"] == "0.5"
    assert final["filled_avg_price"] == "9.984"
    assert api.account()["cash"] == "95.008"
    assert api.account()["buying_power"] == "95.008"
    assert api.positions()[0]["qty"] == "0.5"
    rows = api.activities()
    assert [row["qty"] for row in rows] == ["0.3", "0.2"]
    assert len({row["id"] for row in rows}) == 2
    assert all(row["order_id"] == order["id"] and row["fee"] == "0" for row in rows)
    assert api.orders() == [] and len(api.orders(status="closed")) == 1


def test_partial_average_and_cost_basis_remain_parseable_for_repeating_decimals():
    sim = SyntheticAlpaca()
    api = api_for(sim)
    order = api.submit_order(terms(qty="0.7"), permit="buy")
    sim.fill(order["client_order_id"], "0.3", "9.98")
    sim.fill(order["client_order_id"], "0.4", "9.99")
    result = api.order_by_client_id(order["client_order_id"])
    assert result["status"] == "filled"
    assert abs(Decimal(result["filled_avg_price"]) - Decimal("9.985714285714285714285714285714285714")) < Decimal("1e-35")
    sell = api.submit_order(terms("synthetic-sell", side="sell", qty="0.2", limit_price="9.99"), permit="sell")
    sim.fill(sell["client_order_id"], "0.2", "9.99")
    assert api.positions()[0]["qty"] == "0.5"
    assert Decimal(api.positions()[0]["cost_basis"]) > 0


def test_reduce_only_sell_and_flat_state_without_capital_reset():
    sim = SyntheticAlpaca()
    api = api_for(sim)
    buy = api.submit_order(terms(), permit="buy")
    sim.fill(buy["client_order_id"], "1", "10")
    sell = api.submit_order(terms("sell", side="sell", limit_price="9.99"), permit="sell")
    sim.fill(sell["client_order_id"], "1", "9.99")
    assert api.positions() == []
    assert api.account()["cash"] == "99.99"
    restored = SyntheticAlpaca.from_state(sim.export_state())
    assert api_for(restored).account()["cash"] == "99.99"
    assert len(api_for(restored).orders(status="all")) == 2


def test_duplicate_client_id_never_recreates_order():
    sim = SyntheticAlpaca()
    api = api_for(sim)
    first = api.submit_order(terms(), permit="first")
    with pytest.raises(PaperBrokerError) as caught:
        api.submit_order(terms(qty="2"), permit="second")
    assert caught.value.http_status == 422 and caught.value.ambiguous
    assert len(sim.orders) == 1
    assert api.order_by_client_id(first["client_order_id"])["qty"] == "1"


@pytest.mark.parametrize("kind,accepted", [("timeout_before", False), ("timeout_after_accept", True)])
def test_lost_submission_response_reconstructs_same_broker_state(kind, accepted):
    sim = SyntheticAlpaca()
    api = api_for(sim)
    sim.queue_fault("POST", "/v2/orders", kind)
    with pytest.raises(PaperBrokerError) as caught:
        api.submit_order(terms(), permit="submit")
    assert caught.value.ambiguous
    frozen = json.loads(json.dumps(sim.export_state()))
    restored = SyntheticAlpaca.from_state(frozen)
    second_api = api_for(restored)
    found = second_api.order_by_client_id(terms()["client_order_id"])
    assert (found is not None) is accepted
    assert len([call for call in restored.calls if call["method"] == "POST"]) == 1
    assert len(restored.orders) == int(accepted)


def test_lost_cancel_ack_requires_reconciliation_and_can_race_final_fill():
    sim = SyntheticAlpaca()
    api = api_for(sim)
    order = api.submit_order(terms(), permit="submit")
    sim.queue_fault("DELETE", "/v2/orders", "timeout_after_accept")
    with pytest.raises(PaperBrokerError) as caught:
        api.cancel_order(order["id"], permit="cancel")
    assert caught.value.ambiguous
    assert api.order_by_client_id(order["client_order_id"])["status"] == "pending_cancel"
    sim.fill(order["client_order_id"], "1", "10")
    with pytest.raises(ValueError, match="synthetic_cancellation_not_pending"):
        sim.finish_cancel(order["client_order_id"])
    assert api.order_by_client_id(order["client_order_id"])["status"] == "filled"


@pytest.mark.parametrize("kind,status", [("rate_limit", 429), ("server_error", 503), ("malformed", 200)])
def test_scripted_read_faults_are_bounded_and_recover_explicitly(kind, status):
    sim = SyntheticAlpaca()
    api = api_for(sim)
    sim.queue_fault("GET", "/v2/account", kind)
    with pytest.raises(PaperBrokerError) as caught:
        api.account()
    assert caught.value.http_status == status and not caught.value.ambiguous
    assert len(sim.calls) == 1
    sim.advance(2)
    assert api.account()["cash"] == "100"
    assert len(sim.calls) == 2


def test_partial_expiry_and_fill_terms_are_not_fabricated():
    sim = SyntheticAlpaca()
    api = api_for(sim)
    order = api.submit_order(terms(), permit="submit")
    before = sim.export_state()
    for qty, price in (("2", "10"), ("1", "10.01"), ("0", "10")):
        with pytest.raises(ValueError):
            sim.fill(order["client_order_id"], qty, price)
    assert sim.export_state() == before
    sim.fill(order["client_order_id"], "0.4", "10")
    sim.expire(order["client_order_id"])
    final = api.order_by_client_id(order["client_order_id"])
    assert final["status"] == "expired" and final["filled_qty"] == "0.4"
    with pytest.raises(ValueError, match="synthetic_order_not_fillable"):
        sim.fill(order["client_order_id"], "0.1", "10")


def test_synthetic_broker_rejects_short_sale_overbudget_and_closed_session():
    sim = SyntheticAlpaca()
    api = api_for(sim)
    with pytest.raises(PaperBrokerError) as caught:
        api.submit_order(terms(side="sell"), permit="no-inventory")
    assert caught.value.http_status == 403
    api.submit_order(terms("reserve", qty="10"), permit="reserve")
    with pytest.raises(PaperBrokerError) as caught:
        api.submit_order(terms("overbudget"), permit="overbudget")
    assert caught.value.http_status == 403
    sim.is_open = False
    assert not api.clock_status()["is_open"]
    with pytest.raises(PaperBrokerError) as caught:
        api.submit_order(terms("closed"), permit="closed")
    assert caught.value.http_status == 403 and len(sim.orders) == 1


def test_activity_pagination_preserves_every_execution_without_duplicate_ids():
    sim = SyntheticAlpaca()
    api = api_for(sim)
    order = api.submit_order(terms(qty="2"), permit="buy")
    for _ in range(105):
        sim.fill(order["client_order_id"], "0.01", "10")
    rows = api.activities()
    assert len(rows) == 105 and len({row["id"] for row in rows}) == 105
    assert sum(Decimal(row["qty"]) for row in rows) == Decimal("1.05")


def test_fixture_export_is_detached_rejects_non_test_and_never_captures_headers():
    sim = SyntheticAlpaca()
    sim.request("GET", "https://paper-api.alpaca.markets/v2/account", {"APCA-API-KEY-ID": "DO_NOT_RETAIN"}, None)
    exported = sim.export_state()
    assert "DO_NOT_RETAIN" not in json.dumps(exported)
    cloned = SyntheticAlpaca.from_state(exported)
    exported["cash"] = "999999"
    assert cloned.cash == sim.cash == "100"
    for field, invalid in (("provenance", "PAPER_API"), ("initial_cash", "100000"), ("is_open", 1)):
        changed = deepcopy(sim.export_state())
        changed[field] = invalid
        with pytest.raises(ValueError, match="synthetic_state_contract_invalid"):
            SyntheticAlpaca.from_state(changed)


def test_fixture_clock_never_rewinds():
    sim = SyntheticAlpaca()
    for invalid in (-1, float("nan"), True):
        with pytest.raises(ValueError):
            sim.advance(invalid)
