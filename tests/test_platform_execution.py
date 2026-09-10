"""Gateway integration tests with synthetic data, paper fills and fake brokers.

The live fixtures deliberately supply a test-only validated registry and full
synthetic risk snapshots. They do not promote a production strategy or connect
to a live adapter. Mutating workers are always released and joined by events.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import socket
import threading
from types import SimpleNamespace
import uuid

import pytest

from atlas.approval import ApprovalError, issue_approval
from atlas.brokers.paper import PaperBroker
from atlas.domain import AccountSnapshot, BrokerCapabilities, BrokerError, BrokerRejected, OrderResult, Position, Quote
from atlas.ledger import ConflictError, Ledger, digest
from atlas.risk import RiskPolicy, account_fingerprint
from atlas.service import AtlasService, ServiceError, StopRequested

NOW = 1788969600
ACCOUNT = "synthetic-account-for-execution-tests"
KEY = b"fixture-only-human-approval-key-32bytes"
SYMBOL = "TEST"
STRATEGY = "fixture_strategy"
D = Decimal


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FixtureRegistry:
    """A test double that never loads a model or claims measured validation."""
    def __init__(self, *, live=False):
        self.live = live
        self.enabled = True
        self.direction = "buy"

    def list_strategies(self):
        return [{"strategy_id": STRATEGY, "enabled": self.enabled, "version": "test-only"}]

    def analyze(self, symbol, asset_class, bars, *, now):
        data_hash = digest({"bars": bars})
        signal = {
            "strategy_id": STRATEGY, "version": "test-only", "symbol": symbol,
            "asset_class": asset_class, "direction": self.direction,
            "confidence": "0.75" if self.live else None,
            "probability": None, "expected_return": None,
            "live_validated": self.live, "data_timestamp": bars[-1]["timestamp"],
            "max_age_seconds": 300, "data_hash": data_hash,
            "evidence": "synthetic test contract, no production model",
        }
        signal["signal_id"] = digest(signal)
        signal["computed_at"] = now
        return {"symbol": symbol, "signals": [signal] if self.enabled else [],
                "live_validated": self.live, "fusion": None}


class FakeBroker:
    """In-memory account with injectable responses; no transport exists."""
    def __init__(self, clock, *, live=False):
        self.clock = clock
        self.capabilities = BrokerCapabilities(
            "synthetic-broker", ("equity",), ("real" if live else "paper",),
            server_idempotency=False, network_enabled=False,
        )
        self.account = AccountSnapshot(ACCOUNT, "real" if live else "paper", "USD",
                                       D("10000"), D("10000"), D("10000"), (), NOW, D("0"))
        self.quote = Quote(SYMBOL, "equity", D("99.99"), D("100"), NOW, True,
                           D("1000"), "synthetic-test-provider", quantity_increment=D("1"),
                           minimum_quantity=D("1"))
        self.submit_calls = []
        self.cancel_calls = []
        self.history_calls = []
        self.orders = {}
        self.submit_behavior = None
        self.cancel_behavior = None
        self.account_behavior = None
        self.quote_behavior = None
        self.before_submit = None
        self.before_cancel = None
        self.history_missing = False

    def get_account(self):
        return self.account_behavior() if self.account_behavior else self.account

    def get_quote(self, *args, **kwargs):
        return self.quote_behavior() if self.quote_behavior else self.quote

    def get_open_orders(self):
        return [value for value in self.orders.values() if isinstance(value, OrderResult)
                and value.status in {"accepted", "partially_filled"}]

    def default_fill(self, order):
        return OrderResult("broker-" + order.client_order_id, order.client_order_id, "filled",
                           order.quantity, order.limit_price, D("0"), D("0"))

    def submit_order(self, order):
        self.submit_calls.append(order)
        if self.before_submit:
            self.before_submit(order)
        result = self.submit_behavior(order) if self.submit_behavior else self.default_fill(order)
        if isinstance(result, OrderResult):
            self.orders[order.client_order_id] = result
        return result

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        if self.before_cancel:
            self.before_cancel(order_id)
        original = next(value for value in self.orders.values() if value.broker_order_id == order_id)
        result = self.cancel_behavior(original) if self.cancel_behavior else replace(original, status="cancelled")
        if isinstance(result, OrderResult):
            self.orders[original.client_order_id] = result
        return result

    def get_order_status(self, *, broker_order_id=None, client_order_id=None):
        self.history_calls.append((broker_order_id, client_order_id))
        if self.history_missing:
            return None
        if client_order_id:
            return self.orders.get(client_order_id)
        return next((value for value in self.orders.values() if value.broker_order_id == broker_order_id), None)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("execution tests must not use a network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def make_service(tmp_path):
    ledgers, paper_brokers = [], []
    sequence = 0

    def make(*, mode="paper", paper=False, paper_fee_bps="0", policy_changes=None, live_evidence=True):
        nonlocal sequence
        sequence += 1
        clock = Clock()
        registry = FixtureRegistry(live=mode.startswith("live-"))
        broker = FakeBroker(clock, live=mode.startswith("live-"))
        if paper:
            broker = PaperBroker({SYMBOL: broker.quote}, cash=D("10000"), account_id=ACCOUNT,
                                 path=tmp_path / f"paper-{sequence}.db", clock=clock, fee_bps=D(paper_fee_bps))
            paper_brokers.append(broker)
        policy = RiskPolicy(mode=mode, expected_account_id=ACCOUNT,
                            live_enabled=mode.startswith("live-"), auto_enabled=mode == "live-auto",
                            allowed_symbols=frozenset({SYMBOL}), enabled_strategies=frozenset({STRATEGY}),
                            live_approved_strategies=frozenset({STRATEGY}) if mode.startswith("live-") else frozenset(),
                            max_order_notional=D("1000"), max_position_notional=D("5000"),
                            max_concentration=D("0.5"), max_daily_trades=10)
        if policy_changes:
            policy = replace(policy, **policy_changes)
        scope = "live" if mode.startswith("live-") else mode
        ledger = Ledger(tmp_path / f"execution-{sequence}.db", scope, clock=clock)
        ledgers.append(ledger)
        datasets = {SYMBOL: {"asset_class": "equity", "source": "synthetic fixture only",
                    "evidence_kind": "verified_live" if mode.startswith("live-") and live_evidence else "synthetic",
                    "bars": [{"timestamp": NOW - 60, "close": 99}, {"timestamp": NOW, "close": 100}]}}
        service = AtlasService(broker, ledger, policy, datasets=datasets, registry=registry,
                               approval_key=KEY, kill_switch_path=tmp_path / f"kill-{sequence}", clock=clock,
                               call_timeout=0.15, shutdown_grace=0.01)
        return SimpleNamespace(service=service, broker=broker, ledger=ledger, clock=clock, registry=registry,
                               policy=policy, datasets=datasets)

    yield make
    for broker in paper_brokers:
        broker.close()
    for ledger in ledgers:
        ledger.close()


def prepare(h, key="proposal-1", quantity="1"):
    signal = h.service.analyze(SYMBOL)["signals"][0]
    return h.service.create_trade_proposal(signal["signal_id"], quantity, key)["proposal"]


def approve(h, proposal, **kwargs):
    return issue_approval(proposal, KEY, now=int(h.clock()), **kwargs)


def execute(h, proposal, receipt=None):
    return h.service.execute_trade_proposal(proposal["id"], receipt)


def release_workers(service, release):
    release.set()
    for done in list(service._mutations):
        assert done.wait(3), "fake mutating worker did not terminate"


def accepted(order):
    return OrderResult("broker-" + order.client_order_id, order.client_order_id, "accepted")


def test_prepare_only_and_paper_execution_replay_uses_one_actual_simulator_order(make_service):
    h = make_service(paper=True)
    proposal = prepare(h)
    assert proposal["status"] == "prepared" and proposal["risk"]["passed"]
    assert h.ledger.get_intent(proposal["id"]) is None
    assert h.broker.db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    result = execute(h, proposal)
    assert result["intent"]["status"] == "filled" and result["new_submission"]
    assert h.broker.get_account().cash == D("9900")
    assert execute(h, proposal)["new_submission"] is False
    assert h.broker.db.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
    assert str(uuid.UUID(result["intent"]["client_order_id"])) == result["intent"]["client_order_id"]


def test_paper_buy_sell_pnl_is_measured_once_from_broker_outcomes(make_service):
    h = make_service(paper=True)
    buy = prepare(h)
    execute(h, buy)
    h.broker.quotes[SYMBOL] = replace(h.broker.quotes[SYMBOL], bid=D("105"), ask=D("105.01"))
    h.registry.direction = "sell"
    sell = prepare(h, "sell")
    execute(h, sell)
    report = h.service.performance()
    assert report["closed_sell_executions"] == 1
    assert h.broker.get_account().cash == D("10005")
    assert D(h.ledger.daily_stats(account_fingerprint("paper", ACCOUNT), NOW)["realized_pnl"]) == D("5")
    execute(h, sell)
    assert h.service.performance()["closed_sell_executions"] == 1


def test_same_signal_different_proposal_key_cannot_submit_twice(make_service):
    h = make_service()
    first, second = prepare(h), prepare(h, "second-key")
    execute(h, first)
    with pytest.raises(ConflictError, match="signal already"):
        execute(h, second)
    assert len(h.broker.submit_calls) == 1


def test_proposal_idempotency_rejects_changed_quantity(make_service):
    h = make_service()
    first = prepare(h)
    assert prepare(h)["id"] == first["id"]
    with pytest.raises(ConflictError, match="terms"):
        prepare(h, quantity="2")
    assert h.broker.submit_calls == []


def test_intent_is_visible_from_another_connection_before_broker_call(make_service):
    h = make_service()
    proposal = prepare(h)
    observed = []

    def before(order):
        with Ledger(h.ledger.path, "paper") as independent:
            row = independent.get_intent(proposal["id"])
            assert row["status"] == "submitting"
            assert row["client_order_id"] == order.client_order_id
            observed.append(row)

    h.broker.before_submit = before
    assert execute(h, proposal)["intent"]["status"] == "filled"
    assert len(observed) == 1


@pytest.mark.parametrize("change", ["account", "mode", "quote_stale", "quote_drift", "buying_power", "daily_loss"])
def test_fresh_state_changes_after_proposal_prevent_submission(make_service, change):
    h = make_service()
    proposal = prepare(h)
    if change == "account":
        h.broker.account = replace(h.broker.account, account_id="other-account")
    elif change == "mode":
        h.broker.account = replace(h.broker.account, mode="real")
    elif change == "quote_stale":
        h.broker.quote = replace(h.broker.quote, timestamp=NOW - 100)
    elif change == "quote_drift":
        h.broker.quote = replace(h.broker.quote, ask=D("102"), bid=D("101.99"))
    elif change == "buying_power":
        h.broker.account = replace(h.broker.account, buying_power=D("0"))
    else:
        h.broker.account = replace(h.broker.account, daily_realized_pnl=D("-100"))
    result = execute(h, proposal)
    assert result["intent"]["status"] == "rejected"
    assert result["new_submission"] is False
    assert h.broker.submit_calls == []


def test_account_and_quote_currency_cannot_jointly_change_after_approval(make_service):
    h = make_service(mode="live-assisted")
    proposal = prepare(h)
    receipt = approve(h, proposal)
    h.broker.account = replace(h.broker.account, currency="EUR")
    h.broker.quote = replace(h.broker.quote, currency="EUR")
    result = execute(h, proposal, receipt)
    assert result["new_submission"] is False
    assert h.broker.submit_calls == []


@pytest.mark.parametrize("change", ["policy", "signal", "strategy"])
def test_policy_or_evidence_change_invalidates_snapshot_before_claim(make_service, change):
    h = make_service()
    proposal = prepare(h)
    if change == "policy":
        h.service.policy = replace(h.policy, version="new-policy")
    elif change == "signal":
        h.service.datasets[SYMBOL]["bars"][-1]["close"] = 101
    else:
        h.registry.enabled = False
    with pytest.raises(ServiceError):
        execute(h, proposal)
    assert h.ledger.get_intent(proposal["id"]) is None
    assert h.broker.submit_calls == []


@pytest.mark.parametrize("mechanism", ["kill", "shutdown"])
def test_stop_before_claim_creates_no_execution_intent(make_service, mechanism):
    h = make_service()
    proposal = prepare(h)
    if mechanism == "kill":
        h.service.kill_switch_path.touch()
    else:
        h.service.stop.set()
    with pytest.raises(StopRequested):
        execute(h, proposal)
    assert h.ledger.get_intent(proposal["id"]) is None
    assert h.broker.submit_calls == []


@pytest.mark.parametrize("mechanism", ["kill", "shutdown"])
def test_stop_after_durable_claim_is_proven_not_submitted(make_service, monkeypatch, mechanism):
    h = make_service()
    proposal = prepare(h)
    original = h.ledger.claim_intent

    def claim(*args, **kwargs):
        result = original(*args, **kwargs)
        if mechanism == "kill":
            h.service.kill_switch_path.touch()
        else:
            h.service.stop.set()
        return result

    monkeypatch.setattr(h.ledger, "claim_intent", claim)
    result = execute(h, proposal)
    assert result["intent"]["status"] == "rejected"
    assert result["intent"]["details"]["submitted"] is False
    assert h.broker.submit_calls == []


@pytest.mark.parametrize("mechanism", ["kill", "shutdown"])
def test_stop_during_mutating_thread_persists_unknown(make_service, mechanism):
    h = make_service()
    proposal = prepare(h)
    release = threading.Event()

    def blocked(order):
        if mechanism == "kill":
            h.service.kill_switch_path.touch()
        else:
            h.service.stop.set()
        assert release.wait(3)
        return h.broker.default_fill(order)

    h.broker.submit_behavior = blocked
    try:
        result = execute(h, proposal)
        assert result["intent"]["status"] == "unknown"
        assert len(h.broker.submit_calls) == 1
    finally:
        release_workers(h.service, release)
    assert h.ledger.get_intent(proposal["id"])["status"] == "unknown"


def test_failed_completion_write_after_fill_never_allows_resubmission(make_service, monkeypatch):
    h = make_service()
    proposal, second = prepare(h), prepare(h, "second")

    def unavailable(*args, **kwargs):
        raise OSError("synthetic full-disk failure after broker acknowledgement")

    monkeypatch.setattr(h.ledger, "complete_intent", unavailable)
    with pytest.raises(OSError, match="full-disk"):
        execute(h, proposal)
    assert len(h.broker.submit_calls) == 1
    assert h.ledger.get_intent(proposal["id"])["status"] == "submitting"
    with Ledger(h.ledger.path, "paper", clock=h.clock) as recovered:
        service = AtlasService(h.broker, recovered, h.policy, datasets=h.datasets,
                               registry=h.registry, clock=h.clock, call_timeout=0.15)
        assert service.execute_trade_proposal(proposal["id"])["new_submission"] is False
        with pytest.raises(ConflictError, match="reconciliation"):
            service.execute_trade_proposal(second["id"])
        assert service.reconcile(proposal["id"])["intent"]["status"] == "filled"
    assert len(h.broker.submit_calls) == 1


@pytest.mark.parametrize("state", ["accepted", "unknown"])
def test_outstanding_order_blocks_readiness_without_network_probe(make_service, state):
    h = make_service()
    assert h.service.readiness()["execution_ready"] is True
    proposal = prepare(h)
    h.broker.submit_behavior = accepted if state == "accepted" else lambda _: {"uncertain": True}
    assert execute(h, proposal)["intent"]["status"] == state
    report = h.service.readiness()
    assert report["healthy"] is True and report["execution_ready"] is False
    assert "outstanding_order_requires_reconciliation" in report["blockers"]
    assert report["network_probe_performed"] is False


def test_timeout_blocks_threads_and_restart_until_verified_history(make_service):
    h = make_service()
    first, second = prepare(h), prepare(h, "second")
    release = threading.Event()

    def blocked(order):
        assert release.wait(3)
        return h.broker.default_fill(order)

    h.broker.submit_behavior = blocked
    restarted = None
    try:
        assert execute(h, first)["intent"]["status"] == "unknown"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute, h, second)
            with pytest.raises(ServiceError, match="still in flight"):
                future.result(timeout=2)
        with pytest.raises(ServiceError, match="still in flight"):
            h.service.reconcile(first["id"])
        restarted = Ledger(h.ledger.path, "paper", clock=h.clock)
        service = AtlasService(h.broker, restarted, h.policy, datasets=h.datasets,
                               registry=h.registry, clock=h.clock, call_timeout=0.15)
        with pytest.raises(ConflictError, match="reconciliation"):
            service.execute_trade_proposal(second["id"])
        assert len(h.broker.submit_calls) == 1
    finally:
        release_workers(h.service, release)
        if restarted:
            restarted.close()
    h.broker.history_missing = True
    missing = h.service.reconcile(first["id"])
    assert missing["reconciled"] is False
    assert h.ledger.get_intent(first["id"])["status"] == "unknown"
    h.broker.history_missing = False
    assert h.service.reconcile(first["id"])["intent"]["status"] == "filled"
    assert len(h.broker.submit_calls) == 1


@pytest.mark.parametrize("fault", [
    "shape", "correlation", "state", "missing_id", "negative_quantity", "nan_quantity",
    "too_much", "too_little", "accepted_with_fill", "partial_without_fill", "limit_violation",
    "negative_fee", "nan_pnl",
])
def test_malformed_broker_results_remain_unknown(make_service, fault):
    h = make_service()
    proposal = prepare(h)

    def malformed(order):
        good = h.broker.default_fill(order)
        changes = {
            "correlation": {"client_order_id": "wrong-client-id"},
            "state": {"status": "magic-success"}, "missing_id": {"broker_order_id": None},
            "negative_quantity": {"filled_quantity": D("-1")},
            "nan_quantity": {"filled_quantity": D("NaN")},
            "too_much": {"filled_quantity": D("2")}, "too_little": {"filled_quantity": D("0.5")},
            "accepted_with_fill": {"status": "accepted"},
            "partial_without_fill": {"status": "partially_filled", "filled_quantity": D("0")},
            "limit_violation": {"average_fill_price": D("101")},
            "negative_fee": {"fees": D("-1")}, "nan_pnl": {"realized_pnl": D("NaN")},
        }
        return {"success": True} if fault == "shape" else replace(good, **changes[fault])

    h.broker.submit_behavior = malformed
    result = execute(h, proposal)
    assert result["intent"]["status"] == "unknown"
    assert h.ledger.has_pending(account_fingerprint(h.broker.capabilities.broker, ACCOUNT))
    assert len(h.broker.submit_calls) == 1


def test_confirmed_broker_rejection_is_terminal_and_not_unknown(make_service):
    h = make_service()
    proposal = prepare(h)

    def rejected(_):
        raise BrokerRejected("fixture-confirmed-refusal")

    h.broker.submit_behavior = rejected
    result = execute(h, proposal)
    assert result["intent"]["status"] == "rejected"
    assert not h.ledger.has_pending(account_fingerprint(h.broker.capabilities.broker, ACCOUNT))
    assert execute(h, proposal)["new_submission"] is False
    assert len(h.broker.submit_calls) == 1


@pytest.mark.parametrize("fault", ["decreasing_fill", "accepted_reset", "different_broker_id"])
def test_reconciliation_cannot_erase_known_fills_or_change_broker_identity(make_service, fault):
    h = make_service()
    proposal = prepare(h, quantity="2")
    h.broker.submit_behavior = lambda order: replace(h.broker.default_fill(order),
        status="partially_filled", filled_quantity=D("1"))
    initial = execute(h, proposal)["intent"]
    assert initial["status"] == "accepted" and initial["details"]["filled_quantity"] == "1"
    changes = {
        "decreasing_fill": {"filled_quantity": D("0.5")},
        "accepted_reset": {"status": "accepted", "filled_quantity": D("0")},
        "different_broker_id": {"broker_order_id": "another-broker-order"},
    }
    h.broker.orders[initial["client_order_id"]] = replace(
        h.broker.orders[initial["client_order_id"]], **changes[fault])
    with pytest.raises((ValueError, BrokerError, ConflictError)):
        h.service.reconcile(proposal["id"])
    retained = h.ledger.get_intent(proposal["id"])
    assert retained["details"]["filled_quantity"] == "1"
    assert retained["broker_order_id"] == initial["broker_order_id"]
    assert h.ledger.has_pending(initial["account_fingerprint"])


def test_fake_live_execution_requires_exact_signed_proposal_and_replays_once(make_service):
    h = make_service(mode="live-assisted")
    proposal = prepare(h)
    assert proposal["status"] == "awaiting_approval" and proposal["risk"]["passed"]
    result = execute(h, proposal, approve(h, proposal))
    assert result["intent"]["status"] == "filled"
    assert execute(h, proposal, "yes")["new_submission"] is False
    assert len(h.broker.submit_calls) == 1
    assert h.broker.capabilities.network_enabled is False


@pytest.mark.parametrize("receipt", [None, True, "yes", {"approved": True, "approved_by": "human"}])
def test_generic_yes_cannot_approve_fake_live_order(make_service, receipt):
    h = make_service(mode="live-assisted")
    proposal = prepare(h)
    with pytest.raises(ApprovalError):
        execute(h, proposal, receipt)
    assert h.ledger.get_intent(proposal["id"]) is None
    assert h.broker.submit_calls == []


@pytest.mark.parametrize("change", ["expiry", "signature", "quantity"])
def test_expired_or_tampered_fake_live_approval_fails_before_claim(make_service, change):
    h = make_service(mode="live-assisted")
    proposal = prepare(h)
    receipt = approve(h, proposal, expires_at=NOW + 5)
    if change == "expiry":
        h.clock.now += 5
    elif change == "signature":
        receipt["signature"] = "0" * 64
    else:
        changed = deepcopy(proposal)
        changed["order"]["quantity"] = "2"
        receipt = approve(h, changed)
    with pytest.raises(ApprovalError):
        execute(h, proposal, receipt)
    assert h.ledger.get_intent(proposal["id"]) is None
    assert h.broker.submit_calls == []


def test_live_approval_expiry_is_rechecked_after_durable_claim(make_service, monkeypatch):
    h = make_service(mode="live-assisted")
    proposal = prepare(h)
    receipt = approve(h, proposal, expires_at=NOW + 5)
    original = h.ledger.claim_intent

    def claim(*args, **kwargs):
        intent = original(*args, **kwargs)
        h.clock.now += 5
        return intent

    monkeypatch.setattr(h.ledger, "claim_intent", claim)
    result = execute(h, proposal, receipt)
    assert result["intent"]["status"] == "rejected" and result["new_submission"] is False
    assert result["intent"]["details"]["reason_code"] == "ApprovalError"
    assert h.broker.submit_calls == []


@pytest.mark.parametrize("mode,changes", [
    ("live-assisted", {"live_enabled": False}),
    ("live-auto", {"live_enabled": False}),
    ("live-auto", {"auto_enabled": False}),
    ("live-assisted", {"live_approved_strategies": frozenset()}),
])
def test_live_enable_flags_and_strategy_approval_cannot_be_overridden_by_receipt(make_service, mode, changes):
    h = make_service(mode=mode, policy_changes=changes)
    proposal = prepare(h)
    assert proposal["risk"]["passed"] is False
    with pytest.raises(ServiceError):
        execute(h, proposal, approve(h, proposal))
    assert h.broker.submit_calls == []


def test_live_auto_requires_separate_server_flag_but_no_assisted_receipt(make_service):
    h = make_service(mode="live-auto")
    proposal = prepare(h)
    assert execute(h, proposal)["intent"]["status"] == "filled"
    assert len(h.broker.submit_calls) == 1


@pytest.mark.parametrize("reason", ["data", "strategy", "confidence"])
def test_unverified_data_or_unvalidated_strategy_blocks_even_signed_live_proposal(make_service, reason):
    h = make_service(mode="live-assisted", live_evidence=reason != "data")
    if reason == "strategy":
        h.registry.live = False
    if reason == "confidence":
        h.service.policy = replace(h.policy, min_confidence=D("0.9"))
    proposal = prepare(h)
    assert proposal["risk"]["passed"] is False
    with pytest.raises(ServiceError):
        execute(h, proposal, approve(h, proposal))
    assert h.broker.submit_calls == []


def open_cancellable(h):
    h.broker.submit_behavior = accepted
    proposal = prepare(h)
    receipt = approve(h, proposal) if h.policy.mode == "live-assisted" else None
    assert execute(h, proposal, receipt)["intent"]["status"] == "accepted"
    cancel = h.service.create_cancellation_proposal(proposal["id"], "cancel-key")["proposal"]
    return proposal, cancel


def test_fake_live_cancellation_has_own_approval_and_committed_child_intent(make_service):
    h = make_service(mode="live-assisted")
    target, cancel = open_cancellable(h)
    assert h.broker.cancel_calls == []
    with pytest.raises(ApprovalError):
        h.service.cancel_order(cancel["id"], approve(h, target))
    assert h.ledger.get_intent(cancel["id"]) is None

    def before(order_id):
        with Ledger(h.ledger.path, "live") as independent:
            child = independent.get_intent(cancel["id"])
            assert child["status"] == "cancel_pending" and child["broker_order_id"] == order_id

    h.broker.before_cancel = before
    result = h.service.cancel_order(cancel["id"], approve(h, cancel))
    assert result["target"]["status"] == "cancelled"
    assert result["cancellation"]["status"] == "cancelled"
    assert h.service.cancel_order(cancel["id"])["new_submission"] is False
    assert len(h.broker.cancel_calls) == 1


def test_async_cancellation_is_unknown_until_terminal_history_and_never_retried(make_service):
    h = make_service()
    target, cancel = open_cancellable(h)
    h.broker.cancel_behavior = lambda result: result
    result = h.service.cancel_order(cancel["id"])
    assert result["cancellation"]["status"] == "unknown"
    assert result["target"]["status"] == "accepted"
    assert h.service.reconcile(cancel["id"])["reconciled"] is False
    assert h.service.cancel_order(cancel["id"])["new_submission"] is False
    client_id = h.ledger.get_intent(target["id"])["client_order_id"]
    h.broker.orders[client_id] = replace(h.broker.orders[client_id], status="cancelled")
    final = h.service.reconcile(cancel["id"])
    assert final["target"]["status"] == "cancelled"
    assert final["cancellation"]["status"] == "cancelled"
    assert len(h.broker.cancel_calls) == 1


def test_cancellation_timeout_and_restart_never_repeat_broker_cancel(make_service):
    h = make_service()
    target, cancel = open_cancellable(h)
    release = threading.Event()

    def blocked(original):
        assert release.wait(3)
        return replace(original, status="cancelled")

    h.broker.cancel_behavior = blocked
    try:
        result = h.service.cancel_order(cancel["id"])
        assert result["cancellation"]["status"] == "unknown"
        assert result["target"]["status"] == "accepted"
        with pytest.raises(ServiceError, match="still in flight"):
            h.service.reconcile(cancel["id"])
        with Ledger(h.ledger.path, "paper", clock=h.clock) as recovered:
            service = AtlasService(h.broker, recovered, h.policy, clock=h.clock, call_timeout=0.15)
            assert service.cancel_order(cancel["id"])["new_submission"] is False
            assert len(h.broker.cancel_calls) == 1
    finally:
        release_workers(h.service, release)
    assert h.ledger.get_intent(cancel["id"])["status"] == "unknown"
    terminal = h.service.reconcile(cancel["id"])
    assert terminal["target"]["status"] == "cancelled"
    assert terminal["cancellation"]["status"] == "cancelled"
    assert len(h.broker.cancel_calls) == 1 and len(h.broker.submit_calls) == 1


def test_cancellation_loses_fill_race_records_only_target_pnl(make_service):
    h = make_service()
    h.registry.direction = "sell"
    h.broker.account = replace(h.broker.account,
                               positions=(Position(SYMBOL, D("10"), D("1000"), D("900")),))
    target, cancel = open_cancellable(h)
    terms = target["order"]
    h.broker.cancel_behavior = lambda original: replace(original, status="filled", filled_quantity=D("1"),
        average_fill_price=D(terms["limit_price"]), fees=D("0"), realized_pnl=D("9.99"))
    result = h.service.cancel_order(cancel["id"])
    assert result["cancellation"]["status"] == "rejected"
    assert result["target"]["status"] == "filled"
    assert h.service.performance()["closed_sell_executions"] == 1
    stats = h.ledger.daily_stats(account_fingerprint(h.broker.capabilities.broker, ACCOUNT), NOW)
    assert stats["realized_pnl"] == "9.99" and stats["settlements_count"] == 1
    assert len(h.broker.cancel_calls) == 1
