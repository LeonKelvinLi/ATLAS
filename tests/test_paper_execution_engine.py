"""Full paper gateway with invented exchange state and real durable journals.

No broker credentials or network are permitted. Faults enter at the transport,
exchange-state or isolated-storage boundary, never through bypassed risk checks.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from atlas.paper.broker import AlpacaPaperAPI, PAPER_ORIGIN, Response
from atlas.paper.engine import PaperEngine
from atlas.paper.journal import PaperJournal
from atlas.paper.models import PaperError
from atlas.paper.risk import PaperRiskPolicy
from atlas.paper.simulation import SyntheticAlpaca


@pytest.fixture(autouse=True)
def prohibit_network_and_credentials(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("engine fixtures must not access networks or broker credentials")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("atlas.paper.broker._credentials", forbidden)
    monkeypatch.setattr("atlas.paper.journal._account_lock_root", lambda: tmp_path / "account-locks")


@pytest.fixture
def create_engine(tmp_path):
    journals = []

    def create(*, sim=None, policy=None, initialize=True):
        sim = SyntheticAlpaca() if sim is None else sim
        policy = policy or PaperRiskPolicy(allowed_strategy_ids=("test-strategy",))
        api = AlpacaPaperAPI(transport=sim.request, clock=sim.clock)
        binding = {"mode": "SIMULATION", "account_fingerprint": api.account()["fingerprint"],
                   "strategy_sha256": "a" * 64, "policy_sha256": policy.fingerprint(),
                   "allocated_principal": "100"}
        path = tmp_path / ("state" if not journals else "state-" + str(len(journals)))
        journal = PaperJournal(path, binding, create=True, now=sim.clock())
        journals.append(journal)
        engine = PaperEngine(api, journal, policy, clock=sim.clock)
        session = SimpleNamespace(sim=sim, api=api, binding=binding, policy=policy,
                                  journal=journal, engine=engine, path=path)
        if initialize:
            engine.initialize()
        return session

    def restart(session):
        saved = json.loads(json.dumps(session.sim.export_state()))
        session.journal.close()
        session.sim = SyntheticAlpaca.from_state(saved)
        session.api = AlpacaPaperAPI(transport=session.sim.request, clock=session.sim.clock)
        session.journal = PaperJournal(session.path, session.binding, now=session.sim.clock())
        journals.append(session.journal)
        session.engine = PaperEngine(session.api, session.journal, session.policy, clock=session.sim.clock)
        return session

    create.restart = restart
    yield create
    for journal in journals:
        journal.close()


def decision(session, signal="signal-1", **changes):
    return {"signal_id": signal, "strategy_id": "test-strategy", "strategy_sha256": "a" * 64,
            "data_sha256": "b" * 64, "symbol": "SPY", "side": "buy", "quantity": ".5",
            "limit_price": "10", "signal_time": session.sim.clock(),
            "expires_at": session.sim.clock() + 120, **changes}


def mutations(session, method="POST"):
    return [row for row in session.sim.calls if row["method"] == method]


def submitted(session, **changes):
    result = session.engine.submit(decision(session, **changes))
    assert result["new_submission"] is True, result
    assert result["order"]["state"] == "new", result
    return result["order"]["client_order_id"]


def test_initialize_ignores_distinct_receipt_times_and_preserves_fixed_start(create_engine, monkeypatch):
    sim = SyntheticAlpaca()
    dispatch = sim._dispatch

    def progressing_clock(method, path, query, payload):
        sim.advance(.1)
        return dispatch(method, path, query, payload)

    monkeypatch.setattr(sim, "_dispatch", progressing_clock)
    session = create_engine(sim=sim)
    result = session.engine.status()
    assert result["initialized"] and result["allocated_principal"] == "100"
    assert session.journal.get("day_start_equity") == "100"
    assert session.journal.get("peak_equity") == "100"
    assert not mutations(session)


@pytest.mark.parametrize("cash", ["99", "101", "1000"])
def test_initial_account_cash_must_equal_the_bound_principal(create_engine, cash):
    sim = SyntheticAlpaca()
    sim.cash = cash
    session = create_engine(sim=sim, initialize=False)
    with pytest.raises(PaperError, match="dedicated_cash_account"):
        session.engine.initialize()
    assert session.journal.get("initialized") is False
    assert not mutations(session)


def test_slow_initial_snapshot_cannot_initialize_session(create_engine, monkeypatch):
    sim = SyntheticAlpaca()
    dispatch = sim._dispatch

    def slow_snapshot(method, path, query, payload):
        sim.advance(3)
        return dispatch(method, path, query, payload)

    monkeypatch.setattr(sim, "_dispatch", slow_snapshot)
    session = create_engine(sim=sim, initialize=False)
    with pytest.raises(PaperError, match="initialization_snapshot_stale"):
        session.engine.initialize()
    assert session.journal.get("initialized") is False
    assert not mutations(session)


def test_cancellation_authorization_does_not_inflate_order_telemetry(create_engine):
    session = create_engine()
    cid = submitted(session)
    session.engine.cancel(cid)
    status = session.engine.status()
    assert status["post_authorizations"] == 1
    assert status["broker_orders_observed"] == 1
    assert status["actual_orders_submitted"] == 0
    assert session.journal.order(cid)["cancel"]["wire_authorized"] is True


def test_quote_expiring_after_risk_review_cannot_reach_post(create_engine, monkeypatch):
    session = create_engine()
    dispatch = session.sim._dispatch
    submit = session.api.submit_order

    def old_quote(method, path, query, payload):
        response = dispatch(method, path, query, payload)
        if path == "/v2/stocks/quotes/latest":
            body = json.loads(response.body)
            body["quotes"]["SPY"]["t"] = datetime.fromtimestamp(
                session.sim.clock() - 9, timezone.utc).isoformat()
            return Response(200, json.dumps(body).encode())
        return response

    def pause_after_review(payload, *, permit):
        session.sim.advance(2)
        return submit(payload, permit=permit)

    monkeypatch.setattr(session.sim, "_dispatch", old_quote)
    monkeypatch.setattr(session.api, "submit_order", pause_after_review)
    result = session.engine.submit(decision(session))
    assert not result["new_submission"]
    assert result["order"]["state"] == "http_rejected"
    assert not mutations(session)


def test_reservation_and_intent_are_durable_before_one_post_and_ack_is_not_fill(create_engine, monkeypatch):
    session = create_engine()
    dispatch = session.sim._dispatch
    seen = []

    def inspect_wire_boundary(method, path, query, payload):
        if method == "POST":
            row = session.journal.order(payload["client_order_id"])
            assert row["state"] == "submitting"
            assert row["wire_authorized"] is True
            assert row["reservation_cash"] == "5.005"
            session.journal.integrity_check()
            seen.append(row["client_order_id"])
        return dispatch(method, path, query, payload)

    monkeypatch.setattr(session.sim, "_dispatch", inspect_wire_boundary)
    cid = submitted(session)
    assert seen == [cid]
    result = session.engine.status()
    assert result["fill_count"] == 0 and result["positions"] == []
    assert result["orders"][0]["filled_qty"] == "0"
    assert result["mode"] == "SIMULATION" and result["provenance"] == "TEST"
    assert result["actual_orders_submitted"] == 0 and not result["live_enabled"]
    assert not result["qualification_established"]
    assert session.engine.reconcile()["reconciled"]
    assert len(mutations(session)) == 1


@pytest.mark.parametrize("change", ["quote_delay", "kill_switch"])
def test_final_risk_recheck_blocks_state_changes_after_durable_reservation(create_engine, monkeypatch, change):
    session = create_engine()
    dispatch = session.sim._dispatch
    quote_count = 0

    def changed_after_reservation(method, path, query, payload):
        nonlocal quote_count
        if path == "/v2/stocks/quotes/latest":
            quote_count += 1
            if quote_count == 2:
                assert session.journal.orders()[0]["state"] == "reserved"
                if change == "quote_delay":
                    session.sim.advance(11)  # Earlier account/clock snapshots are now stale.
                else:
                    session.engine.halt()
        return dispatch(method, path, query, payload)

    monkeypatch.setattr(session.sim, "_dispatch", changed_after_reservation)
    original = decision(session)
    result = session.engine.submit(original)
    assert not result["new_submission"]
    assert result["order"]["state"] == "not_submitted"
    assert result["order"]["wire_authorized"] is False
    assert len(session.journal.orders()) == 1 and not mutations(session)
    repeated = session.engine.submit(original)
    assert repeated["replayed"] and not repeated["new_submission"]
    assert not mutations(session)


def test_full_fill_reconciles_cash_inventory_and_stays_distinct_from_qualification(create_engine):
    session = create_engine()
    cid = submitted(session)
    session.sim.fill(cid, "0.5", "9.99")
    result = session.engine.reconcile()
    assert result["reconciled"], result
    assert result["orders"][0]["state"] == "filled"
    assert result["fill_count"] == 1 and result["positions"][0]["qty"] == "0.5"
    assert session.journal.get("last_account")["cash"] == "95.005"
    assert result["fees_complete"] is False
    assert not result["qualification_established"]


def test_partial_fill_and_cancel_race_preserve_filled_inventory(create_engine):
    session = create_engine()
    cid = submitted(session)
    session.sim.fill(cid, "0.2", "9.99")
    assert session.engine.reconcile()["reconciled"]
    canceled = session.engine.cancel(cid)
    assert canceled["order"]["cancel"]["status"] == "acknowledged"
    assert session.sim.orders[cid]["status"] == "pending_cancel"
    session.sim.fill(cid, "0.1", "9.99")
    pending = session.engine.reconcile()
    assert not pending["reconciled"]
    assert "paper_cancellation_not_terminal" in pending["reasons"]
    blocked = session.engine.submit(decision(session, signal="during-cancel"))
    assert not blocked["new_submission"]
    assert len(mutations(session)) == 1
    session.sim.finish_cancel(cid)
    result = session.engine.reconcile()
    assert result["reconciled"], result
    assert result["orders"][0]["state"] == "canceled"
    assert result["orders"][0]["filled_qty"] == "0.3"
    assert result["positions"][0]["qty"] == "0.3" and result["fill_count"] == 2
    submitted(session, signal="owned-exit", side="sell", quantity=".3", limit_price="9.99")
    assert len(mutations(session, "DELETE")) == 1


def test_duplicate_signal_reconciles_without_reposting_and_changed_terms_refuse(create_engine):
    session = create_engine()
    original = decision(session)
    first = session.engine.submit(original)
    repeat = session.engine.submit(deepcopy(original))
    assert repeat["replayed"] and not repeat["new_submission"]
    assert repeat["order"]["client_order_id"] == first["order"]["client_order_id"]
    with pytest.raises(PaperError, match="duplicate_signal_terms_changed"):
        session.engine.submit({**original, "quantity": ".4"})
    assert len(mutations(session)) == 1 and len(session.journal.orders()) == 1


def test_timeout_after_acceptance_reconciles_the_same_order_after_restart(create_engine):
    session = create_engine()
    original = decision(session)
    session.sim.queue_fault("POST", "/v2/orders", "timeout_after_accept")
    response = session.engine.submit(original)
    cid = response["order"]["client_order_id"]
    assert response["order"]["state"] == "unknown"
    assert session.sim.orders[cid]["status"] == "new"
    session.sim.advance(3)
    create_engine.restart(session)
    recovered = session.engine.reconcile()
    assert recovered["reconciled"], recovered
    assert recovered["orders"][0]["state"] == "new"
    repeat = session.engine.submit(original)
    assert repeat["replayed"] and not repeat["new_submission"]
    assert len(mutations(session)) == 1
    assert session.journal.order(cid)["reservation_cash"] == "5.005"


def test_missing_404_cannot_prove_a_timed_out_accepted_order_absent(create_engine, monkeypatch):
    session = create_engine()
    original = decision(session)
    session.sim.queue_fault("POST", "/v2/orders", "timeout_after_accept")
    response = session.engine.submit(original)
    cid = response["order"]["client_order_id"]
    dispatch = session.sim._dispatch

    def temporarily_hidden(method, path, query, payload):
        if method == "GET" and path == "/v2/orders":
            return Response(200, b"[]")
        if method == "GET" and path == "/v2/orders:by_client_order_id":
            return Response(404)
        return dispatch(method, path, query, payload)

    monkeypatch.setattr(session.sim, "_dispatch", temporarily_hidden)
    session.sim.advance(3)
    assert not session.engine.reconcile()["reconciled"]
    repeat = session.engine.submit(original)
    assert repeat["order"]["state"] == "unknown" and repeat["replayed"]
    assert not session.engine.submit(decision(session, signal="new-while-unknown"))["new_submission"]
    assert len(mutations(session)) == 1 and cid in session.sim.orders
    assert session.journal.order(cid)["reservation_cash"] == "5.005"


@pytest.mark.parametrize("fault", ["timeout_before", "malformed", "server_error"])
def test_uncertain_post_failures_retain_intent_and_never_retry(create_engine, fault):
    session = create_engine()
    original = decision(session)
    session.sim.queue_fault("POST", "/v2/orders", fault)
    result = session.engine.submit(original)
    assert result["order"]["state"] == "unknown"
    session.sim.advance(3)
    repeated = session.engine.submit(original)
    assert repeated["replayed"] and not repeated["new_submission"]
    assert repeated["order"]["reservation_cash"] == "5.005"
    assert len(mutations(session)) == 1


def test_http_rejection_does_not_become_an_accepted_order_or_retry(create_engine, monkeypatch):
    session = create_engine()
    dispatch = session.sim._dispatch
    monkeypatch.setattr(session.sim, "_dispatch", lambda method, path, query, payload:
                        Response(403) if method == "POST" else dispatch(method, path, query, payload))
    original = decision(session)
    result = session.engine.submit(original)
    assert result["order"]["state"] == "http_rejected"
    assert session.sim.orders == {}
    session.sim.advance(3)
    repeat = session.engine.submit(original)
    assert repeat["replayed"] and not repeat["new_submission"]
    assert len(mutations(session)) == 1


def test_post_429_defers_without_acceptance_or_tripping_broker_circuit(create_engine):
    session = create_engine()
    original = decision(session)
    session.sim.queue_fault("POST", "/v2/orders", "rate_limit")
    result = session.engine.submit(original)
    assert result["order"]["state"] == "http_rejected"
    assert session.engine.status()["broker_failures"] == 0
    assert not session.engine.status()["entry_halt"]
    assert session.sim.orders == {}
    session.sim.advance(3)
    assert session.engine.submit(original)["replayed"]
    assert len(mutations(session)) == 1


def test_successful_reads_do_not_clear_repeated_order_rejection_circuit(create_engine, monkeypatch):
    session = create_engine()
    dispatch = session.sim._dispatch
    monkeypatch.setattr(session.sim, "_dispatch", lambda method, path, query, payload:
                        Response(403) if method == "POST" else dispatch(method, path, query, payload))
    for index in range(3):
        result = session.engine.submit(decision(session, signal="rejected-" + str(index)))
        assert result["order"]["state"] == "http_rejected"
        session.sim.advance(10)
        assert session.engine.reconcile()["reconciled"]
        assert session.engine.status()["broker_failures"] == index + 1
    create_engine.restart(session)
    assert session.engine.status()["entry_halt"]
    assert session.engine.status()["mutation_failures"] == 3
    assert not session.engine.submit(decision(session, signal="blocked-fourth"))["new_submission"]
    assert len(mutations(session)) == 3  # No fourth POST after restart.


@pytest.mark.parametrize("synchronous", [False, True])
def test_broker_rejected_order_objects_count_once_and_halt_across_restart(create_engine, monkeypatch, synchronous):
    session = create_engine()
    dispatch = session.sim._dispatch

    def reject_order(method, path, query, payload):
        response = dispatch(method, path, query, payload)
        if synchronous and method == "POST":
            row = session.sim.orders[payload["client_order_id"]]
            row["status"] = "rejected"
            return Response(200, json.dumps(row).encode())
        return response

    monkeypatch.setattr(session.sim, "_dispatch", reject_order)
    for index in range(3):
        result = session.engine.submit(decision(session, signal="native-reject-" + str(index)))
        cid = result["order"]["client_order_id"]
        if not synchronous:
            session.sim.orders[cid]["status"] = "rejected"
            assert not session.engine.reconcile()["reconciled"]
        session.sim.advance(10)
        assert session.engine.reconcile()["reconciled"]
        assert session.engine.status()["mutation_failures"] == index + 1
    create_engine.restart(session)
    assert session.engine.reconcile()["reconciled"]
    assert session.engine.status()["mutation_failures"] == 3
    assert session.engine.status()["entry_halt"]
    assert not session.engine.submit(decision(session, signal="blocked-fourth"))["new_submission"]
    assert len(mutations(session)) == 3


def test_repeated_provider_and_local_throttles_are_separate_from_broker_failure_circuit(create_engine, monkeypatch):
    session = create_engine()
    for _ in range(3):
        session.sim.queue_fault("GET", "/v2/account", "rate_limit")
        assert not session.engine.reconcile()["reconciled"]
        session.sim.advance(3)
    assert session.engine.status()["broker_failures"] == 0
    assert not session.engine.status()["entry_halt"]
    monkeypatch.setattr("atlas.paper.broker.MAX_REQUESTS_PER_MINUTE", 3)
    for _ in range(3):
        session.sim.advance(61)
        assert not session.engine.reconcile()["reconciled"]
    assert session.engine.status()["broker_failures"] == 0
    assert not session.engine.status()["entry_halt"]
    assert not mutations(session)


def test_repeated_transport_failures_persist_a_circuit_halt_across_restart(create_engine):
    session = create_engine()
    for _ in range(3):
        session.sim.queue_fault("GET", "/v2/account", "server_error")
        assert not session.engine.reconcile()["reconciled"]
        session.sim.advance(10)
    assert session.engine.status()["broker_failures"] == 3
    assert session.engine.status()["entry_halt"]
    create_engine.restart(session)
    assert session.engine.status()["entry_halt"]
    assert not session.engine.submit(decision(session))["new_submission"]
    assert not mutations(session)


def test_kill_switch_survives_restart_blocks_entries_but_allows_owned_cancellation(create_engine):
    session = create_engine()
    cid = submitted(session)
    session.engine.halt()
    create_engine.restart(session)
    assert session.engine.status()["kill_switch"]
    with pytest.raises(PaperError, match="kill_switch_active"):
        session.engine.submit(decision(session, signal="after-kill"))
    result = session.engine.cancel(cid)
    assert result["cancellation_requested"]
    session.sim.finish_cancel(cid)
    assert session.engine.reconcile()["reconciled"]
    assert session.engine.status()["kill_switch"]
    assert len(mutations(session)) == 1 and len(mutations(session, "DELETE")) == 1


@pytest.mark.parametrize("cash", ["99", "101"])
def test_unexplained_cash_change_cannot_become_reconciled_pnl_or_new_risk_anchor(create_engine, cash):
    session = create_engine()
    initial_peak = session.journal.get("peak_equity")
    session.sim.cash = cash
    result = session.engine.reconcile()
    assert not result["reconciled"]
    assert session.journal.get("peak_equity") == initial_peak
    assert session.journal.get("last_equity") == "100"
    assert not session.engine.submit(decision(session))["new_submission"]
    assert not mutations(session)


def test_unmanaged_inventory_or_order_blocks_new_execution(create_engine):
    session = create_engine()
    session.sim.holdings["SPY"] = {"qty": "1", "cost_basis": "10"}
    session.sim.cash = "90"
    result = session.engine.reconcile()
    assert not result["reconciled"]
    assert "paper_position_reconciliation_mismatch" in result["reasons"]
    assert not session.engine.submit(decision(session))["new_submission"]
    assert not mutations(session)
    session.journal.close()
    other = create_engine()
    other.sim.request("POST", PAPER_ORIGIN + "/v2/orders", {}, {
        "client_order_id": "unmanaged", "symbol": "SPY", "side": "buy", "qty": "0.5",
        "limit_price": "10", "type": "limit", "time_in_force": "day", "extended_hours": False})
    result = other.engine.reconcile()
    assert not result["reconciled"]
    assert "paper_unmanaged_broker_order" in result["reasons"]
    assert not other.engine.submit(decision(other))["new_submission"]
    assert len(mutations(other)) == 1 and other.journal.orders() == []


def test_replacement_waits_for_terminal_cancel_and_submits_only_unfilled_target(create_engine):
    session = create_engine()
    cid = submitted(session)
    session.sim.fill(cid, ".2", "9.99")
    assert session.engine.reconcile()["reconciled"]
    replacement = decision(session, signal="replacement-1", limit_price="9.99")
    first = session.engine.replace(cid, replacement)
    assert first["replacement_stage"] == "awaiting_terminal_reconciliation"
    assert not first["new_submission"] and len(mutations(session)) == 1
    session.sim.fill(cid, ".1", "9.99")
    session.sim.finish_cancel(cid)
    result = session.engine.replace(cid, replacement)
    assert result["new_submission"]
    assert result["remaining_quantity"] == "0.2"
    assert result["order"]["payload"]["qty"] == "0.2"
    assert result["order"]["replacement_parent"] == cid
    assert len(mutations(session)) == 2 and len(mutations(session, "DELETE")) == 1
    repeated = session.engine.replace(cid, replacement)
    assert repeated["replayed"] and not repeated["new_submission"]
    assert len(mutations(session)) == 2


def test_replacement_cannot_expand_change_exposure_or_reset_a_committed_plan(create_engine):
    session = create_engine()
    cid = submitted(session)
    with pytest.raises(PaperError, match="cannot_expand_or_change_exposure"):
        session.engine.replace(cid, decision(session, signal="replacement", quantity=".6"))
    replacement = decision(session, signal="replacement", limit_price="9.99")
    session.engine.replace(cid, replacement)
    with pytest.raises(PaperError, match="replacement_terms_changed"):
        session.engine.replace(cid, {**replacement, "quantity": ".4"})
    assert len(mutations(session)) == 1


def test_full_fill_during_cancel_means_replacement_target_is_already_satisfied(create_engine):
    session = create_engine()
    cid = submitted(session)
    replacement = decision(session, signal="replacement")
    session.engine.replace(cid, replacement)
    session.sim.fill(cid, ".5", "9.99")
    result = session.engine.replace(cid, replacement)
    assert result["replacement_stage"] == "target_already_filled"
    assert not result["new_submission"] and len(mutations(session)) == 1


@pytest.mark.parametrize("reason", ["session_open", "order_notional", "quote_fresh"])
def test_real_risk_pipeline_blocks_closed_session_caps_and_stale_quotes(create_engine, monkeypatch, reason):
    session = create_engine()
    proposed = decision(session)
    if reason == "session_open":
        session.sim.is_open = False
    elif reason == "order_notional":
        proposed["quantity"] = "1"  # $10 plus reserved fees exceeds the $10 cap.
    else:
        dispatch = session.sim._dispatch

        def stale_quote(method, path, query, payload):
            response = dispatch(method, path, query, payload)
            if path == "/v2/stocks/quotes/latest":
                body = json.loads(response.body)
                body["quotes"]["SPY"]["t"] = datetime.fromtimestamp(
                    session.sim.clock() - 11, timezone.utc).isoformat()
                return Response(200, json.dumps(body).encode())
            return response

        monkeypatch.setattr(session.sim, "_dispatch", stale_quote)
    result = session.engine.submit(proposed)
    assert not result["new_submission"]
    assert reason in result["risk"]["reasons"]
    assert not mutations(session) and session.journal.orders() == []


def test_position_cap_uses_reconciled_filled_inventory(create_engine):
    policy = PaperRiskPolicy(allowed_strategy_ids=("test-strategy",), max_position_notional="10")
    session = create_engine(policy=policy)
    cid = submitted(session, quantity=".9")
    session.sim.fill(cid, ".9", "10")
    assert session.engine.reconcile()["reconciled"]
    result = session.engine.submit(decision(session, signal="increase", quantity=".2"))
    assert not result["new_submission"]
    assert "position_exposure" in result["risk"]["reasons"]
    assert len(mutations(session)) == 1


def test_observed_daily_loss_halts_entries_but_allows_checked_owned_reduction(create_engine):
    session = create_engine()
    cid = submitted(session, quantity=".9")
    session.sim.fill(cid, ".9", "10")
    assert session.engine.reconcile()["reconciled"]
    session.sim.quotes["SPY"].update(bid="7", ask="7.01")
    result = session.engine.reconcile()
    assert result["reconciled"] and result["entry_halt"]
    blocked = session.engine.submit(decision(session, signal="new-entry", limit_price="7.01"))
    assert not blocked["new_submission"]
    assert "daily_loss" in blocked["risk"]["reasons"]
    submitted(session, signal="reduce-exposure", side="sell", quantity=".5", limit_price="7")
    assert len(mutations(session)) == 2


def test_corrupt_restored_journal_is_refused_without_reinitialization_or_broker_post(create_engine):
    session = create_engine()
    submitted(session)
    session.journal.close()
    path = session.path / "paper.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE metadata SET value='\"101\"' WHERE key='peak_equity'")
    damaged = path.read_bytes()
    with pytest.raises((PaperError, sqlite3.DatabaseError)):
        PaperJournal(session.path, session.binding, now=session.sim.clock())
    assert path.read_bytes() == damaged
    assert len(mutations(session)) == 1
