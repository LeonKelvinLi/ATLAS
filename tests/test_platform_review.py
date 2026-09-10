"""Independent regression review using only existing fake broker fixtures."""

from dataclasses import replace
from decimal import localcontext

import pytest

from atlas.domain import AccountSnapshot, BrokerError, OrderResult, Position, Quote, decimal, json_value
from atlas.ledger import ConflictError
from atlas.risk import RiskPolicy, evaluate_risk
from atlas.service import ServiceError
from tests.test_platform_execution import (
    D, SYMBOL, approve, deny_network, execute, make_service, open_cancellable, prepare,
)


@pytest.mark.parametrize("change", ["remove", "provenance"])
def test_revoked_dataset_cannot_execute_cached_approved_signal(make_service, change):
    h = make_service(mode="live-assisted")
    proposal = prepare(h)
    receipt = approve(h, proposal)
    if change == "remove":
        del h.service.datasets[SYMBOL]
    else:
        h.service.datasets[SYMBOL]["evidence_kind"] = "synthetic"
    try:
        execute(h, proposal, receipt)
    except ServiceError:
        pass
    assert h.broker.submit_calls == []


def test_cancellation_wrong_broker_id_cannot_resolve_the_approved_target(make_service):
    h = make_service()
    target, cancel = open_cancellable(h)
    h.broker.cancel_behavior = lambda original: replace(original, status="cancelled", broker_order_id="unrelated-order")
    result = h.service.cancel_order(cancel["id"])
    assert result["cancellation"]["status"] == "unknown"
    assert h.ledger.get_intent(target["id"])["status"] == "accepted"


def test_terminal_history_cannot_erase_a_previously_acknowledged_partial_fill(make_service):
    h = make_service()
    proposal = prepare(h)
    h.broker.submit_behavior = lambda order: OrderResult("broker-" + order.client_order_id,
        order.client_order_id, "partially_filled", D("0.5"), order.limit_price, D("0"), D("0"))
    partial = execute(h, proposal)["intent"]
    assert partial["status"] == "accepted"
    original = h.broker.orders[partial["client_order_id"]]
    h.broker.orders[partial["client_order_id"]] = replace(original, status="cancelled", filled_quantity=D("0"))
    try:
        h.service.reconcile(proposal["id"])
    except BrokerError:
        pass
    assert h.ledger.get_intent(proposal["id"])["status"] in {"accepted", "unknown"}


def test_cancelled_partially_filled_sell_remains_in_realized_performance(make_service):
    h = make_service()
    h.registry.direction = "sell"
    h.broker.account = replace(h.broker.account,
        positions=(Position(SYMBOL, D("10"), D("1000"), D("1500")),))
    target = prepare(h)
    h.broker.submit_behavior = lambda order: OrderResult("broker-" + order.client_order_id,
        order.client_order_id, "partially_filled", D("0.5"), order.limit_price, D("0"), D("-25"))
    assert execute(h, target)["intent"]["status"] == "accepted"
    cancel = h.service.create_cancellation_proposal(target["id"], "cancel-partial")["proposal"]
    result = h.service.cancel_order(cancel["id"])
    assert result["target"]["status"] == "cancelled"
    report = h.service.performance()
    assert report["closed_sell_executions"] == 1
    assert report["net_pnl"] == -25


@pytest.mark.parametrize("path", ["reconcile", "cancel"])
def test_concurrent_fill_update_cannot_be_erased_between_broker_read_and_commit(make_service, monkeypatch, path):
    h = make_service()
    target = prepare(h)
    h.broker.submit_behavior = lambda order: OrderResult("broker-" + order.client_order_id,
        order.client_order_id, "partially_filled", D("0.5"), order.limit_price, D("0"), D("0"))
    partial = execute(h, target)["intent"]
    original = h.broker.orders[partial["client_order_id"]]
    newer = replace(original, filled_quantity=D("0.8"))
    complete = h.ledger.complete_intent
    injected = False

    def concurrent_update():
        nonlocal injected
        if not injected:
            injected = True
            complete(target["id"], "accepted", newer.broker_order_id, json_value(newer), now=int(h.clock()))

    if path == "reconcile":
        h.broker.orders[partial["client_order_id"]] = replace(original, status="cancelled")
        def interleaved(*args, **kwargs):
            concurrent_update()
            return complete(*args, **kwargs)
        monkeypatch.setattr(h.ledger, "complete_intent", interleaved)
        operation = lambda: h.service.reconcile(target["id"])
    else:
        cancel = h.service.create_cancellation_proposal(target["id"], "race-cancel")["proposal"]
        complete_cancel = h.ledger.complete_cancellation
        def interleaved_cancel(*args, **kwargs):
            concurrent_update()
            return complete_cancel(*args, **kwargs)
        monkeypatch.setattr(h.ledger, "complete_cancellation", interleaved_cancel)
        operation = lambda: h.service.cancel_order(cancel["id"])
    try:
        operation()
    except (BrokerError, ConflictError):
        pass
    latest = h.ledger.get_intent(target["id"])
    assert latest["status"] in {"accepted", "unknown"}
    assert D(latest["details"]["filled_quantity"]) == D("0.8")


def test_ambient_decimal_precision_cannot_round_an_order_below_its_risk_cap():
    policy = RiskPolicy(mode="paper", expected_account_id="fixture", allowed_symbols={"XYZ"})
    account = AccountSnapshot("fixture", "paper", "USD", D("10000"), D("10000"), D("10000"), (), 100, D("0"))
    quote = Quote("XYZ", "equity", D("10.01"), D("10.01"), 100, True, D("1000"), "fixture",
                  quantity_increment=D("1"), minimum_quantity=D("1"))
    args = dict(symbol="XYZ", asset_class="equity", side="buy", quantity="10", limit_price="10.01",
                strategy={"strategy_id": "momentum", "symbol": "XYZ", "asset_class": "equity",
                          "direction": "buy", "confidence": None, "live_validated": False, "data_timestamp": 100},
                now=100, daily_stats={"intents_count": 0}, pending_intents=False, broker_open_orders=0, kill_switch=False)
    assert evaluate_risk(policy, account, quote, **args)["passed"] is False
    with localcontext() as context:
        context.prec = 2
        assert evaluate_risk(policy, account, quote, **args)["passed"] is False


@pytest.mark.parametrize("currency", [None, "", "usd", "US", "USDD", "123", "ＵＳＤ"])
def test_matching_missing_or_malformed_currencies_never_pass_risk(make_service, currency):
    h = make_service()
    h.broker.account = replace(h.broker.account, currency=currency)
    h.broker.quote = replace(h.broker.quote, currency=currency)
    assert prepare(h)["risk"]["passed"] is False


@pytest.mark.parametrize("value", [None, True, -1, 0, "300", 1.5, float("inf")])
def test_malformed_signal_age_limit_fails_pure_risk_without_type_errors(make_service, value):
    h = make_service()
    signal = h.service.analyze(SYMBOL)["signals"][0]
    signal["max_age_seconds"] = value
    result = evaluate_risk(h.policy, h.broker.account, h.broker.quote,
        symbol=SYMBOL, asset_class="equity", side="buy", quantity="1", limit_price="100",
        strategy=signal, now=int(h.clock()), daily_stats={"intents_count": 0},
        pending_intents=False, broker_open_orders=0, kill_switch=False)
    assert not result["passed"]


@pytest.mark.parametrize("value", ["1e37", "1e-37", "9" * 65, "1." + "1" * 37])
def test_financial_input_precision_and_magnitude_bounds_are_explicit(value):
    with pytest.raises(ValueError):
        decimal(value)


def test_performance_distinguishes_opening_fees_closed_sell_fees_and_unavailable_metrics(make_service):
    h = make_service(paper=True, paper_fee_bps="100")
    buy = prepare(h)
    execute(h, buy)
    h.broker.quotes[SYMBOL] = replace(h.broker.quotes[SYMBOL], bid=D("105"), ask=D("105.01"))
    h.registry.direction = "sell"
    sell = prepare(h, "sell-with-fee")
    execute(h, sell)
    rejected = prepare(h, "risk-rejected", quantity="100000")
    assert rejected["risk"]["passed"] is False
    report = h.service.performance()
    assert report["fees"] == "2.05"
    assert report["closed_sell_execution_fees"] == pytest.approx(1.05)
    assert report["execution_fees"]["unknown_count"] == 0
    assert report["net_pnl"] == pytest.approx(2.95)
    assert report["total_return"] is None and report["turnover"] is None
    assert report["per_regime"] is None and report["probability_buckets"] is None
    assert report["dates_are_fill_dates"] is False
    assert report["risk_rejected_proposals_in_window"] == 1
