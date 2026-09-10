"""Persistent, deterministic long-only limit-order simulator; never a network client.

The simulator fills marketable limits at supplied bid/ask with explicit fees.
It does not model queue priority, hidden liquidity, impact or future fills.
"""

from decimal import Decimal, ROUND_CEILING
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from atlas.domain import (AccountSnapshot, BrokerCapabilities, BrokerError, BrokerRejected,
                          OrderRequest, OrderResult, Position, Quote, UnknownSubmission, decimal,
                          financial_context, financial_value, json_value)

_SCHEMA = (
    "CREATE TABLE account (id TEXT PRIMARY KEY, cash TEXT NOT NULL)",
    "CREATE TABLE positions (symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL, cost TEXT NOT NULL)",
    "CREATE TABLE orders (client_id TEXT PRIMARY KEY, broker_id TEXT UNIQUE NOT NULL, "
    "request TEXT NOT NULL, result TEXT NOT NULL, ts INTEGER NOT NULL)",
)


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("identifier must be a bounded nonempty string")
    return value


class PaperBroker:
    capabilities = BrokerCapabilities(
        broker="paper", asset_classes=("equity", "crypto"), account_modes=("paper",),
        server_idempotency=True, limitations=("synthetic fills; no queue priority, impact or future-fill model",))

    def __init__(self, quotes: dict[str, Quote], *, cash=Decimal("10000"),
                 account_id="paper-demo", path=":memory:", fee_bps=Decimal("0"), clock=time.time):
        self.quotes = dict(quotes)
        self.clock = clock
        self.fee_bps = decimal(fee_bps, nonnegative=True)
        initial_cash = decimal(cash, nonnegative=True)
        self.account_id = _identifier(account_id)
        self._lock = threading.RLock()
        self._failed = False
        self.db = None
        self.path = None
        created_new = str(path) == ":memory:"
        if str(path) != ":memory:":
            path = Path(path).expanduser()
            if not path.is_absolute():
                raise ValueError("paper broker state must use an absolute path")
            path = path.resolve()
            if path.is_relative_to(Path(__file__).resolve().parents[2]):
                raise ValueError("paper broker state must remain outside the repository")
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            path.parent.mkdir(parents=True, exist_ok=True)
            for parent in reversed(missing):
                self._sync_directory(parent.parent)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if path.stat().st_size == 0:
                    raise BrokerError("paper_existing_state_empty_recovery_required")
            else:
                os.close(descriptor)
                created_new = True
            self.path = path
        try:
            self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            if created_new:
                for statement in _SCHEMA:
                    self.db.execute(statement)
                self.db.execute("PRAGMA user_version=1")
                self.db.execute("INSERT INTO account VALUES (?, ?)",
                                (account_id, str(initial_cash)))
            self._validate_state()
            self._commit()
            self.db.execute("PRAGMA journal_mode=WAL")
            if self.path is not None:
                self._sync_directory(self.path.parent)
        except Exception as exc:
            self._failed = True
            if self.db is not None:
                self.db.close()
                self.db = None
            if isinstance(exc, BrokerError):
                raise
            raise BrokerError("paper_state_invalid_or_durability_unavailable") from exc

    def close(self):
        with self._lock:
            if self.db is not None:
                self.db.close()
                self.db = None

    @staticmethod
    def _sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_ready(self):
        if self._failed or self.db is None:
            raise BrokerError("paper_state_unavailable_recovery_required")

    @contextmanager
    def _read_snapshot(self):
        """Cash/holdings/orders must share one SQLite snapshot across processes."""
        with self._lock:
            self._ensure_ready()
            owns_transaction = not self.db.in_transaction
            if owns_transaction:
                self.db.execute("BEGIN")
            try:
                yield
            finally:
                if owns_transaction and self.db.in_transaction:
                    self.db.execute("ROLLBACK")  # Read-only transaction; no durable mutation.

    def _commit(self):
        self.db.execute("COMMIT")
        if self.path is not None:
            self._sync_directory(self.path.parent)

    def _mutation_failed(self, exc):
        try:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            self._failed = True
            raise UnknownSubmission("paper_rollback_unconfirmed") from exc
        if isinstance(exc, (sqlite3.DatabaseError, OSError)):
            self._failed = True
            raise UnknownSubmission("paper_durable_commit_unconfirmed") from exc

    def _validate_state(self):
        """Existing files must prove their identity/schema; never repair/reset."""
        if self.db.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
            raise BrokerError("paper_state_corrupt")
        expected = {" ".join(sql.split()).lower() for sql in _SCHEMA}
        actual = {" ".join(row[0].split()).lower() for row in self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if actual != expected or self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise BrokerError("paper_schema_mismatch_recovery_required")
        accounts = self.db.execute("SELECT * FROM account").fetchall()
        if len(accounts) != 1 or accounts[0]["id"] != self.account_id:
            raise BrokerError("paper_account_identity_mismatch")
        decimal(accounts[0]["cash"], nonnegative=True)
        for row in self.db.execute("SELECT * FROM positions"):
            _identifier(row["symbol"])
            held = decimal(row["quantity"], nonnegative=True)
            cost = decimal(row["cost"], nonnegative=True)
            if held == 0 and cost != 0:
                raise BrokerError("paper_position_cost_inconsistent")
        for row in self.db.execute("SELECT * FROM orders"):
            _identifier(row["client_id"])
            _identifier(row["broker_id"])
            request, result = json.loads(row["request"]), self._result(row["result"])
            quantity = decimal(request["quantity"], positive=True)
            limit = decimal(request["limit_price"], positive=True)
            if (request["client_order_id"] != row["client_id"] or request["side"] not in {"buy", "sell"}
                    or request["order_type"] != "limit" or request["asset_class"] not in self.capabilities.asset_classes
                    or result.client_order_id != row["client_id"] or result.broker_order_id != row["broker_id"]
                    or result.status not in {"accepted", "filled", "cancelled"}
                    or not isinstance(row["ts"], int) or row["ts"] < 0):
                raise BrokerError("paper_order_state_inconsistent")
            _identifier(request["symbol"])
            filled = decimal(result.filled_quantity, nonnegative=True)
            if result.status == "filled":
                price = decimal(result.average_fill_price, positive=True)
                decimal(result.fees, nonnegative=True)
                decimal(result.realized_pnl)
                if filled != quantity or (request["side"] == "buy" and price > limit) or (request["side"] == "sell" and price < limit):
                    raise BrokerError("paper_fill_state_inconsistent")
            elif filled != 0:
                raise BrokerError("paper_nonfilled_order_has_fill")

    @staticmethod
    def _result(payload: str) -> OrderResult:
        data = json.loads(payload)
        for key in ("filled_quantity", "average_fill_price", "fees", "realized_pnl"):
            if data.get(key) is not None:
                data[key] = decimal(data[key])
        return OrderResult(**data)

    @financial_context
    def get_quote(self, symbol, quantity=Decimal("1"), side="buy"):
        self._ensure_ready()
        decimal(quantity, positive=True)
        if side not in {"buy", "sell"} or symbol not in self.quotes:
            raise BrokerError("paper_quote_unavailable")
        quote = self.quotes[symbol]
        if quote.symbol != symbol or quote.asset_class not in self.capabilities.asset_classes or quote.currency != "USD":
            raise BrokerError("paper_quote_contract_mismatch")
        bid, ask = decimal(quote.bid, positive=True), decimal(quote.ask, positive=True)
        if ask < bid:
            raise BrokerError("paper_crossed_quote")
        if isinstance(quote.timestamp, bool) or not isinstance(quote.timestamp, int) or quote.timestamp < 0:
            raise BrokerError("paper_quote_timestamp_invalid")
        price = ask if side == "buy" else bid
        return Quote(**{**quote.__dict__, "bid": bid, "ask": ask,
                        "available_quantity": decimal(quote.available_quantity, nonnegative=True)
                        if quote.available_quantity is not None else None,
                        "fee_per_unit": financial_value(price * self.fee_bps / 10000, rounding=ROUND_CEILING)})

    @financial_context
    def get_account(self):
        with self._read_snapshot():
            now = int(self.clock())
            cash = decimal(self.db.execute("SELECT cash FROM account WHERE id=?", (self.account_id,)).fetchone()[0])
            positions, component_times = [], [now]
            for row in self.db.execute("SELECT * FROM positions"):
                quantity = decimal(row["quantity"], nonnegative=True)
                if quantity == 0:
                    continue
                observed = self.get_quote(row["symbol"])
                if observed.timestamp > now:
                    raise BrokerError("paper_valuation_timestamp_in_future")
                positions.append(Position(row["symbol"], quantity, quantity * observed.bid, decimal(row["cost"])))
                component_times.append(observed.timestamp)
            reserved = Decimal("0")
            pnl = Decimal("0")
            day_start = now // 86400 * 86400
            for row in self.db.execute("SELECT * FROM orders"):
                result, request = self._result(row["result"]), json.loads(row["request"])
                if result.status == "accepted" and request["side"] == "buy":
                    reserved += decimal(request["quantity"]) * decimal(request["limit_price"]) * (1 + self.fee_bps / 10000)
                if row["ts"] >= day_start and result.realized_pnl is not None:
                    pnl += result.realized_pnl
            equity = cash + sum((p.market_value for p in positions), Decimal("0"))
            oldest_component = min(component_times)
            return AccountSnapshot(self.account_id, "paper", "USD", equity, cash,
                                   max(Decimal("0"), cash - reserved), tuple(positions), oldest_component, pnl)

    def get_open_orders(self):
        with self._lock:
            self._ensure_ready()
            return tuple(result for row in self.db.execute("SELECT result FROM orders")
                         if (result := self._result(row["result"])).status == "accepted")

    @financial_context
    def submit_order(self, order: OrderRequest):
        self._ensure_ready()
        _identifier(order.client_order_id)
        if order.account_id != self.account_id or order.side not in {"buy", "sell"} or order.order_type != "limit":
            raise BrokerRejected("paper_order_contract_rejected")
        qty, limit = decimal(order.quantity, positive=True), decimal(order.limit_price, positive=True)
        request = {k: v for k, v in json_value(order).items() if k != "account_id"}
        encoded = json.dumps(request, sort_keys=True)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.db.execute("SELECT * FROM orders WHERE client_id=?", (order.client_order_id,)).fetchone()
                if existing:
                    if existing["request"] != encoded:
                        raise BrokerRejected("client_order_id_reused_for_different_order")
                    self._commit()
                    return self._result(existing["result"])
                quote = self.get_quote(order.symbol, qty, order.side)
                if order.asset_class != quote.asset_class:
                    raise BrokerRejected("paper_asset_class_mismatch")
                if quote.market_open is not True:
                    raise BrokerRejected("paper_market_not_open")
                cash = decimal(self.db.execute("SELECT cash FROM account").fetchone()[0])
                held = self.db.execute("SELECT * FROM positions WHERE symbol=?", (order.symbol,)).fetchone()
                inventory = decimal(held["quantity"]) if held else Decimal("0")
                cost = decimal(held["cost"]) if held else Decimal("0")
                price = quote.ask if order.side == "buy" else quote.bid
                fee = financial_value(qty * price * self.fee_bps / 10000)
                if order.side == "buy" and qty * limit * (1 + self.fee_bps / 10000) > self.get_account().buying_power:
                    raise BrokerRejected("paper_insufficient_buying_power")
                if order.side == "sell" and qty > inventory:
                    raise BrokerRejected("paper_insufficient_inventory")
                marketable = price <= limit if order.side == "buy" else price >= limit
                result = OrderResult(str(uuid.uuid4()), order.client_order_id, "accepted")
                if marketable:
                    if quote.available_quantity is None or qty > quote.available_quantity:
                        raise BrokerRejected("paper_insufficient_liquidity")
                    pnl = Decimal("0")
                    if order.side == "buy":
                        cash = decimal(financial_value(cash - qty * price - fee), nonnegative=True)
                        inventory = decimal(inventory + qty, nonnegative=True)
                        cost = financial_value(cost + qty * price + fee)
                    else:
                        allocated = financial_value(cost * qty / inventory)
                        cash = financial_value(cash + qty * price - fee)
                        pnl = financial_value(qty * price - fee - allocated)
                        inventory = decimal(inventory - qty, nonnegative=True)
                        cost = decimal(cost - allocated, nonnegative=True)
                    self.db.execute("UPDATE account SET cash=?", (str(cash),))
                    if inventory:
                        self.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?)",
                                        (order.symbol, str(inventory), str(cost)))
                    else:
                        self.db.execute("DELETE FROM positions WHERE symbol=?", (order.symbol,))
                    result = OrderResult(result.broker_order_id, order.client_order_id, "filled", qty, price, fee, pnl)
                self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?)", (order.client_order_id,
                                result.broker_order_id, encoded, json.dumps(json_value(result)), int(self.clock())))
                self._commit()
                return result
            except BaseException as exc:
                self._mutation_failed(exc)
                raise

    def get_order_status(self, broker_order_id=None, client_order_id=None):
        self._ensure_ready()
        if bool(broker_order_id) == bool(client_order_id):
            raise BrokerError("provide_one_order_identifier")
        with self._lock:
            column, value = ("broker_id", broker_order_id) if broker_order_id else ("client_id", client_order_id)
            row = self.db.execute(f"SELECT result FROM orders WHERE {column}=?", (value,)).fetchone()
            return self._result(row["result"]) if row else None

    def cancel_order(self, broker_order_id):
        with self._lock:
            self._ensure_ready()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                result = self.get_order_status(broker_order_id=broker_order_id)
                if result is None:
                    raise BrokerError("paper_order_not_found")
                if result.status == "accepted":
                    result = OrderResult(result.broker_order_id, result.client_order_id, "cancelled")
                    self.db.execute("UPDATE orders SET result=? WHERE broker_id=?",
                                    (json.dumps(json_value(result)), broker_order_id))
                self._commit()
                return result
            except BaseException as exc:
                self._mutation_failed(exc)
                raise
