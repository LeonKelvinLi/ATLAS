"""Immutable research lineage and operational holdout claims on one trusted host.

SQLite is the canonical results directory: manifests and artifact bundles commit
atomically, rather than relying on independently updated JSON files. Digests
detect corruption; they do not defend against an administrator rewriting data.
Importing this module creates no state and never opens market data.
"""

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid

PROJECT = Path(__file__).resolve().parents[2]
MAX_BUNDLE_BYTES = 25_000_000
MAX_MANIFEST_BYTES = 1_000_000
_HASH = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"exp_[0-9a-f]{32}")
_SECRET_KEYS = {"password", "secret", "token", "access_token", "refresh_token", "api_key",
                "private_key", "authorization", "cookie", "account_id", "account_number"}
_SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE attempts (id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
       manifest TEXT NOT NULL, manifest_hash TEXT NOT NULL, campaign TEXT NOT NULL,
       variant_count INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL,
       updated_at INTEGER NOT NULL, failure_code TEXT)""",
    """CREATE TABLE results (experiment_id TEXT PRIMARY KEY REFERENCES attempts(id),
       artifacts TEXT NOT NULL, artifacts_hash TEXT NOT NULL, result_hash TEXT NOT NULL)""",
    """CREATE TABLE holdouts (content_hash TEXT NOT NULL, partition_hash TEXT NOT NULL,
       plan_hash TEXT NOT NULL, state TEXT NOT NULL, claim_id TEXT, result_hash TEXT,
       observation_count INTEGER NOT NULL, observation_hash TEXT NOT NULL, record_hash TEXT NOT NULL,
       PRIMARY KEY(content_hash,partition_hash))""",
    """CREATE TABLE protected_observations (content_hash TEXT NOT NULL, partition_hash TEXT NOT NULL,
       observation_key TEXT NOT NULL, PRIMARY KEY(content_hash,partition_hash,observation_key),
       FOREIGN KEY(content_hash,partition_hash) REFERENCES holdouts(content_hash,partition_hash))""",
)


class ResearchStoreError(ValueError):
    """Research state or input could not establish a valid operation."""


class ResearchConflict(ResearchStoreError):
    pass


class HoldoutAccessError(ResearchStoreError):
    pass


def canonical(value, *, maximum=MAX_BUNDLE_BYTES):
    nodes = [0]
    def validate(item, depth=0):
        nodes[0] += 1
        if depth > 24 or nodes[0] > 1_000_000:
            raise ResearchStoreError("research JSON exceeds structural limits")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise ResearchStoreError("invalid research JSON key")
                if key.lower().replace("-", "_") in _SECRET_KEYS:
                    raise ResearchStoreError("credentials and raw account identity are prohibited")
                validate(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child, depth + 1)
        elif isinstance(item, str):
            if len(item) > MAX_MANIFEST_BYTES:
                raise ResearchStoreError("research string exceeds limit")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ResearchStoreError("research numbers must be finite")
        elif isinstance(item, int) and not isinstance(item, bool):
            if abs(item) > 10 ** 100:
                raise ResearchStoreError("research integer exceeds limit")
        elif item is not None and not isinstance(item, bool):
            raise ResearchStoreError("research artifacts must be JSON values")
    validate(value)
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > maximum:
        raise ResearchStoreError("research bundle exceeds byte limit")
    return encoded


def content_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ResearchStoreError("full lowercase SHA256 identity required")
    return value


def _text(value, *, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or not value.isascii():
        raise ResearchStoreError("bounded ASCII identifier required")
    return value


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ResearchStoreError("invalid experiment identifier")
    return value


def _observation_keys(value):
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= 10000:
        raise ResearchStoreError("observation identity list must contain 1 through 10000 keys")
    result = {_hash(item) for item in value}
    if len(result) != len(value):
        raise ResearchStoreError("observation identity list must be unique")
    return result


def validate_manifest(value):
    encoded = canonical(value, maximum=MAX_MANIFEST_BYTES)
    required = {"schema_version", "campaign", "evidence_kind", "source_identity", "dataset", "strategy",
                "features", "execution", "split", "seed"}
    if not isinstance(value, dict) or required - set(value) or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ResearchStoreError("complete version 1 experiment manifest required")
    _text(value["campaign"])
    if value["evidence_kind"] not in {"SYNTHETIC", "TEST", "HISTORICAL_UNVERIFIED", "HISTORICAL_OUT_OF_SAMPLE",
                                      "PAPER", "SHADOW"}:
        raise ResearchStoreError("explicit supported research evidence class required")
    try:
        source = value["source_identity"]
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source["git_commit"]):
            raise ResearchStoreError("exact source commit required")
        for name in ("source_tree_sha256", "lock_sha256"):
            _hash(source[name])
        if not isinstance(source["runtime"], dict) or not source["runtime"]:
            raise ResearchStoreError("runtime identity required")
        dataset = value["dataset"]
        _hash(dataset["content_sha256"])
        _hash(dataset["partition_sha256"])
        _observation_keys(dataset.get("observation_keys"))
        if dataset["role"] not in {"development", "validation", "evaluation"}:
            raise HoldoutAccessError("ordinary research cannot access sealed holdout data")
        strategy = value["strategy"]
        _text(strategy["strategy_id"])
        _text(strategy["version"])
        _hash(strategy["implementation_sha256"])
        if not isinstance(strategy["parameters"], dict):
            raise ResearchStoreError("explicit strategy parameters required")
        _text(value["features"]["version"])
        _hash(value["features"]["definitions_sha256"])
        _text(value["execution"]["version"])
        _hash(value["execution"]["implementation_sha256"])
        if not isinstance(value["execution"]["assumptions"], dict):
            raise ResearchStoreError("execution assumptions required")
        if not isinstance(value["split"], dict) or {"training", "validation", "test"} - set(value["split"]):
            raise ResearchStoreError("training validation and test periods must be explicit")
        if type(value["seed"]) is not int:
            raise ResearchStoreError("integer experiment seed required")
        count = value.get("evaluated_variant_count", 1)
        if type(count) is not int or not 1 <= count <= 10000:
            raise ResearchStoreError("bounded attempted variant count required")
    except (KeyError, TypeError):
        raise ResearchStoreError("experiment identity is incomplete") from None
    return encoded


class ResearchStore:
    def __init__(self, external_root, *, clock=time.time):
        original = Path(external_root).expanduser()
        if not original.is_absolute() or original.is_symlink():
            raise ResearchStoreError("research root must be an absolute external directory without symlink")
        self.root = original.resolve()
        if self.root.is_relative_to(PROJECT):
            raise ResearchStoreError("research state must be outside the repository")
        self.clock, self._lock, self._conn, self._failed = clock, threading.RLock(), None, False
        missing = []
        parent = self.root
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in reversed(missing):
            self._sync(directory.parent)
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ResearchStoreError("research root must be an owned directory without group or other writes")
        self.path = self.root / "research.db"
        new = False
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size == 0:
                raise ResearchStoreError("existing research database is unsafe or empty; recovery required")
        else:
            os.close(fd)
            new = True
        try:
            self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            with self._transaction():
                tables = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not tables and new:
                    for statement in _SCHEMA:
                        self._conn.execute(statement)
                    self._conn.execute("INSERT INTO metadata VALUES ('schema','atlas-research-v1')")
                elif tables != {"metadata", "attempts", "results", "holdouts", "protected_observations"}:
                    raise ResearchStoreError("research database schema is invalid")
                schema = self._conn.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
                if schema is None or schema[0] != "atlas-research-v1":
                    raise ResearchStoreError("research database schema version is invalid")
                expected = {re.search(r"CREATE TABLE (\w+)", statement).group(1): statement for statement in _SCHEMA}
                actual = {row[0]: row[1] for row in self._conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
                if actual != expected or self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
                    raise ResearchStoreError("research database constraints differ from the pinned schema")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._sync(self.root)
            self.integrity_check()
        except Exception:
            self._failed = True
            self.close()
            raise ResearchStoreError("research database cannot be opened safely") from None

    @staticmethod
    def _sync(directory):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _operation(self):
        with self._lock:
            if self._failed or self._conn is None:
                raise ResearchStoreError("research store unavailable; recovery required")
            try:
                yield
            except (sqlite3.DatabaseError, OSError):
                self._failed = True
                raise ResearchStoreError("research durability unavailable; reopen and verify before retrying") from None

    @contextmanager
    def _transaction(self):
        with self._operation():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
                self._sync(self.root)
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _record(self, experiment_id):
        row = self._conn.execute("SELECT * FROM attempts WHERE id=?", (_id(experiment_id),)).fetchone()
        if row is None:
            raise ResearchStoreError("experiment not found")
        try:
            manifest = json.loads(row["manifest"])
            if validate_manifest(manifest) != row["manifest"] or content_hash(manifest) != row["manifest_hash"]:
                raise ValueError
            if (row["campaign"] != manifest["campaign"] or row["variant_count"] != manifest.get("evaluated_variant_count", 1)
                    or row["status"] not in {"reserved", "completed", "failed"}
                    or type(row["created_at"]) is not int or row["updated_at"] < row["created_at"]):
                raise ValueError
            result = self._conn.execute("SELECT * FROM results WHERE experiment_id=?", (experiment_id,)).fetchone()
            artifacts = None
            result_hash = None
            if row["status"] == "completed":
                if result is None:
                    raise ValueError
                artifacts = json.loads(result["artifacts"])
                result_hash = content_hash({"experiment_id": experiment_id, "manifest_hash": row["manifest_hash"],
                                            "artifacts_hash": content_hash(artifacts)})
                if (canonical(artifacts) != result["artifacts"] or content_hash(artifacts) != result["artifacts_hash"]
                        or result_hash != result["result_hash"] or row["failure_code"] is not None):
                    raise ValueError
            elif result is not None or (row["status"] == "failed") != (row["failure_code"] is not None):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            self._failed = True
            raise ResearchStoreError("research record integrity failed; recovery required") from None
        return {"experiment_id": experiment_id, "status": row["status"], "manifest": manifest,
                "manifest_hash": row["manifest_hash"], "artifacts": artifacts, "result_hash": result_hash,
                "failure_code": row["failure_code"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def integrity_check(self):
        with self._operation():
            if self._conn.execute("PRAGMA quick_check").fetchone()[0] != "ok" or self._conn.execute("PRAGMA foreign_key_check").fetchone():
                self._failed = True
                raise ResearchStoreError("research database integrity failed")
            for row in self._conn.execute("SELECT id FROM attempts"):
                self._record(row[0])
            for row in self._conn.execute("SELECT * FROM holdouts"):
                _hash(row["content_hash"])
                _hash(row["partition_hash"])
                _hash(row["plan_hash"])
                if row["state"] not in {"sealed", "claimed", "consumed"}:
                    raise ResearchStoreError("holdout integrity failed")
                if (row["state"] == "sealed") != (row["claim_id"] is None):
                    raise ResearchStoreError("holdout claim integrity failed")
                if (row["state"] == "consumed") != (row["result_hash"] is not None):
                    raise ResearchStoreError("holdout result integrity failed")
                self._protected_keys(row)
            return {"valid": True, "schema": "atlas-research-v1"}

    def _protected_keys(self, row):
        payload = {key: row[key] for key in row.keys() if key != "record_hash"}
        if row["record_hash"] != content_hash(payload):
            self._failed = True
            raise ResearchStoreError("holdout state seal is corrupt")
        keys = [item[0] for item in self._conn.execute("SELECT observation_key FROM protected_observations WHERE content_hash=? AND partition_hash=?",
                                                     (row["content_hash"], row["partition_hash"]))]
        if (type(row["observation_count"]) is not int or row["observation_count"] != len(keys)
                or row["observation_hash"] != content_hash(sorted(keys))):
            self._failed = True
            raise ResearchStoreError("holdout observation coverage is corrupt")
        return {_hash(item) for item in keys}

    def assert_development_access(self, content_sha256, partition_sha256, *, observation_keys=None):
        _hash(content_sha256)
        _hash(partition_sha256)
        requested = _observation_keys(observation_keys)
        with self._operation():
            if self._conn.execute("PRAGMA foreign_key_check").fetchone():
                self._failed = True
                raise ResearchStoreError("research reference integrity is corrupt")
            for row in self._conn.execute("SELECT * FROM holdouts"):
                protected = self._protected_keys(row)
                if row["content_hash"] == content_sha256:
                    raise HoldoutAccessError("sealed content cannot be used by ordinary research, even under a new alias")
                if not protected or requested is None:
                    raise HoldoutAccessError("observation identity coverage cannot establish disjointness from sealed data")
                if requested.intersection(protected):
                    raise HoldoutAccessError("research observations overlap sealed holdout content")

    def reserve(self, manifest, request_id):
        text = validate_manifest(manifest)
        request_id = _text(request_id)
        manifest_hash = content_hash(manifest)
        with self._transaction():
            self.assert_development_access(manifest["dataset"]["content_sha256"], manifest["dataset"]["partition_sha256"],
                                           observation_keys=manifest["dataset"].get("observation_keys"))
            existing = self._conn.execute("SELECT id,manifest_hash FROM attempts WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["manifest_hash"] != manifest_hash:
                    raise ResearchConflict("request identifier already belongs to different experiment terms")
                return {**self._record(existing["id"]), "replayed": True}
            experiment_id, now = "exp_" + uuid.uuid4().hex, int(self.clock())
            self._conn.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,'reserved',?,?,NULL)",
                               (experiment_id, request_id, text, manifest_hash, manifest["campaign"],
                                manifest.get("evaluated_variant_count", 1), now, now))
            return {**self._record(experiment_id), "replayed": False}

    def complete(self, experiment_id, artifacts):
        if not isinstance(artifacts, dict) or not artifacts:
            raise ResearchStoreError("nonempty named artifact bundle required")
        if any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
               or name in {"manifest", "bundle", "export_manifest"} for name in artifacts):
            raise ResearchStoreError("artifact names must be safe ASCII identifiers")
        text = canonical(artifacts)
        with self._transaction():
            record = self._record(experiment_id)
            actual = artifacts.get("evaluated_variant_count")
            if actual is not None and (type(actual) is not int or not 0 <= actual <= record["manifest"].get("evaluated_variant_count", 1)):
                raise ResearchStoreError("actual evaluated variants must not exceed the reserved plan")
            result_hash = content_hash({"experiment_id": experiment_id, "manifest_hash": record["manifest_hash"],
                                        "artifacts_hash": content_hash(artifacts)})
            if record["status"] == "completed":
                if result_hash != record["result_hash"]:
                    raise ResearchConflict("completed research result is immutable")
                return record
            if record["status"] != "reserved":
                raise ResearchConflict("failed experiment cannot be completed or overwritten")
            self._conn.execute("INSERT INTO results VALUES (?,?,?,?)", (experiment_id, text, content_hash(artifacts), result_hash))
            self._conn.execute("UPDATE attempts SET status='completed',updated_at=? WHERE id=?", (int(self.clock()), experiment_id))
            return self._record(experiment_id)

    def fail(self, experiment_id, code):
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ResearchStoreError("sanitized research failure code required")
        with self._transaction():
            record = self._record(experiment_id)
            if record["status"] == "failed" and record["failure_code"] == code:
                return record
            if record["status"] != "reserved":
                raise ResearchConflict("finished experiment status is immutable")
            self._conn.execute("UPDATE attempts SET status='failed',failure_code=?,updated_at=? WHERE id=?",
                               (code, int(self.clock()), experiment_id))
            return self._record(experiment_id)

    def get(self, experiment_id):
        with self._operation():
            return self._record(experiment_id)

    def list(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ResearchStoreError("experiment list limit must be 1 through 100")
        with self._operation():
            ids = [row[0] for row in self._conn.execute("SELECT id FROM attempts ORDER BY created_at DESC,id DESC LIMIT ?", (limit,))]
            return [{key: value for key, value in self._record(experiment_id).items() if key != "artifacts"}
                    for experiment_id in ids]

    def compare(self, experiment_ids):
        if not isinstance(experiment_ids, list) or not 2 <= len(experiment_ids) <= 20 or len(set(experiment_ids)) != len(experiment_ids):
            raise ResearchStoreError("comparison requires 2 through 20 distinct experiment identifiers")
        return {"experiments": [self.get(_id(item)) for item in experiment_ids],
                "causal_superiority_established": False}

    def verify(self, experiment_id):
        record = self.get(experiment_id)
        return {"experiment_id": experiment_id, "valid": True, "status": record["status"],
                "manifest_hash": record["manifest_hash"], "result_hash": record["result_hash"],
                "reproduced": False, "verification": "stored_hashes_only; code/data reproduction is separate"}

    def counts(self, campaign):
        _text(campaign)
        with self._operation():
            ids = [row[0] for row in self._conn.execute("SELECT id FROM attempts WHERE campaign=?", (campaign,))]
            result = {"campaign": campaign, "attempts": len(ids), "completed": 0, "failed": 0, "reserved": 0,
                      "planned_variants": 0, "known_evaluated_variants": 0, "unknown_evaluated_attempts": 0}
            for experiment_id in ids:
                record = self._record(experiment_id)
                result[record["status"]] += 1
                result["planned_variants"] += record["manifest"].get("evaluated_variant_count", 1)
                actual = (record["artifacts"] or {}).get("evaluated_variant_count")
                if type(actual) is int:
                    result["known_evaluated_variants"] += actual
                else:
                    result["unknown_evaluated_attempts"] += 1
            result["evaluated_variants"] = None if result["unknown_evaluated_attempts"] else result["known_evaluated_variants"]
            result["variant_count_basis"] = "plans include failed and unfinished work; actual evaluated counts require completed evidence"
            result["scope"] = "selected_research_store_and_campaign_only"
            result["unrecorded_or_external_attempts"] = None
            return result

    def export(self, experiment_id, destination=None):
        record = self.get(experiment_id)
        if record["status"] != "completed":
            raise ResearchStoreError("only completed verified experiments can be exported")
        if destination is None and (self.root / "results").is_symlink():
            raise ResearchStoreError("immutable result exports cannot follow a results-directory symlink")
        target = self.root / "results" / experiment_id if destination is None else Path(destination).expanduser()
        if not target.is_absolute() or target.is_symlink() or target.resolve().is_relative_to(PROJECT):
            raise ResearchStoreError("export requires an external nonsymlink destination")
        target = target.resolve()
        files = {"manifest.json": canonical(record["manifest"]), "bundle.json": canonical(record)}
        files.update({name + ".json": canonical(value) for name, value in record["artifacts"].items()})
        if len(files) != len(record["artifacts"]) + 2:
            raise ResearchStoreError("artifact name conflicts with immutable export metadata")
        seals = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}
        files["export_manifest.json"] = canonical({"experiment_id": experiment_id, "manifest_hash": record["manifest_hash"],
                                                    "result_hash": record["result_hash"], "files": seals})
        def verify_existing():
            if not target.is_dir() or {path.name for path in target.iterdir()} != set(files):
                raise ResearchConflict("existing export differs from immutable experiment")
            for name, value in files.items():
                path = target / name
                if path.is_symlink() or not path.is_file() or path.stat().st_size != len(value.encode()):
                    raise ResearchConflict("existing export is unsafe or differs")
                if path.read_bytes() != value.encode():
                    raise ResearchConflict("existing export content differs; never overwritten")
            return target
        if target.exists():
            return verify_existing()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sync(target.parent.parent)
        staging = Path(tempfile.mkdtemp(prefix=".research-export-", dir=target.parent))
        try:
            for name, value in files.items():
                with (staging / name).open("xb") as stream:
                    stream.write(value.encode())
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o400)
                    os.fsync(stream.fileno())
            staging.chmod(0o500)
            self._sync(staging)
            try:
                os.rename(staging, target)
            except FileExistsError:
                return verify_existing()
            self._sync(target.parent)
            return target
        finally:
            if staging.exists():
                staging.chmod(0o700)
                shutil.rmtree(staging)

    def seal_holdout(self, content_sha256, partition_sha256, plan_sha256, *, observation_keys=None):
        for value in (content_sha256, partition_sha256, plan_sha256):
            _hash(value)
        keys = _observation_keys(observation_keys)
        with self._transaction():
            row = self._conn.execute("SELECT * FROM holdouts WHERE content_hash=? AND partition_hash=?", (content_sha256, partition_sha256)).fetchone()
            if row:
                if row["plan_hash"] != plan_sha256 or self._protected_keys(row) != (keys or set()):
                    raise ResearchConflict("holdout plan cannot be changed")
                return dict(row)
            for attempt in self._conn.execute("SELECT manifest FROM attempts"):
                prior = json.loads(attempt[0])["dataset"]
                known = _observation_keys(prior.get("observation_keys"))
                if prior["content_sha256"] == content_sha256:
                    raise HoldoutAccessError("previously researched content cannot become a fresh sealed holdout")
                if not known or not keys or keys.intersection(known):
                    raise HoldoutAccessError("prior research observations cannot be shown disjoint from this holdout")
            payload = {"content_hash": content_sha256, "partition_hash": partition_sha256,
                       "plan_hash": plan_sha256, "state": "sealed", "claim_id": None, "result_hash": None,
                       "observation_count": len(keys or []), "observation_hash": content_hash(sorted(keys or []))}
            self._conn.execute("INSERT INTO holdouts VALUES (?,?,?,?,?,?,?,?,?)", (*payload.values(), content_hash(payload)))
            if keys:
                self._conn.executemany("INSERT INTO protected_observations VALUES (?,?,?)", [(content_sha256, partition_sha256, key) for key in sorted(keys)])
            return {"state": "sealed", "content_hash": content_sha256, "partition_hash": partition_sha256, "plan_hash": plan_sha256}

    def claim_holdout(self, content_sha256, partition_sha256, plan_sha256, claim_id):
        for value in (content_sha256, partition_sha256, plan_sha256):
            _hash(value)
        _text(claim_id)
        with self._transaction():
            row = self._conn.execute("SELECT * FROM holdouts WHERE content_hash=? AND partition_hash=?", (content_sha256, partition_sha256)).fetchone()
            if row is None:
                raise HoldoutAccessError("holdout is absent")
            self._protected_keys(row)
            payload = {key: row[key] for key in row.keys() if key != "record_hash"}
            payload.update(state="claimed", claim_id=claim_id)
            updated = self._conn.execute("UPDATE holdouts SET state='claimed',claim_id=?,record_hash=? WHERE content_hash=? AND partition_hash=? AND plan_hash=? AND state='sealed'",
                                         (claim_id, content_hash(payload), content_sha256, partition_sha256, plan_sha256))
            if updated.rowcount != 1:
                raise HoldoutAccessError("holdout is absent, already consumed or claimed; no retry is authorized")
            return {"state": "claimed", "claim_id": claim_id, "content_hash": content_sha256, "partition_hash": partition_sha256}

    def consume_holdout(self, content_sha256, partition_sha256, claim_id, result_sha256):
        for value in (content_sha256, partition_sha256, result_sha256):
            _hash(value)
        _text(claim_id)
        with self._transaction():
            row = self._conn.execute("SELECT * FROM holdouts WHERE content_hash=? AND partition_hash=?", (content_sha256, partition_sha256)).fetchone()
            if row is None:
                raise HoldoutAccessError("holdout is absent")
            self._protected_keys(row)
            payload = {key: row[key] for key in row.keys() if key != "record_hash"}
            payload.update(state="consumed", result_hash=result_sha256)
            updated = self._conn.execute("UPDATE holdouts SET state='consumed',result_hash=?,record_hash=? WHERE content_hash=? AND partition_hash=? AND claim_id=? AND state='claimed'",
                                         (result_sha256, content_hash(payload), content_sha256, partition_sha256, claim_id))
            if updated.rowcount != 1:
                raise HoldoutAccessError("only the original outstanding claim may seal one holdout outcome")
            return {"state": "consumed", "result_hash": result_sha256}
