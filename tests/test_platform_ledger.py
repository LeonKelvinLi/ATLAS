"""Durable execution invariants with synthetic SQLite state and no broker."""

from concurrent.futures import ThreadPoolExecutor
from decimal import localcontext
import json
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest

from atlas.ledger import (
    ConflictError, CorruptLedgerError, Ledger, LedgerError, canonical_json, digest,
)


ACCOUNT = "a" * 64


def proposal(index=1, **updates):
    value = {
        "id": f"proposal-{index}", "signal_id": f"signal-{index}",
        "account_fingerprint": ACCOUNT, "mode": "paper", "policy_hash": "p" * 64,
        "created_at": 100, "expires_at": 1000,
        "strategy_id": "fixture", "strategy_version": "1", "quote_timestamp": 100,
        "order": {"symbol": "TEST", "side": "buy", "quantity": "2", "limit_price": "10.50"},
        "evidence": {"source": "fixture", "timestamp": 100},
    }
    value.update(updates)
    return value


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "state" / "execution.db", "paper", clock=lambda: 200) as value:
        yield value


def save(ledger, index=1, **updates):
    return ledger.save_proposal(proposal(index, **updates), f"key-{index}")


def test_canonical_hash_is_stable_and_rejects_coerced_json_keys():
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})
    with pytest.raises(ValueError):
        canonical_json({1: "ambiguous"})
    for bad in (float("nan"), float("inf"), object()):
        with pytest.raises(ValueError):
            canonical_json({"bad": bad})


def test_exact_proposal_replay_is_immutable_and_conflicting_key_refuses(ledger):
    original = save(ledger)
    assert ledger.save_proposal(original, "key-1") == original
    original["order"]["quantity"] = "999"
    assert ledger.get_proposal("proposal-1")["order"]["quantity"] == "2"
    with pytest.raises(ConflictError, match="idempotency"):
        ledger.save_proposal(original, "key-1")
    with pytest.raises(ConflictError, match="identifier"):
        ledger.save_proposal(proposal(), "different-key")
    assert ledger.get_proposal_by_idempotency_key("key-1") == proposal()
    assert len(ledger.list_proposals()) == 1
    assert len(ledger.list_events()) == 1


@pytest.mark.parametrize("field", ["account_id", "account_number", "password", "access_token", "signing_key"])
def test_proposals_refuse_credential_fields(ledger, field):
    value = proposal()
    value["evidence"][field] = "private-fixture"
    with pytest.raises(ValueError, match="credential or raw account"):
        ledger.save_proposal(value, "key")
    assert ledger.list_proposals() == []


def test_disk_scope_cannot_be_changed(tmp_path):
    path = tmp_path / "execution.db"
    with Ledger(path, "paper") as first:
        save(first)
    with pytest.raises(LedgerError, match="different scope"):
        Ledger(path, "live")
    with Ledger(path, "paper") as again:
        assert again.get_proposal("proposal-1") == proposal()


def test_paths_require_explicit_external_state():
    with pytest.raises(ValueError, match="absolute"):
        Ledger("execution.db", "paper")
    inside = Path(__file__).resolve().parents[1] / "must-not-create-ledger.db"
    with pytest.raises(ValueError, match="outside"):
        Ledger(inside, "paper")
    assert not inside.exists()
    with Ledger(":memory:", "paper") as memory:
        save(memory)
        assert len(memory.list_proposals()) == 1


def test_claim_is_committed_before_return_and_survives_restart(tmp_path):
    path = tmp_path / "execution.db"
    with Ledger(path, "paper") as first:
        save(first)
        save(first, 2)
        claimed = first.claim_intent("proposal-1", 120, "mcp:fixture")
        assert str(uuid.UUID(claimed["client_order_id"])) == claimed["client_order_id"]
        with Ledger(path, "paper") as reader:
            assert reader.get_intent("proposal-1")["client_order_id"] == claimed["client_order_id"]
    with Ledger(path, "paper") as restarted:
        repeat = restarted.claim_intent("proposal-1", 5000, "mcp:retry")
        assert repeat["status"] == "submitting" and repeat["replayed"] is True
        assert repeat["client_order_id"] == claimed["client_order_id"]
        with pytest.raises(ConflictError, match="reconciliation"):
            restarted.claim_intent("proposal-2", 125, "mcp:fixture")


def test_same_proposal_concurrent_claims_create_one_intent(tmp_path):
    path = tmp_path / "execution.db"
    with Ledger(path, "paper") as creator:
        save(creator)
    barrier = threading.Barrier(2)

    def worker(_):
        with Ledger(path, "paper") as connection:
            barrier.wait(timeout=5)
            return connection.claim_intent("proposal-1", 120, "mcp:fixture")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert len({result["client_order_id"] for result in results}) == 1
    assert sorted(result["replayed"] for result in results) == [False, True]
    with Ledger(path, "paper") as reader:
        assert len(reader.list_intents()) == 1
        assert sum(row["event"] == "intent_claimed" for row in reader.list_events()) == 1


def test_concurrent_account_claims_cannot_both_pass_conflict_check(tmp_path):
    path = tmp_path / "execution.db"
    with Ledger(path, "paper") as creator:
        save(creator)
        save(creator, 2)
    barrier = threading.Barrier(2)

    def worker(index):
        with Ledger(path, "paper") as connection:
            barrier.wait(timeout=5)
            try:
                return connection.claim_intent(f"proposal-{index}", 120, "mcp:fixture")["status"]
            except ConflictError:
                return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, (1, 2)))
    assert sorted(results) == ["blocked", "submitting"]


@pytest.mark.parametrize("status", ["submitting", "accepted", "unknown", "cancel_pending"])
def test_all_outstanding_states_block_the_account(ledger, status):
    save(ledger)
    save(ledger, 2)
    ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    if status == "cancel_pending":
        ledger.complete_intent("proposal-1", "accepted", "broker-1")
    if status != "submitting":
        ledger.complete_intent("proposal-1", status, "broker-1")
    with pytest.raises(ConflictError, match="unresolved"):
        ledger.claim_intent("proposal-2", 201, "mcp:fixture")
    assert ledger.claim_intent("proposal-1", 201, "mcp:retry")["replayed"]


def test_terminal_replay_never_reclaims_and_signal_cannot_be_duplicated(ledger):
    save(ledger)
    save(ledger, 2, signal_id="signal-1")
    save(ledger, 3)
    original = ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    ledger.complete_intent("proposal-1", "filled", "broker-1", {"realized_pnl": "0.20"})
    again = ledger.claim_intent("proposal-1", 5000, "mcp:retry")
    assert again["status"] == "filled" and again["replayed"]
    assert again["client_order_id"] == original["client_order_id"]
    with pytest.raises(ConflictError, match="signal already"):
        ledger.claim_intent("proposal-2", 201, "mcp:fixture")
    assert ledger.claim_intent("proposal-3", 201, "mcp:fixture")["replayed"] is False


def test_conflicts_are_scoped_to_account(ledger):
    save(ledger)
    save(ledger, 2, account_fingerprint="b" * 64)
    ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    assert ledger.claim_intent("proposal-2", 120, "mcp:fixture")["status"] == "submitting"


def test_has_pending_checks_full_ledger_and_supports_current_proposal_exclusion(ledger):
    save(ledger)
    ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    assert ledger.has_pending(ACCOUNT)
    assert not ledger.has_pending(ACCOUNT, exclude_proposal_id="proposal-1")
    # Observation limits cannot affect risk decisions.
    ledger.list_intents = lambda limit=100: []
    assert ledger.has_pending(ACCOUNT)
    ledger.complete_intent("proposal-1", "cancelled", "broker-1")
    assert not ledger.has_pending(ACCOUNT)


def test_nonterminal_partial_fill_update_is_audited_and_still_blocks(ledger):
    save(ledger)
    ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    ledger.complete_intent("proposal-1", "accepted", "broker-1", {"filled_quantity": "0"})
    revised = ledger.complete_intent("proposal-1", "accepted", "broker-1",
                                     {"filled_quantity": "1"}, now=201)
    assert revised["details"]["filled_quantity"] == "1"
    assert ledger.has_pending(ACCOUNT)
    assert sum(row["event"] == "intent_accepted" for row in ledger.list_events()) == 2


def test_unknown_record_retains_last_acknowledged_partial_fill(ledger):
    save(ledger)
    ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    ledger.complete_intent("proposal-1", "accepted", "broker-1",
        {"filled_quantity": "1", "average_fill_price": "10.50", "fees": "0.01"}, now=121)
    unknown = ledger.complete_intent("proposal-1", "unknown",
        details={"reason_code": "history_temporarily_unavailable"}, now=122)
    assert unknown["details"]["filled_quantity"] == "1"
    assert unknown["details"]["average_fill_price"] == "10.50"
    assert unknown["details"]["reason_code"] == "history_temporarily_unavailable"
    assert ledger.has_pending(ACCOUNT)


@pytest.mark.parametrize("details", [
    {"filled_quantity": "0.5"}, {"realized_pnl": "0"},
    {"filled_quantity": "1", "broker_order_id": "different-order"},
    {"filled_quantity": "1", "client_order_id": "different-client"},
])
def test_terminal_commit_cannot_erase_fill_or_change_response_identity(ledger, details):
    save(ledger)
    ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    ledger.complete_intent("proposal-1", "accepted", "broker-1", {"filled_quantity": "1"}, now=121)
    with pytest.raises(ConflictError):
        ledger.complete_intent("proposal-1", "cancelled", "broker-1", details, now=122)
    retained = ledger.get_intent("proposal-1")
    assert retained["status"] == "accepted" and retained["details"]["filled_quantity"] == "1"


def test_preexisting_empty_file_is_not_a_new_ledger(tmp_path):
    path = tmp_path / "execution.db"
    path.touch()
    with pytest.raises(CorruptLedgerError, match="existing execution journal is empty"):
        Ledger(path, "paper")
    assert path.read_bytes() == b""


def test_existing_sqlite_file_with_missing_schema_is_not_reinitialized(tmp_path):
    path = tmp_path / "execution.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE lost_schema(value)")
    conn.execute("DROP TABLE lost_schema")
    conn.commit()
    conn.close()
    with pytest.raises(CorruptLedgerError, match="has no schema"):
        Ledger(path, "paper")


@pytest.mark.parametrize("now", [99, 1000, 2000])
def test_expired_or_future_proposal_never_creates_intent(ledger, now):
    save(ledger)
    with pytest.raises(ConflictError, match="expired or not yet"):
        ledger.claim_intent("proposal-1", now, "mcp:fixture")
    assert ledger.get_intent("proposal-1") is None


def test_transition_and_broker_identity_cannot_be_rewritten(ledger):
    save(ledger)
    ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    with pytest.raises(ValueError, match="broker order identifier"):
        ledger.complete_intent("proposal-1", "accepted")
    ledger.complete_intent("proposal-1", "accepted", "broker-1")
    with pytest.raises(ConflictError, match="identity"):
        ledger.complete_intent("proposal-1", "filled", "broker-2")
    with pytest.raises(ConflictError, match="transition"):
        ledger.complete_intent("proposal-1", "submitting")
    final = ledger.complete_intent("proposal-1", "filled", details={"realized_pnl": "0"})
    assert ledger.complete_intent("proposal-1", "filled", details={"realized_pnl": "0"})["replayed"]
    with pytest.raises(ConflictError, match="rewritten"):
        ledger.complete_intent("proposal-1", "cancelled")
    assert ledger.get_intent("proposal-1")["status"] == final["status"]


def test_claim_audit_failure_rolls_back_intent(ledger, monkeypatch):
    save(ledger)

    def unavailable(*args):
        raise RuntimeError("fake durable event failure")

    monkeypatch.setattr(ledger, "_event", unavailable)
    with pytest.raises(RuntimeError, match="durable event"):
        ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    assert ledger.get_intent("proposal-1") is None


def test_failed_commit_directory_sync_never_returns_permission_to_submit(tmp_path, monkeypatch):
    path = tmp_path / "execution.db"
    with Ledger(path, "paper") as writer:
        save(writer)
        save(writer, 2)

        def sync_failed(_):
            raise OSError("fixture directory synchronization failure")

        monkeypatch.setattr(writer, "_sync_directory", sync_failed)
        with pytest.raises(CorruptLedgerError, match="commit may exist"):
            writer.claim_intent("proposal-1", 120, "mcp:fixture")
    with Ledger(path, "paper") as restarted:
        assert restarted.get_intent("proposal-1")["status"] == "submitting"
        with pytest.raises(ConflictError, match="reconciliation"):
            restarted.claim_intent("proposal-2", 121, "mcp:fixture")


def test_live_modes_share_conflict_namespace_and_cannot_enter_paper_database(tmp_path):
    with Ledger(tmp_path / "live.db", "live") as live:
        save(live, mode="live-assisted")
        save(live, 2, mode="live-auto")
        live.claim_intent("proposal-1", 120, "mcp:fixture")
        with pytest.raises(ConflictError, match="reconciliation"):
            live.claim_intent("proposal-2", 121, "mcp:fixture")
    with Ledger(tmp_path / "paper.db", "paper") as paper:
        with pytest.raises(ValueError, match="journal scope"):
            save(paper, mode="live-assisted")


def test_daily_stats_count_intents_and_keep_unknown_pnl_unknown(ledger):
    for index, details in ((1, {"realized_pnl": "0.10"}), (2, {"realized_pnl": "0.20"}), (3, {})):
        save(ledger, index)
        ledger.claim_intent(f"proposal-{index}", 120, "mcp:fixture")
        ledger.complete_intent(f"proposal-{index}", "filled", f"broker-{index}", details)
    save(ledger, 4)
    ledger.claim_intent("proposal-4", 201, "mcp:fixture")
    view = ledger.daily_stats(ACCOUNT, 300)
    assert view["intents_count"] == 4
    assert view["settlements_count"] == 3
    assert view["realized_pnl"] is None
    assert view["known_realized_pnl"] == "0.30"
    assert view["unknown_realized_pnl_count"] == 1
    empty = ledger.daily_stats("b" * 64, 300)
    assert empty["intents_count"] == 0 and empty["realized_pnl"] is None


def test_realized_pnl_day_uses_settlement_time_not_submission_day(ledger):
    save(ledger, expires_at=90000)
    ledger.claim_intent("proposal-1", 86300, "mcp:fixture")
    ledger.complete_intent("proposal-1", "filled", "broker-1", {"realized_pnl": "-2"}, now=86500)
    view = ledger.daily_stats(ACCOUNT, 86500)
    assert view["intents_count"] == 0
    assert view["realized_pnl"] == "-2"
    assert view["accounting_basis"] == "journal_update_time_not_broker_fill_time"
    assert view["dates_are_fill_dates"] is False


def test_daily_pnl_summation_is_independent_of_ambient_decimal_precision(ledger):
    for index, pnl in enumerate(("10.01", "0.01"), 1):
        save(ledger, index)
        ledger.claim_intent(f"proposal-{index}", 120, "mcp:fixture")
        ledger.complete_intent(f"proposal-{index}", "filled", f"broker-{index}", {"realized_pnl": pnl})
    with localcontext() as context:
        context.prec = 2
        assert ledger.daily_stats(ACCOUNT, 300)["realized_pnl"] == "10.02"
        assert context.prec == 2


def test_event_and_response_views_redact_credentials_before_persistence(ledger):
    ledger.record_event("risk_rejected", {"token": "private-value", "nested": {"password": "private-value"}},
                        now=120, caller="mcp:fixture")
    event = ledger.list_events()[0]
    assert event["details"] == {"token": "[REDACTED]", "nested": {"password": "[REDACTED]"}}
    assert "private-value" not in ledger._conn.execute("SELECT details FROM events").fetchone()[0]


def test_corrupt_file_is_preserved_and_never_reinitialized(tmp_path):
    path = tmp_path / "execution.db"
    before = b"incomplete not-a-database journal"
    path.write_bytes(before)
    with pytest.raises(CorruptLedgerError):
        Ledger(path, "paper")
    assert path.read_bytes() == before


def test_tampered_proposal_fails_closed_on_read_and_restart(tmp_path):
    path = tmp_path / "execution.db"
    with Ledger(path, "paper") as writer:
        save(writer)
        writer._conn.execute("UPDATE proposals SET payload=?", (json.dumps(proposal(order={"quantity": "999"})),))
        with pytest.raises(CorruptLedgerError, match="digest"):
            writer.get_proposal("proposal-1")
        with pytest.raises(CorruptLedgerError):
            writer.list_intents()
    with pytest.raises(CorruptLedgerError, match="digest"):
        Ledger(path, "paper")


def test_incomplete_intent_json_is_not_ignored(ledger):
    save(ledger)
    ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    ledger._conn.execute("UPDATE intents SET details='{' WHERE proposal_id='proposal-1'")
    with pytest.raises(CorruptLedgerError, match="intent is invalid"):
        ledger.get_intent("proposal-1")


def test_backup_recovers_committed_wal_and_unknown_block(tmp_path):
    path = tmp_path / "execution.db"
    restored = tmp_path / "backup" / "execution.db"
    with Ledger(path, "paper") as writer:
        writer._conn.execute("PRAGMA wal_autocheckpoint=0")
        save(writer)
        save(writer, 2)
        writer.claim_intent("proposal-1", 120, "mcp:fixture")
        writer.complete_intent("proposal-1", "unknown", details={"reason_code": "timeout"}, now=121)
        assert writer.backup(restored) == restored
    with Ledger(restored, "paper") as backup:
        assert backup.get_intent("proposal-1")["status"] == "unknown"
        with pytest.raises(ConflictError, match="reconciliation"):
            backup.claim_intent("proposal-2", 130, "mcp:fixture")


def test_corruption_never_falls_back_to_plain_copy(ledger, tmp_path):
    save(ledger)
    destination = tmp_path / "preserved-backup.db"
    destination.write_bytes(b"previous-backup-fixture")
    ledger._conn.execute("UPDATE proposals SET digest='not-valid'")
    with pytest.raises(CorruptLedgerError):
        ledger.backup(destination)
    assert destination.read_bytes() == b"previous-backup-fixture"
    assert not list(tmp_path.glob(".atlas-ledger-*"))


def prepare_cancel(ledger, index=2, **changes):
    updates = {
        "kind": "cancel", "signal_id": "cancel:proposal-1",
        "order": {"target_proposal_id": "proposal-1", "broker_order_id": "broker-1"},
    }
    updates.update(changes)
    return save(ledger, index, **updates)


def open_target(ledger):
    save(ledger)
    ledger.claim_intent("proposal-1", 120, "mcp:fixture")
    ledger.complete_intent("proposal-1", "accepted", "broker-1", {"filled_quantity": "0"}, now=121)


@pytest.mark.parametrize("result,child_status,target_status", [
    ("cancelled", "cancelled", "cancelled"),
    ("filled", "rejected", "filled"),
    ("accepted", "rejected", "accepted"),
    ("unknown", "unknown", "accepted"),
])
def test_cancellation_claims_once_and_resolves_both_records(ledger, result, child_status, target_status):
    open_target(ledger)
    prepare_cancel(ledger)
    claimed = ledger.claim_cancellation("proposal-2", "proposal-1", 122, "mcp:fixture")
    assert claimed["status"] == "cancel_pending"
    assert str(uuid.UUID(claimed["client_order_id"])) == claimed["client_order_id"]
    assert ledger.get_intent("proposal-1")["status"] == "accepted"
    replay = ledger.claim_cancellation("proposal-2", "proposal-1", 123, "mcp:retry")
    assert replay["replayed"] and replay["client_order_id"] == claimed["client_order_id"]
    resolved = ledger.complete_cancellation("proposal-2", result, {"realized_pnl": "0.10"}, now=124)
    assert resolved["cancellation"]["status"] == child_status
    assert resolved["target"]["status"] == target_status
    assert ledger.has_pending(ACCOUNT) == (result in {"accepted", "unknown"})
    if result in {"filled", "cancelled"}:
        stats = ledger.daily_stats(ACCOUNT, 130)
        assert stats["intents_count"] == 2
        assert stats["settlements_count"] == 1
        assert stats["realized_pnl"] == "0.10"


def test_cancel_state_uses_only_cancellation_gateway(ledger):
    open_target(ledger)
    prepare_cancel(ledger)
    with pytest.raises(ConflictError, match="claim_cancellation"):
        ledger.claim_intent("proposal-2", 122, "mcp:fixture")
    ledger.claim_cancellation("proposal-2", "proposal-1", 122, "mcp:fixture")
    with pytest.raises(ConflictError, match="complete_cancellation"):
        ledger.complete_intent("proposal-2", "cancelled", "broker-1")


@pytest.mark.parametrize("changes", [
    {"account_fingerprint": "b" * 64},
    {"order": {"target_proposal_id": "proposal-1", "broker_order_id": "different"}},
    {"order": {"target_proposal_id": "proposal-3", "broker_order_id": "broker-1"}},
])
def test_cancel_must_match_target_identity(ledger, changes):
    open_target(ledger)
    prepare_cancel(ledger, **changes)
    with pytest.raises(ConflictError):
        ledger.claim_cancellation("proposal-2", "proposal-1", 122, "mcp:fixture")
    assert ledger.get_intent("proposal-2") is None


def test_unknown_target_must_reconcile_before_cancellation(ledger):
    open_target(ledger)
    ledger.complete_intent("proposal-1", "unknown", details={"reason_code": "stale-status"}, now=122)
    prepare_cancel(ledger)
    with pytest.raises(ConflictError, match="matching acknowledged"):
        ledger.claim_cancellation("proposal-2", "proposal-1", 123, "mcp:fixture")


def test_cancellation_block_survives_restart_and_reconciles_without_retry(tmp_path):
    path = tmp_path / "execution.db"
    with Ledger(path, "paper") as first:
        open_target(first)
        prepare_cancel(first)
        claimed = first.claim_cancellation("proposal-2", "proposal-1", 122, "mcp:fixture")
        first.complete_cancellation("proposal-2", "unknown", {"reason_code": "timeout"}, now=123)
    with Ledger(path, "paper") as restarted:
        repeat = restarted.claim_cancellation("proposal-2", "proposal-1", 124, "mcp:retry")
        assert repeat["status"] == "unknown" and repeat["replayed"]
        assert repeat["client_order_id"] == claimed["client_order_id"]
        assert restarted.has_pending(ACCOUNT)
        restarted.complete_cancellation("proposal-2", "cancelled", {"realized_pnl": "0"}, now=125)
        assert not restarted.has_pending(ACCOUNT)


def test_second_cancellation_cannot_bypass_existing_pending_child(ledger):
    open_target(ledger)
    prepare_cancel(ledger)
    prepare_cancel(ledger, 3)
    ledger.claim_cancellation("proposal-2", "proposal-1", 122, "mcp:fixture")
    with pytest.raises(ConflictError, match="another account execution"):
        ledger.claim_cancellation("proposal-3", "proposal-1", 123, "mcp:fixture")


def test_cancellation_resolution_rolls_back_both_rows_on_audit_failure(ledger, monkeypatch):
    open_target(ledger)
    prepare_cancel(ledger)
    ledger.claim_cancellation("proposal-2", "proposal-1", 122, "mcp:fixture")
    original = ledger._event
    count = 0

    def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("fixture audit write failed")
        original(*args)

    monkeypatch.setattr(ledger, "_event", fail_second)
    with pytest.raises(RuntimeError, match="audit write failed"):
        ledger.complete_cancellation("proposal-2", "cancelled", {"realized_pnl": "0"}, now=123)
    assert ledger.get_intent("proposal-1")["status"] == "accepted"
    assert ledger.get_intent("proposal-2")["status"] == "cancel_pending"


def test_cancellation_losing_fill_race_does_not_duplicate_or_overwrite_pnl(ledger):
    open_target(ledger)
    prepare_cancel(ledger)
    ledger.claim_cancellation("proposal-2", "proposal-1", 122, "mcp:fixture")
    ledger.complete_intent("proposal-1", "filled", details={"realized_pnl": "1.23"}, now=123)
    ledger.complete_cancellation("proposal-2", "filled", {"realized_pnl": "1.23"}, now=124)
    assert ledger.daily_stats(ACCOUNT, 130)["realized_pnl"] == "1.23"
    assert ledger.get_intent("proposal-2")["status"] == "rejected"
