"""Collector backups preserve evidence while rejecting unsafe restore inputs."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from atlas.collection import backup
from atlas.research import data_archive
from atlas.research.data_contract_v1 import default_contract
from test_data_archive import fixture as observation


NOW = 1789344000


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("backup tests must not contact providers")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def write(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)


@pytest.fixture
def state(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    path = root / "state.sqlite3"
    write(path, b"")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO events VALUES (1, 'before snapshot')")
    return root


def add_archive(state, monkeypatch):
    monkeypatch.setattr(data_archive.time, "time", lambda: NOW)
    (state / "archives").mkdir(mode=0o700)
    root = state / "archives" / "generation-v1"
    data_archive.init_archive(root, default_contract("generation-v1", NOW - 180 * 86400, NOW))
    row, capture = observation(event=NOW-300, received=NOW)
    data_archive.append_backfill(root, records=[row], captures=[capture])
    return root


def create(state, tmp_path, **kwargs):
    return backup.create_backup(state, tmp_path / "backups", now=NOW, **kwargs)


def test_backup_restore_preserves_archive_labels_and_database(state, tmp_path, monkeypatch):
    original = add_archive(state, monkeypatch)
    write(state / "specifications" / "strategy-v1.md", b"Frozen rules; no evaluation performed.\n")
    report = create(state, tmp_path)
    manifest = backup.verify_backup(report["backup_path"])
    assert manifest["archives"][0]["acquisition_kinds"] == {
        "historical_backfill": 1, "prospective_observation": 0, "test": 0}
    restored = tmp_path / "restored"
    result = backup.restore_backup(report["backup_path"], restored, now=NOW+100)
    assert result["integrity_verified"] and result["collection_must_record_gap"]
    assert data_archive.verify_archive(restored / "archives" / "generation-v1") == data_archive.verify_archive(original)
    with sqlite3.connect(restored / "state.sqlite3") as db:
        assert db.execute("SELECT value FROM events").fetchone() == ("before snapshot",)
    marker = json.loads((restored / "restore-marker.json").read_bytes())
    assert marker == {"version": 1, "restored_at": NOW+100, "backup_id": report["backup_id"],
                      "manifest_sha256": hashlib.sha256(backup._encoded(manifest)).hexdigest(),
                      "collection_must_record_gap": True}
    assert not (restored / ".restore-incomplete").exists()
    for directory, _, files in os.walk(restored):
        assert Path(directory).stat().st_mode & 0o777 == 0o700
        for name in files:
            assert (Path(directory) / name).stat().st_mode & 0o777 == 0o600


def test_live_sqlite_wal_snapshot_includes_uncheckpointed_commits(state, tmp_path):
    db = sqlite3.connect(state / "state.sqlite3")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO events VALUES (2, 'WAL commit')")
        db.commit()
        for suffix in ("-wal", "-shm"):
            (state / ("state.sqlite3" + suffix)).chmod(0o600)
        report = create(state, tmp_path)
        snapshot = Path(report["backup_path"]) / "payload" / "state.sqlite3"
        with sqlite3.connect(snapshot) as copy:
            assert copy.execute("SELECT value FROM events WHERE id=2").fetchone() == ("WAL commit",)
        assert not Path(str(snapshot) + "-wal").exists()
    finally:
        db.close()


def test_sqlite_snapshot_with_concurrent_writer_is_transactionally_consistent(state, tmp_path):
    ready, stop = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            with sqlite3.connect(state / "state.sqlite3", timeout=1) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("CREATE TABLE pair (id INTEGER PRIMARY KEY, value INTEGER)")
                db.executemany("INSERT INTO pair VALUES (?,0)", [(1,), (2,)])
                db.commit()
                for suffix in ("-wal", "-shm"):
                    (state / ("state.sqlite3" + suffix)).chmod(0o600)
                ready.set()
                while not stop.is_set():
                    db.execute("UPDATE pair SET value=value+1")
                    db.commit()
        except Exception as exc:
            errors.append(type(exc).__name__)
            ready.set()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert ready.wait(5) and not errors
        report = create(state, tmp_path)
        with sqlite3.connect(Path(report["backup_path"]) / "payload" / "state.sqlite3") as copy:
            values = copy.execute("SELECT value FROM pair ORDER BY id").fetchall()
            assert len(values) == 2 and values[0] == values[1]
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive() and not errors


@pytest.mark.parametrize("name", [".env", "api-key.json", "coinbase.pem", "credentials.json", "secret.txt"])
def test_secret_paths_are_explicitly_excluded(state, tmp_path, name):
    write(state / name, b"DO_NOT_COPY_CREDENTIAL")
    result = create(state, tmp_path)
    manifest = backup.verify_backup(result["backup_path"])
    assert {"path": name, "reason": "excluded_secret_path"} in manifest["excluded"]
    assert not (Path(result["backup_path"]) / "payload" / name).exists()
    assert b"DO_NOT_COPY_CREDENTIAL" not in (Path(result["backup_path"]) / "manifest.json").read_bytes()


@pytest.mark.parametrize("content", [b'{"api_key":"not-a-real-key"}', b"-----BEGIN EC PRIVATE KEY-----", b"password=demo"])
def test_secret_content_inside_approved_artifact_refuses(state, tmp_path, content):
    write(state / "registrations" / "study.json", content)
    with pytest.raises(backup.BackupError, match="secret_content"):
        create(state, tmp_path)
    failed = list((tmp_path / "backups").iterdir())
    assert len(failed) == 1 and not (failed[0] / "manifest.json").exists()


@pytest.mark.parametrize("name", ["orders.json", "journal.db", "unexpected.sqlite3", "strategy.py"])
def test_unknown_state_files_are_not_silently_copied(state, tmp_path, name):
    write(state / name, b"unapproved")
    with pytest.raises(backup.BackupError, match="unapproved_state_file"):
        create(state, tmp_path)


def test_corrupt_database_refuses_and_retains_failed_snapshot(state, tmp_path):
    (state / "state.sqlite3").write_bytes(b"not sqlite")
    with pytest.raises(backup.BackupError, match="sqlite_snapshot_failed"):
        create(state, tmp_path)
    failed = list((tmp_path / "backups").iterdir())[0]
    assert not (failed / "manifest.json").exists()


def test_missing_state_does_not_create_or_repair_database(state, tmp_path):
    (state / "state.sqlite3").unlink()
    with pytest.raises(backup.BackupError, match="collector_state_missing"):
        create(state, tmp_path)
    assert not (state / "state.sqlite3").exists()


def test_snapshot_barrier_refuses_while_collection_commits(state, tmp_path):
    lock = state / ".snapshot.lock"
    write(lock, b"")
    with lock.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(backup.BackupError, match="snapshot_busy"):
            create(state, tmp_path)


@pytest.mark.parametrize("kind", ["file", "directory", "secret"])
def test_symlinks_refused_even_for_excluded_names(state, tmp_path, kind):
    if kind == "directory":
        (state / "research_artifacts").symlink_to(tmp_path, target_is_directory=True)
    else:
        (state / (".env" if kind == "secret" else "study.json")).symlink_to(state / "state.sqlite3")
    with pytest.raises(backup.BackupError, match="symlink"):
        create(state, tmp_path)


def test_hardlinks_and_special_files_refused(state, tmp_path):
    os.link(state / "state.sqlite3", tmp_path / "linked")
    with pytest.raises(backup.BackupError, match="owner_type"):
        create(state, tmp_path)
    (tmp_path / "linked").unlink()
    os.mkfifo(state / "pipe", 0o600)
    with pytest.raises(backup.BackupError, match="owner_type"):
        create(state, tmp_path)


def test_loose_permissions_refused(state, tmp_path):
    (state / "state.sqlite3").chmod(0o644)
    with pytest.raises(backup.BackupError, match="permissions"):
        create(state, tmp_path)


@pytest.mark.parametrize("overlap", ["same", "below", "above"])
def test_source_and_backup_destinations_must_not_overlap(state, overlap):
    destination = state if overlap == "same" else state / "backups" if overlap == "below" else state.parent
    with pytest.raises(backup.BackupError, match="overlap"):
        backup.create_backup(state, destination)


@pytest.mark.parametrize("retention", [0, -1, 366, True, 1.5, "7"])
def test_retention_bounds(state, tmp_path, retention):
    with pytest.raises(backup.BackupError, match="retention_invalid"):
        create(state, tmp_path, retention=retention)


def test_retention_only_prunes_complete_verified_managed_snapshots(state, tmp_path):
    destination = tmp_path / "backups"
    first = backup.create_backup(state, destination, retention=1, now=NOW)
    corrupt = destination / "atlas-backup-20260913T000000Z-1111111111111111"
    corrupt.mkdir(mode=0o700)
    unrelated = destination / "operator-backup"
    unrelated.mkdir(mode=0o700)
    second = backup.create_backup(state, destination, retention=1, now=NOW+100)
    assert second["pruned_backups"] == [first["backup_id"]]
    assert second["invalid_backups_retained"] == [corrupt.name]
    assert corrupt.exists() and unrelated.exists() and Path(second["backup_path"]).exists()


def test_hash_change_refuses_restore_before_creating_destination(state, tmp_path):
    result = create(state, tmp_path)
    path = Path(result["backup_path"]) / "payload" / "state.sqlite3"
    with path.open("ab") as stream:
        stream.write(b"tamper")
    destination = tmp_path / "restore"
    with pytest.raises(backup.BackupError, match="hash_mismatch"):
        backup.restore_backup(result["backup_path"], destination)
    assert not destination.exists()


@pytest.mark.parametrize("malicious", ["../escape", "/tmp/escape", "archives/../../escape", "state.sqlite3/../escape", "a\\b", "a//b"])
def test_manifest_path_traversal_refused(state, tmp_path, malicious):
    result = create(state, tmp_path)
    path = Path(result["backup_path"]) / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"][0]["path"] = malicious
    path.write_bytes(backup._encoded(manifest))
    with pytest.raises(backup.BackupError, match="path_invalid"):
        backup.verify_backup(result["backup_path"])


def test_restore_never_overwrites_existing_directory(state, tmp_path):
    result = create(state, tmp_path)
    before = (state / "state.sqlite3").read_bytes()
    with pytest.raises(backup.BackupError, match="new_external"):
        backup.restore_backup(result["backup_path"], state)
    assert (state / "state.sqlite3").read_bytes() == before


def test_copy_failure_retains_incomplete_restore_marker(state, tmp_path, monkeypatch):
    result = create(state, tmp_path)
    original = backup._archive_checks
    destination = tmp_path / "restore"

    def fail_restore(path):
        if path == destination:
            raise backup.BackupError("injected_restore_failure")
        return original(path)

    monkeypatch.setattr(backup, "_archive_checks", fail_restore)
    with pytest.raises(backup.BackupError, match="injected_restore"):
        backup.restore_backup(result["backup_path"], destination)
    assert (destination / ".restore-incomplete").exists()
    assert not (destination / "restore-marker.json").exists()


def test_backup_size_bounds(state, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "MAX_TOTAL_BYTES", 5)
    with pytest.raises(backup.BackupError, match="total_size"):
        create(state, tmp_path)


def test_cli_verify_and_failure_do_not_print_file_contents(state, tmp_path):
    report = create(state, tmp_path)
    script = str(Path(__file__).resolve().parents[1] / "scripts" / "atlas_backup.py")
    checked = subprocess.run([sys.executable, script, "verify", "--backup", report["backup_path"]],
                             capture_output=True, text=True, timeout=20, check=False)
    assert checked.returncode == 0 and json.loads(checked.stdout)["integrity_verified"]
    failed = subprocess.run([sys.executable, script, "restore", "--backup", report["backup_path"],
                             "--destination", str(state)], capture_output=True, text=True, timeout=20, check=False)
    assert failed.returncode == 1 and json.loads(failed.stdout)["error"] == "restore_requires_new_external_directory"
    assert "before snapshot" not in failed.stdout + failed.stderr


def test_empty_sharded_archive_backup_and_restore(state, tmp_path):
    from atlas.collection.archive import init_collection_archive, verify_collection_archive

    (state / "archives").mkdir(mode=0o700)
    root = state / "archives" / "sharded-v1"
    init_collection_archive(root, default_contract("sharded-v1", NOW-180*86400, NOW), clock=lambda: NOW)
    result = create(state, tmp_path)
    destination = tmp_path / "restored-shards"
    backup.restore_backup(result["backup_path"], destination, now=NOW+10)
    assert verify_collection_archive(destination / "archives" / "sharded-v1", clock=lambda: NOW+10)["partition_count"] == 0
    assert (destination / "archives" / "sharded-v1" / ".archive.lock").read_bytes() == b""


def test_populated_daily_shards_restore_preserves_exact_history(state, tmp_path, monkeypatch):
    from atlas.collection.archive import init_collection_archive, acquire_collection_once, verify_collection_archive
    from test_collection_archive import transport

    monkeypatch.setattr(data_archive.time, "time", lambda: NOW+600)
    (state / "archives").mkdir(mode=0o700)
    root = state / "archives" / "sharded-v1"
    init_collection_archive(root, default_contract("sharded-v1", NOW-180*86400, NOW), clock=lambda: NOW)
    for event in (NOW-300, NOW+300):
        acquire_collection_once(root, provider="coinbase", start=event-300, end=event,
            transport=transport([[event-300,100,100,100,100,1]]), clock=lambda: NOW+600)
    original = verify_collection_archive(root)
    assert original["record_count"] >= 3 and original["partition_count"] == 2
    result = backup.create_backup(state, tmp_path / "backups", now=NOW+600)
    destination = tmp_path / "restored-shards"
    backup.restore_backup(result["backup_path"], destination, now=NOW+700)
    assert verify_collection_archive(destination / "archives" / "sharded-v1") == original
    for day in (root / "days").iterdir():
        assert data_archive.verify_archive(day) == data_archive.verify_archive(destination / "archives" / "sharded-v1" / "days" / day.name)


@pytest.mark.parametrize("now", [True, -1, 0, float("nan"), float("inf"), 1e100])
def test_clock_bounds(state, tmp_path, now):
    with pytest.raises(backup.BackupError, match="clock_invalid"):
        backup.create_backup(state, tmp_path / "backups", now=now)


def test_frozen_model_is_opaque_and_restores_identical_bytes_without_pickle_loading(state, tmp_path, monkeypatch):
    import pickle

    def forbidden(*args, **kwargs):
        raise AssertionError("backups must never deserialize model artifacts")

    monkeypatch.setattr(pickle, "load", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)
    (state / "research_artifacts").mkdir(mode=0o700)
    model = state / "research_artifacts" / "models" / "frozen-model.pkl"
    # Deliberately not a valid pickle: successful backup proves no decoding is required.
    content = b"\x80\x05\xffopaque-frozen-model\x00" * 60000
    write(model, content)
    original_hash = hashlib.sha256(content).hexdigest()
    result = create(state, tmp_path)
    manifest = backup.verify_backup(result["backup_path"])
    entry = next(row for row in manifest["files"] if row["path"].endswith(".pkl"))
    assert entry["kind"] == "opaque_artifact" and entry["sha256"] == original_hash
    destination = tmp_path / "restored-model"
    backup.restore_backup(result["backup_path"], destination, now=NOW+10)
    restored = destination / "research_artifacts" / "models" / model.name
    assert restored.read_bytes() == model.read_bytes() == content
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == original_hash


def test_frozen_research_journal_uses_sqlite_online_snapshot_and_restores_wal_commit(state, tmp_path):
    journal = state / "research_artifacts" / "journal.db"
    write(journal, b"")
    db = sqlite3.connect(journal)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE frozen_experiment (id INTEGER PRIMARY KEY, result TEXT)")
        db.execute("INSERT INTO frozen_experiment VALUES (1, 'unchanged historical result')")
        db.commit()
        for suffix in ("-wal", "-shm"):
            Path(str(journal) + suffix).chmod(0o600)
        before_db = journal.read_bytes()
        before_wal = Path(str(journal) + "-wal").read_bytes()
        result = create(state, tmp_path)
        manifest = backup.verify_backup(result["backup_path"])
        entry = next(row for row in manifest["files"] if row["path"].endswith("journal.db"))
        assert entry["kind"] == "sqlite"
        assert {"path": "research_artifacts/journal.db-wal", "reason": "excluded_sqlite_sidecar"} in manifest["excluded"]
        assert journal.read_bytes() == before_db and Path(str(journal) + "-wal").read_bytes() == before_wal
        destination = tmp_path / "restored-journal"
        backup.restore_backup(result["backup_path"], destination, now=NOW+10)
        with sqlite3.connect(destination / "research_artifacts" / "journal.db") as restored:
            assert restored.execute("SELECT * FROM frozen_experiment").fetchall() == [(1, "unchanged historical result")]
            assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        db.close()


@pytest.mark.parametrize("relative", ["model.pkl", "journal.db", "registrations/model.pkl", "specifications/journal.db"])
def test_binary_research_artifacts_require_explicit_research_directory(state, tmp_path, relative):
    write(state / relative, b"unapproved")
    with pytest.raises(backup.BackupError, match="unapproved_state_file"):
        create(state, tmp_path)


def test_corrupt_research_journal_is_not_raw_copied_as_a_successful_snapshot(state, tmp_path):
    journal = state / "research_artifacts" / "journal.db"
    write(journal, b"corrupted SQLite source")
    with pytest.raises(backup.BackupError, match="sqlite_snapshot_failed"):
        create(state, tmp_path)
    assert journal.read_bytes() == b"corrupted SQLite source"
    assert all(not (path / "manifest.json").exists() for path in (tmp_path / "backups").iterdir())


def test_opaque_model_is_size_bounded(state, tmp_path, monkeypatch):
    write(state / "research_artifacts" / "model.pkl", b"bounded binary fixture")
    monkeypatch.setattr(backup, "MAX_OPAQUE_ARTIFACT_BYTES", 8)
    with pytest.raises(backup.BackupError, match="file_size_bound"):
        create(state, tmp_path)


def test_secret_pattern_across_opaque_copy_chunk_boundary_refuses(state, tmp_path):
    secret = b"-----BEGIN EC PRIVATE KEY-----"
    content = b"x" * (1024*1024 - 12) + secret + b"not a real secret"
    write(state / "research_artifacts" / "model.pkl", content)
    with pytest.raises(backup.BackupError, match="secret_content"):
        create(state, tmp_path)


@pytest.mark.parametrize("name", ["api-key.pkl", "credentials.db"])
def test_binary_artifacts_cannot_override_credential_path_exclusion(state, tmp_path, name):
    write(state / "research_artifacts" / name, b"never copied")
    result = create(state, tmp_path)
    manifest = backup.verify_backup(result["backup_path"])
    assert {"path": f"research_artifacts/{name}", "reason": "excluded_secret_path"} in manifest["excluded"]


def test_model_symlink_is_refused(state, tmp_path):
    (state / "research_artifacts").mkdir(mode=0o700)
    (state / "research_artifacts" / "model.pkl").symlink_to(state / "state.sqlite3")
    with pytest.raises(backup.BackupError, match="symlink"):
        create(state, tmp_path)


def test_backup_waits_for_temporary_collector_transaction_then_succeeds(state, tmp_path):
    lock = state / ".snapshot.lock"
    write(lock, b"")
    released = threading.Event()
    with lock.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release():
            released.wait(0.05)
            fcntl.flock(stream, fcntl.LOCK_UN)
            released.set()

        thread = threading.Thread(target=release)
        thread.start()
        try:
            result = backup.create_backup(state, tmp_path / "backups", wait_seconds=1, now=NOW)
            assert result["integrity_verified"] and released.is_set()
        finally:
            released.set()
            thread.join(2)
    assert not thread.is_alive()


def test_backup_wait_for_persistent_collector_lock_is_bounded(state, tmp_path):
    lock = state / ".snapshot.lock"
    write(lock, b"")
    with lock.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        with pytest.raises(backup.BackupError, match="snapshot_busy"):
            backup.create_backup(state, tmp_path / "backups", wait_seconds=1, now=NOW)
        assert 0.9 <= time.monotonic() - start < 3


@pytest.mark.parametrize("wait_seconds", [-1, 301, True, 0.5, "90"])
def test_backup_wait_bound_is_strict(state, tmp_path, wait_seconds):
    with pytest.raises(backup.BackupError, match="wait_seconds_invalid"):
        backup.create_backup(state, tmp_path / "backups", wait_seconds=wait_seconds, now=NOW)
