"""One durable, bounded Alpaca paper connectivity test, not a strategy runner.

Only one buy attempt is permitted for this account. Reservations never replenish,
including after rejection/cancellation. A crash after recording an intent permits
lookup/cancellation only, never another POST. No scheduler or application factory
uses this controller. Unknown P&L and actual fees remain unknown.
"""

from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN
import fcntl
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
                 allow_test_transport=False):
        raw = Path(state_dir).expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise PilotError("pilot_absolute_private_state_required")
        self.path = external_path(raw)
        self.client, self.clock = client, clock
        if (not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64
                or any(c not in "0123456789abcdef" for c in expected_fingerprint)):
            raise PilotError("pilot_expected_fingerprint_required")
        self.fingerprint = expected_fingerprint
        self.client_id = str(uuid5(NAMESPACE, "alpaca-paper-smoke-v1:" + expected_fingerprint))
        self.binding = {**POLICY, "account_fingerprint": expected_fingerprint,
                        "client_order_id": self.client_id}
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

    def _read(self, conn):
        try:
            rows = conn.execute("SELECT payload FROM state").fetchall()
            if len(rows) != 1:
                raise ValueError
            data = json.loads(rows[0][0])
            if data["binding"] != self.binding or type(data["sequence"]) is not int or data["sequence"] < 0:
                raise ValueError
            audit = conn.execute("SELECT id,payload FROM events ORDER BY id").fetchall()
            if (len(audit) != data["sequence"] + 1
                    or any(item[0] != index for index, item in enumerate(audit))
                    or audit[-1][1] != rows[0][0]):
                raise ValueError
            seen_attempt = False
            for index, encoded in audit:
                historical = json.loads(encoded)
                if historical["binding"] != self.binding or historical["sequence"] != index:
                    raise ValueError
                if seen_attempt and historical["intent"] is None:
                    raise ValueError
                seen_attempt = seen_attempt or historical["intent"] is not None
            intent = data["intent"]
            if intent is not None:
                if intent["request"]["client_order_id"] != self.client_id:
                    raise ValueError
                if decimal(intent["reserved_usd"]) > decimal(POLICY["smoke_reservation_usd"]):
                    raise ValueError
                if intent["submit_attempt_recorded"] is not True:
                    raise ValueError
            return data
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
            conn.execute("UPDATE state SET payload=? WHERE id=1", (payload,))
            conn.execute("INSERT INTO events VALUES (?,?,?,?)",
                         (data["sequence"], int(self.clock()), event, payload))
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

    @financial_context
    def _request(self):
        asset, quote = self.client.asset(), self.client.quote()
        if asset["symbol"] != "BTC/USD" or asset["tradable"] is not True or asset["status"] != "active":
            raise PilotError("pilot_asset_unavailable")
        bid, ask = decimal(quote["bid"], positive=True), decimal(quote["ask"], positive=True)
        if (ask < bid or (ask - bid) / ((ask + bid) / 2) * 10000 > Decimal("100")
                or not 0 <= self.clock() - quote["timestamp"] <= 30):
            raise PilotError("pilot_quote_stale_or_wide")
        price_step = decimal(asset["price_increment"], positive=True)
        qty_step = decimal(asset["min_trade_increment"], positive=True)
        price = (bid * Decimal("0.98") / price_step).to_integral_value(rounding=ROUND_DOWN) * price_step
        quantity = (Decimal("2") / Decimal("1.006") / price / qty_step).to_integral_value(rounding=ROUND_DOWN) * qty_step
        reserved = quantity * price * Decimal("1.006")
        if (quantity <= 0 or price <= 0 or reserved > 2
                or quantity < decimal(asset["min_order_size"], positive=True)
                or quantity > decimal(quote["ask_size"], positive=True)):
            raise PilotError("pilot_minimum_or_liquidity_unavailable")
        return {
            "client_order_id": self.client_id, "symbol": "BTC/USD", "side": "buy",
            "type": "limit", "time_in_force": "gtc", "qty": str(quantity), "limit_price": str(price),
        }, str(reserved), quote, asset["id"]

    @financial_context
    def _correlate(self, data, order):
        intent, request = data["intent"], data["intent"]["request"]
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
        self._identity()
        order = self.client.get_order(self.client_id)
        if order is None:
            data["intent"]["state"] = "unknown"
            self._save(conn, data, "lookup_missing_no_resubmit")
            return None
        self._correlate(data, order)
        data["intent"].update(state=order["status"], order=order)
        self._save(conn, data, "order_observed")
        return order

    def _report(self, data):
        intent = data["intent"]
        return {
            "purpose": POLICY["purpose"], "mode": "paper", "evidence_kind": self.evidence_kind,
            "account_fingerprint": self.fingerprint, "starting_cash_usd": "100",
            "smoke_cap_usd_with_modeled_fees": "2", "modeled_fee_bps": "60",
            "actual_order_fees_usd": None, "daily_realized_pnl": None,
            "submit_attempts": int(intent is not None), "remaining_submit_attempts": int(intent is None),
            "halted": self._halted(), "automatic_trading_enabled": False,
            "real_orders": 0, "intent": intent,
            "completed": bool(intent and intent["state"] in TERMINAL
                              and intent.get("order", {}).get("status") == intent["state"]),
            "state_path": str(self.db), "observed_at": int(self.clock()),
        }

    def status(self, *, reconcile=True):
        with self._locked():
            conn = self._connect()
            try:
                data = self._read(conn)
                if reconcile and data["intent"]:
                    self._observe(conn, data)
                report = self._report(data)
                if reconcile:
                    account = self._identity()
                    report["account"] = _safe_account(account)
                    report["positions"] = self.client.positions()
                    orders = self.client.orders()
                    report["unmanaged_order_count"] = sum(o["client_order_id"] != self.client_id for o in orders)
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
                if data["intent"] is not None:
                    # This holds even if the recorded request never reached the broker.
                    self._observe(conn, data)
                    return self._report(data)
                if self._halted():
                    raise PilotError("pilot_halted")
                self._flat(data["baseline_activities"])
                if self.client.get_order(self.client_id) is not None:
                    raise PilotError("pilot_prior_smoke_exists")
                request, reserved, quote, asset_id = self._request()
                data["intent"] = {
                    "request": request, "request_canonical_json": canonical_json(request),
                    "reserved_usd": reserved, "state": "submitting", "submit_attempt_recorded": True,
                    "created_at": int(self.clock()), "quote": quote, "asset_id": asset_id,
                    "cancel_attempt_recorded": False,
                }
                self._save(conn, data, "submit_intent_committed")
                # Recheck account, account history, freshness and kill state AFTER durable claim.
                self._flat(data["baseline_activities"])
                if self._halted() or not 0 <= self.clock() - quote["timestamp"] <= 30:
                    raise PilotError("pilot_pre_submit_gate_closed")
                try:
                    result = self.client.submit_limit_buy(self.client_id, request["qty"], request["limit_price"])
                    self._correlate(data, result)
                except BrokerRejected:
                    data["intent"]["state"] = "confirmed_http_rejection"
                    self._save(conn, data, "submission_rejected")
                    raise
                except Exception:
                    data["intent"]["state"] = "unknown"
                    self._save(conn, data, "submission_unknown")
                    raise UnknownSubmission("pilot_submission_unknown") from None
                data["intent"].update(state=result["status"], order=result)
                self._save(conn, data, "submission_acknowledged")
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
                if not data["intent"]:
                    raise PilotError("pilot_no_order_to_cancel")
                order = self._observe(conn, data)
                if order is None or order["status"] in TERMINAL:
                    return self._report(data)
                if not data["intent"]["cancel_attempt_recorded"]:
                    data["intent"].update(cancel_attempt_recorded=True, state="cancel_pending")
                    self._save(conn, data, "cancel_intent_committed")
                    try:
                        self.client.cancel_order(order["id"])
                    except BrokerError:
                        # A cancellation error is reconciled, never assumed to undo a fill.
                        data["intent"]["cancel_response"] = "unconfirmed"
                    else:
                        data["intent"]["cancel_response"] = "request_accepted_not_terminal"
                    self._save(conn, data, "cancel_request_finished")
                self._observe(conn, data)
                return self._report(data)
            finally:
                self._halt()
                conn.close()

    def halt(self):
        with self._locked():
            self._halt()
        return self.status(reconcile=False)
