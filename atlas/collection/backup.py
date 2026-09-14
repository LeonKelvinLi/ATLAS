"""Private, bounded collector snapshots and restores; no credentials or orders.

Only a collector's explicitly selected state tree is eligible. SQLite databases
are copied through SQLite's online backup API while holding the collector's
snapshot barrier. Failed snapshots are retained and never enter retention.
Approved research_artifacts may contain .db journals and opaque .pkl models;
model bytes are never imported, unpickled, evaluated, or used for trading.
"""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import time


FORMAT = "ATLAS_COLLECTION_BACKUP_V1"
MAX_FILES = 10000
MAX_FILE_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 16 * 1024**3
MAX_ARTIFACT_BYTES = 16 * 1024**2
MAX_OPAQUE_ARTIFACT_BYTES = 256 * 1024**2
MAX_MANIFEST_BYTES = 4 * 1024**2
SNAPSHOT_TIMEOUT_SECONDS = 45
_NAME = re.compile(r"atlas-backup-\d{8}T\d{6}Z-[a-f0-9]{16}")
_HASH = re.compile(r"[a-f0-9]{64}")
_ARTIFACT_DIRS = {"registrations", "specifications", "research_artifacts"}
_ARTIFACT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".csv", ".tsv"}
_SECRET_NAME = re.compile(r"(^|[._-])(env|keys?|credentials?|secrets?|tokens?|passwords?)([._-]|$)", re.I)
_SECRET_CONTENT = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    rb"[\"']?(?:api[_-]?key|api[_-]?secret|private[_-]?key|access[_-]?token|password|authorization)[\"']?\s*[:=]",
    re.I,
)
_REPO = Path(__file__).resolve().parents[2]


class BackupError(ValueError):
    """Fixed diagnostics intentionally omit file contents and credentials."""


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _now(now):
    value = time.time() if now is None else now
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 32503680000:
        raise BackupError("backup_clock_invalid")
    return math.ceil(value)


def _path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise BackupError("backup_requires_absolute_external_path")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise BackupError("backup_symlink_refused")
    if path == _REPO or _REPO in path.parents:
        raise BackupError("backup_requires_absolute_external_path")
    return path


def _trusted(path, *, directory=False):
    try:
        info = path.lstat()
    except OSError:
        raise BackupError("backup_path_unavailable") from None
    if (info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise BackupError("backup_owner_type_or_permissions_invalid")
    return info


def _sync(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_file(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _read(path, limit):
    _trusted(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise BackupError("backup_file_size_bound_exceeded")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise BackupError("backup_file_size_bound_exceeded")
    return content


def _digest(path):
    info = _trusted(path)
    if info.st_size > MAX_FILE_BYTES:
        raise BackupError("backup_file_size_bound_exceeded")
    size, digest = 0, hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise BackupError("backup_owner_type_or_permissions_invalid")
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise BackupError("backup_file_size_bound_exceeded")
            digest.update(chunk)
    return size, digest.hexdigest()


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value:
        raise BackupError("backup_manifest_path_invalid")
    path = PurePosixPath(value)
    if (path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/"))
            or any(ord(c) < 32 for c in value)):
        raise BackupError("backup_manifest_path_invalid")
    return path


def _kind(relative):
    parts = _relative(relative).parts
    if any(_SECRET_NAME.search(part) or part.lower().endswith((".pem", ".key", ".p12", ".pfx")) for part in parts):
        return "excluded_secret_path"
    if relative in {".collector.lock", ".snapshot.lock", "restore-marker.json"}:
        return "excluded_runtime_marker"
    if len(parts) == 3 and parts[0] == "archives" and parts[2] == ".archive.lock":
        return "lock"
    if (relative == "state.sqlite3"
            or (len(parts) >= 2 and parts[0] == "research_artifacts" and PurePosixPath(relative).suffix.lower() == ".db")
            or (len(parts) == 3 and parts[0] == "archives" and parts[2] in {"archive.sqlite3", "catalog.sqlite3"})
            or (len(parts) == 5 and parts[0] == "archives" and parts[2] == "days"
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[3]) and parts[4] == "archive.sqlite3")):
        return "sqlite"
    for suffix in ("-wal", "-shm", "-journal"):
        if relative.endswith(suffix) and _kind(relative[:-len(suffix)]) == "sqlite":
            return "excluded_sqlite_sidecar"
    if len(parts) >= 2 and parts[0] == "research_artifacts" and PurePosixPath(relative).suffix.lower() == ".pkl":
        return "opaque_artifact"
    if len(parts) >= 2 and parts[0] in _ARTIFACT_DIRS and PurePosixPath(relative).suffix.lower() in _ARTIFACT_SUFFIXES:
        return "artifact"
    if len(parts) == 3 and parts[0] == "archives" and parts[2] == "registration.json":
        return "artifact"
    raise BackupError("backup_unapproved_state_file")


def _inventory(root):
    files, excluded, total = [], [], 0
    stack = [root]
    seen = 0
    while stack:
        directory = stack.pop()
        _trusted(directory, directory=True)
        children = list(islice(directory.iterdir(), MAX_FILES + 1 - seen))
        for path in sorted(children):
            seen += 1
            if seen > MAX_FILES:
                raise BackupError("backup_file_count_bound_exceeded")
            if path.is_symlink():
                raise BackupError("backup_symlink_refused")
            if path.is_dir():
                relative = path.relative_to(root).parts
                if (relative[0] not in _ARTIFACT_DIRS | {"archives"}
                        or (relative[0] == "archives" and (len(relative) > 4
                            or (len(relative) >= 3 and relative[2] != "days")
                            or (len(relative) == 4 and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative[3]))))):
                    raise BackupError("backup_unapproved_state_directory")
                stack.append(path)
                continue
            info = _trusted(path)
            relative = path.relative_to(root).as_posix()
            kind = _kind(relative)
            if kind.startswith("excluded_"):
                excluded.append({"path": relative, "reason": kind})
                continue
            limit = (MAX_ARTIFACT_BYTES if kind in {"artifact", "lock"} else
                     MAX_OPAQUE_ARTIFACT_BYTES if kind == "opaque_artifact" else MAX_FILE_BYTES)
            if info.st_size > limit:
                raise BackupError("backup_file_size_bound_exceeded")
            total += info.st_size
            if total > MAX_TOTAL_BYTES:
                raise BackupError("backup_total_size_bound_exceeded")
            files.append({"path": relative, "kind": kind})
    if "state.sqlite3" not in {entry["path"] for entry in files}:
        raise BackupError("backup_collector_state_missing")
    return sorted(files, key=lambda item: item["path"]), sorted(excluded, key=lambda item: item["path"])


@contextmanager
def _barrier(state, *, wait_seconds=0):
    path = state / ".snapshot.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        _trusted(path)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackupError("backup_collector_snapshot_busy") from None
                time.sleep(min(0.1, remaining))
        yield
    finally:
        os.close(fd)


def _sqlite_check(path):
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("backup_sqlite_integrity_failed")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("backup_sqlite_foreign_key_failed")
    except sqlite3.Error:
        raise BackupError("backup_sqlite_unavailable_or_corrupt") from None


def _sqlite_snapshot(source, target):
    _new_file(target, b"")
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SECONDS

    page_size = 4096

    def progress(status, remaining, total):
        if time.monotonic() > deadline or total * page_size > MAX_FILE_BYTES:
            raise BackupError("backup_sqlite_snapshot_bound_exceeded")

    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as src:
            src.execute("PRAGMA trusted_schema=OFF")
            src.execute("PRAGMA query_only=ON")
            page_size = src.execute("PRAGMA page_size").fetchone()[0]
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=0.01)
                dst.execute("PRAGMA journal_mode=DELETE")
                dst.execute("PRAGMA synchronous=FULL")
        _sqlite_check(target)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
    except sqlite3.Error:
        raise BackupError("backup_sqlite_snapshot_failed") from None


def _archive_checks(payload):
    from atlas.research.data_archive import verify_archive

    results = []
    archives = payload / "archives"
    if archives.exists():
        for archive in sorted(archives.iterdir()):
            try:
                if (archive / "registration.json").exists():
                    from atlas.collection.archive import verify_collection_archive
                    result = verify_collection_archive(archive, full=True)
                    keys = ("generation_id", "record_count", "batch_count", "acquisition_kinds",
                            "partition_count", "latest_timestamp", "started_at", "integrity_verified")
                else:
                    result = verify_archive(archive)
                    keys = ("generation_id", "record_count", "batch_count", "acquisition_kinds",
                            "head_sha256", "integrity_verified")
            except (ValueError, OSError):
                raise BackupError("backup_archive_integrity_failed") from None
            results.append({"path": archive.relative_to(payload).as_posix(),
                            **{key: result[key] for key in keys}})
    return results


def _parents(path, root):
    missing = []
    parent = path.parent
    while parent != root:
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o700, exist_ok=True)
        _trusted(parent, directory=True)


def _opaque_artifact(source, target=None):
    """Inspect/copy bounded bytes only. A .pkl suffix never grants load authority."""
    info = _trusted(source)
    if info.st_size > MAX_OPAQUE_ARTIFACT_BYTES:
        raise BackupError("backup_file_size_bound_exceeded")
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(source_fd, "rb") as src:
        opened = os.fstat(src.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise BackupError("backup_owner_type_or_permissions_invalid")
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if identity(opened) != identity(info):
            raise BackupError("backup_source_artifact_changed")
        output_fd = None if target is None else os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        dst = None if output_fd is None else os.fdopen(output_fd, "wb")
        try:
            size, tail = 0, b""
            while chunk := src.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_OPAQUE_ARTIFACT_BYTES:
                    raise BackupError("backup_file_size_bound_exceeded")
                examined = tail + chunk
                if _SECRET_CONTENT.search(examined):
                    raise BackupError("backup_secret_content_refused")
                tail = examined[-256:]
                if dst is not None:
                    dst.write(chunk)
            if size != info.st_size or identity(os.fstat(src.fileno())) != identity(opened):
                raise BackupError("backup_source_artifact_changed")
            if dst is not None:
                dst.flush()
                os.fsync(dst.fileno())
        finally:
            if dst is not None:
                dst.close()


def create_backup(state_dir, backup_dir, *, retention=7, wait_seconds=0, now=None):
    """Create and verify an immutable snapshot, then prune verified old snapshots.

    ``retention`` counts completed snapshots, not elapsed days. A daily host timer
    therefore retains seven daily runs by default. Failed snapshots are retained.
    """
    if type(retention) is not int or not 1 <= retention <= 365:
        raise BackupError("backup_retention_invalid")
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 300:
        raise BackupError("backup_wait_seconds_invalid")
    created = _now(now)
    state, destination = _path(state_dir), _path(backup_dir)
    if state == destination or state in destination.parents or destination in state.parents:
        raise BackupError("backup_source_destination_overlap")
    _trusted(state, directory=True)
    if not destination.exists():
        destination.mkdir(mode=0o700)
        _sync(destination.parent)
    _trusted(destination, directory=True)
    stamp = datetime.fromtimestamp(created, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"atlas-backup-{stamp}-{secrets.token_hex(8)}"
    snapshot = destination / name
    snapshot.mkdir(mode=0o700)
    payload = snapshot / "payload"
    payload.mkdir(mode=0o700)
    try:
        with _barrier(state, wait_seconds=wait_seconds):
            inventory, excluded = _inventory(state)
            files, total = [], 0
            for entry in inventory:
                source, target = state / entry["path"], payload / entry["path"]
                _parents(target, payload)
                if source.name == "registration.json" and source.parent.parent.name == "archives":
                    (target.parent / "days").mkdir(mode=0o700, exist_ok=True)
                if entry["kind"] == "sqlite":
                    _sqlite_snapshot(source, target)
                elif entry["kind"] == "opaque_artifact":
                    _opaque_artifact(source, target)
                else:
                    content = _read(source, MAX_ARTIFACT_BYTES)
                    if _SECRET_CONTENT.search(content) or (entry["kind"] == "lock" and content):
                        raise BackupError("backup_secret_content_refused")
                    _new_file(target, content)
                size, digest = _digest(target)
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise BackupError("backup_total_size_bound_exceeded")
                files.append({**entry, "size": size, "sha256": digest})
            if _inventory(state) != (inventory, excluded):
                raise BackupError("backup_source_inventory_changed")
        manifest = {"format": FORMAT, "backup_id": name, "created_at": created,
                    "consistency": "collector_snapshot_barrier_and_sqlite_online_backup",
                    "files": files, "excluded": excluded, "archives": _archive_checks(payload),
                    "contains_credentials": False, "execution_enabled": False}
        for directory, _, _ in os.walk(payload, topdown=False):
            _sync(Path(directory))
        _new_file(snapshot / "manifest.json", _encoded(manifest))
        _sync(snapshot)
        _sync(destination)
        verify_backup(snapshot)
    except (OSError, sqlite3.Error):
        raise BackupError("backup_failed_snapshot_retained") from None
    pruned, unpruned = _prune(destination, retention, preserve=name)
    return {"backup_id": name, "backup_path": str(snapshot), "integrity_verified": True,
            "file_count": len(files), "total_bytes": total, "pruned_backups": pruned,
            "invalid_backups_retained": unpruned, "execution_enabled": False}


def _json(content):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise BackupError("backup_manifest_duplicate_key")
            value[key] = item
        return value

    try:
        result = json.loads(content, object_pairs_hook=pairs)
        if _encoded(result) != content:
            raise BackupError("backup_manifest_not_canonical")
        return result
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise BackupError("backup_manifest_invalid") from None


def verify_backup(backup):
    """Verify all bytes, allowed paths, SQLite integrity and full archive history."""
    snapshot = _path(backup)
    _trusted(snapshot, directory=True)
    if not _NAME.fullmatch(snapshot.name) or {p.name for p in islice(snapshot.iterdir(), 3)} != {"payload", "manifest.json"}:
        raise BackupError("backup_layout_invalid_or_incomplete")
    manifest = _json(_read(snapshot / "manifest.json", MAX_MANIFEST_BYTES))
    required = {"format", "backup_id", "created_at", "consistency", "files", "excluded", "archives",
                "contains_credentials", "execution_enabled"}
    if (not isinstance(manifest, dict) or set(manifest) != required or manifest["format"] != FORMAT
            or manifest["backup_id"] != snapshot.name or type(manifest["created_at"]) is not int
            or manifest["created_at"] <= 0 or manifest["contains_credentials"] is not False
            or manifest["execution_enabled"] is not False
            or manifest["consistency"] != "collector_snapshot_barrier_and_sqlite_online_backup"
            or not isinstance(manifest["files"], list) or not 1 <= len(manifest["files"]) <= MAX_FILES
            or not isinstance(manifest["excluded"], list) or len(manifest["excluded"]) > MAX_FILES
            or not isinstance(manifest["archives"], list)):
        raise BackupError("backup_manifest_invalid")
    try:
        stamp = datetime.fromtimestamp(manifest["created_at"], timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except (ValueError, OverflowError, OSError):
        raise BackupError("backup_manifest_invalid") from None
    if not snapshot.name.startswith(f"atlas-backup-{stamp}-"):
        raise BackupError("backup_manifest_timestamp_mismatch")
    seen, declared = set(), []
    for entry in manifest["files"]:
        if (not isinstance(entry, dict) or set(entry) != {"path", "kind", "size", "sha256"}
                or type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_FILE_BYTES
                or not isinstance(entry["sha256"], str) or not _HASH.fullmatch(entry["sha256"])):
            raise BackupError("backup_manifest_file_invalid")
        relative = _relative(entry["path"]).as_posix()
        if relative in seen or _kind(relative) not in {"sqlite", "artifact", "opaque_artifact", "lock"} or entry["kind"] != _kind(relative):
            raise BackupError("backup_manifest_file_invalid")
        seen.add(relative)
        declared.append({"path": relative, "kind": entry["kind"]})
    for entry in manifest["excluded"]:
        if (not isinstance(entry, dict) or set(entry) != {"path", "reason"}
                or not _kind(entry["path"]).startswith("excluded_") or entry["reason"] != _kind(entry["path"])
                or entry["path"] in seen):
            raise BackupError("backup_manifest_exclusion_invalid")
        seen.add(entry["path"])
    payload = snapshot / "payload"
    actual, excluded = _inventory(payload)
    if actual != declared or excluded:
        raise BackupError("backup_manifest_inventory_mismatch")
    for entry in manifest["files"]:
        path = payload / entry["path"]
        if _digest(path) != (entry["size"], entry["sha256"]):
            raise BackupError("backup_file_hash_mismatch")
        if entry["kind"] == "sqlite":
            _sqlite_check(path)
        elif entry["kind"] == "opaque_artifact":
            _opaque_artifact(path)
        else:
            content = _read(path, MAX_ARTIFACT_BYTES)
            if _SECRET_CONTENT.search(content) or (entry["kind"] == "lock" and content):
                raise BackupError("backup_secret_content_refused")
    if _archive_checks(payload) != manifest["archives"]:
        raise BackupError("backup_archive_manifest_mismatch")
    return manifest


def _remove_verified(snapshot):
    verify_backup(snapshot)
    for directory, dirs, files in os.walk(snapshot, topdown=False, followlinks=False):
        base = Path(directory)
        _trusted(base, directory=True)
        for name in files:
            _trusted(base / name)
            (base / name).unlink()
        for name in dirs:
            _trusted(base / name, directory=True)
            (base / name).rmdir()
    snapshot.rmdir()


def _prune(destination, retention, *, preserve):
    valid, invalid = [], []
    candidates = list(islice(destination.iterdir(), MAX_FILES + 1))
    if len(candidates) > MAX_FILES:
        raise BackupError("backup_retention_scan_bound_exceeded")
    for path in candidates:
        if not _NAME.fullmatch(path.name):
            continue
        try:
            manifest = verify_backup(path)
            valid.append((manifest["created_at"], path.name, path))
        except (BackupError, OSError):
            invalid.append(path.name)
    valid.sort(reverse=True)
    keep = {name for _, name, _ in valid[:retention]} | {preserve}
    pruned = []
    for _, name, path in valid:
        if name not in keep:
            try:
                _remove_verified(path)
                pruned.append(name)
            except (BackupError, OSError):
                invalid.append(name)
    _sync(destination)
    return sorted(pruned), sorted(set(invalid))


def restore_backup(backup, destination, *, now=None):
    """Restore to a new external directory; never overwrite or activate a service."""
    snapshot, target = _path(backup), _path(destination)
    restored = _now(now)
    if target.exists() or not target.parent.is_dir() or snapshot in target.parents:
        raise BackupError("restore_requires_new_external_directory")
    manifest = verify_backup(snapshot)
    if restored < manifest["created_at"]:
        raise BackupError("restore_clock_precedes_snapshot")
    marker = {"version": 1, "restored_at": restored, "backup_id": manifest["backup_id"],
              "manifest_sha256": hashlib.sha256(_encoded(manifest)).hexdigest(),
              "collection_must_record_gap": True}
    target.mkdir(mode=0o700)
    try:
        _new_file(target / ".restore-incomplete", b"Restore is incomplete. Do not start collection.\n")
        for entry in manifest["files"]:
            source, output = snapshot / "payload" / entry["path"], target / entry["path"]
            _parents(output, target)
            if output.name == "registration.json" and output.parent.parent.name == "archives":
                (output.parent / "days").mkdir(mode=0o700, exist_ok=True)
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            # These are immutable, verified online snapshots, never live databases.
            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(source_fd, "rb") as src, os.fdopen(fd, "wb") as dst:
                if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                    raise BackupError("restore_source_type_invalid")
                size = 0
                while chunk := src.read(min(1024 * 1024, entry["size"] + 1 - size)):
                    size += len(chunk)
                    if size > entry["size"]:
                        raise BackupError("restore_file_size_changed")
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if _digest(output) != (entry["size"], entry["sha256"]):
                raise BackupError("restore_file_hash_mismatch")
            if entry["kind"] == "sqlite":
                _sqlite_check(output)
        if _archive_checks(target) != manifest["archives"]:
            raise BackupError("restore_archive_manifest_mismatch")
        _new_file(target / "restore-marker.json", _encoded(marker))
        (target / ".restore-incomplete").unlink()
        for directory, _, _ in os.walk(target, topdown=False):
            _sync(Path(directory))
        _sync(target.parent)
    except (OSError, sqlite3.Error):
        raise BackupError("restore_failed_destination_retained") from None
    return {"restored_to": str(target), "backup_id": manifest["backup_id"],
            "integrity_verified": True, "collection_must_record_gap": True,
            "file_count": len(manifest["files"]), "execution_enabled": False}
