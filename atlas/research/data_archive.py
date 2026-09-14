"""Private append-only V1 observations. No strategy or order execution authority.

Transactions preserve whole captures, including repeated unchanged snapshots.
Hashes detect internal inconsistency; they are not provider signatures or WORM
storage. An owner who replaces the whole archive can also replace its history.
"""

from contextlib import contextmanager
import math
import os
import re
import sqlite3
import stat
import time

from atlas.research.data_contract_v1 import validate_contract, validate_record
from atlas.research.imports import _encoded, _external_path, _json, _sha, _sync_dir

MAX_BATCH_RECORDS = 20000
MAX_RECORDS = 1_000_000
MAX_BATCHES = 10000
MAX_BATCH_BYTES = 32 * 1024 * 1024
MAX_CAPTURE_BYTES = 2_000_000
MAX_DB_BYTES = 4 * 1024**3
_HASH = re.compile(r"[a-f0-9]{64}")
_TABLES = {
    "manifest": "CREATE TABLE manifest (id INTEGER PRIMARY KEY CHECK(id=1), body BLOB NOT NULL, hash TEXT NOT NULL)",
    "batches": "CREATE TABLE batches (id INTEGER PRIMARY KEY, body BLOB NOT NULL, hash TEXT NOT NULL)",
    "captures": "CREATE TABLE captures (batch_id INTEGER NOT NULL REFERENCES batches(id), position INTEGER NOT NULL, body BLOB NOT NULL, PRIMARY KEY(batch_id, position))",
    "observations": "CREATE TABLE observations (batch_id INTEGER NOT NULL REFERENCES batches(id), position INTEGER NOT NULL, body BLOB NOT NULL, PRIMARY KEY(batch_id, position))",
    "strategy_freeze": "CREATE TABLE strategy_freeze (id INTEGER PRIMARY KEY CHECK(id=1), body BLOB NOT NULL, hash TEXT NOT NULL)",
}
_TRIGGERS = {
    f"{table}_no_{verb.lower()}": f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'archive_is_append_only'); END"
    for table in _TABLES for verb in ("UPDATE", "DELETE")
}


class DataArchiveError(ValueError):
    """Fixed local diagnostic; never include remote bodies or credentials."""


def receipt_time(clock=None):
    value = (time.time if clock is None else clock)()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise DataArchiveError("archive_clock_invalid")
    return math.ceil(value)


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _identity(row):
    return tuple(row[k] for k in ("record_type", "symbol", "asset_class", "venue", "event_time", "interval_seconds"))


def _kind(origin, event_time, created_at, prior=None):
    candidate = ("test" if origin == "test" else "historical_backfill" if origin == "historical_import"
                 else "prospective_observation" if event_time >= created_at else "historical_backfill")
    lineage = {candidate, prior["acquisition_kind"]} if prior else {candidate}
    return "test" if "test" in lineage else "historical_backfill" if "historical_backfill" in lineage else candidate


def _value(row):
    # The first receipt of an unchanged observation stays authoritative.
    return {k: v for k, v in row.items() if k not in {
        "available_time", "ingestion_time", "revision", "previous_observation_hash",
        "raw_capture_sha256", "acquisition_kind"}}


def bucket_for(contract, event_time, interval_seconds=0):
    for name, window in contract["windows"].items():
        if window["start"] <= event_time - interval_seconds and event_time < window["end_exclusive"]:
            return name
    return "outside_study"


def _trusted(path, *, directory=False):
    info = path.lstat()
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise DataArchiveError("archive_owner_type_or_permissions_invalid")


@contextmanager
def _connection(root, *, write=False):
    root = _external_path(root)
    try:
        _trusted(root, directory=True)
        if {p.name for p in root.iterdir()} - {"archive.sqlite3", "archive.sqlite3-journal"}:
            raise DataArchiveError("archive_unexpected_files")
        path = _external_path(root / "archive.sqlite3")
        _trusted(path)
        if path.stat().st_size > MAX_DB_BYTES:
            raise DataArchiveError("archive_size_bound_exceeded")
        journal = root / "archive.sqlite3-journal"
        if journal.exists() or journal.is_symlink():
            _trusted(journal)
        db = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=0)
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("PRAGMA synchronous=FULL")
            else:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            if write:
                db.commit()
                _sync_dir(root)
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        raise DataArchiveError("archive_storage_unavailable_or_busy") from None


def init_archive(root, contract, *, clock=None):
    contract = validate_contract(contract)
    now = receipt_time(clock)
    if contract["created_at"] != now:
        raise DataArchiveError("archive_registration_must_use_current_receipt_time")
    root = _external_path(root)
    if root.exists() or not root.parent.is_dir():
        raise DataArchiveError("archive_requires_new_external_directory")
    manifest = {"archive_version": 1, "contract": contract,
                "clock_basis": "system_utc" if clock is None else "injected_test_clock"}
    body = _encoded(manifest)
    # Failed initialization is retained, never silently repaired or recreated.
    try:
        root.mkdir(mode=0o700)
        path = root / "archive.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            for sql in (*_TABLES.values(), *_TRIGGERS.values()):
                db.execute(sql)
            db.execute("INSERT INTO manifest VALUES (1, ?, ?)", (body, _sha(body)))
        _sync_dir(root)
        _sync_dir(root.parent)
    except (OSError, sqlite3.Error):
        raise DataArchiveError("archive_initialization_failed_retained_for_inspection") from None
    return verify_archive(root, clock=clock)


def _decode(body, *, max_bytes=MAX_BATCH_BYTES):
    if not isinstance(body, bytes) or not 0 < len(body) <= max_bytes:
        raise DataArchiveError("archive_object_size_invalid")
    obj = _json(body)
    if _encoded(obj) != body:
        raise DataArchiveError("archive_noncanonical_object")
    return obj


def _validate_calendar(entries, captures, now):
    # Coverage performs detailed session/range validation as well.
    from datetime import date
    if not isinstance(entries, list) or len(entries) > 31:
        raise DataArchiveError("archive_calendar_count_invalid")
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"start_date", "end_date", "sessions", "source_id", "raw_capture_sha256", "received_at"}
                or entry["source_id"] != "alpaca-paper-calendar-v2"
                or entry["raw_capture_sha256"] not in captures
                or type(entry["received_at"]) is not int or not 0 < entry["received_at"] <= now
                or not isinstance(entry["sessions"], list) or len(entry["sessions"]) > 366):
            raise DataArchiveError("archive_calendar_metadata_invalid")
        try:
            start, end = date.fromisoformat(entry["start_date"]), date.fromisoformat(entry["end_date"])
            if not 0 <= (end - start).days <= 366:
                raise ValueError
        except (ValueError, TypeError):
            raise DataArchiveError("archive_calendar_range_invalid") from None
    from atlas.research.data_coverage import _calendar
    _calendar(entries, now)


def _freeze(db, contract, now):
    rows = db.execute("SELECT id,body,hash FROM strategy_freeze").fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0][0] != 1:
        raise DataArchiveError("archive_strategy_freeze_invalid")
    _, body, digest = rows[0]
    freeze = _decode(body, max_bytes=4096)
    if (set(freeze) != {"strategy_sha256", "frozen_at"} or _sha(body) != digest
            or not _hash(freeze["strategy_sha256"]) or type(freeze["frozen_at"]) is not int
            or not contract["created_at"] <= freeze["frozen_at"] < contract["windows"]["prospective_holdout"]["start"]
            or freeze["frozen_at"] > now):
        raise DataArchiveError("archive_strategy_freeze_invalid")
    return freeze


def _scan(db, now):
    if db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0] > MAX_DB_BYTES:
        raise DataArchiveError("archive_size_bound_exceeded")
    actual = {row[0]: row[1] for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger')")}
    if actual != {**_TABLES, **_TRIGGERS} or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise DataArchiveError("archive_schema_or_integrity_invalid")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DataArchiveError("archive_orphan_rows")
    rows = db.execute("SELECT id,body,hash FROM manifest").fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise DataArchiveError("archive_manifest_missing")
    _, body, previous = rows[0]
    manifest = _decode(body, max_bytes=100000)
    if (set(manifest) != {"archive_version", "contract", "clock_basis"} or manifest["archive_version"] != 1
            or manifest["clock_basis"] not in {"system_utc", "injected_test_clock"} or _sha(body) != previous):
        raise DataArchiveError("archive_manifest_invalid")
    contract = validate_contract(manifest["contract"])
    last_time = contract["created_at"]
    if now < last_time:
        raise DataArchiveError("archive_clock_regressed")
    count = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    if count > MAX_BATCHES or db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] > MAX_RECORDS:
        raise DataArchiveError("archive_scan_bound_exceeded")
    latest, records, calendars = {}, [], []
    for expected, (seq, body, digest) in enumerate(db.execute("SELECT id,body,hash FROM batches ORDER BY id"), 1):
        batch = _decode(body)
        if (seq != expected or digest != _sha(body) or not isinstance(batch, dict)
                or set(batch) != {"sequence", "previous_batch_hash", "ingested_at", "origin", "provenance", "captures", "observations", "calendar_coverage"}
                or batch["sequence"] != seq or batch["previous_batch_hash"] != previous
                or type(batch["ingested_at"]) is not int or not last_time <= batch["ingested_at"] <= now
                or batch["origin"] not in {"historical_import", "builtin_get", "test"}
                or not isinstance(batch["provenance"], str) or len(batch["provenance"]) > 200):
            raise DataArchiveError("archive_batch_chain_invalid")
        caps = db.execute("SELECT position,body FROM captures WHERE batch_id=? ORDER BY position", (seq,)).fetchall()
        descriptors = batch["captures"]
        if not isinstance(descriptors, list) or not 1 <= len(caps) == len(descriptors) <= 31:
            raise DataArchiveError("archive_capture_count_invalid")
        capture_hashes, capture_times, byte_count, previous_receipt = set(), {}, len(body), 0
        for position, ((stored_pos, raw), desc) in enumerate(zip(caps, descriptors)):
            if (stored_pos != position or not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CAPTURE_BYTES
                    or not isinstance(desc, dict) or set(desc) != {"role", "request_url", "received_at", "sha256", "bytes"}
                    or desc["sha256"] != _sha(raw) or desc["bytes"] != len(raw)
                    or type(desc["received_at"]) is not int or not max(1, previous_receipt) <= desc["received_at"] <= batch["ingested_at"]
                    or not isinstance(desc["role"], str) or not 1 <= len(desc["role"]) <= 80
                    or not isinstance(desc["request_url"], str) or not 1 <= len(desc["request_url"]) <= 8192):
                raise DataArchiveError("archive_raw_capture_integrity_invalid")
            capture_hashes.add(desc["sha256"])
            capture_times.setdefault(desc["sha256"], set()).add(desc["received_at"])
            previous_receipt = desc["received_at"]
            byte_count += len(raw)
        _validate_calendar(batch["calendar_coverage"], capture_hashes, batch["ingested_at"])
        for entry in batch["calendar_coverage"]:
            if entry["received_at"] not in capture_times.get(entry["raw_capture_sha256"], set()):
                raise DataArchiveError("archive_calendar_capture_time_mismatch")
            # The original capture receipt is preserved in the batch. Imported
            # calendar assertions cannot become visible before local ingestion.
            calendars.append({**entry, "ingestion_time": batch["ingested_at"]})
        observations = db.execute("SELECT position,body FROM observations WHERE batch_id=? ORDER BY position", (seq,)).fetchall()
        if not isinstance(batch["observations"], list) or not len(observations) == len(batch["observations"]) <= MAX_BATCH_RECORDS:
            raise DataArchiveError("archive_observation_count_invalid")
        for position, ((stored_pos, raw), desc) in enumerate(zip(observations, batch["observations"])):
            row = _decode(raw, max_bytes=16384)
            canonical = validate_record(row, contract, now=batch["ingested_at"])["record"]
            identity = _identity(row)
            prior = latest.get(identity)
            if (stored_pos != position or canonical != row or desc != {"sha256": _sha(raw), "bucket": bucket_for(contract, row["event_time"], row["interval_seconds"])}
                    or row["raw_capture_sha256"] not in capture_hashes or row["ingestion_time"] != batch["ingested_at"]
                    or row["available_time"] not in capture_times.get(row["raw_capture_sha256"], set())
                    or row["revision"] != (prior[0]["revision"] + 1 if prior else 0)
                    or row["previous_observation_hash"] != (prior[1] if prior else None)
                    or (prior and (_value(row) == _value(prior[0]) or row["available_time"] < prior[0]["available_time"]
                                   or row["source_id"] != prior[0]["source_id"]))):
                raise DataArchiveError("archive_observation_revision_or_hash_invalid")
            if (batch["origin"] == "test" or manifest["clock_basis"] == "injected_test_clock") and row["acquisition_kind"] != "test":
                raise DataArchiveError("archive_test_promotion_forbidden")
            if row["acquisition_kind"] != _kind(batch["origin"], row["event_time"], contract["created_at"], prior[0] if prior else None):
                raise DataArchiveError("archive_acquisition_kind_invalid")
            records.append(row)
            latest[identity] = (row, _sha(raw))
            byte_count += len(raw)
        if byte_count > MAX_BATCH_BYTES:
            raise DataArchiveError("archive_batch_size_bound_exceeded")
        previous, last_time = digest, batch["ingested_at"]
    freeze = _freeze(db, contract, now)
    from atlas.research.data_coverage import _calendar
    _calendar(calendars, now)  # Cross-batch holiday/session contradictions fail closed.
    return {"manifest": manifest, "contract": contract, "latest": latest, "records": records,
            "calendar_coverage": calendars, "strategy_freeze": freeze, "batch_count": count,
            "head_sha256": previous, "last_ingestion_time": last_time}


def _summary(state):
    kinds = {kind: 0 for kind in ("historical_backfill", "prospective_observation", "test")}
    buckets = {name: 0 for name in (*state["contract"]["windows"], "outside_study")}
    for row in state["records"]:
        kinds[row["acquisition_kind"]] += 1
        buckets[bucket_for(state["contract"], row["event_time"], row["interval_seconds"])] += 1
    return {"contract_version": state["contract"]["version"], "generation_id": state["contract"]["generation_id"],
            "batch_count": state["batch_count"], "record_count": len(state["records"]),
            "unique_observation_count": len(state["latest"]), "acquisition_kinds": kinds,
            "bucket_record_counts": buckets, "head_sha256": state["head_sha256"],
            "strategy_freeze": state["strategy_freeze"], "integrity_verified": True,
            "provider_authenticity_verified": False, "qualification_established": False,
            "orders_submitted": 0, "execution_enabled": False}


def verify_archive(root, *, clock=None):
    with _connection(root) as db:
        return _summary(_scan(db, receipt_time(clock)))


def load_contract(root):
    with _connection(root) as db:
        return _scan(db, receipt_time())["contract"]


def _append(root, *, records, captures, calendar_coverage, origin, provenance, clock=None):
    if (not isinstance(records, list) or len(records) > MAX_BATCH_RECORDS
            or not isinstance(captures, list) or not 1 <= len(captures) <= 31
            or type(calendar_coverage) not in (list, tuple) or len(calendar_coverage) > 31
            or not isinstance(provenance, str) or not 1 <= len(provenance) <= 200):
        raise DataArchiveError("archive_batch_input_invalid")
    # Materialize bounded inputs before taking the transaction lock.
    if len(_encoded(records)) > MAX_BATCH_BYTES:
        raise DataArchiveError("archive_batch_size_bound_exceeded")
    with _connection(root, write=True) as db:
        now = receipt_time(clock)
        state = _scan(db, now)
        if state["batch_count"] >= MAX_BATCHES:
            raise DataArchiveError("archive_batch_count_bound_exceeded")
        if state["manifest"]["clock_basis"] == "injected_test_clock":
            origin = "test"
        descriptors, raw_bodies, capture_hashes = [], [], set()
        for capture in captures:
            if not isinstance(capture, dict) or set(capture) != {"role", "request_url", "received_at", "body"}:
                raise DataArchiveError("archive_capture_input_invalid")
            raw = capture["body"]
            if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CAPTURE_BYTES:
                raise DataArchiveError("archive_capture_size_invalid")
            descriptor = {k: v for k, v in capture.items() if k != "body"}
            descriptor.update(sha256=_sha(raw), bytes=len(raw))
            descriptors.append(descriptor)
            raw_bodies.append(raw)
            capture_hashes.add(descriptor["sha256"])
        _validate_calendar(list(calendar_coverage), capture_hashes, now)
        output, seen, unchanged = [], set(), 0
        for incoming in records:
            if not isinstance(incoming, dict):
                raise DataArchiveError("archive_record_invalid")
            row = dict(incoming)
            row.update(revision=0, previous_observation_hash=None)
            row["acquisition_kind"] = _kind(origin, row.get("event_time", 0), state["contract"]["created_at"])
            row = validate_record(row, state["contract"], now=now)["record"]
            row["ingestion_time"] = now
            if row["raw_capture_sha256"] not in capture_hashes:
                raise DataArchiveError("archive_record_raw_capture_missing")
            identity = _identity(row)
            if identity in seen:
                raise DataArchiveError("archive_duplicate_input_observation")
            seen.add(identity)
            prior = state["latest"].get(identity)
            if prior and row["source_id"] != prior[0]["source_id"]:
                raise DataArchiveError("archive_source_change_requires_new_generation")
            if prior and _value(row) == _value(prior[0]):
                unchanged += 1
                continue
            if prior:
                row.update(revision=prior[0]["revision"] + 1, previous_observation_hash=prior[1])
                row["acquisition_kind"] = _kind(origin, row["event_time"], state["contract"]["created_at"], prior[0])
            output.append(row)
        if len(output) + len(state["records"]) > MAX_RECORDS:
            raise DataArchiveError("archive_record_count_bound_exceeded")
        seq = state["batch_count"] + 1
        encoded = [_encoded(row) for row in output]
        batch = {"sequence": seq, "previous_batch_hash": state["head_sha256"], "ingested_at": now,
                 "origin": origin, "provenance": provenance, "captures": descriptors,
                 "observations": [{"sha256": _sha(raw), "bucket": bucket_for(state["contract"], row["event_time"], row["interval_seconds"])} for row, raw in zip(output, encoded)],
                 "calendar_coverage": list(calendar_coverage)}
        body = _encoded(batch)
        if len(body) + sum(map(len, encoded)) + sum(map(len, raw_bodies)) > MAX_BATCH_BYTES:
            raise DataArchiveError("archive_batch_size_bound_exceeded")
        db.execute("INSERT INTO batches VALUES (?,?,?)", (seq, body, _sha(body)))
        db.executemany("INSERT INTO captures VALUES (?,?,?)", [(seq, i, raw) for i, raw in enumerate(raw_bodies)])
        db.executemany("INSERT INTO observations VALUES (?,?,?)", [(seq, i, raw) for i, raw in enumerate(encoded)])
        checked = _scan(db, now)  # Full validation happens inside the same transaction.
        result = {**_summary(checked), "appended_records": len(output), "unchanged_observations": unchanged,
                  "captures_retained": len(raw_bodies)}
    return result


def append_backfill(root, *, records, captures, calendar_coverage=(), clock=None):
    """Explicit imports are always backfills; caller-supplied labels grant nothing."""
    return _append(root, records=records, captures=captures, calendar_coverage=calendar_coverage,
                   origin="historical_import", provenance="explicit_import_not_provider_authenticated", clock=clock)


def acquire_once(root, *, provider, start, end, key_file=None, transport=None, clock=None):
    """One bounded data-only collection; no scheduler or broker session."""
    from atlas.research.data_collector import DataCollectorError, collect_once
    contract = load_contract(root)
    origin = "test" if transport is not None or clock is not None else "builtin_get"
    try:
        bundle = collect_once(contract, provider=provider, start=start, end=end, key_file=key_file,
                              transport=transport, clock=clock)
    except DataCollectorError as exc:
        # Preserve safely timestamped completed responses, including malformed
        # JSON, without admitting partial/invalid observations to the dataset.
        captures = exc.captures
        if captures:
            result = _append(root, records=[], captures=captures, calendar_coverage=[], origin=origin,
                             provenance="collection_failed:" + exc.code, clock=clock)
        else:
            result = {**verify_archive(root, clock=clock), "captures_retained": 0, "appended_records": 0}
        return {**result, "collection_complete": False, "error": exc.code}
    result = _append(root, **{key: bundle[key] for key in ("records", "captures", "calendar_coverage", "provenance")},
                     origin=origin, clock=clock)
    return {**result, "collection_complete": True}


def freeze_strategy(root, *, strategy_sha256, clock=None):
    """Pin a specification digest before holdout begins; no strategy is selected."""
    if not _hash(strategy_sha256):
        raise DataArchiveError("archive_strategy_sha256_invalid")
    with _connection(root, write=True) as db:
        now = receipt_time(clock)
        state = _scan(db, now)
        if state["strategy_freeze"] is not None:
            raise DataArchiveError("archive_strategy_already_frozen_new_generation_required")
        if now >= state["contract"]["windows"]["prospective_holdout"]["start"]:
            raise DataArchiveError("archive_strategy_freeze_too_late_new_generation_required")
        freeze = {"strategy_sha256": strategy_sha256, "frozen_at": now}
        body = _encoded(freeze)
        db.execute("INSERT INTO strategy_freeze VALUES (1,?,?)", (body, _sha(body)))
    return freeze


def read_bucket(root, *, bucket, as_of, strategy_sha256=None, clock=None):
    """As-of observations only; holdout requires elapsed window and frozen digest."""
    from atlas.research.data_contract_v1 import as_of_records
    with _connection(root) as db:
        now = receipt_time(clock)
        state = _scan(db, now)
        if bucket not in state["contract"]["windows"]:
            raise DataArchiveError("archive_bucket_invalid")
        if type(as_of) is not int or not 0 < as_of <= now:
            raise DataArchiveError("archive_as_of_invalid")
        if bucket == "prospective_holdout":
            freeze = state["strategy_freeze"]
            if (not freeze or freeze["strategy_sha256"] != strategy_sha256
                    or now < state["contract"]["windows"][bucket]["end_exclusive"]):
                raise DataArchiveError("archive_holdout_sealed")
        records = [row for row in state["records"] if bucket_for(state["contract"], row["event_time"], row["interval_seconds"]) == bucket]
        return as_of_records(records, as_of=as_of)


def archive_coverage(root, *, clock=None):
    from atlas.research.data_coverage import coverage_report
    with _connection(root) as db:
        now = receipt_time(clock)
        state = _scan(db, now)
        return {"archive": _summary(state), "coverage": coverage_report(
            state["contract"], state["records"], now=now,
            calendar_coverage=state["calendar_coverage"], strategy_freeze=state["strategy_freeze"])}
