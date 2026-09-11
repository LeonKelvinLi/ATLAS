"""Durable $100 synthetic Coinbase accounting, with no broker write interface.

The reference SMA is an unvalidated comparison baseline. Simulated IOC fills
consume displayed depth with explicitly adverse slippage and aggregate fee
rounding; they are never broker fills or evidence of future execution quality.
"""

from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from atlas.brokers.coinbase_public import (MAX_AGE_SECONDS, ORIGIN, amount, timestamp,
                                          validate_book, validate_product)
from atlas.config import external_path
from atlas.domain import decimal, financial_context, financial_value

CONFIG_KEYS = {"schema", "product_id", "allocation_usd", "order_budget_usd", "taker_fee_bps",
               "slippage_bps", "max_loss_usd", "daily_loss_usd", "short_window", "long_window",
               "entry_threshold_bps", "interval_seconds", "strategy"}
ZERO = Decimal("0")
HUNDRED = Decimal("100")


class SimulationError(ValueError):
    """Stable diagnostic code, excluding remote bodies and account secrets."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value, *, positive=False):
    try:
        return amount(value, positive=positive)
    except ValueError:
        raise SimulationError("simulation_amount_invalid") from None


def _str(value):
    return format(value, "f")


def _money(value, *, up=False):
    return financial_value(value, rounding=ROUND_CEILING if up else ROUND_FLOOR)


def _floor(value, quantum):
    return (value / quantum).to_integral_value(rounding=ROUND_FLOOR) * quantum


def _ceil(value, quantum):
    return (value / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum


def validate_config(config):
    if (not isinstance(config, dict) or set(config) != CONFIG_KEYS or type(config["schema"]) is not int
            or config["schema"] != 1 or config["product_id"] != "BTC-USD"
            or config["strategy"] not in {"sma_reference_v1", "round_trip_smoke_v1"}):
        raise SimulationError("simulation_config_invalid")
    result = dict(config)
    for key in ("allocation_usd", "order_budget_usd", "taker_fee_bps", "slippage_bps",
                "max_loss_usd", "daily_loss_usd", "entry_threshold_bps"):
        value = _number(config[key], positive=key not in {"taker_fee_bps", "slippage_bps", "entry_threshold_bps"})
        result[key] = _str(value)
    if (_number(result["allocation_usd"]) != HUNDRED or _number(result["order_budget_usd"]) > HUNDRED
            or _number(result["max_loss_usd"]) > 5 or _number(result["daily_loss_usd"]) > 5
            or _number(result["taker_fee_bps"]) > 1000 or _number(result["slippage_bps"]) > 100
            or _number(result["entry_threshold_bps"]) > 1000):
        raise SimulationError("simulation_config_bounds_invalid")
    if (any(type(config[k]) is not int for k in ("short_window", "long_window", "interval_seconds"))
            or not 2 <= config["short_window"] < config["long_window"] <= 200
            or not 1 <= config["interval_seconds"] <= 60):
        raise SimulationError("simulation_config_cadence_invalid")
    return result


@financial_context
def _snapshot(value, *, now):
    try:
        if (not isinstance(value, dict) or value.get("public_only") is not True or value.get("source") != ORIGIN
                or value.get("transport_provenance") not in {"verified_tls", "injected_test_transport"}):
            raise ValueError
        observed = amount(value.get("observed_at"))
        product_observed = amount(value.get("product_observed_at"))
        if not product_observed <= observed <= now or now - product_observed > MAX_AGE_SECONDS:
            raise ValueError
        product = validate_product(value.get("product"))
        book = validate_book({"pricebook": value.get("pricebook")}, now=now)
        if product["post_only"]:
            raise ValueError  # This engine only models marketable IOC-style execution.
        price_step, base_step = amount(product["price_increment"]), amount(product["base_increment"])
        for side in ("bids", "asks"):
            for row in book[side]:
                if amount(row["price"]) % price_step or amount(row["size"]) % base_step:
                    raise ValueError
        return {"product": product, "pricebook": book, "observed_at": _str(observed),
                "product_observed_at": _str(product_observed), "source": ORIGIN,
                "public_only": True, "transport_provenance": value["transport_provenance"]}
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise SimulationError("simulation_snapshot_invalid_or_stale") from None


def _initial():
    return {"cash": "100", "quantity": "0", "basis": "0", "realized": "0", "fees": "0",
            "last_observed": None, "last_provider": None, "samples": [], "samples_seen": 0,
            "orders": 0, "fills": 0, "halt_reasons": [], "smoke_phase": "ready",
            "day": None, "day_start_equity": "100", "daily_loss": "0", "total_loss": "0",
            "liquidation_equity": "100", "unpriced_quantity": "0", "unrealized_mark": "0",
            "last_mid": None, "dust_quantity": "0", "peak_equity": "100", "max_drawdown": "0",
            "max_drawdown_percent": "0", "round_trips": 0, "gap_count": 0,
            "exposure_seconds": "0", "exposure_gap_seconds": "0"}


def _state_valid(state, config):
    if not isinstance(state, dict) or set(state) != set(_initial()):
        raise SimulationError("simulation_journal_state_invalid")
    cash, qty, basis, fees = (_number(state[k]) for k in ("cash", "quantity", "basis", "fees"))
    try:
        realized = decimal(state["realized"])
        decimal(state["unrealized_mark"])
    except ValueError:
        raise SimulationError("simulation_journal_state_invalid") from None
    if (cash + basis != HUNDRED + realized or (qty == 0 and basis != 0)
            or basis > HUNDRED or any(type(state[k]) is not int or state[k] < 0 for k in ("orders", "fills", "samples_seen", "round_trips", "gap_count"))
            or state["smoke_phase"] not in {"ready", "exit", "done"}
            or not isinstance(state["halt_reasons"], list) or len(set(state["halt_reasons"])) != len(state["halt_reasons"])
            or any(not isinstance(x, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", x) for x in state["halt_reasons"])
            or not isinstance(state["samples"], list) or len(state["samples"]) > config["long_window"]):
        raise SimulationError("simulation_journal_state_invalid")
    prior = ZERO
    for sample in state["samples"]:
        if not isinstance(sample, dict) or set(sample) != {"at", "mid"} or _number(sample["at"]) <= prior:
            raise SimulationError("simulation_journal_state_invalid")
        _number(sample["mid"], positive=True)
        prior = _number(sample["at"])
    for name in ("day_start_equity", "daily_loss", "total_loss", "liquidation_equity", "unpriced_quantity", "dust_quantity",
                 "peak_equity", "max_drawdown", "max_drawdown_percent", "exposure_seconds", "exposure_gap_seconds"):
        _number(state[name])


def _levels(snapshot, config, side):
    product = snapshot["product"]
    price_step = _number(product["price_increment"])
    slip = _number(config["slippage_bps"]) / 10000
    levels = []
    for row in snapshot["pricebook"]["asks" if side == "buy" else "bids"]:
        raw = _number(row["price"])
        adjusted = _ceil(raw * (1 + slip), price_step) if side == "buy" else _floor(raw * (1 - slip), price_step)
        if adjusted <= 0:
            raise SimulationError("simulation_slippage_price_invalid")
        levels.append((adjusted, _number(row["size"])))
    return levels


def _fee(notional, config, product):
    return _ceil(notional * _number(config["taker_fee_bps"]) / 10000, _number(product["quote_increment"]))


def _affordable_units(*, units, step, notional, price, budget, config, product, prior_quantity=ZERO):
    low, high = 0, units
    while low < high:
        middle = (low + high + 1) // 2
        total = _money(notional + middle * step * price, up=True)
        worst = _money((prior_quantity + middle * step) * price, up=True)
        if (total + _fee(total, config, product) <= budget
                and worst + _fee(worst, config, product) <= budget
                and worst <= _number(product["quote_max_size"])):
            low = middle
        else:
            high = middle - 1
    return low


def _execute(state, snapshot, config, side):
    """Return a simulated IOC receipt; zero or partial execution is explicit."""
    product, levels = snapshot["product"], _levels(snapshot, config, side)
    step = _number(product["base_increment"])
    cash, held = _number(state["cash"]), _number(state["quantity"])
    budget = min(cash, HUNDRED, _number(config["order_budget_usd"]))
    if side == "buy":
        max_qty = min(_number(product["base_max_size"]), budget / levels[0][0])
        units = _affordable_units(units=int(_floor(max_qty, step) / step), step=step, notional=ZERO,
                                  price=levels[0][0], budget=budget, config=config, product=product)
        requested = units * step
    else:
        requested = _floor(min(held, _number(product["base_max_size"]),
                               _number(product["quote_max_size"]) / levels[0][0]), step)
    if (requested < _number(product["base_min_size"]) or requested * levels[0][0] < _number(product["quote_min_size"])):
        return {"side": side, "status": "unfilled_below_minimum", "requested_quantity_btc": _str(requested),
                "filled_quantity_btc": "0", "notional_usd": "0", "fee_usd": "0", "fills": [],
                "simulated": True, "broker_submitted": False}
    remaining, notional, fills = requested, ZERO, []
    for price, depth in levels:
        take = _floor(min(remaining, depth), step)
        if side == "buy":
            take = step * _affordable_units(units=int(take / step), step=step, notional=notional,
                                            price=price, budget=budget, config=config, product=product,
                                            prior_quantity=requested - remaining)
        if take > 0:
            cost = _money(take * price, up=side == "buy")
            fills.append({"quantity_btc": _str(take), "price_usd": _str(price), "notional_usd": _str(cost), "simulated": True})
            notional += cost
            remaining -= take
        if remaining == 0:
            break
    quantity = requested - remaining
    fee = _fee(notional, config, product) if quantity > 0 else ZERO
    worst_notional = _money(quantity * _number(fills[-1]["price_usd"]), up=True) if fills else ZERO
    worst_all_in = worst_notional + _fee(worst_notional, config, product)
    if (side == "buy" and notional + fee > budget) or (side == "sell" and quantity > held):
        raise SimulationError("simulation_execution_budget_invariant")
    if side == "buy" and worst_all_in > budget:
        raise SimulationError("simulation_limit_reserve_invariant")
    if side == "sell" and notional < fee:
        return {"side": side, "status": "unfilled_fee_exceeds_proceeds", "requested_quantity_btc": _str(requested),
                "filled_quantity_btc": "0", "notional_usd": "0", "fee_usd": "0", "fills": [],
                "simulated": True, "broker_submitted": False}
    return {"side": side, "status": "filled" if remaining == 0 else ("partial_canceled" if quantity > 0 else "unfilled_depth"),
            "requested_quantity_btc": _str(requested), "filled_quantity_btc": _str(quantity),
            "unfilled_quantity_btc": _str(remaining), "notional_usd": _str(notional), "fee_usd": _str(fee),
            "simulated_budget_denominated_ioc": True,
            "buy_budget_usd": _str(budget) if side == "buy" else None,
            "worst_filled_price_usd": fills[-1]["price_usd"] if fills else None,
            "buy_limit_worst_case_all_in_usd": _str(worst_all_in) if side == "buy" else None,
            "fills": fills, "simulated": True, "broker_submitted": False,
            "fill_assumption": "displayed_depth_with_adverse_slippage_and_aggregate_fee_rounding"}


def _mark(state, snapshot, config):
    qty, cash, basis = (_number(state[k]) for k in ("quantity", "cash", "basis"))
    base_step = _number(snapshot["product"]["base_increment"])
    marketable = _floor(qty, base_step)
    remaining, gross = marketable, ZERO
    for price, depth in _levels(snapshot, config, "sell"):
        take = _floor(min(remaining, depth), base_step)
        gross += _money(take * price)
        remaining -= take
        if remaining == 0:
            break
    net = max(ZERO, gross - _fee(gross, config, snapshot["product"]))
    remaining += qty - marketable
    bid = _number(snapshot["pricebook"]["bids"][0]["price"])
    product = snapshot["product"]
    dust = qty if (qty > 0 and (qty < _number(product["base_min_size"])
                                or qty * bid < _number(product["quote_min_size"]))) else ZERO
    # Sub-minimum dust is retained and marked, but cannot be called liquid cash.
    if dust > 0:
        remaining, net = qty, ZERO
    state.update(liquidation_equity=_str(cash + net), unpriced_quantity=_str(remaining),
                 unrealized_mark=_str(_money(qty * bid) - basis), dust_quantity=_str(dust))
    state["total_loss"] = _str(max(ZERO, HUNDRED - cash - net))
    state["daily_loss"] = _str(max(ZERO, _number(state["day_start_equity"]) - cash - net))
    peak = max(_number(state["peak_equity"]), cash + net)
    drawdown = peak - cash - net
    state["peak_equity"] = _str(peak)
    state["max_drawdown"] = _str(max(_number(state["max_drawdown"]), drawdown))
    state["max_drawdown_percent"] = _str(max(_number(state["max_drawdown_percent"]), _money(drawdown / peak * 100)))
    for field, setting, reason in (("total_loss", "max_loss_usd", "total_loss_limit"),
                                   ("daily_loss", "daily_loss_usd", "daily_loss_limit")):
        if _number(state[field]) >= _number(config[setting]) and reason not in state["halt_reasons"]:
            state["halt_reasons"].append(reason)


@financial_context
def _transition(previous, snapshot, config):
    state = json.loads(_json(previous))
    observed = _number(snapshot["observed_at"])
    provider = timestamp(snapshot["pricebook"]["time"])
    gap = False
    if state["last_observed"] is not None:
        old_observed, old_provider = _number(state["last_observed"]), _number(state["last_provider"])
        if observed <= old_observed or provider <= old_provider:
            raise SimulationError("simulation_snapshot_time_not_increasing")
        if observed - old_observed < config["interval_seconds"]:
            raise SimulationError("simulation_snapshot_before_cadence")
        gap = observed - old_observed > config["interval_seconds"] * 3
        if _number(state["quantity"]) > 0:
            field = "exposure_gap_seconds" if gap else "exposure_seconds"
            state[field] = _str(_number(state[field]) + observed - old_observed)
        if gap:
            state["samples"] = []
            state["gap_count"] += 1
    day = datetime.fromtimestamp(float(observed), tz=timezone.utc).strftime("%Y-%m-%d")
    if day != state["day"]:
        state["day"] = day
        state["day_start_equity"] = state["liquidation_equity"]
    _mark(state, snapshot, config)
    qty = _number(state["quantity"])
    bid, ask = (_number(snapshot["pricebook"][side][0]["price"]) for side in ("bids", "asks"))
    spread = (ask - bid) / ((ask + bid) / 2) * 10000
    decision, reason = "hold", "warmup"
    prior_count = len(state["samples"])
    if qty > 0 and state["halt_reasons"]:
        decision, reason = "sell", "halt_exit"
    elif config["strategy"] == "round_trip_smoke_v1":
        if state["smoke_phase"] == "ready" and not state["halt_reasons"]:
            decision, reason = "buy", "accounting_smoke_entry"
        elif state["smoke_phase"] == "exit" and qty > 0:
            decision, reason = "sell", "accounting_smoke_exit"
        else:
            reason = "accounting_smoke_complete_or_halted"
    elif not gap and prior_count >= config["long_window"]:
        mids = [_number(item["mid"]) for item in state["samples"]]
        short = sum(mids[-config["short_window"]:]) / config["short_window"]
        long = sum(mids[-config["long_window"]:]) / config["long_window"]
        if qty > 0 and short <= long:
            decision, reason = "sell", "prior_sma_exit"
        elif qty == 0 and (short / long - 1) * 10000 >= _number(config["entry_threshold_bps"]):
            decision, reason = "buy", "prior_sma_entry"
        else:
            reason = "prior_sma_hold"
    if decision == "buy" and (state["halt_reasons"] or spread > 25):
        decision, reason = "hold", "entry_halted" if state["halt_reasons"] else "entry_spread_exceeds_25_bps"
    if gap and decision == "hold":
        reason = "observation_gap_warmup_reset"
    order = None
    if decision in {"buy", "sell"}:
        order = _execute(state, snapshot, config, decision)
        state["orders"] += 1
        state["fills"] += len(order["fills"])
        filled, notional, fee = (_number(order[k]) for k in ("filled_quantity_btc", "notional_usd", "fee_usd"))
        cash, basis, qty = (_number(state[k]) for k in ("cash", "basis", "quantity"))
        if filled > 0:
            state["fees"] = _str(_number(state["fees"]) + fee)
            if decision == "buy":
                if qty != 0:
                    raise SimulationError("simulation_long_only_single_position_invariant")
                state.update(cash=_str(cash - notional - fee), quantity=_str(filled), basis=_str(notional + fee))
                if config["strategy"] == "round_trip_smoke_v1":
                    state["smoke_phase"] = "exit"
            else:
                released = basis if filled == qty else _money(basis * filled / qty)
                state.update(cash=_str(cash + notional - fee), quantity=_str(qty - filled), basis=_str(basis - released),
                             realized=_str(decimal(state["realized"]) + notional - fee - released))
                if filled == qty:
                    state["round_trips"] += 1
                if config["strategy"] == "round_trip_smoke_v1" and filled == qty:
                    state["smoke_phase"] = "done"
        elif config["strategy"] == "round_trip_smoke_v1" and decision == "buy":
            # A smoke entry is one attempt, not a persistent accumulation loop.
            state["smoke_phase"] = "done"
    mid = _money((bid + ask) / 2)
    state["samples"].append({"at": _str(observed), "mid": _str(mid)})
    state["samples"] = state["samples"][-config["long_window"]:]
    state.update(last_observed=_str(observed), last_provider=_str(provider), last_mid=_str(mid),
                 samples_seen=state["samples_seen"] + 1)
    _mark(state, snapshot, config)
    _state_valid(state, config)
    return {"state": state, "snapshot": snapshot,
            "decision": {"action": decision, "reason": reason, "prior_sample_count": prior_count,
                         "signal_uses_current_sample": config["strategy"] == "round_trip_smoke_v1",
                         "gap_reset": gap}, "order": order, "fills": [] if order is None else order["fills"]}


class SimulationLedger:
    """One exclusive writer, one SQLite transaction per observation or stop."""

    def __init__(self, path, config, *, clock=time.time, create=True):
        self.config = validate_config(config)
        self._clock = clock
        self._connection = None
        self._lock = None
        self._state = None
        self._sequence = -1
        self._digest = ""
        try:
            original = Path(path).expanduser()
            self.path = external_path(original)
            if original.is_symlink() or not self.path.parent.is_dir():
                raise SimulationError("simulation_journal_path_invalid")
            parent = self.path.parent.stat()
            if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                raise SimulationError("simulation_journal_parent_requires_0700")
            lock_path = self.path.with_name(self.path.name + ".lock")
            existed_before_lock = self.path.exists()
            if type(create) is not bool:
                raise SimulationError("simulation_create_flag_invalid")
            if not existed_before_lock and (not create or lock_path.exists()):
                raise SimulationError("simulation_existing_journal_required")
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self._lock = os.fdopen(lock_fd, "r+b")
            self._owned_file(os.fstat(lock_fd))
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SimulationError("simulation_writer_already_active") from None
            exists = self.path.exists()
            if not exists and (not create or existed_before_lock):
                raise SimulationError("simulation_existing_journal_required")
            if not exists:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
            self._owned_file(self.path.lstat())
            # mode=rw also closes the lstat-to-connect disappearance race:
            # SQLite itself is forbidden to create a replacement empty file.
            self._connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0, isolation_level=None)
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA synchronous=FULL")
            if exists:
                self.check_integrity()
            else:
                self._connection.executescript("BEGIN IMMEDIATE; CREATE TABLE metadata (config TEXT NOT NULL);"
                    "CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, snapshot_key TEXT UNIQUE, "
                    "body TEXT NOT NULL, previous_hash TEXT NOT NULL, digest TEXT NOT NULL); COMMIT;")
                self._connection.execute("INSERT INTO metadata VALUES (?)", (_json(self.config),))
                self._append("initialized", {"state": _initial()}, None)
                self._sync_directory(self.path.parent)
        except SimulationError:
            self.close()
            raise
        except Exception:
            self.close()
            raise SimulationError("simulation_journal_open_failed") from None

    @staticmethod
    def _sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _owned_file(info):
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SimulationError("simulation_journal_requires_owner_0600")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._connection is not None:
            with suppress(Exception):
                self._connection.close()
            self._connection = None
        if self._lock is not None:
            with suppress(Exception):
                self._lock.close()
            self._lock = None

    @financial_context
    def check_integrity(self):
        try:
            if self._connection is None or self._connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise SimulationError("simulation_journal_integrity_failed")
            metadata = self._connection.execute("SELECT config FROM metadata").fetchall()
            if metadata != [(_json(self.config),)]:
                raise SimulationError("simulation_journal_config_mismatch")
            rows = self._connection.execute("SELECT seq,kind,snapshot_key,body,previous_hash,digest FROM events ORDER BY seq").fetchall()
            if not rows:
                raise SimulationError("simulation_journal_missing_initialization")
            previous, prior_state = "", None
            for expected, (seq, kind, key, encoded, prior, digest) in enumerate(rows):
                body = json.loads(encoded)
                computed = hashlib.sha256(_json([seq, kind, key, body, prior]).encode()).hexdigest()
                if seq != expected or prior != previous or digest != computed or encoded != _json(body):
                    raise SimulationError("simulation_journal_audit_invalid")
                if seq == 0:
                    if kind != "initialized" or key is not None or body != {"state": _initial()}:
                        raise SimulationError("simulation_journal_initialization_invalid")
                elif kind == "step":
                    if key != hashlib.sha256(_json(body["snapshot"]).encode()).hexdigest():
                        raise SimulationError("simulation_journal_snapshot_key_invalid")
                    clean = _snapshot(body["snapshot"], now=_number(body["snapshot"]["observed_at"]))
                    if clean != body["snapshot"]:
                        raise SimulationError("simulation_journal_snapshot_invalid")
                    if body != _transition(prior_state, body["snapshot"], self.config):
                        raise SimulationError("simulation_journal_replay_mismatch")
                elif kind == "stop":
                    expected_state = json.loads(_json(prior_state))
                    reason = body.get("reason")
                    if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", reason):
                        raise SimulationError("simulation_journal_stop_invalid")
                    if reason not in expected_state["halt_reasons"]:
                        expected_state["halt_reasons"].append(reason)
                    if body != {"state": expected_state, "reason": reason}:
                        raise SimulationError("simulation_journal_stop_invalid")
                else:
                    raise SimulationError("simulation_journal_event_invalid")
                _state_valid(body["state"], self.config)
                previous, prior_state = digest, body["state"]
            self._state, self._sequence, self._digest = prior_state, rows[-1][0], previous
            return {"ok": True, "event_count": len(rows), "audit_sha256": previous}
        except SimulationError:
            raise
        except Exception:
            raise SimulationError("simulation_journal_integrity_failed") from None

    def _append(self, kind, body, snapshot_key):
        if self._connection is None:
            raise SimulationError("simulation_journal_closed")
        seq = self._sequence + 1
        digest = hashlib.sha256(_json([seq, kind, snapshot_key, body, self._digest]).encode()).hexdigest()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                                     (seq, kind, snapshot_key, _json(body), self._digest, digest))
            self._connection.execute("COMMIT")
        except Exception:
            with suppress(Exception):
                self._connection.execute("ROLLBACK")
            self.close()  # COMMIT may have succeeded before an I/O error surfaced.
            raise SimulationError("simulation_atomic_commit_failed") from None
        self._state, self._sequence, self._digest = body["state"], seq, digest

    @financial_context
    def step(self, snapshot):
        if self._connection is None:
            raise SimulationError("simulation_journal_closed")
        # An exact duplicate is safe to return even after it has become stale.
        try:
            candidate = _snapshot(snapshot, now=decimal(snapshot["observed_at"], positive=True))
            duplicate_key = hashlib.sha256(_json(candidate).encode()).hexdigest()
            duplicate = self._connection.execute("SELECT body FROM events WHERE snapshot_key=?", (duplicate_key,)).fetchone()
        except Exception:
            raise SimulationError("simulation_snapshot_invalid") from None
        if duplicate is not None:
            body = json.loads(duplicate[0])
            return {**self.status(), "idempotent_replay": True, "step": body}
        clean = _snapshot(snapshot, now=decimal(self._clock(), positive=True))
        key = hashlib.sha256(_json(clean).encode()).hexdigest()
        body = _transition(self._state, clean, self.config)
        self._append("step", body, key)
        return {**self.status(), "idempotent_replay": False, "step": body}

    @financial_context
    def status(self):
        if self._state is None or self._connection is None:
            raise SimulationError("simulation_journal_closed")
        s = self._state
        return {"mode": "synthetic_simulation", "strategy": self.config["strategy"], "config": dict(self.config),
                "synthetic_cash_usd": s["cash"], "holdings_btc": s["quantity"], "cost_basis_usd": s["basis"],
                "realized_pnl_usd": s["realized"], "cumulative_fees_usd": s["fees"],
                "unrealized_mark_pnl_usd": s["unrealized_mark"], "liquidation_equity_estimate_usd": s["liquidation_equity"],
                "liquidation_unpriced_btc": s["unpriced_quantity"], "dust_btc": s["dust_quantity"],
                "total_loss_usd": s["total_loss"], "daily_loss_usd": s["daily_loss"], "utc_day": s["day"],
                "day_start_liquidation_equity_usd": s["day_start_equity"], "halted": bool(s["halt_reasons"]),
                "peak_liquidation_equity_usd": s["peak_equity"], "max_drawdown_usd": s["max_drawdown"],
                "max_drawdown_percent": s["max_drawdown_percent"], "completed_round_trips": s["round_trips"],
                "observation_gap_count": s["gap_count"], "modeled_exposure_seconds": s["exposure_seconds"],
                "unobserved_exposure_gap_seconds": s["exposure_gap_seconds"],
                "halt_reasons": list(s["halt_reasons"]), "samples_seen": s["samples_seen"],
                "warmup_samples": len(s["samples"]), "event_count": self._sequence + 1,
                "simulated_orders": s["orders"], "simulated_fills": s["fills"], "last_observed_at": s["last_observed"],
                "last_provider_at": s["last_provider"], "smoke_phase": s["smoke_phase"],
                "execution_enabled": False, "broker_orders_submitted": 0, "account_connected": False,
                "strategy_evidence_eligible": False, "strategy_profitability_validated": False,
                "audit_sha256": self._digest,
                "marking_basis": "bid_depth_less_adverse_slippage_and_rounded_exit_fee; unavailable_depth_and_dust_zero_for_loss_gates",
                "loss_halts_are_sticky": True, "accounting_only_smoke": self.config["strategy"] == "round_trip_smoke_v1"}

    def stop(self, reason="manual"):
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", reason):
            raise SimulationError("simulation_stop_reason_invalid")
        self.status()
        if reason not in self._state["halt_reasons"]:
            state = json.loads(_json(self._state))
            state["halt_reasons"].append(reason)
            self._append("stop", {"state": state, "reason": reason}, None)
        return self.status()

    def backup(self, path):
        self.check_integrity()
        try:
            original = Path(path).expanduser()
            destination = external_path(original)
            if original.is_symlink() or destination.exists() or not destination.parent.is_dir():
                raise SimulationError("simulation_backup_destination_invalid")
            if destination.parent.stat().st_uid != os.getuid() or stat.S_IMODE(destination.parent.stat().st_mode) != 0o700:
                raise SimulationError("simulation_backup_parent_requires_0700")
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            target = sqlite3.connect(destination)
            try:
                self._connection.backup(target)
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise SimulationError("simulation_backup_integrity_failed")
            finally:
                target.close()
            with open(destination, "rb") as stream:
                os.fsync(stream.fileno())
            self._sync_directory(destination.parent)
            return {"path": str(destination), "event_count": self._sequence + 1, "audit_sha256": self._digest,
                    "execution_enabled": False}
        except SimulationError:
            raise
        except Exception:
            raise SimulationError("simulation_backup_failed") from None
