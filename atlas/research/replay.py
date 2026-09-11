"""Causal close-publication replay; no broker connection or order authority.

Completed-bar volume is usable only at its publication event. These are
explicit bar execution proxies, not exchange queue or intrabar simulations.
Orders never fill on their decision event and terminal inventory is marked,
never liquidated merely because the supplied sample ends.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from atlas.domain import decimal, financial_context, financial_value, json_value


D = Decimal
OPEN = {"accepted", "partially_filled"}
EVIDENCE = {"SYNTHETIC", "TEST", "HISTORICAL", "HISTORICAL_UNVERIFIED", "HISTORICAL_OUT_OF_SAMPLE", "PAPER", "SHADOW"}
SCHEMA_VERSION = 1


def _json(value):
    return json.dumps(json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value):
    if type(value) is not int or value < 0:
        raise ValueError("replay times must be nonnegative integer UTC seconds")
    return value


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("replay identifiers must be nonempty bounded strings")
    return value


@dataclass(frozen=True)
class ExecutionModel:
    name: str = "normal"
    version: str = "close-publication-v1"
    fee_bps: Decimal = D("1")
    spread_bps: Decimal = D("5")
    slippage_bps: Decimal = D("2")
    latency_seconds: int = 1
    participation_rate: Decimal = D("0.05")
    quantity_increment: Decimal = D("0.000001")
    minimum_quantity: Decimal = D("0.000001")
    max_position_weight: Decimal = D("1")
    max_positions: int = 20
    max_quote_age_seconds: int = 300
    max_drawdown: Decimal | None = None
    max_turnover: Decimal | None = None
    max_daily_loss: Decimal | None = None
    trading_enabled: bool = True
    broker_enabled: bool = True
    account_enabled: bool = True
    data_quality_passed: bool = True
    disabled_strategies: tuple[str, ...] = ()
    disabled_symbols: tuple[str, ...] = ()

    def __post_init__(self):
        _label(self.name)
        _label(self.version)
        for key in ("fee_bps", "spread_bps", "slippage_bps"):
            value = decimal(getattr(self, key), nonnegative=True)
            if value >= 10000:
                raise ValueError("execution costs must be below 10000 bps")
            object.__setattr__(self, key, value)
        for key in ("participation_rate", "quantity_increment", "minimum_quantity", "max_position_weight"):
            object.__setattr__(self, key, decimal(getattr(self, key), positive=True))
        if self.participation_rate > 1 or self.max_position_weight > 1:
            raise ValueError("participation and position weights cannot exceed one")
        for key in ("latency_seconds", "max_positions", "max_quote_age_seconds"):
            value = getattr(self, key)
            if type(value) is not int or value < (0 if key == "latency_seconds" else 1):
                raise ValueError("invalid execution timing or position limit")
        for key in ("max_drawdown", "max_turnover", "max_daily_loss"):
            value = getattr(self, key)
            if value is not None:
                object.__setattr__(self, key, decimal(value, positive=True))
        if self.max_drawdown is not None and self.max_drawdown > 1:
            raise ValueError("maximum drawdown must be a fraction")
        for key in ("trading_enabled", "broker_enabled", "account_enabled", "data_quality_passed"):
            if type(getattr(self, key)) is not bool:
                raise ValueError("research permission flags must be booleans")
        for key in ("disabled_strategies", "disabled_symbols"):
            values = getattr(self, key)
            if isinstance(values, str):
                raise ValueError("disabled identifiers must be a collection")
            object.__setattr__(self, key, tuple(sorted({_label(value) for value in values})))

    @classmethod
    def scenario(cls, name, **overrides):
        settings = {
            "optimistic": dict(fee_bps="0", spread_bps="1", slippage_bps="0", latency_seconds=0, participation_rate="0.10"),
            "normal": dict(fee_bps="1", spread_bps="5", slippage_bps="2", latency_seconds=1, participation_rate="0.05"),
            "pessimistic": dict(fee_bps="2", spread_bps="10", slippage_bps="5", latency_seconds=60, participation_rate="0.02"),
            "stress": dict(fee_bps="5", spread_bps="25", slippage_bps="15", latency_seconds=300, participation_rate="0.01"),
        }
        if name not in settings:
            raise ValueError("unknown execution scenario")
        return cls(name=name, **{**settings[name], **overrides})

    def public(self):
        return json_value(asdict(self))


class ReplayEngine:
    """Incremental replay with optional transactional checkpoints.

    A restart requires the exact same canonical input and assumptions. It
    resumes at a committed publication/decision group, never reseeds cash or
    repeats fills. Separate runs must use separate paths. The state file is
    research-only and cannot be opened by the production execution journal.
    """

    def __init__(self, bars, decisions, *, model, initial_cash="10000", state_path=None,
                 evidence_class="SYNTHETIC", context_metadata=None, require_existing_state=False):
        if not isinstance(model, ExecutionModel) or evidence_class not in EVIDENCE:
            raise ValueError("explicit execution model and evidence class required")
        self.model, self.evidence_class = model, evidence_class
        self.initial_cash = decimal(initial_cash, positive=True)
        self.bars = self._bars(bars)
        self.decisions = self._decisions(decisions)
        self.config = {"schema_version": SCHEMA_VERSION, "execution_model": model.public(),
                       "initial_cash": str(self.initial_cash), "evidence_class": evidence_class,
                       "bars_hash": _hash(self.bars), "decisions_hash": _hash(self.decisions)}
        if context_metadata is not None:
            # Extensions must bind their assumptions before a checkpoint is
            # opened, so changing risk or venue terms cannot resume old state.
            self.config["context_metadata"] = json.loads(_json(context_metadata))
        self.run_id = _hash(self.config)
        grouped = {}
        for bar in self.bars:
            grouped.setdefault(bar["information_available_time"], {"bars": [], "decisions": []})["bars"].append(bar)
        for decision in self.decisions:
            grouped.setdefault(decision["decision_time"], {"bars": [], "decisions": []})["decisions"].append(decision)
        end_time = max(grouped)
        for decision in self.decisions:
            if decision["action"] == "order" and decision["expires_at"] <= end_time:
                grouped.setdefault(decision["expires_at"], {"bars": [], "decisions": []})
        self.groups = sorted(grouped.items())
        self.state = {"cursor": 0, "last_time": None, "cash": str(self.initial_cash), "positions": {},
                      "quotes": {}, "orders": {}, "fills": [], "trades": [], "equity_curve": [],
                      "events": [], "bar_capacity_used": {}, "realized_pnl": "0",
                      "daily_realized_pnl": {}, "turnover_notional": "0", "peak_equity": str(self.initial_cash)}
        self.path, self.db, self.failed, self.closed = None, None, False, False
        self.require_existing_state = require_existing_state
        if state_path is not None:
            self._open_state(state_path)

    @staticmethod
    def _bars(bars):
        result, identities = [], set()
        for raw in bars:
            row = dict(raw)
            for name in ("symbol", "asset_class", "venue"):
                row[name] = _label(row.get(name))
            if row["asset_class"] not in {"equity", "crypto"} or row.get("currency") != "USD":
                raise ValueError("replay supports long-only USD equity/spot crypto")
            if row.get("volume_unit") != ("shares" if row["asset_class"] == "equity" else "base_asset"):
                raise ValueError("replay requires declared share or base-asset volume units")
            row["event_time"] = _time(row.get("event_time"))
            row["information_available_time"] = _time(row.get("information_available_time"))
            if row["information_available_time"] < row["event_time"]:
                raise ValueError("completed bar cannot be available before its close")
            if type(row.get("interval_seconds")) is not int or row["interval_seconds"] <= 0:
                raise ValueError("explicit positive bar interval required")
            for key in ("open", "high", "low", "close"):
                row[key] = str(decimal(row[key], positive=True))
            if not D(row["low"]) <= min(D(row["open"]), D(row["close"])) <= max(D(row["open"]), D(row["close"])) <= D(row["high"]):
                raise ValueError("impossible OHLC prices")
            row["volume"] = str(decimal(row["volume"], nonnegative=True))
            if (row.get("bid") is None) != (row.get("ask") is None):
                raise ValueError("bid and ask must be supplied together")
            if row.get("bid") is not None:
                row["bid"], row["ask"] = str(decimal(row["bid"], positive=True)), str(decimal(row["ask"], positive=True))
                if D(row["bid"]) > D(row["ask"]):
                    raise ValueError("crossed replay quote")
            row["revision"] = _time(row.get("revision", 0))
            identity = (row["symbol"], row["event_time"], row["revision"])
            if identity in identities:
                raise ValueError("duplicate market bar revision")
            identities.add(identity)
            result.append(row)
            if len(result) > 200000:
                raise ValueError("replay input exceeds bounded research limit")
        if not result:
            raise ValueError("replay requires market observations")
        return sorted(result, key=lambda r: (r["information_available_time"], r["event_time"], r["symbol"], r["revision"]))

    @staticmethod
    def _decisions(decisions):
        result = {}
        for raw in decisions:
            row = dict(raw)
            row["signal_id"] = _label(row.get("signal_id"))
            row["decision_time"] = _time(row.get("decision_time"))
            row["action"] = row.get("action", "order")
            if row["action"] == "cancel":
                row["target_signal_id"] = _label(row.get("target_signal_id"))
            elif row["action"] == "order":
                row["symbol"] = _label(row.get("symbol"))
                row["strategy_id"] = _label(row.get("strategy_id"))
                row["strategy_version"] = _label(row.get("strategy_version", row.get("version")))
                row["expires_at"] = _time(row.get("expires_at"))
                if row["expires_at"] <= row["decision_time"]:
                    raise ValueError("decision must expire after creation")
                row["order_type"] = row.get("order_type", "market")
                if row["order_type"] not in {"market", "limit"}:
                    raise ValueError("replay supports market or limit orders")
                if row["order_type"] == "limit":
                    row["limit_price"] = str(decimal(row.get("limit_price"), positive=True))
                if "target_weight" in row:
                    if "quantity" in row or "side" in row:
                        raise ValueError("choose target weight or explicit quantity")
                    weight = decimal(row["target_weight"], nonnegative=True)
                    if weight > 1:
                        raise ValueError("target weight must be between zero and one")
                    row["target_weight"] = str(weight)
                else:
                    if row.get("side") not in {"buy", "sell"}:
                        raise ValueError("explicit order requires buy or sell side")
                    row["quantity"] = str(decimal(row.get("quantity"), positive=True))
            else:
                raise ValueError("unknown replay action")
            existing = result.get(row["signal_id"])
            if existing is not None and existing != row:
                raise ValueError("signal id cannot be reused for a changed decision")
            result[row["signal_id"]] = row
            if len(result) > 200000:
                raise ValueError("too many replay decisions")
        return sorted(result.values(), key=lambda r: (r["decision_time"], 0 if r["action"] == "cancel" else 1, r["signal_id"]))

    def _open_state(self, state_path):
        path = Path(state_path).expanduser()
        if not path.is_absolute() or path.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
            raise ValueError("replay state requires an absolute path outside the repository")
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        new = False
        if not self.require_existing_state:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if path.stat().st_size == 0:
                    raise ValueError("existing empty replay state requires recovery")
            else:
                os.close(fd)
                new = True
        self.path = path
        try:
            # mode=rw closes the disappearance race between a resume precheck
            # and SQLite open. A resume must never reseed missing research cash.
            self.db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=10)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("BEGIN IMMEDIATE")
            if new:
                self.db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                self.db.execute("CREATE TABLE checkpoint (id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,digest TEXT NOT NULL)")
                self.db.execute("CREATE TABLE events (id TEXT PRIMARY KEY,sequence INTEGER UNIQUE NOT NULL,payload TEXT NOT NULL)")
                self.db.execute("INSERT INTO metadata VALUES ('run_id',?)", (self.run_id,))
                self.db.execute("INSERT INTO metadata VALUES ('config',?)", (_json(self.config),))
                self.db.execute("INSERT INTO checkpoint VALUES (1,?,?)", (_json(self.state), _hash(self.state)))
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            else:
                tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables != {"metadata", "checkpoint", "events"} or self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise ValueError("replay schema mismatch requires explicit recovery")
                if self.db.execute("SELECT value FROM metadata WHERE key='run_id'").fetchone() != (self.run_id,):
                    raise ValueError("replay inputs, execution model, evidence or initial cash changed")
                raw, expected = self.db.execute("SELECT payload,digest FROM checkpoint WHERE id=1").fetchone()
                restored = json.loads(raw)
                if _hash(restored) != expected or not 0 <= restored["cursor"] <= len(self.groups):
                    raise ValueError("replay checkpoint corruption")
                stored_events = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM events ORDER BY sequence")]
                if stored_events != restored["events"]:
                    raise ValueError("replay event history disagrees with checkpoint")
                self.state = restored
            if self.db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("replay state corruption")
            self.db.execute("COMMIT")
            self._sync()
        except Exception:
            self.failed = True
            self.close()
            raise

    def _sync(self):
        if self.path is not None:
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self.closed = True

    def _event(self, kind, timestamp, **details):
        event = {"sequence": len(self.state["events"]), "kind": kind, "timestamp": timestamp, **json_value(details)}
        event["event_id"] = _hash({"run_id": self.run_id, **event})
        self.state["events"].append(event)

    @staticmethod
    def _sleeve(order):
        return _json([order["strategy_id"], order["strategy_version"], order["symbol"]])

    def _position(self, order):
        return self.state["positions"].get(self._sleeve(order), {
            "symbol": order["symbol"], "strategy_id": order["strategy_id"], "strategy_version": order["strategy_version"],
            "quantity": "0", "cost": "0", "entry_notional": "0", "entry_fees": "0"})

    def _floor(self, value):
        return (value / self.model.quantity_increment).to_integral_value(rounding=ROUND_DOWN) * self.model.quantity_increment

    def _prices(self, bar):
        midpoint = ((D(bar["bid"]) + D(bar["ask"])) / 2 if bar.get("bid") is not None else D(bar["close"]))
        half = midpoint * self.model.spread_bps / 20000
        bid = min(D(bar["bid"]), midpoint - half) if bar.get("bid") is not None else midpoint - half
        ask = max(D(bar["ask"]), midpoint + half) if bar.get("ask") is not None else midpoint + half
        return bid, ask, midpoint

    def _equity(self):
        return D(self.state["cash"]) + sum((D(pos["quantity"]) * self._prices(self.state["quotes"][pos["symbol"]])[0]
                                           for pos in self.state["positions"].values()), D("0"))

    def _stale(self, now):
        symbols = {pos["symbol"] for pos in self.state["positions"].values()}
        return sorted(symbol for symbol in symbols if now - self.state["quotes"][symbol]["event_time"] > self.model.max_quote_age_seconds)

    def _reserved_cash(self, exclude=None):
        return sum((D(order["remaining_quantity"]) * D(order["reservation_price"]) * (1 + self.model.fee_bps / 10000)
                    for key, order in self.state["orders"].items()
                    if key != exclude and order["status"] in OPEN and order["side"] == "buy"), D("0"))

    def _buy_veto(self, order, now):
        for key in ("trading_enabled", "broker_enabled", "account_enabled", "data_quality_passed"):
            if not getattr(self.model, key):
                return key
        if order["strategy_id"] in self.model.disabled_strategies:
            return "strategy_disabled"
        if order["symbol"] in self.model.disabled_symbols:
            return "symbol_disabled"
        equity = self._equity()
        peak = max(D(self.state["peak_equity"]), equity)
        if self.model.max_drawdown is not None and peak > 0 and 1 - equity / peak >= self.model.max_drawdown:
            return "maximum_drawdown"
        if self.model.max_daily_loss is not None and D(self.state["daily_realized_pnl"].get(str(now // 86400), "0")) <= -self.model.max_daily_loss:
            return "maximum_daily_loss"
        if self.model.max_turnover is not None and D(self.state["turnover_notional"]) >= self.initial_cash * self.model.max_turnover:
            return "maximum_turnover"
        return None

    def _cancel(self, order, now, reason):
        order["status"], order["terminal_time"], order["reason"] = "cancelled", now, reason
        self._event("cancelled", now, signal_id=order["signal_id"], reason=reason,
                    filled_quantity=order["filled_quantity"], remaining_quantity=order["remaining_quantity"])

    @financial_context
    def _decision(self, decision, now):
        if decision["action"] == "cancel":
            target = self.state["orders"].get(decision["target_signal_id"])
            if target is not None and target["status"] in OPEN:
                self._cancel(target, now, "explicit_cancellation")
            else:
                self._event("cancel_noop", now, signal_id=decision["signal_id"], target_signal_id=decision["target_signal_id"])
            return
        order = {**decision, "order_id": _hash(decision), "eligible_after": now + self.model.latency_seconds,
                 "status": "rejected", "quantity": "0", "filled_quantity": "0", "remaining_quantity": "0",
                 "fees": "0", "average_fill_price": None, "reservation_price": "0"}
        self.state["orders"][order["signal_id"]] = order

        def reject(reason):
            order["reason"] = reason
            self._event("rejected", now, signal_id=order["signal_id"], reason=reason)

        quote = self.state["quotes"].get(order["symbol"])
        if quote is None or now - quote["event_time"] > self.model.max_quote_age_seconds or self._stale(now):
            reject("missing_or_stale_market_data")
            return
        bid, ask, midpoint = self._prices(quote)
        position = self._position(order)
        inventory = D(position["quantity"])
        if any(other["status"] in OPEN and self._sleeve(other) == self._sleeve(order) for other in self.state["orders"].values()):
            reject("sleeve_has_open_order")
            return
        if "target_weight" in decision:
            target = self._equity() * D(decision["target_weight"])
            # Compute the target in instrument units. Dividing the sell
            # value delta by bid would request more than held inventory
            # whenever spread is positive, including every zero target.
            delta = self._floor(target / midpoint) - inventory
            order["side"] = "buy" if delta >= 0 else "sell"
            qty = self._floor(abs(delta))
        else:
            order["side"] = decision["side"]
            qty = D(decision["quantity"])
            if qty % self.model.quantity_increment:
                reject("quantity_increment")
                return
        order["decision_price"] = str(midpoint)
        order["quantity"] = order["remaining_quantity"] = str(qty)
        if qty < self.model.minimum_quantity:
            reject("below_minimum_or_no_change")
            return
        if order["side"] == "sell" and qty > inventory:
            reject("insufficient_sleeve_inventory")
            return
        reservation = D(order["limit_price"]) if order["order_type"] == "limit" else ask * (1 + self.model.slippage_bps / 10000)
        order["reservation_price"] = str(reservation)
        if order["side"] == "buy":
            veto = self._buy_veto(order, now)
            if veto:
                reject(veto)
                return
            available = D(self.state["cash"]) - self._reserved_cash()
            affordable = self._floor(max(D("0"), available) / (reservation * (1 + self.model.fee_bps / 10000)))
            if "target_weight" in decision:
                qty = min(qty, affordable)
                order["quantity"] = order["remaining_quantity"] = str(qty)
            if qty < self.model.minimum_quantity or qty > affordable:
                reject("insufficient_cash")
                return
            symbol_value = sum((D(p["quantity"]) * bid for p in self.state["positions"].values() if p["symbol"] == order["symbol"]), D("0"))
            if symbol_value + qty * ask > self._equity() * self.model.max_position_weight:
                reject("position_weight_limit")
                return
            symbols = {p["symbol"] for p in self.state["positions"].values()}
            if order["symbol"] not in symbols and len(symbols) >= self.model.max_positions:
                reject("position_count_limit")
                return
        order["status"] = "accepted"
        self._event("order_accepted", now, signal_id=order["signal_id"], quantity=qty,
                    eligible_after=order["eligible_after"], side=order["side"], order_type=order["order_type"])

    @financial_context
    def _match(self, bar, now):
        if now - bar["event_time"] > self.model.max_quote_age_seconds or self._stale(now):
            self._event("stale_market_no_fill", now, symbol=bar["symbol"])
            return
        bid, ask, midpoint = self._prices(bar)
        key = _json([bar["symbol"], bar["event_time"]])
        capacity = self._floor(D(bar["volume"]) * self.model.participation_rate)
        used = D(self.state["bar_capacity_used"].get(key, "0"))
        for order in sorted(self.state["orders"].values(), key=lambda o: (o["decision_time"], o["signal_id"])):
            if (order["status"] not in OPEN or order["symbol"] != bar["symbol"]
                    or now <= order["decision_time"] or now < order["eligible_after"]):
                continue
            executable = ask if order["side"] == "buy" else bid
            price = executable * (1 + self.model.slippage_bps / 10000 if order["side"] == "buy" else 1 - self.model.slippage_bps / 10000)
            if order["order_type"] == "limit" and ((order["side"] == "buy" and price > D(order["limit_price"]))
                                                     or (order["side"] == "sell" and price < D(order["limit_price"]))):
                continue
            qty = self._floor(min(D(order["remaining_quantity"]), max(D("0"), capacity - used)))
            if order["side"] == "buy":
                veto = self._buy_veto(order, now)
                if veto:
                    self._cancel(order, now, veto)
                    continue
                available = max(D("0"), D(self.state["cash"]) - self._reserved_cash(exclude=order["signal_id"]))
                qty = min(qty, self._floor(available / (price * (1 + self.model.fee_bps / 10000))))
                symbol_value = sum((D(p["quantity"]) * bid for p in self.state["positions"].values() if p["symbol"] == order["symbol"]), D("0"))
                headroom = max(D("0"), self._equity() * self.model.max_position_weight - symbol_value)
                qty = min(qty, self._floor(headroom / price))
                symbols = {p["symbol"] for p in self.state["positions"].values()}
                if order["symbol"] not in symbols and len(symbols) >= self.model.max_positions:
                    qty = D("0")
                if self.model.max_turnover is not None:
                    budget = max(D("0"), self.initial_cash * self.model.max_turnover - D(self.state["turnover_notional"]))
                    qty = min(qty, self._floor(budget / price))
            else:
                qty = min(qty, D(self._position(order)["quantity"]))
            if qty < self.model.minimum_quantity:
                continue
            self._fill(order, qty, price, executable, midpoint, now, bar)
            used += qty
        self.state["bar_capacity_used"][key] = str(used)

    @financial_context
    def _fill(self, order, qty, price, executable, midpoint, now, bar):
        position = self._position(order)
        inventory, cost = D(position["quantity"]), D(position["cost"])
        entry_notional, entry_fees = D(position["entry_notional"]), D(position["entry_fees"])
        fee = financial_value(qty * price * self.model.fee_bps / 10000)
        cash = D(self.state["cash"])
        pnl, allocated, opening_fee, gross = D("0"), D("0"), D("0"), D("0")
        if order["side"] == "buy":
            cash -= qty * price + fee
            inventory += qty
            cost += qty * price + fee
            entry_notional += qty * price
            entry_fees += fee
        else:
            ratio = qty / inventory
            allocated = cost if qty == inventory else financial_value(cost * ratio)
            opening_fee = entry_fees if qty == inventory else financial_value(entry_fees * ratio)
            allocated_notional = allocated - opening_fee
            cash += qty * price - fee
            pnl = qty * price - fee - allocated
            gross = qty * price - allocated_notional
            inventory -= qty
            cost -= allocated
            entry_notional -= allocated_notional
            entry_fees -= opening_fee
        if cash < 0 or inventory < 0 or cost < 0:
            raise ValueError("replay accounting invariant failed")
        self.state["cash"] = str(financial_value(cash))
        if inventory:
            self.state["positions"][self._sleeve(order)] = {**position, "quantity": str(inventory),
                "cost": str(financial_value(cost)), "entry_notional": str(financial_value(entry_notional)),
                "entry_fees": str(financial_value(entry_fees))}
        else:
            self.state["positions"].pop(self._sleeve(order), None)
        prior_filled = D(order["filled_quantity"])
        order["average_fill_price"] = str(financial_value(((D(order["average_fill_price"]) * prior_filled if prior_filled else 0) + qty * price) / (prior_filled + qty)))
        order["filled_quantity"] = str(prior_filled + qty)
        order["remaining_quantity"] = str(D(order["remaining_quantity"]) - qty)
        order["fees"] = str(D(order["fees"]) + fee)
        order["status"] = "filled" if D(order["remaining_quantity"]) == 0 else "partially_filled"
        if order["status"] == "filled":
            order["terminal_time"] = now
        fill = {"fill_id": _hash([self.run_id, order["signal_id"], len(self.state["fills"])]),
                "order_id": order["order_id"], "signal_id": order["signal_id"], "symbol": order["symbol"],
                "strategy_id": order["strategy_id"], "strategy_version": order["strategy_version"],
                "side": order["side"], "quantity": str(qty), "fill_price": str(financial_value(price)),
                "fees": str(fee), "allocated_entry_fees": str(opening_fee), "realized_pnl": str(financial_value(pnl)),
                "decision_time": order["decision_time"], "submission_time": order["eligible_after"],
                "fill_time": now, "market_event_time": bar["event_time"], "decision_price": order["decision_price"],
                "arrival_midpoint": str(midpoint), "spread_cost": str(financial_value(abs(executable - midpoint) * qty)),
                "slippage_cost": str(financial_value(abs(price - executable) * qty)),
                "implementation_shortfall": str(financial_value((price - D(order["decision_price"])) * qty * (1 if order["side"] == "buy" else -1))),
                "latency_seconds": now - order["decision_time"], "execution_model": self.model.name,
                "fill_basis": "subsequent_close_publication_proxy"}
        self.state["fills"].append(fill)
        self.state["turnover_notional"] = str(D(self.state["turnover_notional"]) + qty * price)
        if order["side"] == "sell":
            self.state["trades"].append({"fill_id": fill["fill_id"], "strategy_id": order["strategy_id"],
                "strategy_version": order["strategy_version"], "symbol": order["symbol"], "timestamp": now,
                "quantity": str(qty), "gross_pnl": str(financial_value(gross)), "fees": str(opening_fee + fee),
                "net_pnl": str(financial_value(pnl)), "slippage": None, "record_basis": "realized_sell_fill"})
            self.state["realized_pnl"] = str(D(self.state["realized_pnl"]) + financial_value(pnl))
            day = str(now // 86400)
            self.state["daily_realized_pnl"][day] = str(D(self.state["daily_realized_pnl"].get(day, "0")) + financial_value(pnl))
        self._event("fill", now, **fill)

    @financial_context
    def step(self):
        if self.closed:
            raise ValueError("closed replay cannot advance; reopen its durable state")
        if self.failed:
            raise ValueError("replay state requires recovery")
        cursor = self.state["cursor"]
        if cursor >= len(self.groups):
            return False
        now, group = self.groups[cursor]
        if self.state["last_time"] is not None and now <= self.state["last_time"]:
            raise ValueError("replay event clock cannot rewind")
        before, event_count = (_json(self.state) if self.db is not None else None), len(self.state["events"])
        try:
            if self.db is not None:
                self.db.execute("BEGIN IMMEDIATE")
                stored = self.db.execute("SELECT digest FROM checkpoint WHERE id=1").fetchone()[0]
                if stored != _hash(self.state):
                    raise ValueError("another replay worker advanced this checkpoint; reopen before continuing")
            for order in self.state["orders"].values():
                if order["status"] in OPEN and now >= order["expires_at"]:
                    self._cancel(order, now, "signal_expired")
            updates = {}
            for bar in group["bars"]:
                previous = updates.get(bar["symbol"], self.state["quotes"].get(bar["symbol"]))
                if previous is not None and bar["event_time"] <= previous["event_time"]:
                    self._event("revision_not_executable", now, symbol=bar["symbol"], market_event_time=bar["event_time"], revision=bar["revision"])
                    continue
                updates[bar["symbol"]] = bar
            self.state["quotes"].update(updates)
            for bar in sorted(updates.values(), key=lambda row: row["symbol"]):
                self._match(bar, now)
            for decision in group["decisions"]:
                self._decision(decision, now)
            snapshot = {"timestamp": now, "equity": str(financial_value(self._equity())),
                        "cash": self.state["cash"], "reserved_cash": str(financial_value(self._reserved_cash())),
                        "realized_pnl": self.state["realized_pnl"], "stale_symbols": self._stale(now),
                        "daily_realized_pnl": self.state["daily_realized_pnl"].get(str(now // 86400), "0"),
                        "daily_pnl_basis": "UTC_simulated_fill_time_cost_basis_net_sell_pnl"}
            self.state["peak_equity"] = str(max(D(self.state["peak_equity"]), D(snapshot["equity"])))
            self.state["equity_curve"].append(snapshot)
            self._event("equity_snapshot", now, **{key: value for key, value in snapshot.items() if key != "timestamp"})
            self.state["cursor"], self.state["last_time"] = cursor + 1, now
            if self.db is not None:
                for event in self.state["events"][event_count:]:
                    self.db.execute("INSERT INTO events VALUES (?,?,?)", (event["event_id"], event["sequence"], _json(event)))
                self.db.execute("UPDATE checkpoint SET payload=?,digest=? WHERE id=1", (_json(self.state), _hash(self.state)))
                self.db.execute("COMMIT")
                self._sync()
            return True
        except BaseException:
            if self.db is not None and self.db.in_transaction:
                self.db.execute("ROLLBACK")
            if before is not None:
                self.state = json.loads(before)
            self.failed = True
            raise

    def advance(self, *, max_events=None):
        if max_events is not None and (type(max_events) is not int or max_events < 0):
            raise ValueError("max_events must be a nonnegative integer")
        count = 0
        while (max_events is None or count < max_events) and self.step():
            count += 1
        return self.result()

    @financial_context
    def result(self):
        from atlas.analytics import performance_report

        if self.failed:
            raise ValueError("replay state requires recovery before reporting")
        orders = list(self.state["orders"].values())
        fills, trades, curve = self.state["fills"], self.state["trades"], self.state["equity_curve"]
        equity = self._equity()
        cost = sum((D(p["cost"]) for p in self.state["positions"].values()), D("0"))
        market_value = equity - D(self.state["cash"])
        fees = sum((D(f["fees"]) for f in fills), D("0"))
        slippage = sum((D(f["slippage_cost"]) for f in fills), D("0"))
        closed_fees = sum((D(trade["fees"]) for trade in trades), D("0"))
        turnover_notional = sum((D(f["quantity"]) * D(f["fill_price"]) for f in fills), D("0"))
        average_equity = sum((D(r["equity"]) for r in curve), D("0")) / len(curve) if curve else self.initial_cash
        kind = "synthetic" if self.evidence_class == "SYNTHETIC" else "test" if self.evidence_class == "TEST" else "backtest"
        metrics = performance_report(trades, initial_capital=str(self.initial_cash), equity_curve=curve,
                                     evidence_kind=kind, fee_slippage_assumptions=self.model.public())
        metrics["warnings"] = [warning for warning in metrics["warnings"]
            if warning != "Some fees or slippage are unknown; aggregate costs are unavailable."]
        metrics["warnings"].append("Replay execution costs are known for all simulated fills; closed-sell slippage allocation is unavailable.")
        metrics.update({"evidence_class": self.evidence_class, "turnover": str(turnover_notional / average_equity) if average_equity > 0 else None,
                        "turnover_notional": str(turnover_notional), "turnover_basis": "gross_fill_notional_over_event_observation_mean_equity",
                        "fees": str(fees), "fees_paid": str(fees), "fees_basis": "all_simulated_execution_fills_including_open_inventory",
                        "closed_sell_allocated_fees": str(closed_fees),
                        "closed_sell_allocated_fees_basis": "allocated_entry_fees_plus_exit_fees_for_realized_sell_fills",
                        "slippage": str(slippage), "slippage_basis": "all_simulated_fills_adverse_price_vs_executable_quote",
                        "closed_sell_allocated_slippage": None,
                        "holding_period_distribution": None,
                        "holding_period_reason": "average_cost_sleeves_can_mix_entry_lots; independent_round_trip_holding_periods_not_inferred",
                        "net_return": str(equity / self.initial_cash - 1), "gross_return": None,
                        "gross_return_reason": "requires a matched zero-cost rerun because fills and sizes can change",
                        "net_pnl_basis": "realized_sell_fill_cost_basis_pnl; net_return_includes_marked_open_inventory",
                        "trade_count_basis": "realized_sell_fills_not_independent_round_trips"})
        by_strategy, by_asset, by_symbol = {}, {}, {}
        for position in self.state["positions"].values():
            quote = self.state["quotes"][position["symbol"]]
            value = D(position["quantity"]) * self._prices(quote)[0]
            strategy = _json([position["strategy_id"], position["strategy_version"]])
            for target, key in ((by_strategy, strategy), (by_asset, quote["asset_class"]), (by_symbol, position["symbol"])):
                target[key] = target.get(key, D("0")) + value
        return json.loads(_json({"schema_version": 1, "run_id": self.run_id, "evidence_class": self.evidence_class,
            "execution_model": self.model.public(), "execution_model_hash": _hash(self.model.public()),
            "complete": self.state["cursor"] == len(self.groups), "event_cursor": self.state["cursor"],
            "equity_curve": curve, "orders": orders, "fills": fills, "trades": trades, "events": self.state["events"],
            "portfolio": {"cash": self.state["cash"], "equity": str(financial_value(equity)),
                "positions": list(self.state["positions"].values()), "reserved_cash": str(financial_value(self._reserved_cash())),
                "realized_pnl": self.state["realized_pnl"], "unrealized_pnl": str(financial_value(market_value - cost)),
                "gross_exposure": str(market_value), "net_exposure": str(market_value),
                "exposure_by_strategy": by_strategy, "exposure_by_asset_class": by_asset, "exposure_by_symbol": by_symbol,
                "daily_realized_pnl": self.state["daily_realized_pnl"],
                "daily_pnl_basis": "UTC_simulated_fill_time_cost_basis_net_sell_pnl",
                "stale_symbols": self._stale(self.state["last_time"]) if self.state["last_time"] is not None else []},
            "execution_quality": {"fill_count": len(fills), "order_count": len(orders),
                "partial_fill_order_count": sum(0 < D(o["filled_quantity"]) < D(o["quantity"]) for o in orders),
                "rejected_order_count": sum(o["status"] == "rejected" for o in orders),
                "cancelled_order_count": sum(o["status"] == "cancelled" for o in orders),
                "unfilled_order_count": sum(o["status"] in OPEN for o in orders),
                "fees": str(fees), "spread_cost": str(sum((D(f["spread_cost"]) for f in fills), D("0"))),
                "slippage_cost": str(slippage),
                "implementation_shortfall": str(sum((D(f["implementation_shortfall"]) for f in fills), D("0")))},
            "metrics": metrics, "limitations": [
                "Close-publication execution proxy; no exchange queue, intrabar path or guaranteed executable close.",
                "Completed volume is known only at publication and shared across all orders for that event.",
                "Market orders can remain partially unfilled until expiry under the bar liquidity proxy.",
                "Provided bid/ask is widened only to the configured minimum spread; missing bid/ask uses modeled spread.",
                "Cancellations take effect after market matching at their explicit timestamp, with zero additional cancel latency.",
                "Long-only USD; no corporate-action processing, dividends, borrow, interest or cash flows.",
                "Sample end does not force liquidation; terminal inventory and stale marks remain visible.",
                "Strategy sleeves own separate cost bases and cannot sell each other's inventory.",
                "Synthetic and test data establish engineering behavior only; no live capability exists."]}))


def run_replay(bars, decisions, *, model, initial_cash="10000", state_path=None, evidence_class="SYNTHETIC"):
    engine = ReplayEngine(bars, decisions, model=model, initial_cash=initial_cash,
                          state_path=state_path, evidence_class=evidence_class)
    try:
        return engine.advance()
    finally:
        engine.close()
