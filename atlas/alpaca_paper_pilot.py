"""One durable, bounded Alpaca paper connectivity test, not a strategy runner.

The original one-attempt binding is never changed. An explicit, audited grant
can permit one distinct correction after a proven original refusal. Reservations
never replenish, including after rejection/cancellation. A recorded intent permits
lookup/cancellation only, never another POST. No scheduler or application factory
uses this controller. Unknown P&L and actual fees remain unknown.
"""

from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from uuid import UUID, uuid5

from atlas.config import external_path
from atlas.domain import BrokerError, BrokerRejected, UnknownSubmission, decimal, financial_context
from atlas.ledger import canonical_json
from atlas.risk import account_fingerprint


class PilotError(BrokerError):
    """A fixed, credential-free operational error."""


POLICY = {
    "schema": 1, "origin": "https://paper-api.alpaca.markets",
    "purpose": "one_shot_paper_connectivity", "starting_cash_usd": "100",
    "allocation_ceiling_usd": "100", "smoke_reservation_usd": "2",
    "modeled_fee_bps": "60", "max_attempts_lifetime": 1,
    "symbol": "BTC/USD", "side": "buy", "type": "limit", "time_in_force": "gtc",
    "limit_below_bid_bps": "200", "max_spread_bps": "100", "max_age_seconds": 30,
}
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})
NAMESPACE = UUID("d136b55a-d628-4529-8daf-2b925cc6e2fa")
CORRECTION_REASON = "broad_limit_price_compatibility"
_DENIAL_STATUSES = frozenset({400, 401, 403, 422, 429})
_DENIAL_REASONS = frozenset({"price_precision", "quantity_precision", "minimum_order",
                            "account_permission", "rate_limit", "other"})


def _denial_diagnostic(value):
    """Copy only the adapter's closed, non-text diagnostic contract."""
    keys = {"http_status", "provider_code", "reason_code", "reason_basis", "provider_message_retained"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid rejection diagnostic")
    status, code = value["http_status"], value["provider_code"]
    if (type(status) is not int or status not in _DENIAL_STATUSES
            or (code is not None and (type(code) is not int
                or not status * 100000 <= code < (status + 1) * 100000))
            or value["reason_code"] not in _DENIAL_REASONS
            or value["reason_basis"] not in {"provider_message_hint", "http_status", "unavailable"}
            or value["provider_message_retained"] is not False):
        raise ValueError("invalid rejection diagnostic")
    return dict(value)


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private(path, *, directory=False):
    info = path.lstat()
    permitted = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not permitted or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)):
        raise PilotError("pilot_private_path_required")


def _safe_account(value):
    return {k: v for k, v in value.items() if k not in {"id", "account_number", "account_id"}}


class PaperPilot:
    def __init__(self, state_dir, client, expected_fingerprint, *, clock=time.time,
                 allow_test_transport=False, correction=False):
        raw = Path(state_dir).expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise PilotError("pilot_absolute_private_state_required")
        self.path = external_path(raw)
        self.client, self.clock = client, clock
        if (not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64
                or any(c not in "0123456789abcdef" for c in expected_fingerprint)):
            raise PilotError("pilot_expected_fingerprint_required")
        self.fingerprint = expected_fingerprint
        if type(correction) is not bool:
            raise PilotError("pilot_correction_scope_invalid")
        self.correction = correction
        self.intent_key = "correction_intent" if correction else "intent"
        self.original_client_id = str(uuid5(NAMESPACE, "alpaca-paper-smoke-v1:" + expected_fingerprint))
        self.correction_client_id = str(uuid5(NAMESPACE, "alpaca-paper-smoke-correction-v1:" + expected_fingerprint))
        self.client_id = self.correction_client_id if correction else self.original_client_id
        self.binding = {**POLICY, "account_fingerprint": expected_fingerprint,
                        "client_order_id": self.original_client_id}
        if getattr(client, "transport_provenance", None) != "verified_tls" and not allow_test_transport:
            raise PilotError("pilot_verified_paper_transport_required")
        self.evidence_kind = ("verified_paper_rest" if not allow_test_transport else "injected_test_transport")
        self.db = self.path / "pilot.sqlite3"
        self.kill = self.path / "HALTED"

    @contextmanager
    def _locked(self, *, create=False):
        if create and not self.path.exists():
            _private(self.path.parent, directory=True)
            self.path.mkdir(mode=0o700)
            _sync_dir(self.path.parent)
        if not self.path.exists():
            raise PilotError("pilot_setup_required")
        _private(self.path, directory=True)
        lock_path = self.path / "operation.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            _private(lock_path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PilotError("pilot_operation_in_progress") from None
            yield
        finally:
            os.close(fd)

    def _connect(self):
        if not self.db.exists():
            raise PilotError("pilot_setup_required")
        _private(self.db)
        try:
            conn = sqlite3.connect(str(self.db), timeout=5, isolation_level=None)
            conn.execute("PRAGMA synchronous=FULL")
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise PilotError("pilot_state_corrupt")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != {"state", "events"}:
                raise PilotError("pilot_state_schema_invalid")
            return conn
        except Exception:
            if "conn" in locals():
                conn.close()
            raise PilotError("pilot_state_unavailable") from None

    @staticmethod
    def _audit_hash(audit):
        return hashlib.sha256(canonical_json({"events": [list(row) for row in audit]}).encode()).hexdigest()

    @financial_context
    def _validate_intent(self, intent, identifier):
        if intent is None:
            return None
        request = intent["request"]
        if (set(request) != {"client_order_id", "symbol", "side", "type", "time_in_force", "qty", "limit_price"}
                or request["client_order_id"] != identifier
                or any(request[k] != POLICY[k] for k in ("symbol", "side", "type", "time_in_force"))
                or intent["request_canonical_json"] != canonical_json(request)
                or intent["submit_attempt_recorded"] is not True
                or type(intent["cancel_attempt_recorded"]) is not bool
                or type(intent["created_at"]) is not int or intent["created_at"] < 0
                or str(UUID(intent["asset_id"])) != intent["asset_id"]):
            raise ValueError
        reserved = decimal(intent["reserved_usd"], positive=True)
        if (reserved > 2 or reserved != decimal(request["qty"], positive=True)
                * decimal(request["limit_price"], positive=True) * Decimal("1.006")):
            raise ValueError
        if "rejection_diagnostic" in intent:
            _denial_diagnostic(intent["rejection_diagnostic"])
        return {k: intent[k] for k in ("request", "request_canonical_json", "reserved_usd",
                                      "created_at", "quote", "asset_id", "submit_attempt_recorded")}

    def _rejection_proof(self, audit):
        """A missing lookup alone never proves that an attempted order was refused."""
        claims, refusals, proof = 0, 0, None
        for _, _, event, encoded in audit:
            historical = json.loads(encoded)
            intent = historical["intent"]
            if intent is None:
                continue
            if (intent.get("order") is not None or intent.get("cancel_attempt_recorded") is not False
                    or event in {"submission_unknown", "submission_acknowledged", "order_observed"}):
                raise PilotError("pilot_original_rejection_not_proven")
            if event == "submit_intent_committed":
                claims += 1
            if event == "submission_rejected":
                refusals += 1
                if claims != 1 or intent["state"] != "confirmed_http_rejection":
                    raise PilotError("pilot_original_rejection_not_proven")
                if intent.get("rejection_source") == "structured_http_refusal":
                    proof = {"kind": "structured_http_refusal",
                             "diagnostic": _denial_diagnostic(intent["rejection_diagnostic"])}
                elif "rejection_source" not in intent and "rejection_diagnostic" not in intent:
                    # Older controller events identified this handler but discarded
                    # the status/body. Preserve that narrower evidence exactly.
                    proof = {"kind": "legacy_rejection_handler",
                             "http_status": None, "provider_code": None, "reason_code": None}
                else:
                    raise PilotError("pilot_original_rejection_not_proven")
            if intent["state"] == "unknown" and (refusals != 1 or event != "lookup_missing_no_resubmit"):
                raise PilotError("pilot_original_rejection_not_proven")
        if claims != 1 or refusals != 1 or proof is None:
            raise PilotError("pilot_original_rejection_not_proven")
        return proof

    def _grant_terms(self):
        return {"schema": 1, "reason": CORRECTION_REASON, "reason_confirmed": False,
                "original_client_order_id": self.original_client_id,
                "client_order_id": self.correction_client_id, "max_additional_attempts": 1,
                "smoke_reservation_usd": "2", "combined_reservation_ceiling_usd": "4"}

    def _validate_audit(self, audit):
        if not audit:
            raise ValueError
        original_terms = correction_terms = first_grant = frozen_original = baseline = None
        previous_original = previous_correction = None
        grant_count = 0
        correction_revoked = False
        for index, row in enumerate(audit):
            number, stamp, event, encoded = row
            historical = json.loads(encoded)
            canonical_json(historical)
            if (number != index or historical["binding"] != self.binding
                    or type(historical["sequence"]) is not int or historical["sequence"] != index):
                raise ValueError
            observed_baseline = {k: historical[k] for k in ("initialized_at", "baseline", "baseline_activities")}
            if index == 0:
                if (event != "initialized" or historical["intent"] is not None
                        or historical.get("correction_grant") is not None
                        or historical.get("correction_intent") is not None):
                    raise ValueError
                baseline = observed_baseline
            elif observed_baseline != baseline:
                raise ValueError
            original = historical["intent"]
            checked_original = self._validate_intent(original, self.original_client_id)
            if original_terms is not None and checked_original != original_terms:
                raise ValueError
            original_terms = checked_original or original_terms
            grant = historical.get("correction_grant")
            if grant is not None and first_grant is None:
                expected_keys = set(self._grant_terms()) | {"created_at", "original_audit_sequence",
                    "original_audit_sha256", "original_reserved_usd", "rejection_evidence"}
                if (set(grant) != expected_keys or event != "correction_granted" or index == 0
                        or any(grant[k] != v for k, v in self._grant_terms().items())
                        or type(grant["created_at"]) is not int or not 0 <= stamp - grant["created_at"] <= 30
                        or type(grant["original_audit_sequence"]) is not int
                        or grant["original_audit_sequence"] != index - 1
                        or grant["original_audit_sha256"] != self._audit_hash(audit[:index])
                        or original is None or original != previous_original
                        or grant["original_reserved_usd"] != original["reserved_usd"]
                        or grant["rejection_evidence"] != self._rejection_proof(audit[:index])):
                    raise ValueError
                first_grant, frozen_original = grant, original
            if event == "correction_granted":
                grant_count += 1
            if first_grant is not None and (grant != first_grant or original != frozen_original or grant_count != 1):
                raise ValueError
            revoked = historical.get("correction_revoked", False)
            if type(revoked) is not bool or (correction_revoked and not revoked) or (revoked and grant is None):
                raise ValueError
            if revoked and not correction_revoked and event != "correction_grant_revoked":
                raise ValueError
            correction_revoked = revoked
            correction_intent = historical.get("correction_intent")
            checked_correction = self._validate_intent(correction_intent, self.correction_client_id)
            if correction_intent is not None and (grant is None or revoked or event == "correction_granted"):
                raise ValueError
            if correction_terms is not None and checked_correction != correction_terms:
                raise ValueError
            correction_terms = checked_correction or correction_terms
            for before, after in ((previous_original, original), (previous_correction, correction_intent)):
                if before is not None:
                    if before["cancel_attempt_recorded"] and not after["cancel_attempt_recorded"]:
                        raise ValueError
                    for key in ("rejection_source", "rejection_diagnostic"):
                        if key in before and after.get(key) != before[key]:
                            raise ValueError
            if sum((decimal(item["reserved_usd"]) for item in (original, correction_intent) if item), Decimal(0)) > 4:
                raise ValueError
            previous_original, previous_correction = original, correction_intent
        return historical

    def _read(self, conn):
        try:
            rows = conn.execute("SELECT payload FROM state").fetchall()
            audit = conn.execute("SELECT id,ts,event,payload FROM events ORDER BY id").fetchall()
            if len(rows) != 1 or not audit or audit[-1][3] != rows[0][0]:
                raise ValueError
            return self._validate_audit(audit)
        except Exception:
            raise PilotError("pilot_binding_or_state_invalid") from None

    def _save(self, conn, data, event):
        # Lock covers all network operations; SQLite atomically records state+audit.
        previous = data["sequence"]
        data["sequence"] += 1
        try:
            conn.execute("BEGIN IMMEDIATE")
            stored = json.loads(conn.execute("SELECT payload FROM state WHERE id=1").fetchone()[0])
            if stored["sequence"] != previous or stored["binding"] != self.binding:
                raise PilotError("pilot_state_changed")
            payload = canonical_json(data)
            audit = conn.execute("SELECT id,ts,event,payload FROM events ORDER BY id").fetchall()
            timestamp = int(self.clock())
            self._validate_audit([*audit, (data["sequence"], timestamp, event, payload)])
            conn.execute("UPDATE state SET payload=? WHERE id=1", (payload,))
            conn.execute("INSERT INTO events VALUES (?,?,?,?)",
                         (data["sequence"], timestamp, event, payload))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise PilotError("pilot_persistence_failed") from None

    def _halt(self):
        fd = os.open(self.kill, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fsync(fd)
        os.close(fd)
        _sync_dir(self.path)

    def _halted(self):
        try:
            self.kill.lstat()
        except FileNotFoundError:
            return False
        return True

    def _identity(self):
        value = self.client.account()
        if account_fingerprint("alpaca_paper", value["id"]) != self.fingerprint:
            raise PilotError("pilot_account_mismatch")
        return value

    def _flat(self, expected_activities=None):
        began = self.clock()
        account = self._identity()
        positions, orders = self.client.positions(), self.client.orders()
        activities = self.client.activities()
        repeated = self._identity()
        for value in (account, repeated):
            if (value["currency"] != "USD" or value["status"] != "ACTIVE"
                    or value["crypto_status"] != "ACTIVE"
                    or any(value[k] is not False for k in (
                        "account_blocked", "trading_blocked", "trade_suspended_by_user"))
                    or any(decimal(value[k]) != Decimal("100") for k in (
                        "cash", "equity", "non_marginable_buying_power"))):
                raise PilotError("pilot_clean_100_account_required")
        if positions or orders:
            raise PilotError("pilot_unmanaged_positions_or_history")
        if expected_activities is not None and activities != expected_activities:
            raise PilotError("pilot_unexplained_account_activity")
        if not 0 <= self.clock() - began <= 30:
            raise PilotError("pilot_account_snapshot_stale")
        return _safe_account(repeated), activities

    def setup(self):
        if self.correction:
            raise PilotError("pilot_original_setup_required")
        with self._locked(create=True):
            if self.db.exists():
                conn = self._connect()
                try:
                    return self._report(self._read(conn))
                finally:
                    conn.close()
            if self._halted():
                raise PilotError("pilot_halted")
            baseline, activities = self._flat()
            if self.client.get_order(self.client_id) is not None:
                raise PilotError("pilot_prior_smoke_exists")
            data = {"binding": self.binding, "sequence": 0, "initialized_at": int(self.clock()),
                    "baseline": baseline, "baseline_activities": activities, "intent": None}
            fd = os.open(self.db, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            # Interrupted initialization leaves an unusable DB; it is never silently replaced.
            conn = sqlite3.connect(str(self.db), isolation_level=None)
            try:
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
                conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL)")
                encoded = canonical_json(data)
                conn.execute("INSERT INTO state VALUES (1,?)", (encoded,))
                conn.execute("INSERT INTO events VALUES (0,?,?,?)", (int(self.clock()), "initialized", encoded))
                conn.execute("COMMIT")
                _sync_dir(self.path)
            finally:
                conn.close()
            return self._report(data)

    def _correction_gate(self, data):
        began = self.clock()
        self._flat(data["baseline_activities"])
        for identifier in (self.original_client_id, self.correction_client_id):
            if self.client.get_order(identifier) is not None:
                raise PilotError("pilot_prior_smoke_exists")
        self._identity()
        if not 0 <= self.clock() - began <= 30:
            raise PilotError("pilot_account_snapshot_stale")

    def grant_correction(self):
        """Append one scoped correction grant; never reset state or remove HALTED."""
        with self._locked():
            conn = self._connect()
            try:
                data = self._read(conn)
                if data.get("correction_grant") is not None:
                    # Once the new intent exists, only its ordinary reconciliation
                    # path runs. A filled correction must never need a flat reset.
                    return self._report(data)
                audit = conn.execute("SELECT id,ts,event,payload FROM events ORDER BY id").fetchall()
                proof = self._rejection_proof(audit)
                self._correction_gate(data)
                self._halt()
                data["correction_grant"] = {
                    **self._grant_terms(), "created_at": int(self.clock()),
                    "original_audit_sequence": data["sequence"],
                    "original_audit_sha256": self._audit_hash(audit),
                    "original_reserved_usd": data["intent"]["reserved_usd"],
                    "rejection_evidence": proof,
                }
                data["correction_intent"] = None
                self._save(conn, data, "correction_granted")
                return self._report(data)
            finally:
                conn.close()

    def _active(self, data):
        return data.get(self.intent_key)

    def _event(self, name):
        return "correction_" + name if self.correction else name

    @financial_context
    def _request(self):
        asset, quote = self.client.asset(), self.client.quote()
        if asset["symbol"] != "BTC/USD" or asset["tradable"] is not True or asset["status"] != "active":
            raise PilotError("pilot_asset_unavailable")
        bid, ask = decimal(quote["bid"], positive=True), decimal(quote["ask"], positive=True)
        if (ask < bid or (ask - bid) / ((ask + bid) / 2) * 10000 > Decimal("100")
                or not 0 <= self.clock() - quote["timestamp"] <= 30):
            raise PilotError("pilot_quote_stale_or_wide")
        asset_price_step = decimal(asset["price_increment"], positive=True)
        price_step = asset_price_step
        target = bid * Decimal("0.98")
        if target >= 1:
            # Conservative compatibility with the broad >=$1 cent grid. This
            # is not a claim that price precision caused an earlier rejection.
            cent = Decimal("0.01")
            if asset_price_step <= cent and cent % asset_price_step == 0:
                price_step = cent
            elif asset_price_step > cent and asset_price_step % cent == 0:
                price_step = asset_price_step
            else:
                raise PilotError("pilot_price_grids_incompatible")
        qty_step = decimal(asset["min_trade_increment"], positive=True)
        price = (target / price_step).to_integral_value(rounding=ROUND_DOWN) * price_step
        if price <= 0:
            raise PilotError("pilot_minimum_or_liquidity_unavailable")
        quantity = (Decimal("2") / Decimal("1.006") / price / qty_step).to_integral_value(rounding=ROUND_DOWN) * qty_step
        reserved = quantity * price * Decimal("1.006")
        if (quantity <= 0 or price <= 0 or reserved > 2
                or quantity < decimal(asset["min_order_size"], positive=True)
                or quantity > decimal(quote["ask_size"], positive=True)):
            raise PilotError("pilot_minimum_or_liquidity_unavailable")
        return {
            "client_order_id": self.client_id, "symbol": "BTC/USD", "side": "buy",
            "type": "limit", "time_in_force": "gtc", "qty": format(decimal(quantity), "f"),
            "limit_price": format(decimal(price), "f"),
        }, str(reserved), quote, asset["id"]

    @financial_context
    def _correlate(self, data, order):
        intent = self._active(data)
        request = intent["request"]
        if order["asset_id"] != intent["asset_id"]:
            raise PilotError("pilot_order_asset_mismatch")
        for field in ("client_order_id", "symbol", "side", "type", "time_in_force"):
            if order[field] != request[field]:
                raise PilotError("pilot_order_correlation_failed")
        if any(decimal(order[k]) != decimal(request[k]) for k in ("qty", "limit_price")):
            raise PilotError("pilot_order_terms_changed")
        old = intent.get("order")
        if old is not None:
            if (old["id"] != order["id"] or decimal(order["filled_qty"]) < decimal(old["filled_qty"])
                    or (old["status"] in TERMINAL and order["status"] != old["status"])
                    or (old["status"] in TERMINAL and decimal(order["filled_qty"]) != decimal(old["filled_qty"]))):
                raise PilotError("pilot_order_progress_regressed")
            if old["status"] in TERMINAL:
                previous_average = old.get("filled_avg_price")
                latest_average = order.get("filled_avg_price")
                if ((previous_average is None) != (latest_average is None)
                        or (previous_average is not None
                            and decimal(previous_average) != decimal(latest_average))):
                    raise PilotError("pilot_terminal_fill_economics_changed")
            if decimal(old["filled_qty"]) > 0:
                old_value = decimal(old["filled_qty"]) * decimal(old["filled_avg_price"], positive=True)
                new_value = decimal(order["filled_qty"]) * decimal(order["filled_avg_price"], positive=True)
                if new_value < old_value:
                    raise PilotError("pilot_cumulative_fill_value_regressed")
        if decimal(order["filled_qty"]) > decimal(request["qty"]):
            raise PilotError("pilot_order_overfilled")
        if order.get("filled_avg_price") is not None and decimal(order["filled_avg_price"]) > decimal(request["limit_price"]):
            raise PilotError("pilot_buy_limit_violated")

    def _observe(self, conn, data):
        if not self.correction and data.get("correction_grant") is not None:
            # The original attempt becomes immutable evidence for the grant.
            # Full account/history reads in status still expose later activity.
            self._identity()
            if self.client.get_order(self.original_client_id) is not None:
                raise PilotError("pilot_original_order_appeared_after_grant")
            return data["intent"].get("order")
        self._identity()
        order = self.client.get_order(self.client_id)
        intent = self._active(data)
        if order is None:
            intent["state"] = "unknown"
            self._save(conn, data, self._event("lookup_missing_no_resubmit"))
            return None
        self._correlate(data, order)
        intent.update(state=order["status"], order=order)
        self._save(conn, data, self._event("order_observed"))
        return order

    @financial_context
    def _report(self, data):
        intent = self._active(data)
        original, correction_intent = data["intent"], data.get("correction_intent")
        original_reserve = decimal(original["reserved_usd"]) if original else Decimal(0)
        correction_reserve = decimal(correction_intent["reserved_usd"]) if correction_intent else Decimal(0)
        grant = data.get("correction_grant")
        return {
            "purpose": POLICY["purpose"], "mode": "paper", "evidence_kind": self.evidence_kind,
            "account_fingerprint": self.fingerprint, "starting_cash_usd": "100",
            "smoke_cap_usd_with_modeled_fees": "2", "modeled_fee_bps": "60",
            "actual_order_fees_usd": None, "daily_realized_pnl": None,
            "submit_attempts": int(original is not None) + int(correction_intent is not None),
            "active_submit_attempts": int(intent is not None),
            "active_scope": "correction" if self.correction else "original",
            "remaining_submit_attempts": int(intent is None and (not self.correction
                or (grant is not None and not data.get("correction_revoked", False)))),
            "halted": self._halted(), "automatic_trading_enabled": False,
            "real_orders": 0, "intent": intent, "original_intent": original,
            "correction_grant": grant, "correction_revoked": data.get("correction_revoked", False),
            "original_reserved_usd": str(original_reserve),
            "correction_reserved_usd": str(correction_reserve),
            "total_reserved_usd_with_modeled_fees": str(original_reserve + correction_reserve),
            "allocation_ceiling_usd": "100", "modeled_combined_reservation_ceiling_usd": "4" if grant else "2",
            "completed": bool(intent and intent["state"] in TERMINAL
                              and intent.get("order", {}).get("status") == intent["state"]),
            "state_path": str(self.db), "observed_at": int(self.clock()),
        }

    def status(self, *, reconcile=True):
        with self._locked():
            conn = self._connect()
            try:
                data = self._read(conn)
                if reconcile and self._active(data):
                    self._observe(conn, data)
                report = self._report(data)
                if reconcile:
                    account = self._identity()
                    report["account"] = _safe_account(account)
                    report["positions"] = self.client.positions()
                    orders = self.client.orders()
                    owned_ids = {self.original_client_id}
                    if data.get("correction_grant") is not None:
                        owned_ids.add(self.correction_client_id)
                    report["unmanaged_order_count"] = sum(o["client_order_id"] not in owned_ids for o in orders)
                    report["original_order_present_after_correction_grant"] = bool(
                        data.get("correction_grant") and any(o["client_order_id"] == self.original_client_id for o in orders))
                    if report["original_order_present_after_correction_grant"]:
                        raise PilotError("pilot_original_order_appeared_after_grant")
                    report["open_order_count"] = sum(o["status"] not in TERMINAL for o in orders)
                    report["activities"] = self.client.activities()
                    report["fees_may_be_reported_later"] = True
                return report
            finally:
                conn.close()

    def submit_once(self):
        with self._locked():
            conn = self._connect()
            try:
                data = self._read(conn)
                if self._active(data) is not None:
                    # This holds even if the recorded request never reached the broker.
                    self._observe(conn, data)
                    return self._report(data)
                if self.correction and data.get("correction_grant") is None:
                    raise PilotError("pilot_correction_grant_required")
                if self.correction and data.get("correction_revoked", False):
                    raise PilotError("pilot_correction_grant_revoked")
                if not self.correction and self._halted():
                    raise PilotError("pilot_halted")
                if self.correction:
                    self._correction_gate(data)
                else:
                    self._flat(data["baseline_activities"])
                    if self.client.get_order(self.client_id) is not None:
                        raise PilotError("pilot_prior_smoke_exists")
                request, reserved, quote, asset_id = self._request()
                intent = {
                    "request": request, "request_canonical_json": canonical_json(request),
                    "reserved_usd": reserved, "state": "submitting", "submit_attempt_recorded": True,
                    "created_at": int(self.clock()), "quote": quote, "asset_id": asset_id,
                    "cancel_attempt_recorded": False,
                }
                data[self.intent_key] = intent
                self._save(conn, data, self._event("submit_intent_committed"))
                # Recheck account, account history, freshness and kill state AFTER durable claim.
                if self.correction:
                    self._correction_gate(data)
                else:
                    self._flat(data["baseline_activities"])
                if (not self.correction and self._halted()) or not 0 <= self.clock() - quote["timestamp"] <= 30:
                    raise PilotError("pilot_pre_submit_gate_closed")
                try:
                    result = self.client.submit_limit_buy(self.client_id, request["qty"], request["limit_price"])
                    self._correlate(data, result)
                except BrokerRejected as exc:
                    intent["state"] = "confirmed_http_rejection"
                    intent["rejection_source"] = "broker_rejection_without_http_evidence"
                    diagnostic = getattr(exc, "diagnostic", None)
                    if callable(diagnostic):
                        try:
                            intent["rejection_diagnostic"] = _denial_diagnostic(diagnostic())
                            intent["rejection_source"] = "structured_http_refusal"
                        except Exception:
                            pass  # Never store arbitrary provider text or exception fields.
                    self._save(conn, data, self._event("submission_rejected"))
                    raise
                except Exception:
                    intent["state"] = "unknown"
                    self._save(conn, data, self._event("submission_unknown"))
                    raise UnknownSubmission("pilot_submission_unknown") from None
                intent.update(state=result["status"], order=result)
                self._save(conn, data, self._event("submission_acknowledged"))
                return self._report(data)
            except Exception:
                self._halt()
                raise
            finally:
                conn.close()

    def cancel_once(self):
        # Halt blocks new exposure, not cancellation of this exact owned order.
        with self._locked():
            conn = self._connect()
            try:
                data = self._read(conn)
                intent = self._active(data)
                if not intent:
                    raise PilotError("pilot_no_order_to_cancel")
                order = self._observe(conn, data)
                if order is None or order["status"] in TERMINAL:
                    return self._report(data)
                if not intent["cancel_attempt_recorded"]:
                    intent.update(cancel_attempt_recorded=True, state="cancel_pending")
                    self._save(conn, data, self._event("cancel_intent_committed"))
                    try:
                        self.client.cancel_order(order["id"])
                    except BrokerError:
                        # A cancellation error is reconciled, never assumed to undo a fill.
                        intent["cancel_response"] = "unconfirmed"
                    else:
                        intent["cancel_response"] = "request_accepted_not_terminal"
                    self._save(conn, data, self._event("cancel_request_finished"))
                self._observe(conn, data)
                return self._report(data)
            finally:
                self._halt()
                conn.close()

    def halt(self):
        with self._locked():
            self._halt()
            conn = self._connect()
            try:
                data = self._read(conn)
                if (data.get("correction_grant") is not None and data.get("correction_intent") is None
                        and not data.get("correction_revoked", False)):
                    data["correction_revoked"] = True
                    self._save(conn, data, "correction_grant_revoked")
            finally:
                conn.close()
        return self.status(reconcile=False)
