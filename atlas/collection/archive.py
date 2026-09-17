"""Daily UTC partitions of one immutable V1 data registration, without trading.

Observations are routed by event date, so restarts and cross-midnight overlap
preserve one revision chain. Old shards are scanned one at a time on startup or
backup; each append verifies only its bounded daily shard. The catalog detects
missing shards. No holdout price export is exposed by this collection facade.
"""

from contextlib import contextmanager
from datetime import datetime, timezone, date
import fcntl
import os
from pathlib import Path
import sqlite3

from atlas.collection.primitives import _encoded, _external_path, _json, _read, _sha, _sync_dir
from atlas.research import data_archive as store
from atlas.research.data_contract_v1 import validate_contract
from atlas.research.data_collector import collect_once, DataCollectorError, safe_failure_diagnostic

MAX_DAYS = 3650
CATALOG_SQL = "CREATE TABLE partitions(day TEXT PRIMARY KEY, created_at INTEGER NOT NULL)"
CATALOG_TRIGGERS = tuple(
    f"CREATE TRIGGER no_{verb.lower()} BEFORE {verb} ON partitions BEGIN SELECT RAISE(ABORT,'append_only'); END"
    for verb in ("DELETE", "UPDATE")
)


class CollectionArchiveError(ValueError):
    pass


def _write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(_encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def init_collection_archive(root, contract, *, clock=None):
    root = _external_path(root)
    contract = validate_contract(contract)
    now = store.receipt_time(clock)
    if contract["created_at"] != now or root.exists() or not root.parent.is_dir():
        raise CollectionArchiveError("collection_registration_requires_new_path_and_current_clock")
    root.mkdir(mode=0o700)
    (root / "days").mkdir(mode=0o700)
    registration = {"partition_version": 1, "contract": contract,
                    "clock_basis": "system_utc" if clock is None else "injected_test_clock"}
    _write_new(root / "registration.json", registration)
    for name in ("catalog.sqlite3", ".archive.lock"):
        fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    with sqlite3.connect(root / "catalog.sqlite3") as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute(CATALOG_SQL)
        for sql in CATALOG_TRIGGERS:
            db.execute(sql)
    _sync_dir(root / "days")
    _sync_dir(root)
    _sync_dir(root.parent)
    return verify_collection_archive(root, clock=clock)


def _registration(root, now):
    root = _external_path(root)
    store._trusted(root, directory=True)
    store._trusted(root / "days", directory=True)
    permitted = {"registration.json", "catalog.sqlite3", "catalog.sqlite3-journal", ".archive.lock", "days"}
    if {p.name for p in root.iterdir()} - permitted:
        raise CollectionArchiveError("collection_archive_unexpected_file")
    for name in ("registration.json", "catalog.sqlite3", ".archive.lock"):
        store._trusted(_external_path(root / name))
    registration = _json(_read(root / "registration.json", max_bytes=100000))
    if (set(registration) != {"partition_version", "contract", "clock_basis"}
            or registration["partition_version"] != 1
            or registration["clock_basis"] not in {"system_utc", "injected_test_clock"}):
        raise CollectionArchiveError("collection_registration_invalid")
    contract = validate_contract(registration["contract"])
    if contract["created_at"] > now:
        raise CollectionArchiveError("collection_clock_regressed")
    return root, registration


@contextmanager
def _catalog(root, *, write=False):
    journal = root / "catalog.sqlite3-journal"
    if journal.exists() or journal.is_symlink():
        store._trusted(_external_path(journal))
    db = sqlite3.connect((root / "catalog.sqlite3").as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=0)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise CollectionArchiveError("collection_catalog_integrity_failed")
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table'").fetchall()
        if schema != [(CATALOG_SQL,)]:
            raise CollectionArchiveError("collection_catalog_schema_invalid")
        triggers = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
        if triggers != [(sql,) for sql in CATALOG_TRIGGERS]:
            raise CollectionArchiveError("collection_catalog_guards_invalid")
        yield db
        if write:
            db.commit()
            _sync_dir(root)
    finally:
        db.close()


def _days(root, db):
    rows = db.execute("SELECT day FROM partitions ORDER BY day").fetchall()
    if len(rows) > MAX_DAYS:
        raise CollectionArchiveError("collection_partition_bound_exceeded")
    days = [row[0] for row in rows]
    actual = {p.name for p in (root / "days").iterdir()}
    for day in days:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise CollectionArchiveError("collection_partition_date_invalid")
        store._trusted(_external_path(root / "days" / day), directory=True)
    if actual != set(days):
        raise CollectionArchiveError("collection_missing_or_unregistered_partition")
    return days


def _day(event):
    return datetime.fromtimestamp(event, timezone.utc).date().isoformat()


def _check_partition(state, registration, day):
    if state["contract"] != registration["contract"] or state["manifest"]["clock_basis"] != registration["clock_basis"]:
        raise CollectionArchiveError("collection_partition_contract_mismatch")
    if any(_day(row["event_time"]) != day for row in state["records"]):
        raise CollectionArchiveError("collection_record_in_wrong_partition")


def _shard(root, day, registration, now, db):
    path = root / "days" / day
    known = db.execute("SELECT day FROM partitions WHERE day=?", (day,)).fetchone()
    if known:
        if not path.exists():
            raise CollectionArchiveError("collection_registered_partition_missing")
        with store._connection(path, write=True) as child:
            _check_partition(store._scan(child, now), registration, day)
    else:
        if db.execute("SELECT COUNT(*) FROM partitions").fetchone()[0] >= MAX_DAYS:
            raise CollectionArchiveError("collection_partition_bound_exceeded")
        if path.exists():
            # A process can die after initializing a shard and before catalog
            # commit. Only an intact, matching shard may be adopted on restart.
            with store._connection(path, write=True) as child:
                _check_partition(store._scan(child, now), registration, day)
        else:
            store._create_archive(path, {"archive_version": 1, "contract": registration["contract"],
                                         "clock_basis": registration["clock_basis"]})
        db.execute("INSERT INTO partitions VALUES (?,?)", (day, now))
    return path


def verify_collection_archive(root, *, clock=None, full=True, recover=False):
    now = store.receipt_time(clock)
    root, registration = _registration(root, now)
    if type(full) is not bool or type(recover) is not bool:
        raise CollectionArchiveError("collection_verification_mode_invalid")
    totals = {"record_count": 0, "batch_count": 0, "latest_timestamp": None,
              "acquisition_kinds": {"historical_backfill": 0, "prospective_observation": 0, "test": 0}}
    with _catalog(root, write=recover) as db:
        if recover:
            # Writable SQLite opens recover hot rollback journals using SQLite's
            # own transaction protocol. They never truncate corrupt databases.
            for path in sorted((root / "days").iterdir()):
                if date.fromisoformat(path.name).isoformat() != path.name:
                    raise CollectionArchiveError("collection_partition_date_invalid")
                _shard(root, path.name, registration, now, db)
        days = _days(root, db)
        for day in days:
            with store._connection(root / "days" / day, write=recover) as child:
                if full:
                    state = store._scan(child, now)
                    _check_partition(state, registration, day)
                    summary = store._summary(state)
                    rows = state["records"]
                    latest = max((row["event_time"] for row in rows), default=None)
                else:
                    # Operational metadata only. Do not present this as hash
                    # integrity verification; startup/backups run the full scan.
                    summary = {"record_count": child.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
                               "batch_count": child.execute("SELECT COUNT(*) FROM batches").fetchone()[0]}
                    kinds = dict(child.execute("SELECT json_extract(body,'$.acquisition_kind'),COUNT(*) FROM observations GROUP BY 1"))
                    summary["acquisition_kinds"] = {kind: kinds.get(kind, 0) for kind in totals["acquisition_kinds"]}
                    latest = child.execute("SELECT MAX(json_extract(body,'$.event_time')) FROM observations").fetchone()[0]
                for key in ("record_count", "batch_count"):
                    totals[key] += summary[key]
                for key in totals["acquisition_kinds"]:
                    totals["acquisition_kinds"][key] += summary["acquisition_kinds"][key]
                if latest is not None:
                    totals["latest_timestamp"] = max(totals["latest_timestamp"] or 0, latest)
    return {**totals, "partition_count": len(days), "generation_id": registration["contract"]["generation_id"],
            "contract_sha256": _sha(_encoded(registration["contract"])),
            "started_at": registration["contract"]["created_at"], "integrity_verified": full,
            "verification_scope": "all_daily_partitions" if full else "operational_counts_only",
            "qualification_established": False, "execution_enabled": False, "orders_submitted": 0}


def acquire_collection_once(root, *, provider, start, end, key_file=None, transport=None,
                            clock=None, acquisition_mode="prospective"):
    if acquisition_mode not in {"prospective", "outage_backfill"}:
        raise CollectionArchiveError("collection_acquisition_mode_invalid")
    now = store.receipt_time(clock)
    root, registration = _registration(root, now)
    fd = os.open(root / ".archive.lock", os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            bundle = collect_once(registration["contract"], provider=provider, start=start, end=end,
                                  key_file=key_file, transport=transport, clock=clock)
            failure = None
        except DataCollectorError as exc:
            failure = safe_failure_diagnostic(exc.code, status=exc.status,
                retry_after_seconds=exc.retry_after_seconds, role=exc.role)
            bundle = {"records": [], "captures": exc.captures, "calendar_coverage": [],
                      "provenance": "collection_failed:" + failure["error"]}
        current = store.receipt_time(clock)
        grouped = {}
        for row in bundle["records"]:
            # Even after registration, a stale response is historical backfill.
            # A recovered gap can never regain prospective status through later
            # revisions; the underlying archive enforces sticky lineage.
            backfill = acquisition_mode == "outage_backfill" or current - row["event_time"] > registration["contract"]["policies"][
                "max_bar_lag_seconds" if row["record_type"] == "bar" else "max_quote_age_seconds"]
            origin = "test" if transport is not None or clock is not None else "historical_import" if backfill else "builtin_get"
            grouped.setdefault((_day(row["event_time"]), origin), []).append(row)
        if not grouped and bundle["captures"]:
            grouped[(_day(current), "test" if transport is not None or clock is not None else "builtin_get")] = []
        # Closed-market snapshots can repeat Friday's quote for days. Retain the
        # new receipt on today's partition instead of growing Friday forever.
        # Actual changes remain on the original event day and keep its chain.
        unchanged_old = 0
        for (day, origin), records in list(grouped.items()):
            path = root / "days" / day
            if day == _day(current) or not path.exists() or not records:
                continue
            with store._connection(path) as child:
                state = store._scan(child, current)
                _check_partition(state, registration, day)
                retained = []
                for row in records:
                    prior = state["latest"].get(store._identity(row))
                    if prior and store._value(row) == store._value(prior[0]):
                        unchanged_old += 1
                    else:
                        retained.append(row)
                if retained:
                    grouped[(day, origin)] = retained
                else:
                    del grouped[(day, origin)]
        if unchanged_old:
            grouped.setdefault((_day(current), "test" if transport is not None or clock is not None else "builtin_get"), [])
        results = []
        with _catalog(root, write=True) as db:
            for (day, origin), records in sorted(grouped.items()):
                # Each append samples its own durable ingestion time. A later
                # group can revisit this same shard after that time advances;
                # validate against the current clock, not the earlier routing
                # sample. The scan still rejects genuinely future batch times.
                shard = _shard(root, day, registration, store.receipt_time(clock), db)
                results.append(store._append(shard, records=records, captures=bundle["captures"],
                    calendar_coverage=bundle["calendar_coverage"], origin=origin,
                    provenance=bundle["provenance"] + (":outage_backfill" if acquisition_mode == "outage_backfill" else ""), clock=clock))
        return {"collection_complete": failure is None, **(failure or {}),
                "appended_records": sum(r["appended_records"] for r in results),
                "unchanged_observations": unchanged_old + sum(r["unchanged_observations"] for r in results),
                "captures_retained": sum(r["captures_retained"] for r in results),
                "response_bytes": sum(len(c["body"]) for c in bundle["captures"]),
                "collection_observations": [{key: row[key] for key in ("symbol", "record_type", "event_time")} for row in bundle["records"]],
                "collection_calendar_coverage": bundle["calendar_coverage"],
                "acquisition_mode": acquisition_mode, "orders_submitted": 0, "execution_enabled": False}
    finally:
        os.close(fd)
