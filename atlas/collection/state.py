"""Durable collector checkpoints and append-only operational evidence."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from atlas.collection.config import CollectionError, encoded, external_path, fingerprint, private, read_json


SCHEMA = (
    "CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)",
    "CREATE TABLE providers (name TEXT PRIMARY KEY, body TEXT NOT NULL)",
    "CREATE TABLE events (id INTEGER PRIMARY KEY, time INTEGER NOT NULL, kind TEXT NOT NULL, provider TEXT, body TEXT NOT NULL)",
    "CREATE TABLE gaps (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, symbol TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, reason TEXT NOT NULL, detected_at INTEGER NOT NULL, resolved_at INTEGER, UNIQUE(provider,symbol,start,end,reason))",
    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events_append_only'); END",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events_append_only'); END",
)


def clock_sample(clock=None):
    """Validate wall time without rounding it across a request boundary."""
    value = (time.time if clock is None else clock)()
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 32503680000:
        raise CollectionError("collector_clock_invalid")
    return value


def utc_now(clock=None):
    return math.ceil(clock_sample(clock))


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def file_lock(root, name, *, blocking=False, timeout=0):
    root = external_path(str(root))
    private(root, directory=True)
    path = root / name
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            private(path)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            yield
        finally:
            os.close(fd)
    except (OSError, BlockingIOError):
        raise CollectionError("collector_lock_unavailable") from None


@contextmanager
def connection(root, *, write=False):
    root = external_path(str(root))
    private(root, directory=True)
    path = root / "state.sqlite3"
    private(path)
    try:
        if path.stat().st_size > 4 * 1024**3:
            raise CollectionError("collector_state_size_bound_exceeded")
        db = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=5)
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA synchronous=FULL")
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            if write:
                db.commit()
                sync_dir(root)
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        raise CollectionError("collector_state_unavailable_or_corrupt") from None


def initialize_state(config, now, *, test_clock=False):
    root = Path(config["state_dir"])
    path = root / "state.sqlite3"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            for sql in SCHEMA:
                db.execute(sql)
            meta = {"version": 1, "config_sha256": fingerprint(config), "registered_at": now,
                    "test_clock": test_clock, "heartbeat_at": now, "running": False,
                    "continuous_since": None, "restore_marker_sha256": None}
            db.execute("INSERT INTO metadata VALUES (1,?)", (encoded(meta).decode(),))
            for provider in config["providers"]:
                checkpoint = {"last_attempt_at": None, "last_success_at": None,
                    "last_observation_at": None, "last_bar_event": None, "last_quote_event": None,
                    "last_by_symbol": {}, "failures": 0, "next_attempt_at": now,
                    "cursor_end": now // 300 * 300, "recovery_cursor": None, "recovery_until": None,
                    "calendar_coverage": [], "last_error": None}
                db.execute("INSERT INTO providers VALUES (?,?)", (provider, encoded(checkpoint).decode()))
            event(db, now, "initialized")
        sync_dir(root)
    except (OSError, sqlite3.Error):
        raise CollectionError("collector_initialization_failed_retained_for_inspection") from None


def metadata(db):
    rows = db.execute("SELECT body FROM metadata").fetchall()
    if len(rows) != 1:
        raise CollectionError("collector_state_metadata_invalid")
    return json.loads(rows[0][0])


def save_metadata(db, meta):
    db.execute("UPDATE metadata SET body=? WHERE id=1", (encoded(meta).decode(),))


def provider_state(db, provider):
    row = db.execute("SELECT body FROM providers WHERE name=?", (provider,)).fetchone()
    if row is None:
        raise CollectionError("collector_provider_state_missing")
    return json.loads(row[0])


def save_provider(db, provider, state):
    db.execute("UPDATE providers SET body=? WHERE name=?", (encoded(state).decode(), provider))


def event(db, now, kind, provider=None, **detail):
    db.execute("INSERT INTO events(time,kind,provider,body) VALUES (?,?,?,?)",
               (now, kind, provider, encoded(detail).decode()))


def gap(db, now, provider, symbol, start, end, reason):
    if end <= start:
        return
    change = db.execute("INSERT OR IGNORE INTO gaps(provider,symbol,start,end,reason,detected_at) VALUES (?,?,?,?,?,?)",
                       (provider, symbol, start, end, reason, now)).rowcount
    if change:
        event(db, now, "gap_detected", provider, symbol=symbol, start=start, end=end, reason=reason)


def snapshot(config, *, thorough=True):
    with connection(config["state_dir"]) as db:
        if thorough and db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise CollectionError("collector_state_integrity_failed")
        meta = metadata(db)
        if meta["config_sha256"] != fingerprint(config):
            raise CollectionError("collector_configuration_changed_requires_explicit_migration")
        states = {name: json.loads(body) for name, body in db.execute("SELECT name,body FROM providers")}
        if set(states) != set(config["providers"]):
            raise CollectionError("collector_provider_configuration_mismatch")
        count, unresolved = (db.execute("SELECT count(*),coalesce(sum(resolved_at IS NULL),0) FROM gaps").fetchone()
                             if thorough else (None, None))
        return {"metadata": meta, "providers": states, "gap_count": count,
                "unresolved_gaps": unresolved,
                "event_count": db.execute("SELECT count(*) FROM events").fetchone()[0] if thorough else None}


def apply_restore_marker(config, now):
    path = Path(config["state_dir"]) / "restore-marker.json"
    if not path.exists() and not path.is_symlink():
        return
    marker = read_json(path)
    if (type(marker) is not dict or set(marker) != {"version", "restored_at", "backup_id", "manifest_sha256", "collection_must_record_gap"}
            or marker["version"] != 1 or type(marker["restored_at"]) is not int
            or not 0 < marker["restored_at"] <= now or marker["collection_must_record_gap"] is not True
            or type(marker["backup_id"]) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", marker["backup_id"])
            or type(marker["manifest_sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", marker["manifest_sha256"])):
        raise CollectionError("collector_restore_marker_invalid")
    digest = hashlib.sha256(encoded(marker)).hexdigest()
    with connection(config["state_dir"], write=True) as db:
        meta = metadata(db)
        if meta["restore_marker_sha256"] == digest:
            return
        for provider in config["providers"]:
            state = provider_state(db, provider)
            start = state["cursor_end"]
            gap(db, now, provider, "*", start, now // 300 * 300, "restore_discontinuity")
            state.update(recovery_cursor=start, recovery_until=now // 300 * 300, next_attempt_at=now)
            save_provider(db, provider, state)
        meta.update(restore_marker_sha256=digest, continuous_since=None, running=False)
        save_metadata(db, meta)
        event(db, now, "restore_discontinuity", backup_id=marker["backup_id"])
