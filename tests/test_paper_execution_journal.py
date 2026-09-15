"""Adversarial paper journal/model fixtures in private temporary directories."""

from copy import deepcopy
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import json
import shutil
from uuid import UUID

import pytest

from atlas.ledger import digest
from atlas.paper import journal as module
from atlas.paper.journal import PaperJournal
from atlas.paper.models import PaperError, client_order_id, decision, payload, validate_order


NOW = 1789560000.5
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_account_locks(tmp_path, monkeypatch):
    root = tmp_path / 'account-locks'
    monkeypatch.setattr(module, '_account_lock_root', lambda: root)
    return root


@pytest.fixture
def binding():
    return {"mode": "SIMULATION", "account_fingerprint": "a" * 64, "strategy_sha256": "b" * 64,
            "policy_sha256": "c" * 64, "allocated_principal": "100"}


@pytest.fixture
def reviewed():
    return decision({"signal_id": "signal-1", "strategy_id": "paper_test", "strategy_sha256": "b" * 64,
                     "data_sha256": "d" * 64, "symbol": "SPY", "side": "buy", "quantity": "0.09",
                     "limit_price": "100", "signal_time": NOW, "expires_at": NOW + 60})


@pytest.fixture
def intent(binding, reviewed):
    identifier = client_order_id(binding, reviewed)
    return {"client_order_id": identifier, "decision": reviewed, "decision_sha256": digest(reviewed),
            "payload": payload(reviewed, identifier), "created_at": NOW, "phase": "reserved"}


@pytest.fixture
def observation(intent):
    return {**intent["payload"], "id": "85bdf031-0261-4db5-9119-87c5c7632c8e", "asset_class": "us_equity",
            "status": "partially_filled", "filled_qty": "0.02", "filled_avg_price": "100",
            "created_at": NOW, "updated_at": NOW + 1, "replaces": None, "replaced_by": None}


def test_create_commit_reopen_checks_binding_and_private_files(tmp_path, binding, intent):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        with journal.transaction("intent_reserved", {"client_id": intent["client_order_id"]}, now=NOW + 1):
            journal.put_order(intent, new=True)
        assert journal.integrity_check()
        assert journal.by_signal(intent["decision"]["signal_id"]) == intent
    with PaperJournal(path, binding) as journal:
        assert journal.order(intent["client_order_id"]) == intent
        assert len(journal.events()) == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(child.stat().st_mode) == 0o600 for child in path.iterdir())


@pytest.mark.parametrize("key,value", [("mode", "PAPER"), ("account_fingerprint", "e" * 64),
                                       ("strategy_sha256", "e" * 64), ("policy_sha256", "e" * 64),
                                       ("allocated_principal", "99")])
def test_existing_binding_cannot_change(tmp_path, binding, key, value):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW):
        pass
    before = (path / "paper.sqlite3").read_bytes()
    with pytest.raises(PaperError, match="binding"):
        PaperJournal(path, {**binding, key: value})
    assert (path / "paper.sqlite3").read_bytes() == before
    with PaperJournal(path, binding) as journal:
        assert journal.integrity_check()


def test_missing_state_reopen_does_not_create_directory(tmp_path, binding):
    path = tmp_path / "missing"
    with pytest.raises((PaperError, OSError)):
        PaperJournal(path, binding)
    assert not path.exists()


def test_existing_empty_directory_reopen_does_not_create_lock_or_database(tmp_path, binding):
    path = tmp_path / "unrelated-empty"
    path.mkdir(mode=0o700)
    with pytest.raises((PaperError, OSError)):
        PaperJournal(path, binding)
    assert list(path.iterdir()) == []


def test_missing_lock_is_not_reconstructed_on_reopen(tmp_path, binding):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW):
        pass
    (path / ".writer.lock").unlink()
    before = (path / "paper.sqlite3").read_bytes()
    with pytest.raises((PaperError, OSError)):
        PaperJournal(path, binding)
    assert not (path / ".writer.lock").exists()
    assert (path / "paper.sqlite3").read_bytes() == before


def test_empty_database_is_not_reseeded(tmp_path, binding):
    path = tmp_path / "paper-state"
    path.mkdir(mode=0o700)
    for name in (".writer.lock", "paper.sqlite3"):
        (path / name).touch(mode=0o600)
    with pytest.raises(PaperError, match="empty"):
        PaperJournal(path, binding)
    assert (path / "paper.sqlite3").read_bytes() == b""


def test_initialization_refuses_existing_nonempty_state(tmp_path, binding):
    path = tmp_path / "old-state"
    path.mkdir(mode=0o700)
    old = path / "journal.db"
    old.write_bytes(b"untouched legacy bytes")
    with pytest.raises(PaperError):
        PaperJournal(path, binding, create=True, now=NOW)
    assert set(p.name for p in path.iterdir()) == {"journal.db"}
    assert old.read_bytes() == b"untouched legacy bytes"


@pytest.mark.parametrize("git_marker", ["directory", "worktree_file"])
def test_state_cannot_be_initialized_inside_another_checkout(tmp_path, binding, git_marker):
    checkout = tmp_path / "another-checkout"
    checkout.mkdir()
    marker = checkout / ".git"
    if git_marker == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: /synthetic/other-git-dir\n")
    selected = checkout / "new-paper-state"
    with pytest.raises(PaperError, match="isolated_state"):
        PaperJournal(selected, binding, create=True, now=NOW)
    assert not selected.exists()


@pytest.mark.parametrize("which", ["directory", "database", "lock", "wal"])
def test_symlinks_never_redirect_state_reads_or_writes(tmp_path, binding, which):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW):
        pass
    outside = tmp_path / "outside"
    outside.write_bytes(b"must remain unchanged")
    outside.chmod(0o600)
    if which == "directory":
        linked = tmp_path / "linked"
        linked.symlink_to(path, target_is_directory=True)
        selected = linked
    else:
        target = path / {"database": "paper.sqlite3", "lock": ".writer.lock", "wal": "paper.sqlite3-wal"}[which]
        target.unlink(missing_ok=True)
        target.symlink_to(outside)
        selected = path
    with pytest.raises((PaperError, OSError, sqlite3.Error)):
        PaperJournal(selected, binding)
    assert outside.read_bytes() == b"must remain unchanged"


@pytest.mark.parametrize("corruption", ["state", "event", "tail_deleted", "database"])
def test_corruption_is_detected_without_reinitialization(tmp_path, binding, corruption):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        with journal.transaction("mark", {}, now=NOW + 1):
            journal.set("anchor", "100")
    db_path = path / "paper.sqlite3"
    if corruption == "database":
        db_path.write_bytes(b"not sqlite data")
    else:
        with sqlite3.connect(db_path) as db:
            if corruption == "state": db.execute("UPDATE metadata SET value='\"200\"' WHERE key='anchor'")
            if corruption == "event": db.execute("UPDATE events SET body='{}' WHERE id=1")
            if corruption == "tail_deleted": db.execute("DELETE FROM events WHERE id=2")
    before = db_path.read_bytes()
    with pytest.raises((PaperError, sqlite3.Error)):
        PaperJournal(path, binding)
    assert db_path.read_bytes() == before


def test_immutable_order_and_unique_signal_are_transactional(tmp_path, binding, intent):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        with journal.transaction("reserved", {}, now=NOW + 1):
            journal.put_order(intent, new=True)
        changed = deepcopy(intent)
        changed["decision"]["quantity"] = "0.08"
        changed["decision_sha256"] = digest(changed["decision"])
        with pytest.raises(PaperError, match="immutable"):
            with journal.transaction("bad_update", {}, now=NOW + 2):
                journal.put_order(changed)
    with PaperJournal(path, binding) as journal:
        assert journal.order(intent["client_order_id"]) == intent
        with pytest.raises(sqlite3.IntegrityError):
            with journal.transaction("duplicate", {}, now=NOW + 3):
                journal.put_order(intent, new=True)
    with PaperJournal(path, binding) as journal:
        assert len(journal.orders()) == 1 and len(journal.events()) == 2


def test_fill_id_cannot_be_reused_with_changed_economics(tmp_path, binding):
    path = tmp_path / "paper-state"
    fill = {"id": "fill-1", "qty": "0.01", "price": "100"}
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        with journal.transaction("fill", {}, now=NOW + 1):
            journal.put_fill(fill)
        with journal.transaction("repeat_observation", {}, now=NOW + 2):
            journal.put_fill(fill)
        assert journal.fills() == [fill]
        with pytest.raises(PaperError, match="fill_identity"):
            with journal.transaction("bad_fill", {}, now=NOW + 3):
                journal.put_fill({**fill, "price": "50"})
    with PaperJournal(path, binding) as journal:
        assert journal.fills() == [fill]


@pytest.mark.parametrize("private_body", [
    {"api_secret": "synthetic-secret-fixture"},
    {"apiSecret": "synthetic-secret-fixture"},
    {"APCA-API-SECRET-KEY": "synthetic-secret-fixture"},
    {"nested": [{"clientSecret": "synthetic-secret-fixture"}]},
    {"nested": {"accessToken": "synthetic-token-fixture"}},
    {"unexpected": "-----BEGIN " + "EC PRIVATE KEY-----\nsynthetic-fixture"},
    {"unexpected": "Bearer synthetic-token-fixture"},
])
def test_secret_fields_are_refused_before_commit(tmp_path, binding, private_body):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        with pytest.raises(PaperError, match="private_material_refused"):
            with journal.transaction("metadata", {}, now=NOW + 1):
                journal.set("unsafe", private_body)
    with PaperJournal(path, binding) as journal:
        assert journal.get("unsafe") is None and len(journal.events()) == 1


@pytest.mark.parametrize("surface", ["metadata_key", "event_body"])
def test_private_material_is_rejected_on_each_metadata_audit_surface(tmp_path, binding, surface):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        with pytest.raises(PaperError, match="private_material_refused"):
            with journal.transaction("unsafe", {"api_secret": "fixture"} if surface == "event_body" else {}, now=NOW + 1):
                journal.set("api_secret" if surface == "metadata_key" else "ordinary", "fixture")
    with PaperJournal(path, binding) as journal:
        assert journal.get("ordinary") is None and journal.get("api_secret") is None
        assert len(journal.events()) == 1


def test_binding_cannot_be_mutated_inside_a_valid_transaction(tmp_path, binding):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        with pytest.raises(PaperError, match="immutable_journal_binding"):
            with journal.transaction("unsafe_rebind", {}, now=NOW + 1):
                journal.set("binding", {**binding, "mode": "PAPER"})
    with PaperJournal(path, binding) as journal:
        assert journal.binding == binding and journal.get("binding") == binding
        assert len(journal.events()) == 1


def test_clock_regression_rolls_back_and_requires_reopen(tmp_path, binding):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        with pytest.raises(PaperError, match="clock_regressed"):
            with journal.transaction("regressed", {}, now=NOW - 1):
                journal.set("unsafe", "value")
        with pytest.raises(PaperError, match="unavailable"):
            journal.event("later", {}, now=NOW + 1)
    with PaperJournal(path, binding) as journal:
        assert journal.get("unsafe") is None


def test_directory_fsync_failure_keeps_uncertain_commit_and_poisoned_handle(tmp_path, binding, monkeypatch):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        def failed_sync(_):
            raise OSError("synthetic fsync failure")
        with monkeypatch.context() as patch:
            patch.setattr(module, "_sync", failed_sync)
            with pytest.raises(OSError):
                with journal.transaction("committed", {}, now=NOW + 1):
                    journal.set("may_be_committed", True)
        with pytest.raises(PaperError, match="unavailable"):
            journal.event("retry", {}, now=NOW + 2)
    with PaperJournal(path, binding) as journal:
        assert journal.get("may_be_committed") is True and journal.integrity_check()


def child(code, path, binding):
    source = "import sys,json,os\nsys.path.insert(0," + repr(str(ROOT)) + ")\n"
    source += "from atlas.paper.journal import PaperJournal\nfrom atlas.paper.models import PaperError\n"
    source += "from pathlib import Path\nfrom atlas.paper import journal as module\n"
    source += "module._account_lock_root=lambda:Path(" + repr(str(path.parent / 'account-locks')) + ")\n"
    source += "path=" + repr(str(path)) + "\nbinding=" + repr(binding) + "\n" + code
    return subprocess.run([sys.executable, "-B", "-c", source], capture_output=True, text=True, timeout=10)


def test_other_process_cannot_acquire_writer_or_change_state(tmp_path, binding):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW) as journal:
        result = child("try:\n PaperJournal(path,binding)\nexcept PaperError as e:\n print(str(e))\nelse:\n sys.exit(9)\n", path, binding)
        assert result.returncode == 0 and result.stdout.strip() == "paper_single_account_writer_already_active"
        assert len(journal.events()) == 1


@pytest.mark.parametrize('mode', ['SIMULATION', 'PAPER'])
def test_same_account_cannot_use_parallel_state_directories(tmp_path, binding, isolated_account_locks, mode):
    bound = {**binding, 'mode': mode}
    first, second = tmp_path / 'first', tmp_path / 'second'
    with PaperJournal(first, bound, create=True, now=NOW) as journal:
        with pytest.raises(PaperError, match='single_account_writer_already_active'):
            PaperJournal(second, bound, create=True, now=NOW)
        assert not second.exists() and len(journal.events()) == 1
        # Changing strategy/policy cannot split the same account's lock domain.
        with pytest.raises(PaperError, match='single_account_writer_already_active'):
            PaperJournal(second, {**bound, 'strategy_sha256': 'f' * 64}, create=True, now=NOW)
    lock = isolated_account_locks / (mode.lower() + '-' + bound['account_fingerprint'] + '.lock')
    assert lock.is_file() and stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(isolated_account_locks.stat().st_mode) == 0o700
    with PaperJournal(first, bound) as journal:
        assert journal.integrity_check()


def test_other_process_cannot_initialize_second_directory_for_same_account(tmp_path, binding):
    first, second = tmp_path / 'first', tmp_path / 'second'
    with PaperJournal(first, binding, create=True, now=NOW):
        result = child("try:\n PaperJournal(path,binding,create=True,now=" + repr(NOW) + ")\nexcept PaperError as e:\n print(str(e))\nelse:\n sys.exit(9)\n", second, binding)
        assert result.returncode == 0 and result.stdout.strip() == 'paper_single_account_writer_already_active'
        assert not second.exists()


def test_failed_constructor_releases_account_lock(tmp_path, binding):
    bad = tmp_path / 'bad-state'
    bad.mkdir(mode=0o700)
    (bad / 'untouched').write_text('existing bytes')
    with pytest.raises(PaperError):
        PaperJournal(bad, binding, create=True, now=NOW)
    with PaperJournal(tmp_path / 'valid-state', binding, create=True, now=NOW) as journal:
        assert journal.integrity_check()


def test_paper_registration_pins_directory_and_full_binding_after_release(tmp_path, binding, isolated_account_locks):
    bound = {**binding, 'mode': 'PAPER'}
    first, second = tmp_path / 'first', tmp_path / 'second'
    with PaperJournal(first, bound, create=True, now=NOW):
        pass
    lock = isolated_account_locks / ('paper-' + binding['account_fingerprint'] + '.lock')
    receipt = lock.read_bytes()
    recorded = json.loads(receipt)
    assert recorded['journal_directory'] == str(first.resolve())
    assert recorded['binding_sha256'] == digest(bound)
    with pytest.raises(PaperError, match='registration_binding_or_path_mismatch'):
        PaperJournal(second, bound, create=True, now=NOW)
    assert not (second / 'paper.sqlite3').exists() and lock.read_bytes() == receipt
    with pytest.raises(PaperError, match='registration_binding_or_path_mismatch'):
        PaperJournal(first, {**bound, 'policy_sha256': 'e' * 64})
    assert lock.read_bytes() == receipt
    with PaperJournal(first, bound) as journal:
        assert journal.integrity_check()


def test_paper_deleted_journal_cannot_reset_the_registered_account(tmp_path, binding, isolated_account_locks):
    bound, path = {**binding, 'mode': 'PAPER'}, tmp_path / 'original'
    with PaperJournal(path, bound, create=True, now=NOW):
        pass
    lock = isolated_account_locks / ('paper-' + binding['account_fingerprint'] + '.lock')
    original_claim = lock.read_bytes()
    shutil.rmtree(path)  # Disposable fixture, simulating lost/deleted local state.
    with pytest.raises(PaperError, match='session_already_registered'):
        PaperJournal(path, bound, create=True, now=NOW)
    assert not (path / 'paper.sqlite3').exists() and lock.read_bytes() == original_claim


@pytest.mark.parametrize('contents', [b'', b'{"schema":', b'not json'])
def test_incomplete_paper_registration_is_never_reused(tmp_path, binding, isolated_account_locks, contents):
    isolated_account_locks.mkdir(mode=0o700)
    lock = isolated_account_locks / ('paper-' + binding['account_fingerprint'] + '.lock')
    lock.write_bytes(contents)
    lock.chmod(0o600)
    with pytest.raises(PaperError, match='registration_invalid'):
        PaperJournal(tmp_path / 'new', {**binding, 'mode': 'PAPER'}, create=True, now=NOW)
    assert lock.read_bytes() == contents
    assert not (tmp_path / 'new/paper.sqlite3').exists()


@pytest.mark.parametrize('case', ['directory_symlink', 'directory_permissions', 'file_symlink', 'file_permissions', 'file_hardlink'])
def test_account_lock_paths_require_private_unlinked_files(tmp_path, binding, isolated_account_locks, case):
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o700)
    if case == 'directory_symlink':
        isolated_account_locks.symlink_to(outside, target_is_directory=True)
    else:
        isolated_account_locks.mkdir(mode=0o700)
        if case == 'directory_permissions':
            isolated_account_locks.chmod(0o755)
        else:
            lock = isolated_account_locks / ('simulation-' + binding['account_fingerprint'] + '.lock')
            target = outside / 'unchanged'
            target.write_bytes(b'unchanged')
            target.chmod(0o600)
            if case == 'file_symlink':
                lock.symlink_to(target)
            elif case == 'file_hardlink':
                lock.hardlink_to(target)
            else:
                lock.write_bytes(b'unchanged')
                lock.chmod(0o644)
    with pytest.raises((PaperError, OSError)):
        PaperJournal(tmp_path / 'state', binding, create=True, now=NOW)
    assert not (tmp_path / 'state').exists()
    if (outside / 'unchanged').exists():
        assert (outside / 'unchanged').read_bytes() == b'unchanged'


@pytest.mark.parametrize("committed", [False, True])
def test_process_crash_retains_only_committed_transaction(tmp_path, binding, committed):
    path = tmp_path / "paper-state"
    with PaperJournal(path, binding, create=True, now=NOW):
        pass
    code = "with PaperJournal(path,binding) as j:\n with j.transaction('crash_test',{},now=" + repr(NOW + 1) + "):\n  j.set('sentinel','committed')\n"
    code += " " if committed else "  "
    code += "os._exit(7)\n"
    result = child(code, path, binding)
    assert result.returncode == 7, result.stderr
    with PaperJournal(path, binding) as journal:
        assert journal.get("sentinel") == ("committed" if committed else None)
        assert journal.integrity_check() and len(journal.events()) == (2 if committed else 1)


def test_client_id_survives_retry_but_changed_terms_cannot_create_new_id(binding, reviewed):
    original = client_order_id(binding, reviewed)
    assert str(UUID(original)) == original
    assert client_order_id(binding, {**reviewed, "quantity": "0.08"}) == original
    assert client_order_id({**binding, "mode": "PAPER"}, reviewed) != original


@pytest.mark.parametrize("field,value", [("type", "market"), ("side", "sell"), ("symbol", "QQQ"),
                                         ("limit_price", "99"), ("qty", "0.08"), ("extended_hours", True),
                                         ("asset_class", "crypto"), ("status", "unknown-provider-status"),
                                         ("id", True), ("id", None)])
def test_order_identity_changes_are_refused(intent, observation, field, value):
    with pytest.raises(PaperError):
        validate_order({**observation, field: value}, intent["payload"])


@pytest.mark.parametrize("filled,average", [("0.03", "10"), ("0.02", "99"), ("0.03", "101")])
def test_cumulative_partial_fill_economics_cannot_regress(intent, observation, filled, average):
    new = {**observation, "filled_qty": filled, "filled_avg_price": average, "updated_at": NOW + 2}
    with pytest.raises(PaperError):
        validate_order(new, intent["payload"], observation)


def test_partial_fill_increment_must_itself_obey_limit(intent, observation):
    previous = {**observation, "filled_avg_price": "90"}
    # Total average remains below limit, but the added .01 would cost $110/share.
    new = {**observation, "filled_qty": "0.03", "filled_avg_price": "96.666666666666666666666666666666666666", "updated_at": NOW + 2}
    with pytest.raises(PaperError):
        validate_order(new, intent["payload"], previous)


@pytest.mark.parametrize("side,previous_average,new_average", [("buy", "90", "96"), ("sell", "110", "104")])
def test_favorable_prior_fill_cannot_hide_incremental_limit_violation(intent, observation, side, previous_average, new_average):
    intended = {**intent["payload"], "side": side}
    previous = {**observation, "side": side, "filled_avg_price": previous_average}
    current = {**previous, "filled_qty": "0.04", "filled_avg_price": new_average, "updated_at": NOW + 2}
    with pytest.raises(PaperError, match="progress_mismatch"):
        validate_order(current, intended, previous)


@pytest.mark.parametrize("side,previous_average,new_average", [("buy", "90", "95"), ("sell", "110", "105")])
def test_incremental_fill_exactly_at_limit_is_accepted(intent, observation, side, previous_average, new_average):
    intended = {**intent["payload"], "side": side}
    previous = {**observation, "side": side, "filled_avg_price": previous_average}
    current = {**previous, "filled_qty": "0.04", "filled_avg_price": new_average, "updated_at": NOW + 2}
    assert validate_order(current, intended, previous) == current


def test_unchanged_quantity_allows_equivalent_decimal_average(intent, observation):
    current = {**observation, "filled_avg_price": "100.000", "updated_at": NOW + 2}
    assert validate_order(current, intent["payload"], observation) == current


def test_first_fill_from_unfilled_observation_is_accepted(intent, observation):
    previous = {**observation, "filled_qty": "0", "filled_avg_price": None, "status": "accepted"}
    current = {**observation, "updated_at": NOW + 2}
    assert validate_order(current, intent["payload"], previous) == current


def test_valid_partial_then_canceled_preserves_inventory(intent, observation):
    first = validate_order(observation, intent["payload"])
    second = {**observation, "filled_qty": "0.03", "filled_avg_price": "99", "updated_at": NOW + 2}
    second = validate_order(second, intent["payload"], first)
    canceled = {**second, "status": "canceled", "updated_at": NOW + 3}
    assert validate_order(canceled, intent["payload"], second)["filled_qty"] == "0.03"
    with pytest.raises(PaperError):
        validate_order({**canceled, "status": "filled", "filled_qty": "0.09"}, intent["payload"], canceled)
