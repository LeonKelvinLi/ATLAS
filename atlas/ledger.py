"""Durable proposal and execution state, independent of brokers and transports.

Constructing a Ledger is explicit: importing this module creates no state.
SQLite is intended for one server on local persistent storage. Scope binding
prevents a paper database being reopened as a live database. This journal is
an audit trail, not a cryptographic defense against a compromised host.
"""

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import Callable
import uuid

from atlas.domain import financial_context


class LedgerError(RuntimeError):
    """Durable state could not establish a safe operation."""


class ConflictError(LedgerError):
    """A duplicate or outstanding operation prevents a new claim."""


class CorruptLedgerError(LedgerError):
    """State is unreadable or inconsistent; never replace it with an empty DB."""


_PENDING = frozenset({"pending", "submitting", "accepted", "unknown", "cancel_pending"})
_TERMINAL = frozenset({"filled", "rejected", "cancelled"})
_MODE_SCOPES = {"research": "research", "paper": "paper",
                "live-assisted": "live", "live-auto": "live"}
_TRANSITIONS = {
    "pending": {"submitting", "unknown", "rejected"},
    "submitting": {"accepted", "unknown", "filled", "rejected", "cancelled"},
    "accepted": {"filled", "cancel_pending", "cancelled", "unknown", "rejected"},
    "unknown": {"accepted", "filled", "rejected", "cancelled", "cancel_pending"},
    "cancel_pending": {"accepted", "filled", "cancelled", "unknown", "rejected"},
}
_SECRET_KEYS = frozenset({
    "password", "username", "email", "secret", "token", "access_token", "refresh_token",
    "api_key", "apikey", "private_key", "signing_key", "verification_key", "authorization",
    "cookie", "cookies", "session_cookie", "mfa_seed", "account_number", "account_id",
})
_SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE proposals (
       scope TEXT NOT NULL, id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
       digest TEXT NOT NULL, payload TEXT NOT NULL,
       PRIMARY KEY(scope,id), UNIQUE(scope,idempotency_key))""",
    """CREATE TABLE intents (
       scope TEXT NOT NULL, proposal_id TEXT NOT NULL, signal_id TEXT NOT NULL,
       account_fingerprint TEXT NOT NULL, proposal_hash TEXT NOT NULL,
       client_order_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
       ('pending','submitting','accepted','unknown','cancel_pending','filled','rejected','cancelled')),
       broker_order_id TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
       caller TEXT NOT NULL, details TEXT NOT NULL,
       PRIMARY KEY(scope,proposal_id), UNIQUE(scope,client_order_id),
       UNIQUE(scope,account_fingerprint,signal_id),
       FOREIGN KEY(scope,proposal_id) REFERENCES proposals(scope,id))""",
    """CREATE TABLE events (
       id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
       event TEXT NOT NULL, ts INTEGER NOT NULL, caller TEXT NOT NULL,
       proposal_id TEXT, details TEXT NOT NULL)""",
    "CREATE INDEX pending_account ON intents(scope,account_fingerprint,status)",
)


def _validate_json(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for child in value.values():
            _validate_json(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_json(child)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError("payload must contain JSON values only")


def canonical_json(payload: dict) -> str:
    """Stable, finite JSON; never coerce unknown objects or object keys."""
    _validate_json(payload)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)
    if len(text.encode("utf-8")) > 1_000_000:
        raise ValueError("payload exceeds the journal record limit")
    return text


def digest(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{label} must be a nonempty bounded string")
    return value


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("timestamp must be a nonnegative integer")
    return value


def _redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower().replace("-", "_") in _SECRET_KEYS
                else _redact(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(child) for child in value]
    return value


def _reject_secrets(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower().replace("-", "_") in _SECRET_KEYS:
                raise ValueError("proposal contains a credential or raw account identity field")
            _reject_secrets(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secrets(child)


def _external_path(value) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("ledger paths must be explicit absolute paths outside the repository")
    resolved = path.resolve()
    project = Path(__file__).resolve().parents[1]
    if resolved.is_relative_to(project):
        raise ValueError("operational ledger state must be outside the repository")
    return resolved


class Ledger:
    def __init__(self, path, scope: str, *, clock: Callable[[], float] = time.time):
        self.scope = _text(scope, "scope")
        if self.scope not in {"research", "paper", "live"}:
            raise ValueError("journal scope must be research, paper, or live")
        self.clock = clock
        self._lock = threading.RLock()
        self._failed = False
        self._conn = None
        self.path = None if str(path) == ":memory:" else _external_path(path)
        created_new = self.path is None
        if self.path is not None:
            missing = []
            parent = self.path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            self.path.parent.mkdir(parents=True, exist_ok=True)
            for parent in reversed(missing):
                self._sync_directory(parent.parent)
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if self.path.stat().st_size == 0:
                    raise CorruptLedgerError("existing execution journal is empty; recovery is required")
            else:
                os.close(descriptor)
                created_new = True
        try:
            self._conn = sqlite3.connect(str(self.path) if self.path is not None else ":memory:",
                                         isolation_level=None, check_same_thread=False, timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute("PRAGMA synchronous=FULL")
            with self._transaction():
                tables = {row[0] for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if not tables:
                    if not created_new:
                        raise CorruptLedgerError("existing execution journal has no schema; recovery is required")
                    for statement in _SCHEMA:
                        self._conn.execute(statement)
                    self._conn.execute("INSERT INTO metadata VALUES ('scope',?)", (self.scope,))
                    self._conn.execute("PRAGMA user_version=1")
                else:
                    if tables != {"metadata", "proposals", "intents", "events"}:
                        raise CorruptLedgerError("unexpected execution journal schema")
                    row = self._conn.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()
                    if row is None:
                        raise CorruptLedgerError("execution journal scope is missing")
                    if row[0] != self.scope:
                        raise LedgerError("execution journal belongs to a different scope")
                    if self._conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                        raise LedgerError("unsupported execution journal schema version")
            self._conn.execute("PRAGMA journal_mode=WAL")
            if self.path is not None:
                self._sync_directory(self.path.parent)
            self.integrity_check()
        except sqlite3.DatabaseError as exc:
            if self._conn is not None:
                self._conn.close()
            self._failed = True
            raise CorruptLedgerError("execution journal cannot be opened safely") from exc
        except Exception:
            if self._conn is not None:
                self._conn.close()
            self._failed = True
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def _operation(self):
        with self._lock:
            if self._failed or self._conn is None:
                raise CorruptLedgerError("execution journal is unavailable; recovery is required")
            try:
                yield
            except sqlite3.DatabaseError as exc:
                self._failed = True
                raise CorruptLedgerError("execution journal operation failed; recovery is required") from exc
            except CorruptLedgerError:
                self._failed = True
                raise

    @contextmanager
    def _transaction(self):
        with self._operation():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
                if self.path is not None:
                    try:
                        self._sync_directory(self.path.parent)
                    except OSError as exc:
                        raise CorruptLedgerError("journal commit may exist but directory synchronization failed; recovery is required") from exc
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def integrity_check(self):
        with self._operation():
            results = [row[0] for row in self._conn.execute("PRAGMA integrity_check")]
            if results != ["ok"] or self._conn.execute("PRAGMA foreign_key_check").fetchone():
                raise CorruptLedgerError("execution journal integrity check failed")
            for row in self._conn.execute("SELECT * FROM proposals"):
                if row["scope"] != self.scope:
                    raise CorruptLedgerError("execution journal contains a foreign scope")
                self._proposal(row)
            for row in self._conn.execute("SELECT * FROM intents"):
                if row["scope"] != self.scope:
                    raise CorruptLedgerError("execution journal contains a foreign scope")
                original = self._conn.execute("SELECT digest FROM proposals WHERE scope=? AND id=?",
                                              (self.scope, row["proposal_id"])).fetchone()
                if original is None or original[0] != row["proposal_hash"]:
                    raise CorruptLedgerError("execution intent does not match its proposal")
                self._intent(row)
            for row in self._conn.execute("SELECT scope,details FROM events"):
                try:
                    if row["scope"] != self.scope or not isinstance(json.loads(row["details"]), dict):
                        raise ValueError("invalid audit event")
                except (ValueError, TypeError) as exc:
                    raise CorruptLedgerError("execution audit event is invalid") from exc

    def _proposal(self, row):
        try:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict) or digest(payload) != row["digest"] or payload.get("id") != row["id"]:
                raise ValueError("proposal mismatch")
            if _MODE_SCOPES.get(payload.get("mode")) != self.scope:
                raise ValueError("proposal belongs to another scope")
            _reject_secrets(payload)
            return payload
        except (ValueError, TypeError, KeyError) as exc:
            raise CorruptLedgerError("stored proposal failed its immutable digest check") from exc

    def _intent(self, row, *, replayed=False):
        try:
            result = dict(row)
            result["details"] = json.loads(result["details"])
            if result["status"] not in _PENDING | _TERMINAL or not isinstance(result["details"], dict):
                raise ValueError("invalid state")
            for key in ("proposal_id", "signal_id", "account_fingerprint", "proposal_hash", "client_order_id", "caller"):
                _text(result[key], key)
            if result["scope"] != self.scope or _timestamp(result["updated_at"]) < _timestamp(result["created_at"]):
                raise ValueError("invalid execution scope or timestamps")
            proposal = self._conn.execute("SELECT * FROM proposals WHERE scope=? AND id=?",
                                           (self.scope, result["proposal_id"])).fetchone()
            if proposal is None:
                raise ValueError("proposal missing")
            payload = self._proposal(proposal)
            if (result["proposal_hash"] != proposal["digest"]
                    or result["signal_id"] != payload["signal_id"]
                    or result["account_fingerprint"] != payload["account_fingerprint"]):
                raise ValueError("execution identity mismatch")
            if result["status"] in {"accepted", "filled", "cancel_pending", "cancelled"}:
                _text(result["broker_order_id"], "broker order identifier")
            result["replayed"] = replayed
            return _redact(result)
        except (ValueError, TypeError, KeyError) as exc:
            raise CorruptLedgerError("stored execution intent is invalid") from exc

    def _event(self, event, details, now, caller, proposal_id):
        self._conn.execute(
            "INSERT INTO events(scope,event,ts,caller,proposal_id,details) VALUES (?,?,?,?,?,?)",
            (self.scope, _text(event, "event"), _timestamp(now), _text(caller, "caller"),
             proposal_id, canonical_json(_redact(details))))

    def record_event(self, event: str, details: dict, *, now: int, caller: str,
                     proposal_id: str | None = None):
        if not isinstance(details, dict):
            raise ValueError("event details must be a JSON object")
        canonical_json(details)
        with self._transaction():
            self._event(event, details, now, caller, proposal_id)

    def save_proposal(self, payload: dict, idempotency_key: str) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("proposal must be a JSON object")
        canonical = canonical_json(payload)
        _reject_secrets(payload)
        for key in ("id", "signal_id", "account_fingerprint", "mode", "policy_hash"):
            _text(payload.get(key), key)
        if _MODE_SCOPES.get(payload["mode"]) != self.scope:
            raise ValueError("proposal mode does not belong to this journal scope")
        for key in ("order", "evidence"):
            if not isinstance(payload.get(key), dict):
                raise ValueError(f"proposal {key} must be a JSON object")
        created = _timestamp(payload.get("created_at"))
        expiry = _timestamp(payload.get("expires_at"))
        if expiry <= created:
            raise ValueError("proposal expiry must follow creation")
        _text(idempotency_key, "idempotency key")
        hashed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._transaction():
            existing = self._conn.execute("SELECT * FROM proposals WHERE scope=? AND idempotency_key=?",
                                          (self.scope, idempotency_key)).fetchone()
            if existing is not None:
                original = self._proposal(existing)
                if existing["digest"] != hashed:
                    raise ConflictError("idempotency key already belongs to a different proposal")
                return original
            if self._conn.execute("SELECT 1 FROM proposals WHERE scope=? AND id=?",
                                   (self.scope, payload["id"])).fetchone():
                raise ConflictError("proposal identifier already exists")
            self._conn.execute("INSERT INTO proposals VALUES (?,?,?,?,?)",
                               (self.scope, payload["id"], idempotency_key, hashed, canonical))
            self._event("proposal_created", {"proposal_hash": hashed}, created, "decision-engine", payload["id"])
        return json.loads(canonical)

    def get_proposal(self, proposal_id: str) -> dict:
        with self._operation():
            row = self._conn.execute("SELECT * FROM proposals WHERE scope=? AND id=?",
                                     (self.scope, proposal_id)).fetchone()
            if row is None:
                raise KeyError("proposal not found")
            return self._proposal(row)

    def get_proposal_by_idempotency_key(self, idempotency_key: str) -> dict | None:
        with self._operation():
            row = self._conn.execute("SELECT * FROM proposals WHERE scope=? AND idempotency_key=?",
                                     (self.scope, idempotency_key)).fetchone()
            return self._proposal(row) if row is not None else None

    def get_intent(self, proposal_id: str) -> dict | None:
        with self._operation():
            row = self._conn.execute("SELECT * FROM intents WHERE scope=? AND proposal_id=?",
                                     (self.scope, proposal_id)).fetchone()
            return self._intent(row) if row is not None else None

    def has_pending(self, account_fingerprint: str, exclude_proposal_id: str | None = None) -> bool:
        """Query every outstanding intent, never a truncated observation list."""
        with self._operation():
            query = ("SELECT 1 FROM intents WHERE scope=? AND account_fingerprint=? "
                     "AND status IN (?,?,?,?,?)")
            parameters = [self.scope, account_fingerprint, *sorted(_PENDING)]
            if exclude_proposal_id is not None:
                query += " AND proposal_id<>?"
                parameters.append(exclude_proposal_id)
            return self._conn.execute(query + " LIMIT 1", parameters).fetchone() is not None

    def claim_intent(self, proposal_id: str, now: int, caller: str) -> dict:
        _timestamp(now)
        _text(caller, "caller")
        with self._transaction():
            row = self._conn.execute("SELECT * FROM intents WHERE scope=? AND proposal_id=?",
                                     (self.scope, proposal_id)).fetchone()
            if row is not None:
                return self._intent(row, replayed=True)
            proposal = self.get_proposal(proposal_id)
            if proposal.get("kind") == "cancel":
                raise ConflictError("cancellation proposals require claim_cancellation")
            if now < proposal["created_at"] or now >= proposal["expires_at"]:
                raise ConflictError("proposal is expired or not yet valid")
            account = proposal["account_fingerprint"]
            pending = self._conn.execute(
                "SELECT 1 FROM intents WHERE scope=? AND account_fingerprint=? AND status IN (?,?,?,?,?)",
                (self.scope, account, *sorted(_PENDING))).fetchone()
            if pending:
                raise ConflictError("account has an unresolved execution intent; reconciliation is required")
            duplicate = self._conn.execute(
                "SELECT 1 FROM intents WHERE scope=? AND account_fingerprint=? AND signal_id=?",
                (self.scope, account, proposal["signal_id"])).fetchone()
            if duplicate:
                raise ConflictError("this signal already has an execution intent for the account")
            identifier = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.scope, proposal_id, proposal["signal_id"], account, digest(proposal), identifier,
                 "submitting", None, now, now, caller, "{}"))
            self._event("intent_claimed", {"client_order_id": identifier, "status": "submitting"},
                        now, caller, proposal_id)
            row = self._conn.execute("SELECT * FROM intents WHERE scope=? AND proposal_id=?",
                                     (self.scope, proposal_id)).fetchone()
            result = self._intent(row)
        return result  # COMMIT precedes the only return that permits a submission.

    def complete_intent(self, proposal_id: str, status: str, broker_order_id=None,
                        details=None, *, now: int | None = None) -> dict:
        if status not in _PENDING | _TERMINAL:
            raise ValueError("unknown execution state")
        if details is not None and not isinstance(details, dict):
            raise ValueError("intent details must be a JSON object")
        if details is not None:
            canonical_json(details)
        stamp = _timestamp(int(self.clock()) if now is None else now)
        clean = _redact(details) if details is not None else None
        if clean is not None:
            canonical_json(clean)
        if broker_order_id is not None:
            if isinstance(broker_order_id, bool) or not isinstance(broker_order_id, (int, str)) or not str(broker_order_id).strip():
                raise ValueError("broker order identifier is invalid")
            broker_order_id = str(broker_order_id)
        with self._transaction():
            row = self._conn.execute("SELECT * FROM intents WHERE scope=? AND proposal_id=?",
                                     (self.scope, proposal_id)).fetchone()
            if row is None:
                raise KeyError("execution intent not found")
            if self.get_proposal(proposal_id).get("kind") == "cancel":
                raise ConflictError("cancellation intents require complete_cancellation")
            previous = self._intent(row)
            selected_id = broker_order_id if broker_order_id is not None else previous["broker_order_id"]
            selected_details = clean if clean is not None else previous["details"]
            if previous["broker_order_id"] is not None and selected_id != previous["broker_order_id"]:
                raise ConflictError("broker order identity cannot change")
            selected_details = self._checked_progress(previous, selected_details, selected_id, status)
            if stamp < previous["updated_at"]:
                raise ConflictError("execution timestamp cannot move backwards")
            if previous["status"] in _TERMINAL:
                if previous["status"] == status and previous["details"] == selected_details and previous["broker_order_id"] == selected_id:
                    previous["replayed"] = True
                    return previous
                raise ConflictError("execution state cannot be rewritten")
            if previous["status"] == status and previous["details"] == selected_details and previous["broker_order_id"] == selected_id:
                previous["replayed"] = True
                return previous
            if previous["status"] != status and status not in _TRANSITIONS.get(previous["status"], set()):
                raise ConflictError("execution state transition is not allowed")
            if status in {"accepted", "filled", "cancel_pending", "cancelled"} and selected_id is None:
                raise ValueError("acknowledged execution state requires a broker order identifier")
            self._conn.execute(
                "UPDATE intents SET status=?,broker_order_id=?,details=?,updated_at=? WHERE scope=? AND proposal_id=?",
                (status, selected_id, canonical_json(selected_details), stamp, self.scope, proposal_id))
            self._event("intent_" + status,
                        {"from_status": previous["status"], "broker_order_id": selected_id,
                         "details": selected_details}, stamp, previous["caller"], proposal_id)
            result = self._intent(self._conn.execute(
                "SELECT * FROM intents WHERE scope=? AND proposal_id=?", (self.scope, proposal_id)).fetchone())
        return result

    @staticmethod
    def _checked_progress(previous, details, broker_order_id, status):
        """Protect known broker progress while holding the write transaction.

        Service validation may have used an older snapshot from another
        connection. Only this check sees the state immediately before commit.
        An uncertain response retains the last acknowledged fill evidence.
        """
        details = dict(details)
        for key, expected in (("broker_order_id", broker_order_id),
                              ("client_order_id", previous["client_order_id"])):
            if key in details and details[key] != expected:
                raise ConflictError("broker response identity cannot change")

        def quantity(value):
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ConflictError("acknowledged fill quantity is invalid")
            try:
                amount = Decimal(str(value))
            except InvalidOperation:
                raise ConflictError("acknowledged fill quantity is invalid") from None
            if not amount.is_finite() or amount < 0:
                raise ConflictError("acknowledged fill quantity is invalid")
            return amount

        known = previous["details"].get("filled_quantity")
        known_quantity = quantity(known) if known is not None else Decimal("0")
        if status == "unknown" and "filled_quantity" not in details and known is not None:
            # These remain the last acknowledged observations; root status
            # stays unknown and continues to block new orders.
            details = {**previous["details"], **details}
        if "filled_quantity" in details:
            if quantity(details["filled_quantity"]) < known_quantity:
                raise ConflictError("acknowledged fill quantity cannot decrease")
        elif known_quantity > 0:
            raise ConflictError("acknowledged fill quantity cannot disappear")
        return details

    def claim_cancellation(self, cancel_proposal_id: str, target_proposal_id: str,
                           now: int, caller: str) -> dict:
        """Claim one approved cancellation while exempting only its open target."""
        _timestamp(now)
        _text(caller, "caller")
        with self._transaction():
            proposal = self.get_proposal(cancel_proposal_id)
            if (proposal.get("kind") != "cancel"
                    or proposal["order"].get("target_proposal_id") != target_proposal_id
                    or proposal["signal_id"] != "cancel:" + target_proposal_id):
                raise ConflictError("cancellation proposal does not identify the exact target")
            existing = self.get_intent(cancel_proposal_id)
            if existing is not None:
                existing["replayed"] = True
                return existing
            if now < proposal["created_at"] or now >= proposal["expires_at"]:
                raise ConflictError("cancellation proposal is expired or not yet valid")
            target = self.get_intent(target_proposal_id)
            if (target is None or target["status"] != "accepted"
                    or target["broker_order_id"] is None
                    or target["account_fingerprint"] != proposal["account_fingerprint"]
                    or proposal["order"].get("broker_order_id") != target["broker_order_id"]):
                raise ConflictError("cancellation requires the matching acknowledged account order")
            if self.has_pending(proposal["account_fingerprint"], exclude_proposal_id=target_proposal_id):
                raise ConflictError("another account execution requires reconciliation before cancellation")
            if self._conn.execute(
                "SELECT 1 FROM intents WHERE scope=? AND account_fingerprint=? AND signal_id=?",
                (self.scope, proposal["account_fingerprint"], proposal["signal_id"])).fetchone():
                raise ConflictError("this target already has a cancellation intent")
            identifier = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.scope, cancel_proposal_id, proposal["signal_id"], proposal["account_fingerprint"],
                 digest(proposal), identifier, "cancel_pending", target["broker_order_id"], now, now,
                 caller, canonical_json({"target_proposal_id": target_proposal_id})))
            self._event("cancellation_claimed", {"target_proposal_id": target_proposal_id,
                        "client_order_id": identifier}, now, caller, cancel_proposal_id)
            result = self.get_intent(cancel_proposal_id)
        return result

    def complete_cancellation(self, cancel_id: str, resultstatus: str, details=None,
                              *, now: int | None = None) -> dict:
        """Resolve cancellation and target atomically from a verified broker result.

        `accepted` is an explicitly verified still-open target, not a queued
        cancellation acknowledgement. Queued or ambiguous responses are
        `unknown`. A fill rejects the cancellation and fills only the target,
        avoiding duplicate financial results for the cancellation request.
        """
        if resultstatus not in {"accepted", "cancelled", "filled", "unknown"}:
            raise ValueError("cancellation result must be accepted, cancelled, filled, or unknown")
        if details is not None and not isinstance(details, dict):
            raise ValueError("cancellation details must be a JSON object")
        if details is not None:
            canonical_json(details)
        clean = _redact(details or {})
        canonical_json(clean)
        stamp = _timestamp(int(self.clock()) if now is None else now)
        with self._transaction():
            proposal = self.get_proposal(cancel_id)
            if proposal.get("kind") != "cancel":
                raise ConflictError("proposal is not a cancellation")
            target_id = proposal["order"].get("target_proposal_id")
            child, target = self.get_intent(cancel_id), self.get_intent(target_id)
            if child is None or target is None:
                raise ConflictError("cancellation or target intent is missing")
            if (child["account_fingerprint"] != target["account_fingerprint"]
                    or child["broker_order_id"] != target["broker_order_id"]):
                raise CorruptLedgerError("cancellation and target identities disagree")
            if child["status"] in _TERMINAL:
                child["replayed"] = True
                return {"cancellation": child, "target": target}
            if child["status"] not in {"cancel_pending", "unknown"}:
                raise ConflictError("cancellation intent has an unexpected state")
            if stamp < max(child["updated_at"], target["updated_at"]):
                raise ConflictError("cancellation timestamp cannot move backwards")
            if resultstatus != "unknown":
                self._resolve_cancellation_row(target, resultstatus, clean, stamp, child["caller"])
            child_status = ("unknown" if resultstatus == "unknown" else
                            "cancelled" if resultstatus == "cancelled" else "rejected")
            child_details = {"target_proposal_id": target_id,
                             "cancellation_outcome": resultstatus, "result": clean}
            self._resolve_cancellation_row(child, child_status, child_details, stamp, child["caller"])
            result = {"cancellation": self.get_intent(cancel_id), "target": self.get_intent(target_id)}
        return result

    def _resolve_cancellation_row(self, previous, status, details, stamp, caller):
        details = self._checked_progress(previous, details, previous["broker_order_id"], status)
        if previous["status"] in _TERMINAL:
            if previous["status"] != status:
                raise ConflictError("verified cancellation contradicts the terminal target state")
            return  # A prior terminal record remains immutable.
        if previous["status"] != status and status not in _TRANSITIONS.get(previous["status"], set()):
            raise ConflictError("cancellation state transition is not allowed")
        self._conn.execute("UPDATE intents SET status=?,details=?,updated_at=? WHERE scope=? AND proposal_id=?",
                           (status, canonical_json(details), stamp, self.scope, previous["proposal_id"]))
        self._event("cancellation_resolution", {"from_status": previous["status"],
                    "status": status, "details": details}, stamp, caller, previous["proposal_id"])

    def list_proposals(self, limit=100):
        limit = self._limit(limit)
        with self._operation():
            return [self._proposal(row) for row in self._conn.execute(
                "SELECT * FROM proposals WHERE scope=? ORDER BY rowid DESC LIMIT ?", (self.scope, limit))]

    def list_intents(self, limit=100):
        limit = self._limit(limit)
        with self._operation():
            return [self._intent(row) for row in self._conn.execute(
                "SELECT * FROM intents WHERE scope=? ORDER BY created_at DESC,rowid DESC LIMIT ?", (self.scope, limit))]

    def list_events(self, limit=100):
        limit = self._limit(limit)
        with self._operation():
            events = []
            for row in self._conn.execute("SELECT * FROM events WHERE scope=? ORDER BY id DESC LIMIT ?",
                                          (self.scope, limit)):
                event = dict(row)
                try:
                    event["details"] = json.loads(event["details"])
                    if not isinstance(event["details"], dict):
                        raise ValueError("event details are not an object")
                except (ValueError, TypeError) as exc:
                    raise CorruptLedgerError("execution event is unreadable") from exc
                events.append(_redact(event))
            return events

    @staticmethod
    def _limit(limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("view limit must be between 1 and 1000")
        return limit

    @financial_context
    def daily_stats(self, account_fingerprint: str, now: int):
        now = _timestamp(now)
        start = now // 86400 * 86400
        with self._operation():
            count = self._conn.execute(
                "SELECT count(*) FROM intents WHERE scope=? AND account_fingerprint=? AND created_at>=? AND created_at<?",
                (self.scope, account_fingerprint, start, start + 86400)).fetchone()[0]
            settled = [self._intent(row) for row in self._conn.execute(
                "SELECT * FROM intents WHERE scope=? AND account_fingerprint=? AND status IN ('filled','cancelled') AND updated_at>=? AND updated_at<?",
                (self.scope, account_fingerprint, start, start + 86400))
                if self.get_proposal(row["proposal_id"]).get("kind") != "cancel"]
            profits = []
            for row in settled:
                value = row["details"].get("realized_pnl")
                if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    continue
                try:
                    amount = Decimal(str(value))
                except InvalidOperation:
                    continue
                if amount.is_finite():
                    profits.append(amount)
            unknown = len(settled) - len(profits)
            known = str(sum(profits, Decimal("0"))) if profits else None
            return {"intents_count": count, "settlements_count": len(settled),
                    "realized_pnl": known if not unknown else None,
                    "known_realized_pnl": known, "unknown_realized_pnl_count": unknown,
                    "day_start": start, "scope": self.scope,
                    "accounting_basis": "journal_update_time_not_broker_fill_time",
                    "dates_are_fill_dates": False}

    def backup(self, destination) -> Path:
        destination = _external_path(destination)
        if self.path is not None and destination == self.path:
            raise ValueError("backup destination must differ from the ledger")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".atlas-ledger-", suffix=".db", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary)
        try:
            with self._operation():
                self.integrity_check()
                target = sqlite3.connect(temporary)
                try:
                    self._conn.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise CorruptLedgerError("execution backup failed integrity verification")
                finally:
                    target.close()
            descriptor = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, destination)
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return destination
        except sqlite3.DatabaseError as exc:
            raise CorruptLedgerError("execution backup failed; no plain-copy fallback is permitted") from exc
        finally:
            temporary.unlink(missing_ok=True)
