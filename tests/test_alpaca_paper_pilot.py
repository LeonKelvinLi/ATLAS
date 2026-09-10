"""Failure-path tests for the one-shot paper controller; no real client or keys."""

from copy import deepcopy
from decimal import Decimal as D
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import threading
from uuid import UUID

import pytest

from atlas.alpaca_paper_pilot import PaperPilot, PilotError
from atlas.domain import BrokerError, BrokerRejected, UnknownSubmission
from atlas.risk import account_fingerprint


ACCOUNT_ID = "36272a23-5f15-4b84-8a39-6cd4011fbb4d"
OTHER_ACCOUNT_ID = "24b3e886-47ad-41a6-a34f-438ea7d02469"
ORDER_ID = "dcb77f59-5db2-4f3c-a791-7859935b5ec2"
ASSET_ID = "35d39f33-f8a7-4b6f-b493-d1eea14150ce"
FINGERPRINT = account_fingerprint("alpaca_paper", ACCOUNT_ID)
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("paper pilot tests must never access a network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


class FakeClient:
    """Broker-shaped state whose failure timing is controlled by the test."""

    transport_provenance = "injected_test_transport"

    def __init__(self):
        self.account_value = {
            "id": ACCOUNT_ID, "currency": "USD", "status": "ACTIVE", "crypto_status": "ACTIVE",
            "cash": "100", "equity": "100", "non_marginable_buying_power": "100",
            "buying_power": "200", "accrued_fees": None, "daily_realized_pnl": None,
            "account_blocked": False, "trading_blocked": False, "trade_suspended_by_user": False,
            "observed_at": NOW,
        }
        self.position_rows = []
        self.history = []
        self.activity_rows = []
        self.order = None
        self.account_calls = 0
        self.account_hook = None
        self.post_calls = []
        self.cancel_calls = []
        self.submit_hook = None
        self.cancel_hook = None
        self.lookup_hook = None
        self.quote_hook = None
        self.asset_value = {
            "id": ASSET_ID, "symbol": "BTC/USD", "tradable": True, "status": "active",
            "min_order_size": "0.00001", "min_trade_increment": "0.00000001",
            "price_increment": "0.01",
        }
        self.quote_value = {
            "symbol": "BTC/USD", "bid": "100000", "ask": "100001",
            "ask_size": "1", "timestamp": NOW, "source": "synthetic-test-quote",
        }

    def account(self):
        self.account_calls += 1
        if self.account_hook:
            self.account_hook(self)
        return deepcopy(self.account_value)

    def positions(self):
        return deepcopy(self.position_rows)

    def orders(self):
        return deepcopy(self.history)

    def activities(self):
        return deepcopy(self.activity_rows)

    def asset(self):
        return deepcopy(self.asset_value)

    def quote(self):
        if self.quote_hook:
            self.quote_hook(self)
        return deepcopy(self.quote_value)

    def get_order(self, client_id):
        if self.lookup_hook:
            return self.lookup_hook(self, client_id)
        return deepcopy(self.order)

    def submit_limit_buy(self, client_id, quantity, limit_price):
        self.post_calls.append((client_id, quantity, limit_price))
        if self.submit_hook:
            return self.submit_hook(self, client_id, quantity, limit_price)
        self.accept(client_id, quantity, limit_price)
        return deepcopy(self.order)

    def accept(self, client_id, quantity, limit_price):
        self.order = {
            "id": ORDER_ID, "asset_id": ASSET_ID, "client_order_id": client_id, "symbol": "BTC/USD",
            "side": "buy", "type": "limit", "time_in_force": "gtc",
            "qty": quantity, "limit_price": limit_price, "filled_qty": "0",
            "filled_avg_price": None, "status": "new", "fees": None, "realized_pnl": None,
        }
        self.history = [deepcopy(self.order)]

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        if self.cancel_hook:
            return self.cancel_hook(self)
        self.order["status"] = "canceled"
        self.history = [deepcopy(self.order)]


@pytest.fixture
def pilot(tmp_path):
    tmp_path.chmod(0o700)
    client = FakeClient()
    controller = PaperPilot(tmp_path / "pilot", client, FINGERPRINT,
                            clock=lambda: NOW, allow_test_transport=True)
    controller.setup()
    return controller, client


def restart(controller, client):
    return PaperPilot(controller.path, client, FINGERPRINT, clock=lambda: NOW,
                      allow_test_transport=True)


def stored(controller):
    with sqlite3.connect("file:" + str(controller.db) + "?mode=ro", uri=True) as conn:
        return json.loads(conn.execute("SELECT payload FROM state").fetchone()[0])


def test_intent_is_durable_before_post_and_restart_never_reposts(pilot):
    controller, client = pilot

    def verify_then_accept(fake, client_id, quantity, limit_price):
        committed = stored(controller)["intent"]
        assert committed["submit_attempt_recorded"] is True
        assert committed["state"] == "submitting"
        assert committed["request"]["client_order_id"] == client_id
        assert json.loads(committed["request_canonical_json"]) == committed["request"]
        assert D(quantity) * D(limit_price) * D("1.006") == D(committed["reserved_usd"])
        assert D(committed["reserved_usd"]) <= 2
        assert D(limit_price) < D(fake.quote_value["bid"])
        assert str(UUID(client_id)) == client_id
        fake.accept(client_id, quantity, limit_price)
        return deepcopy(fake.order)

    client.submit_hook = verify_then_accept
    first = controller.submit_once()
    again = restart(controller, client).submit_once()
    assert len(client.post_calls) == 1
    assert first["intent"]["request"] == again["intent"]["request"]
    assert again["remaining_submit_attempts"] == 0
    assert again["evidence_kind"] == "injected_test_transport"
    assert again["automatic_trading_enabled"] is False
    assert again["actual_order_fees_usd"] is again["daily_realized_pnl"] is None


@pytest.mark.parametrize("accepted", [False, True])
def test_unknown_submission_and_missing_lookup_never_repeat_post_after_restart(pilot, accepted):
    controller, client = pilot

    def timeout(fake, client_id, quantity, limit_price):
        if accepted:
            fake.accept(client_id, quantity, limit_price)
        raise UnknownSubmission("synthetic transport timeout")

    client.submit_hook = timeout
    with pytest.raises(UnknownSubmission):
        controller.submit_once()
    assert stored(controller)["intent"]["state"] == "unknown"
    report = restart(controller, client).submit_once()
    assert len(client.post_calls) == 1
    assert report["remaining_submit_attempts"] == 0
    assert report["halted"] is True
    assert report["completed"] is False


def test_crash_during_post_retains_attempt_even_without_unknown_save(pilot):
    controller, client = pilot

    def crash(*args):
        raise KeyboardInterrupt("simulated process interruption")

    client.submit_hook = crash
    with pytest.raises(KeyboardInterrupt):
        controller.submit_once()
    assert stored(controller)["intent"]["state"] == "submitting"
    assert restart(controller, client).submit_once()["remaining_submit_attempts"] == 0
    assert len(client.post_calls) == 1


@pytest.mark.parametrize("event,expected_posts", [
    ("submit_intent_committed", 0), ("submission_acknowledged", 1),
])
def test_persistence_failure_never_permits_unjournaled_or_repeated_post(pilot, monkeypatch, event, expected_posts):
    controller, client = pilot
    save = controller._save

    def fail_at(conn, data, label):
        if label == event:
            raise PilotError("synthetic persistence failure")
        return save(conn, data, label)

    monkeypatch.setattr(controller, "_save", fail_at)
    with pytest.raises(PilotError):
        controller.submit_once()
    assert len(client.post_calls) == expected_posts
    restored = restart(controller, client)
    if expected_posts:
        assert restored.submit_once()["remaining_submit_attempts"] == 0
    else:
        with pytest.raises(PilotError, match="halted"):
            restored.submit_once()
    assert len(client.post_calls) == expected_posts


def test_committed_but_unacknowledged_save_still_consumes_attempt(pilot, monkeypatch):
    controller, client = pilot
    save = controller._save

    def commit_then_fail(conn, data, label):
        save(conn, data, label)
        if label == "submit_intent_committed":
            raise PilotError("synthetic commit acknowledgment failure")

    monkeypatch.setattr(controller, "_save", commit_then_fail)
    with pytest.raises(PilotError):
        controller.submit_once()
    assert stored(controller)["intent"] is not None
    assert restart(controller, client).submit_once()["remaining_submit_attempts"] == 0
    assert client.post_calls == []


def test_concurrent_controller_cannot_post_while_first_call_is_in_flight(pilot):
    controller, client = pilot
    entered, release = threading.Event(), threading.Event()
    outcome = []

    def hold(fake, client_id, quantity, limit_price):
        entered.set()
        assert release.wait(5), "test did not release synthetic request"
        fake.accept(client_id, quantity, limit_price)
        return deepcopy(fake.order)

    def submit():
        try:
            outcome.append(controller.submit_once())
        except BaseException as exc:
            outcome.append(exc)

    client.submit_hook = hold
    worker = threading.Thread(target=submit)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(PilotError, match="operation_in_progress"):
            restart(controller, client).submit_once()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(client.post_calls) == 1
    assert len(outcome) == 1 and isinstance(outcome[0], dict)


@pytest.mark.parametrize("stage", ["setup", "before_submit", "after_claim"])
def test_exact_account_identity_required_at_each_submission_gate(tmp_path, stage):
    tmp_path.chmod(0o700)
    client = FakeClient()
    controller = PaperPilot(tmp_path / "pilot", client, FINGERPRINT,
                            clock=lambda: NOW, allow_test_transport=True)
    if stage == "setup":
        client.account_value["id"] = OTHER_ACCOUNT_ID
        action = controller.setup
    else:
        controller.setup()
        if stage == "before_submit":
            client.account_value["id"] = OTHER_ACCOUNT_ID
        else:
            client.quote_hook = lambda fake: fake.account_value.update(id=OTHER_ACCOUNT_ID)
        action = controller.submit_once
    with pytest.raises(PilotError, match="account_mismatch"):
        action()
    assert client.post_calls == []
    if stage == "after_claim":
        assert stored(controller)["intent"] is not None


@pytest.mark.parametrize("changes", [
    {"cash": "100000", "equity": "100000", "non_marginable_buying_power": "100000"},
    {"cash": "99.99"}, {"equity": "99.99"}, {"non_marginable_buying_power": "200"},
    {"currency": "EUR"}, {"crypto_status": None}, {"trading_blocked": True},
])
def test_setup_cannot_adopt_nonmatching_budget_or_permissions(tmp_path, changes):
    tmp_path.chmod(0o700)
    client = FakeClient()
    client.account_value.update(changes)
    controller = PaperPilot(tmp_path / "pilot", client, FINGERPRINT,
                            clock=lambda: NOW, allow_test_transport=True)
    with pytest.raises(PilotError, match="clean_100"):
        controller.setup()
    assert not controller.db.exists() and client.post_calls == []


@pytest.mark.parametrize("changed", ["closed_order", "position", "activities", "cash"])
def test_account_change_after_initialization_blocks_without_adoption(pilot, changed):
    controller, client = pilot
    baseline = deepcopy(stored(controller)["baseline"])
    if changed == "closed_order":
        client.history = [{"status": "canceled", "client_order_id": "untracked"}]
    elif changed == "position":
        client.position_rows = [{"symbol": "BTC/USD", "qty": "0.001"}]
    elif changed == "activities":
        client.activity_rows = [{"id": "unexplained-transfer", "net_amount": "1"}]
    else:
        client.account_value.update(cash="101", equity="101")
    with pytest.raises(PilotError):
        controller.submit_once()
    assert client.post_calls == []
    assert stored(controller)["baseline"] == baseline


def test_activity_change_after_durable_claim_prevents_post_and_no_second_attempt(pilot):
    controller, client = pilot
    client.quote_hook = lambda fake: fake.activity_rows.append({"id": "new-activity"})
    with pytest.raises(PilotError, match="unexplained_account_activity"):
        controller.submit_once()
    assert stored(controller)["intent"] is not None
    assert restart(controller, client).submit_once()["remaining_submit_attempts"] == 0
    assert client.post_calls == []


@pytest.mark.parametrize("after_claim", [False, True])
def test_kill_prevents_new_exposure_even_if_created_during_preparation(pilot, after_claim):
    controller, client = pilot
    if after_claim:
        client.quote_hook = lambda fake: controller.kill.touch(mode=0o600)
    else:
        controller.halt()
    with pytest.raises(PilotError):
        controller.submit_once()
    assert client.post_calls == []


def test_partial_fill_cancel_retains_fill_and_unknown_fees_then_never_resubmits(pilot):
    controller, client = pilot
    controller.submit_once()
    quantity = D(client.order["qty"]) / 2
    client.order.update(status="partially_filled", filled_qty=str(quantity), filled_avg_price="97999")
    report = controller.cancel_once()
    assert client.cancel_calls == [ORDER_ID]
    assert report["completed"] is True
    assert D(report["intent"]["order"]["filled_qty"]) == quantity
    assert report["intent"]["order"]["status"] == "canceled"
    assert report["actual_order_fees_usd"] is report["daily_realized_pnl"] is None
    reservation = report["intent"]["reserved_usd"]
    repeated = restart(controller, client).submit_once()
    assert repeated["intent"]["reserved_usd"] == reservation
    assert len(client.post_calls) == 1 and repeated["remaining_submit_attempts"] == 0


def test_cancel_acknowledgment_is_not_terminal_and_cancel_is_not_retried(pilot):
    controller, client = pilot
    controller.submit_once()
    client.cancel_hook = lambda fake: fake.order.update(status="pending_cancel")
    pending = controller.cancel_once()
    assert pending["completed"] is False
    assert pending["intent"]["cancel_attempt_recorded"] is True
    assert controller.cancel_once()["completed"] is False
    assert client.cancel_calls == [ORDER_ID]
    client.order["status"] = "canceled"
    assert controller.status()["completed"] is True
    assert len(client.post_calls) == 1


def test_cancel_transport_error_requires_lookup_not_a_second_cancel(pilot):
    controller, client = pilot
    controller.submit_once()

    def fail_cancel(fake):
        raise BrokerError("synthetic cancellation response unavailable")

    client.cancel_hook = fail_cancel
    assert controller.cancel_once()["completed"] is False
    assert restart(controller, client).cancel_once()["completed"] is False
    assert client.cancel_calls == [ORDER_ID]


@pytest.mark.parametrize("mutation", ["quantity", "value", "id", "terminal_status", "terminal_average"])
def test_broker_progress_cannot_regress_or_rewrite_terminal_financials(pilot, mutation):
    controller, client = pilot
    controller.submit_once()
    partial = D(client.order["qty"]) / 2
    client.order.update(status="partially_filled", filled_qty=str(partial), filled_avg_price="97000")
    if mutation.startswith("terminal"):
        client.order["status"] = "canceled"
    controller.status()
    before = stored(controller)["intent"]["order"]
    if mutation == "quantity":
        client.order["filled_qty"] = str(partial / 2)
    elif mutation == "value":
        client.order["filled_avg_price"] = "96000"
    elif mutation == "id":
        client.order["id"] = OTHER_ACCOUNT_ID
    elif mutation == "terminal_status":
        client.order["status"] = "partially_filled"
    else:
        client.order["filled_avg_price"] = "97999"
    with pytest.raises(PilotError):
        controller.status()
    assert stored(controller)["intent"]["order"] == before
    assert len(client.post_calls) == 1


@pytest.mark.parametrize("field,value", [
    ("client_order_id", OTHER_ACCOUNT_ID), ("symbol", "ETH/USD"), ("side", "sell"),
    ("qty", "0.0001"), ("limit_price", "98001"), ("time_in_force", "ioc"),
    ("asset_id", OTHER_ACCOUNT_ID),
])
def test_wrong_post_response_correlation_is_unknown_and_cannot_repost(pilot, field, value):
    controller, client = pilot

    def wrong(fake, client_id, quantity, limit_price):
        fake.accept(client_id, quantity, limit_price)
        response = deepcopy(fake.order)
        response[field] = value
        return response

    client.submit_hook = wrong
    with pytest.raises(UnknownSubmission):
        controller.submit_once()
    assert stored(controller)["intent"]["state"] == "unknown"
    assert restart(controller, client).submit_once()["remaining_submit_attempts"] == 0
    assert len(client.post_calls) == 1


def test_lost_terminal_lookup_is_unknown_not_a_fresh_completion(pilot):
    controller, client = pilot
    controller.submit_once()
    assert controller.cancel_once()["completed"] is True
    client.lookup_hook = lambda fake, client_id: None
    report = controller.status()
    assert report["intent"]["state"] == "unknown"
    assert report["completed"] is False
    assert len(client.post_calls) == 1


def test_existing_initialization_never_reseeds_cash_or_discards_intent(pilot):
    controller, client = pilot
    controller.submit_once()
    before = stored(controller)
    client.account_value.update(cash="98.01", equity="99.97")
    report = restart(controller, client).setup()
    assert report["remaining_submit_attempts"] == 0
    assert stored(controller) == before
    assert client.account_value["cash"] == "98.01"
    assert len(client.post_calls) == 1


@pytest.mark.parametrize("contents", [b"", b"not a SQLite database"])
def test_existing_incomplete_or_corrupt_database_is_never_reinitialized(pilot, contents):
    controller, client = pilot
    controller.db.write_bytes(contents)
    with pytest.raises(PilotError):
        restart(controller, client).setup()
    assert controller.db.read_bytes() == contents
    assert client.post_calls == []


def test_audit_inconsistent_state_cannot_erase_a_committed_attempt(pilot):
    controller, client = pilot

    def vanished(*args):
        raise UnknownSubmission("synthetic request has no acknowledgment")

    client.submit_hook = vanished
    with pytest.raises(UnknownSubmission):
        controller.submit_once()
    state = stored(controller)
    state["intent"] = None
    with sqlite3.connect(controller.db) as conn:
        conn.execute("UPDATE state SET payload=?", (json.dumps(state),))
    # Offline inspection itself must reject inconsistent state; the halt flag
    # is independent and cannot serve as a replacement for journal integrity.
    with pytest.raises(PilotError):
        restart(controller, client).status(reconcile=False)
    assert len(client.post_calls) == 1


def test_existing_broker_smoke_prevents_new_state_path_from_adopting_account(pilot, tmp_path):
    controller, client = pilot
    controller.submit_once()
    other = PaperPilot(tmp_path / "another-pilot", client, FINGERPRINT,
                       clock=lambda: NOW, allow_test_transport=True)
    assert other.client_id == controller.client_id
    with pytest.raises(PilotError):
        other.setup()
    assert not other.db.exists()
    assert len(client.post_calls) == 1


def test_simulated_transport_is_never_implicitly_authorized(tmp_path):
    tmp_path.chmod(0o700)
    with pytest.raises(PilotError, match="verified_paper_transport_required"):
        PaperPilot(tmp_path / "pilot", FakeClient(), FINGERPRINT, clock=lambda: NOW)


def load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts/alpaca_paper_pilot.py"
    spec = importlib.util.spec_from_file_location("paper_pilot_cli_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_reconciles_delayed_acceptance_and_cancels_without_reposting(pilot):
    controller, client = pilot
    lookups = []
    sleeps = []

    def timeout_then_delayed_accept(fake, client_id, quantity, limit_price):
        def delayed(current, requested_id):
            lookups.append(requested_id)
            if len(lookups) == 1:
                return None
            if current.order is None:
                current.accept(client_id, quantity, limit_price)
            return deepcopy(current.order)
        fake.lookup_hook = delayed
        raise UnknownSubmission("synthetic delayed broker acceptance")

    client.submit_hook = timeout_then_delayed_accept
    result = load_cli().run_smoke(controller, sleeper=sleeps.append)
    assert len(client.post_calls) == 1
    assert client.cancel_calls == [ORDER_ID]
    assert sleeps == [2]
    assert result["completed"] is True and result["halted"] is True
    assert result["submission_error"] == "pilot_submission_unknown"
    assert result["actual_order_fees_usd"] is result["daily_realized_pnl"] is None


def test_smoke_polling_is_bounded_and_pending_cancel_stays_unresolved(pilot):
    controller, client = pilot
    client.cancel_hook = lambda fake: fake.order.update(status="pending_cancel")
    sleeps = []
    result = load_cli().run_smoke(controller, sleeper=sleeps.append)
    assert sleeps == [2] * 5
    assert result["completed"] is False and result["polling_finished"] is True
    assert result["halted"] is True
    assert len(client.post_calls) == 1 and client.cancel_calls == [ORDER_ID]


def test_smoke_unexpected_reconciliation_failure_still_halts(pilot, monkeypatch):
    controller, client = pilot

    def failed_cancel():
        raise PilotError("synthetic reconciliation failure")

    monkeypatch.setattr(controller, "cancel_once", failed_cancel)
    with pytest.raises(PilotError, match="synthetic reconciliation failure"):
        load_cli().run_smoke(controller, sleeper=lambda _: None)
    assert controller.kill.exists()
    assert stored(controller)["intent"]["submit_attempt_recorded"] is True
    assert len(client.post_calls) == 1


def test_smoke_preserves_pre_submission_gate_failure_when_no_intent_exists(pilot):
    controller, client = pilot
    client.account_value["cash"] = "99"
    try:
        result = load_cli().run_smoke(controller, sleeper=lambda _: None)
    except BrokerError as exc:
        reported_cause = str(exc)
    else:
        reported_cause = result["submission_error"]
    assert "pilot_clean_100_account_required" in reported_cause
    assert stored(controller)["intent"] is None
    assert client.post_calls == client.cancel_calls == []
    assert controller.kill.exists()


def test_cli_selection_cannot_override_state_path_or_policy(tmp_path):
    tmp_path.chmod(0o700)
    config = tmp_path / "selection.json"
    config.write_text(json.dumps({
        "schema": 1, "key_file": str(tmp_path / "unused-key-file.json"),
        "expected_account_fingerprint": FINGERPRINT, "state_dir": str(tmp_path / "reset"),
    }))
    config.chmod(0o600)
    with pytest.raises(PilotError, match="selection_file_invalid"):
        load_cli().load_selection(config)


def test_cli_help_does_not_read_configuration_or_construct_client(capsys, monkeypatch):
    cli = load_cli()

    def forbidden(*args, **kwargs):
        pytest.fail("--help must not read configuration or construct a client")

    monkeypatch.setattr(cli, "load_selection", forbidden)
    monkeypatch.setattr(cli, "AlpacaPaperClient", forbidden)
    with pytest.raises(SystemExit) as result:
        cli.main(["--help"])
    assert result.value.code == 0
    assert "smoke" in capsys.readouterr().out
