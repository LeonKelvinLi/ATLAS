"""Pure, fail-closed risk checks for the isolated PAPER equity execution path.

Trusted journal state binds account, strategy, capital, counters and reservations.
Broker snapshots must be complete and fresh. No input grants live authority.
Buys reserve limit notional plus fees; sells reserve owned shares. The order cap
applies to both sides; larger exits require separately checked smaller orders.
Loss, count and exposure ceilings halt entries, while kill, identity, data, session and
reconciliation checks also apply to exits. Thresholds do not guarantee losses.
This module performs no I/O and never changes the supplied dictionaries.
"""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import re

from atlas.domain import decimal, financial_context


D = Decimal
HASH = re.compile(r"[a-f0-9]{64}")
SYMBOL = re.compile(r"[A-Z]{1,8}(?:\.[A-Z])?")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
DECISION = {"signal_id", "strategy_id", "strategy_sha256", "data_sha256", "symbol",
            "side", "quantity", "limit_price", "signal_time", "expires_at"}
OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel",
                 "accepted_for_bidding", "pending_replace", "done_for_day", "suspended", "stopped", "calculated"}


def _amount(value, *, positive=False):
    if type(value) is not str:
        raise ValueError("paper_financial_string_required")
    result = decimal(value, positive=positive, nonnegative=True)
    if result > D("1e12"):
        raise ValueError("paper_financial_bound_exceeded")
    return result


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 32503680000:
        raise ValueError("paper_timestamp_invalid")
    return value


def _count(value):
    if type(value) is not int or not 0 <= value <= 1000000:
        raise ValueError("paper_counter_invalid")
    return value


def _hash(value):
    return type(value) is str and HASH.fullmatch(value) is not None


def _label(value):
    return type(value) is str and LABEL.fullmatch(value) is not None


def _symbol(value):
    return type(value) is str and SYMBOL.fullmatch(value) is not None


@dataclass(frozen=True)
class PaperRiskPolicy:
    allocated_principal_limit: D = D("100")
    max_order_notional: D = D("10")
    max_position_notional: D = D("25")
    max_portfolio_notional: D = D("75")
    max_positions: int = 3
    max_daily_loss: D = D("2")
    max_drawdown_fraction: D = D("0.05")
    max_attempts_per_minute: int = 5
    max_attempts_per_day: int = 30
    max_quote_age_seconds: int = 10
    max_snapshot_age_seconds: int = 10
    max_spread_bps: D = D("50")
    max_slippage_bps: D = D("50")
    fee_buffer_bps: D = D("10")
    max_broker_failures: int = 3
    max_signal_age_seconds: int = 300
    max_intent_ttl_seconds: int = 300
    allowed_symbols: tuple[str, ...] = ("SPY", "QQQ", "GLD", "USO")
    allowed_strategy_ids: tuple[str, ...] = ()

    @financial_context
    def __post_init__(self):
        money = ("allocated_principal_limit", "max_order_notional", "max_position_notional",
                 "max_portfolio_notional", "max_daily_loss", "max_drawdown_fraction",
                 "max_spread_bps", "max_slippage_bps", "fee_buffer_bps")
        for name in money:
            value = getattr(self, name)
            if not isinstance(value, (str, D)):
                raise ValueError("paper_policy_decimal_required")
            value = decimal(value, positive=True)
            object.__setattr__(self, name, value)
        if (not self.max_order_notional <= self.max_position_notional <= self.max_portfolio_notional
                <= self.allocated_principal_limit <= 100
                or self.max_daily_loss > self.allocated_principal_limit
                or self.max_drawdown_fraction > 1
                or max(self.max_spread_bps, self.max_slippage_bps, self.fee_buffer_bps) > 1000):
            raise ValueError("paper_policy_financial_limits_invalid")
        bounds = {"max_positions": 100, "max_attempts_per_minute": 60, "max_attempts_per_day": 1000,
                  "max_quote_age_seconds": 30, "max_snapshot_age_seconds": 30,
                  "max_broker_failures": 10, "max_signal_age_seconds": 3600, "max_intent_ttl_seconds": 300}
        for name, maximum in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("paper_policy_counter_or_age_invalid")
        if self.max_attempts_per_minute > self.max_attempts_per_day:
            raise ValueError("paper_policy_attempt_limits_invalid")
        for name, validator in (("allowed_symbols", _symbol), ("allowed_strategy_ids", _label)):
            values = getattr(self, name)
            if (not isinstance(values, (tuple, list, set, frozenset)) or len(values) > 100
                    or any(not validator(value) for value in values) or len(set(values)) != len(values)):
                raise ValueError("paper_policy_allowlist_invalid")
            object.__setattr__(self, name, tuple(sorted(values)))

    def public(self):
        return {name: str(value) if isinstance(value, D) else list(value) if isinstance(value, tuple) else value
                for name, value in self.__dict__.items()}

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.public(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@financial_context
def evaluate(policy: PaperRiskPolicy, decision: dict, snapshot: dict, state: dict, *, now: float) -> dict:
    """Return reasons and exact reservations; any malformed input fails closed.

    ``unresolved_orders`` is a count. Extra local reservations remain charged;
    every broker open order must match a reservation. The journal must exclude
    this proposed intent's own reservation when rechecking its unchanged terms.
    Account equity/anchors are the dedicated paper allocation's marked values.
    """
    checks = {}
    cash_reserve, qty_reserve, fees, reducing = D(0), D(0), D(0), False

    def check(name, passed):
        checks[name] = bool(passed)

    def result():
        passed = bool(checks) and all(checks.values())
        return {"passed": passed, "reasons": [name for name, ok in checks.items() if not ok],
                "checks": checks, "reservation_cash": str(cash_reserve if passed else D(0)),
                "reservation_qty": str(qty_reserve if passed else D(0)), "reduce_only": reducing,
                "estimated_fees": str(fees)}

    try:
        if not isinstance(policy, PaperRiskPolicy) or any(type(value) is not dict for value in (decision, snapshot, state)):
            raise ValueError
        _time(now)
        if set(decision) != DECISION:
            raise ValueError
        if any(not _label(decision[key]) for key in ("signal_id", "strategy_id")) or not _symbol(decision["symbol"]):
            raise ValueError
        if any(not _hash(decision[key]) for key in ("strategy_sha256", "data_sha256")) or decision["side"] not in {"buy", "sell"}:
            raise ValueError
        symbol, buy = decision["symbol"], decision["side"] == "buy"
        quantity, price = (_amount(decision[key], positive=True) for key in ("quantity", "limit_price"))
        signal, expiry = _time(decision["signal_time"]), _time(decision["expires_at"])
        check("signal_fresh", 0 <= now - signal <= policy.max_signal_age_seconds)
        check("intent_unexpired", signal <= now < expiry and 0 < expiry - signal <= policy.max_intent_ttl_seconds)
        check("strategy_identity", _hash(state["strategy_sha256"]) and decision["strategy_sha256"] == state["strategy_sha256"]
              and decision["strategy_id"] in policy.allowed_strategy_ids)
        check("symbol_allowed", symbol in policy.allowed_symbols)
        check("order_precision", quantity % D("0.000000001") == 0
              and price % (D(".01") if price >= 1 else D(".0001")) == 0)

        account, quote, clock, asset = (snapshot[key] for key in ("account", "quote", "clock", "asset"))
        if any(type(value) is not dict for value in (account, quote, clock, asset)):
            raise ValueError
        check("account_identity", _hash(state["account_fingerprint"])
              and account["fingerprint"] == state["account_fingerprint"])
        check("account_permission", account["status"] == "ACTIVE" and account["currency"] == "USD"
              and account["blocked"] is False and _amount(account["multiplier"], positive=True) == 1
              and _amount(account["short_market_value"]) == 0)
        cash, equity, buying_power, long_value = (_amount(account[key]) for key in ("cash", "equity", "buying_power", "long_market_value"))
        check("cash_account", equity > 0 and buying_power <= cash)
        observed = _time(snapshot["observed_at"])
        account_at, quote_observed, clock_observed = (_time(value["observed_at"]) for value in (account, quote, clock))
        check("snapshot_fresh", 0 <= now - observed <= policy.max_snapshot_age_seconds
              and all(0 <= now - at <= policy.max_snapshot_age_seconds and at <= observed
                      for at in (account_at, quote_observed, clock_observed)))
        quote_at, clock_at = _time(quote["at"]), _time(clock["at"])
        check("quote_fresh", 0 <= now - quote_at <= policy.max_quote_age_seconds and quote_at <= quote_observed)
        check("session_open", clock["is_open"] is True and 0 <= now - clock_at <= policy.max_snapshot_age_seconds
              and clock_at <= clock_observed and _time(clock["next_close"]) > now and _time(clock["next_open"]) > 0)
        check("asset_permission", asset["symbol"] == symbol and asset["asset_class"] == "us_equity"
              and asset["status"] == "active" and asset["tradable"] is True
              and type(asset["fractionable"]) is bool and (quantity % 1 == 0 or asset["fractionable"]))
        bid, ask, bid_size, ask_size = (_amount(quote[key], positive=True) for key in ("bid", "ask", "bid_size", "ask_size"))
        check("quote_valid", quote["symbol"] == symbol and bid <= ask)
        check("spread", bid <= ask and (ask - bid) / ((ask + bid) / 2) * 10000 <= policy.max_spread_bps)
        executable = ask if buy else bid
        check("limit_distance", abs(price - executable) / executable * 10000 <= policy.max_slippage_bps)
        check("quote_liquidity", quantity <= (ask_size if buy else bid_size))

        positions, orders, reservations = snapshot["positions"], snapshot["open_orders"], state["reservations"]
        if any(type(items) is not list or len(items) > 500 for items in (positions, orders, reservations)):
            raise ValueError
        held, values, gross = {}, {}, D(0)
        for position in positions:
            name = position["symbol"]
            if not _symbol(name) or name in held:
                raise ValueError
            qty, value, mark = (_amount(position[key], positive=True) for key in ("qty", "market_value", "current_price"))
            _amount(position["cost_basis"])
            if abs(qty * mark - value) > D(".01"):
                raise ValueError
            held[name], values[name] = qty, max(value, qty * mark)
            gross += values[name]
        check("account_values_consistent", abs(gross - long_value) <= D(".01") * max(1, len(positions))
              and abs(cash + long_value - equity) <= D(".01"))
        committed_cash, committed_sell, reserved_values, owned_reservations = D(0), {}, {}, {}
        for reservation in reservations:
            identifier, name, side = (reservation[key] for key in ("client_order_id", "symbol", "side"))
            if not _label(identifier) or identifier in owned_reservations or not _symbol(name) or side not in {"buy", "sell"}:
                raise ValueError
            remaining = _amount(reservation["remaining_qty"], positive=True)
            reserved = _amount(reservation["remaining_cash"])
            if (side == "buy" and reserved <= 0) or (side == "sell" and reserved != 0):
                raise ValueError
            owned_reservations[identifier] = (name, side, remaining, reserved)
            if side == "buy":
                committed_cash += reserved
                reserved_values[name] = reserved_values.get(name, D(0)) + reserved
            else:
                committed_sell[name] = committed_sell.get(name, D(0)) + remaining
        seen_orders = set()
        for order in orders:
            identifier = order["client_order_id"]
            if not _label(identifier) or identifier in seen_orders or order["status"] not in OPEN_STATUSES:
                raise ValueError
            seen_orders.add(identifier)
            qty, filled, limit = _amount(order["qty"], positive=True), _amount(order["filled_qty"]), _amount(order["limit_price"], positive=True)
            if not 0 <= filled < qty:
                raise ValueError
            expected = owned_reservations.get(identifier)
            if (expected is None or expected[:3] != (order["symbol"], order["side"], qty - filled)
                    or order["side"] == "buy" and expected[3] < (qty - filled) * limit * (1 + policy.fee_buffer_bps / 10000)):
                raise ValueError
        # Non-margin buying power already includes broker open-order holds.
        # Cash is unreserved; only local, not-yet-open holds additionally reduce BP.
        broker_reserved_cash = sum((owned_reservations[key][3] for key in seen_orders
                                    if owned_reservations[key][1] == "buy"), D(0))
        local_reserved_cash = committed_cash - broker_reserved_cash
        check("existing_reservations_covered", committed_cash <= cash and local_reserved_cash <= buying_power
              and all(qty <= held.get(name, D(0)) for name, qty in committed_sell.items()))

        principal = _amount(state["allocated_principal"], positive=True)
        day_start, peak = (_amount(state[key], positive=True) for key in ("day_start_equity", "peak_equity"))
        check("allocation_ceiling", principal <= policy.allocated_principal_limit <= 100)
        check("equity_anchors_valid", peak >= max(equity, day_start))
        for key in ("kill_switch", "reconciliation_ok", "entry_halt"):
            if type(state[key]) is not bool:
                raise ValueError
        check("kill_switch_clear", state["kill_switch"] is False)
        check("reconciled", state["reconciliation_ok"] is True and _count(state["unresolved_orders"]) == 0)
        check("broker_circuit", _count(state["broker_failures"]) < policy.max_broker_failures)
        check("cooldown_elapsed", _time(state["cooldown_until"]) <= now)
        attempts = _count(state["daily_attempts"])
        recent = state["recent_attempt_times"]
        if type(recent) is not list or len(recent) > 10000 or any(_time(at) > now for at in recent):
            raise ValueError
        if recent != sorted(recent):
            raise ValueError

        notional = quantity * price
        fees = notional * policy.fee_buffer_bps / 10000
        reducing = not buy and quantity <= held.get(symbol, D(0)) - committed_sell.get(symbol, D(0))
        check("inventory", buy or reducing)
        check("order_notional", notional + fees <= policy.max_order_notional)
        check("minimum_buy_notional", not buy or notional >= 1)
        cash_reserve, qty_reserve = (notional + fees, D(0)) if buy else (D(0), quantity)
        check("cash_available", not buy or cash_reserve <= min(cash - committed_cash, buying_power - local_reserved_cash))
        risk_basis = min(principal, equity)
        post_gross = gross + committed_cash + (cash_reserve if buy else -values.get(symbol, D(0)) * quantity / held.get(symbol, D(1)))
        post_symbol = values.get(symbol, D(0)) + reserved_values.get(symbol, D(0)) + (cash_reserve if buy else D(0))
        position_names = set(held) | set(reserved_values) | ({symbol} if buy else set())
        check("portfolio_exposure", reducing or post_gross <= min(policy.max_portfolio_notional, risk_basis))
        check("position_exposure", reducing or post_symbol <= min(policy.max_position_notional, risk_basis))
        check("position_count", reducing or len(position_names) <= policy.max_positions)
        check("daily_loss", reducing or day_start - equity < policy.max_daily_loss)
        check("drawdown", reducing or (peak - equity) / peak < policy.max_drawdown_fraction)
        check("entry_halt_clear", reducing or not state["entry_halt"])
        check("minute_attempt_limit", reducing or sum(now - at < 60 for at in recent) < policy.max_attempts_per_minute)
        check("daily_attempt_limit", reducing or attempts < policy.max_attempts_per_day)
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
        check("complete_valid_inputs", False)
    return result()
