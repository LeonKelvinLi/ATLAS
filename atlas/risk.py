"""Deterministic, broker-independent pre-trade constraints.

External text and MCP callers cannot change these server-owned limits.
Unknown portfolio/risk data is a failed check, never a zero balance or loss.
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from types import MappingProxyType

from atlas.domain import AccountSnapshot, Quote, decimal, financial_context, json_value


def account_fingerprint(broker: str, account_id: str) -> str:
    return hashlib.sha256(f"{broker}\0{account_id}".encode()).hexdigest()


@dataclass(frozen=True)
class RiskPolicy:
    mode: str = "research"
    expected_account_id: str | None = field(default=None, repr=False)
    live_enabled: bool = False
    auto_enabled: bool = False
    allowed_symbols: frozenset[str] = frozenset()
    denied_symbols: frozenset[str] = frozenset()
    enabled_strategies: frozenset[str] = frozenset({"momentum", "mean_reversion"})
    live_approved_strategies: frozenset[str] = frozenset()
    max_order_notional: Decimal = Decimal("100")
    max_position_notional: Decimal = Decimal("500")
    max_concentration: Decimal = Decimal("0.10")
    max_daily_loss: Decimal = Decimal("100")
    max_daily_trades: int = 10
    max_quote_age_seconds: int = 30
    max_account_age_seconds: int = 30
    max_slippage_bps: Decimal = Decimal("25")
    max_spread_bps: Decimal = Decimal("100")
    min_confidence: Decimal = Decimal("0")
    proposal_ttl_seconds: int = 120
    version: str = "risk-v1"

    def __post_init__(self):
        if self.mode not in {"research", "paper", "live-assisted", "live-auto"}:
            raise ValueError("invalid ATLAS mode")
        if type(self.live_enabled) is not bool or type(self.auto_enabled) is not bool:
            raise ValueError("live enable flags must be booleans")
        for name in ("max_order_notional", "max_position_notional", "max_daily_loss",
                     "max_concentration"):
            object.__setattr__(self, name, decimal(getattr(self, name), positive=True))
        for name in ("max_slippage_bps", "max_spread_bps", "min_confidence"):
            object.__setattr__(self, name, decimal(getattr(self, name), nonnegative=True))
        if self.max_concentration > 1 or self.min_confidence > 1:
            raise ValueError("concentration/confidence must not exceed one")
        for name in ("max_daily_trades", "max_quote_age_seconds", "max_account_age_seconds",
                     "proposal_ttl_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("allowed_symbols", "denied_symbols", "enabled_strategies",
                     "live_approved_strategies"):
            value = getattr(self, name)
            if isinstance(value, str) or any(not isinstance(item, str) or not item for item in value):
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, frozenset(value))

    def public(self) -> dict:
        # Account numbers are deliberately omitted from both reports and hashes.
        return {name: sorted(value) if isinstance(value, frozenset) else json_value(value)
                for name, value in self.__dict__.items() if name != "expected_account_id"}

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.public(), sort_keys=True).encode()).hexdigest()


@financial_context
def evaluate_risk(policy: RiskPolicy, account: AccountSnapshot, quote: Quote, *,
                  symbol: str, asset_class: str, side: str, quantity, limit_price,
                  strategy: dict, now: int, daily_stats: dict,
                  pending_intents: bool, broker_open_orders: int,
                  kill_switch: bool, reference_price=None) -> dict:
    checks = []

    def check(code, passed, detail):
        checks.append({"code": code, "passed": bool(passed), "detail": detail})

    live = policy.mode.startswith("live-")
    check("execution_mode", policy.mode != "research", f"mode={policy.mode}")
    check("live_enable", not live or policy.live_enabled, "server live enable flag")
    check("auto_enable", policy.mode != "live-auto" or policy.auto_enabled, "separate automatic execution flag")
    check("kill_switch", not kill_switch, "operator kill switch")
    check("account_identity", bool(policy.expected_account_id)
          and account.account_id == policy.expected_account_id, "expected account must match")
    check("account_mode", account.mode == ("real" if live else "paper"), "expected account type")
    check("account_tradable", account.tradable is True, "broker account permission")
    check("account_freshness", isinstance(account.observed_at, int)
          and not isinstance(account.observed_at, bool)
          and 0 <= now - account.observed_at <= policy.max_account_age_seconds, "fresh account snapshot")
    check("symbol_allowed", symbol in policy.allowed_symbols and symbol not in policy.denied_symbols,
          "server symbol allow/deny rules")
    check("strategy_enabled", strategy.get("strategy_id") in policy.enabled_strategies,
          "server strategy registry and allowlist")
    check("strategy_contract", strategy.get("symbol") == symbol
          and strategy.get("asset_class") == asset_class
          and strategy.get("direction") == side and asset_class in {"equity", "crypto"},
          "signal, symbol, direction and spot/equity contract must agree")
    check("live_validation", not live or (strategy.get("live_validated") is True
          and strategy.get("strategy_id") in policy.live_approved_strategies),
          "research baselines and legacy binary models are not live-approved spot strategies")
    confidence = strategy.get("confidence")
    try:
        confidence = decimal(confidence) if confidence is not None else None
        confidence_ok = confidence is not None and 0 <= confidence <= 1 and confidence >= policy.min_confidence
    except ValueError:
        confidence_ok = False
    check("confidence", confidence_ok or (not live and policy.min_confidence == 0 and confidence is None),
          "calibrated confidence required for live; unavailable remains unavailable")
    check("pending_reconciliation", not pending_intents, "any unresolved account intent blocks new orders")
    check("open_orders", broker_open_orders == 0, "conservative single outstanding order per account")
    count = daily_stats.get("intents_count")
    check("daily_trade_count", isinstance(count, int) and not isinstance(count, bool)
          and 0 <= count < policy.max_daily_trades, "all durable submission attempts count")
    check("quote_contract", quote.symbol == symbol and quote.asset_class == asset_class,
          "quote must describe the proposed instrument")
    valid_currency = lambda value: (isinstance(value, str) and len(value) == 3
                                    and value.isascii() and value.isalpha() and value.isupper())
    check("currency", valid_currency(account.currency) and valid_currency(quote.currency)
          and account.currency == quote.currency, "known three-letter portfolio and quote currency must agree")
    check("quote_freshness", isinstance(quote.timestamp, int) and not isinstance(quote.timestamp, bool)
          and 0 <= now - quote.timestamp <= policy.max_quote_age_seconds
          and (not live or quote.timestamp_basis == "provider"), "fresh provider timestamp required for live")
    signal_ts = strategy.get("data_timestamp")
    signal_max_age = strategy.get("max_age_seconds", 300)
    check("signal_freshness", isinstance(signal_ts, int) and not isinstance(signal_ts, bool)
          and type(signal_max_age) is int and signal_max_age > 0
          and 0 <= now - signal_ts <= signal_max_age, "server signal needs a positive integer age limit and must remain current")
    check("market_hours", quote.market_open is True, "market must be confirmed open")
    check("side", side in {"buy", "sell"}, "long-only buy or inventory-backed sell")

    amounts = {}
    try:
        qty = decimal(quantity, positive=True)
        price = decimal(limit_price, positive=True)
        bid, ask = decimal(quote.bid, positive=True), decimal(quote.ask, positive=True)
        fee = decimal(quote.fee_per_unit, nonnegative=True)
        equity = decimal(account.equity, positive=True)
        cash = decimal(account.cash, nonnegative=True)
        buying_power = decimal(account.buying_power, nonnegative=True)
        notional, fees = qty * price, qty * fee
        check("price_sanity", ask >= bid, "positive uncrossed quote")
        spread = (ask - bid) / ((ask + bid) / 2) * 10000
        check("spread", 0 <= spread <= policy.max_spread_bps, "bid/ask spread within policy")
        current = ask if side == "buy" else bid
        limit_distance = abs(price - current) / current * 10000
        check("price_limit", limit_distance <= policy.max_slippage_bps,
              "immutable limit price remains near the executable quote")
        if reference_price is not None:
            reference = decimal(reference_price, positive=True)
            check("price_drift", abs(current - reference) / reference * 10000 <= policy.max_slippage_bps,
                  "quote change since proposal is within tolerance")
        check("order_notional", notional + fees <= policy.max_order_notional, "notional plus estimated fees")
        position_value = Decimal("0")
        inventory = Decimal("0")
        for position in account.positions:
            value = decimal(position.market_value, nonnegative=True)
            held = decimal(position.quantity, nonnegative=True)
            if position.symbol == symbol:
                position_value += value
                inventory += held
        post_value = max(Decimal("0"), position_value + (notional if side == "buy" else -notional))
        check("buying_power", side != "buy" or notional + fees <= min(cash, buying_power),
              "cash and available buying power cover the order")
        check("inventory", side != "sell" or qty <= inventory, "short selling is not enabled")
        check("position_size", post_value <= policy.max_position_notional, "post-trade symbol value limit")
        # Charge fees against equity when measuring post-trade concentration.
        check("concentration", equity > fees and post_value <= (equity - fees) * policy.max_concentration,
              "post-trade symbol concentration limit")
        loss = decimal(account.daily_realized_pnl)
        check("daily_loss", loss > -policy.max_daily_loss, "verified daily realized P&L limit")
        liquidity = decimal(quote.available_quantity, nonnegative=True)
        check("liquidity", qty <= liquidity, "verified available quantity covers requested quantity")
        if quote.quantity_increment is not None:
            increment = decimal(quote.quantity_increment, positive=True)
            check("quantity_increment", qty % increment == 0, "broker lot/quantity increment")
        if quote.minimum_quantity is not None:
            check("minimum_quantity", qty >= decimal(quote.minimum_quantity, positive=True), "broker minimum quantity")
        amounts = json_value({"notional": notional, "estimated_fees": fees,
                              "position_value_after": post_value, "spread_bps": spread})
    except (ValueError, TypeError, ArithmeticError):
        check("complete_finite_risk_data", False,
              "finite prices, valuations, cash, daily loss and liquidity are required; missing is not zero")
    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "policy_hash": policy.fingerprint(), **amounts}


@financial_context
def position_size(policy: RiskPolicy, account: AccountSnapshot, quote: Quote) -> dict:
    """Maximum buy quantity under known limits; not a return forecast."""
    try:
        price = decimal(quote.ask, positive=True) + decimal(quote.fee_per_unit, nonnegative=True)
        equity = decimal(account.equity, positive=True)
        current = sum((decimal(p.market_value, nonnegative=True) for p in account.positions
                       if p.symbol == quote.symbol), Decimal("0"))
        budget = max(Decimal("0"), min(policy.max_order_notional,
                     policy.max_position_notional - current,
                     equity * policy.max_concentration - current,
                     decimal(account.cash, nonnegative=True), decimal(account.buying_power, nonnegative=True)))
        qty = budget / price
        qty = min(qty, decimal(quote.available_quantity, nonnegative=True))
        increment = decimal(quote.quantity_increment, positive=True)
        qty = (qty / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        if quote.minimum_quantity is not None and qty < decimal(quote.minimum_quantity, positive=True):
            qty = Decimal("0")
        return {"available": True, "maximum_quantity": str(qty), "policy_budget": str(budget),
                "maximum_notional": str(qty * price),
                "requires_full_risk_check": True, "basis": "policy ceilings, not optimized allocation"}
    except (ValueError, TypeError, ArithmeticError):
        return {"available": False, "reason": "insufficient verified portfolio or quote data"}


@dataclass(frozen=True)
class PortfolioRiskPolicy:
    """Optional trusted portfolio gates, independent of baseline order policy.

    Exposures use account-currency notionals, leverage/weights are fractions,
    turnover is a multiple of a declared fixed equity basis, and volatility
    requires an order-bound projection from a separately vetted provider.
    """
    max_drawdown: Decimal | None = None
    max_gross_leverage: Decimal | None = None
    max_net_leverage: Decimal | None = None
    max_asset_class_weights: dict = field(default_factory=dict)
    max_correlated_weight: Decimal | None = None
    max_positions: int | None = None
    max_turnover: Decimal | None = None
    max_portfolio_volatility: Decimal | None = None
    max_snapshot_age_seconds: int = 30
    require_health: bool = True
    allowed_strategy_health: frozenset[str] = frozenset({"HEALTHY", "WATCH"})
    volatility_basis: str = "annualized_daily_returns"

    def __post_init__(self):
        for name in ("max_drawdown", "max_gross_leverage", "max_net_leverage", "max_correlated_weight",
                     "max_turnover", "max_portfolio_volatility"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, decimal(getattr(self, name), positive=True))
        for name in ("max_drawdown", "max_correlated_weight"):
            if getattr(self, name) is not None and getattr(self, name) > 1:
                raise ValueError("portfolio fractions cannot exceed one")
        weights = {}
        if not isinstance(self.max_asset_class_weights, (dict, MappingProxyType)):
            raise ValueError("asset-class weights require a mapping")
        for name, value in self.max_asset_class_weights.items():
            if name not in {"equity", "crypto"}:
                raise ValueError("unknown long-only asset class")
            weights[name] = decimal(value, positive=True)
            if weights[name] > 1:
                raise ValueError("asset-class weights cannot exceed one")
        object.__setattr__(self, "max_asset_class_weights", MappingProxyType(weights))
        for name in ("max_positions", "max_snapshot_age_seconds"):
            value = getattr(self, name)
            if name == "max_snapshot_age_seconds" and value is None:
                raise ValueError("snapshot age limit is required")
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("portfolio count/age limits must be positive integers")
        if type(self.require_health) is not bool:
            raise ValueError("require_health must be boolean")
        if isinstance(self.allowed_strategy_health, str):
            raise ValueError("allowed strategy health requires a collection")
        states = frozenset(self.allowed_strategy_health)
        if not states or not states <= {"HEALTHY", "WATCH"}:
            raise ValueError("degraded/disabled strategy health cannot authorize new risk")
        object.__setattr__(self, "allowed_strategy_health", states)
        if not isinstance(self.volatility_basis, str) or not self.volatility_basis.strip():
            raise ValueError("volatility basis must be explicit")

    def public(self):
        return {name: ({key: str(value) for key, value in item.items()} if name == "max_asset_class_weights"
                       else sorted(item) if isinstance(item, frozenset) else json_value(item))
                for name, item in self.__dict__.items()}

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.public(), sort_keys=True).encode()).hexdigest()


def portfolio_order_fingerprint(symbol, asset_class, side, quantity, limit_price, estimated_fees):
    terms = {"symbol": symbol, "asset_class": asset_class, "side": side,
             "quantity": str(decimal(quantity, positive=True)), "limit_price": str(decimal(limit_price, positive=True)),
             "estimated_fees": str(decimal(estimated_fees, nonnegative=True))}
    return hashlib.sha256(json.dumps(terms, sort_keys=True).encode()).hexdigest()


@financial_context
def evaluate_portfolio_risk(policy: PortfolioRiskPolicy, snapshot, *, now, expected_account_fingerprint,
                            symbol, asset_class, strategy_id, side, quantity, limit_price, estimated_fees=None):
    """Evaluate fresh trusted data; missing configured inputs fail closed.

    This function cannot fetch or authenticate a provider. MCP input must
    never be passed as the snapshot. A strictly inventory-backed reduction
    may reduce an already breached entry limit, but does not waive identity,
    freshness, finite data or complete projection checks.
    """
    checks, projections = [], {}

    def check(code, passed, detail):
        checks.append({"code": code, "passed": bool(passed), "detail": detail})

    def result(reducing=False):
        return {"passed": all(row["passed"] for row in checks), "checks": checks,
                "risk_reducing": reducing, "projections": json_value(projections),
                "policy_hash": policy.fingerprint(), "basis": "trusted fresh long-only portfolio snapshot"}

    if not isinstance(snapshot, dict):
        check("portfolio_snapshot", False, "trusted portfolio snapshot is unavailable")
        return result()
    timestamp = snapshot.get("observed_at")
    check("portfolio_identity", isinstance(expected_account_fingerprint, str) and bool(expected_account_fingerprint)
          and snapshot.get("account_fingerprint") == expected_account_fingerprint, "exact account binding")
    check("portfolio_freshness", type(now) is int and now >= 0 and type(timestamp) is int
          and 0 <= now - timestamp <= policy.max_snapshot_age_seconds, "fresh portfolio source timestamp")
    check("portfolio_order_contract", side in {"buy", "sell"} and asset_class in {"equity", "crypto"}
          and isinstance(symbol, str) and bool(symbol) and isinstance(strategy_id, str) and bool(strategy_id),
          "bounded long-only instrument and strategy identity")
    try:
        equity = decimal(snapshot.get("equity"), positive=True)
        mark = decimal(snapshot.get("order_mark_price"), positive=True)
        qty, price = decimal(quantity, positive=True), decimal(limit_price, positive=True)
        fees = decimal(estimated_fees, nonnegative=True)
        raw_positions = snapshot.get("positions")
        if not isinstance(raw_positions, (list, tuple)):
            raise ValueError("complete positions required")
        inventory, symbol_value, classes, quantities = Decimal("0"), Decimal("0"), {}, {}
        for position in raw_positions:
            name, kind = position["symbol"], position["asset_class"]
            if not isinstance(name, str) or not name or kind not in {"equity", "crypto"}:
                raise ValueError("invalid position identity")
            held, value = decimal(position["quantity"], nonnegative=True), decimal(position["market_value"], nonnegative=True)
            if (held == 0) != (value == 0):
                raise ValueError("position quantity/value disagree")
            classes[kind] = classes.get(kind, Decimal("0")) + value
            quantities[name] = quantities.get(name, Decimal("0")) + held
            if name == symbol:
                if kind != asset_class:
                    raise ValueError("symbol asset class mismatch")
                inventory += held
                symbol_value += value
        gross = sum(classes.values(), Decimal("0"))
        removed = symbol_value * qty / inventory if side == "sell" and inventory else Decimal("0")
        reducing = side == "sell" and 0 < qty <= inventory and removed > 0
        check("portfolio_inventory", side != "sell" or reducing, "sell must strictly reduce verified long inventory")
        added = qty * mark if side == "buy" else -removed
        # Positive limit-price gains never create extra risk budget.
        execution_loss = max(Decimal("0"), qty * price - added) if side == "buy" else max(Decimal("0"), removed - qty * price)
        post_equity = equity - fees - execution_loss
        post_gross = gross + added
        if post_equity <= 0 or post_gross < 0:
            raise ValueError("invalid projected portfolio")
        post_classes = dict(classes)
        post_classes[asset_class] = post_classes.get(asset_class, Decimal("0")) + added
        post_quantities = dict(quantities)
        post_quantities[symbol] = post_quantities.get(symbol, Decimal("0")) + (qty if side == "buy" else -qty)
        projections.update(equity=post_equity, gross_exposure=post_gross, net_exposure=post_gross,
                           gross_leverage=post_gross / post_equity, net_leverage=post_gross / post_equity,
                           position_count=sum(value > 0 for value in post_quantities.values()),
                           asset_class_weights={key: value / post_equity for key, value in post_classes.items()})
        for key in ("gross_exposure", "net_exposure"):
            if key in snapshot:
                check("portfolio_" + key + "_consistent", decimal(snapshot[key], nonnegative=True) == gross,
                      "long-only position totals must agree with supplied aggregates")
        for key in ("global_enabled", "broker_enabled", "account_enabled", "data_quality_passed"):
            value = snapshot.get(key)
            check("portfolio_" + key, value is True,
                  "explicit authority and valid data are required for every order, including exits")
        if policy.require_health:
            health = snapshot.get("strategy_health", {}).get(strategy_id) if isinstance(snapshot.get("strategy_health"), dict) else None
            check("portfolio_strategy_health", health in {"HEALTHY", "WATCH", "DEGRADED", "DISABLED"}
                  and (health in policy.allowed_strategy_health or reducing), "known strategy health; no automatic reactivation")
            enabled = snapshot.get("symbol_enabled", {}).get(symbol) if isinstance(snapshot.get("symbol_enabled"), dict) else None
            check("portfolio_symbol_enabled", enabled is True, "explicit symbol permission is required")
        for name, measure in (("max_gross_leverage", post_gross / post_equity), ("max_net_leverage", post_gross / post_equity)):
            cap = getattr(policy, name)
            if cap is not None:
                check("portfolio_" + name, measure <= cap or reducing, "post-trade limit; an existing breach can be reduced")
        for name, cap in policy.max_asset_class_weights.items():
            check("portfolio_asset_class_" + name, post_classes.get(name, Decimal("0")) / post_equity <= cap or reducing,
                  "complete position classification; bounded reductions may exit an existing breach")
        if policy.max_positions is not None:
            check("portfolio_position_count", projections["position_count"] <= policy.max_positions or reducing, "maximum distinct held symbols")
        if policy.max_correlated_weight is not None:
            buckets = snapshot.get("correlated_exposure_by_symbol")
            if not isinstance(buckets, dict) or symbol not in buckets:
                raise ValueError("correlated group exposure unavailable")
            correlated = decimal(buckets[symbol], nonnegative=True)
            if not symbol_value <= correlated <= gross:
                raise ValueError("correlated group exposure inconsistent")
            post_correlated = correlated + added
            projections["correlated_weight"] = post_correlated / post_equity
            check("portfolio_correlated_exposure", post_correlated / post_equity <= policy.max_correlated_weight or reducing,
                  "trusted predefined group; no caller-inferred correlation")
        if policy.max_drawdown is not None:
            drawdown = decimal(snapshot.get("drawdown"), nonnegative=True)
            if drawdown > 1:
                raise ValueError("drawdown invalid")
            projected_dd = 1 - post_equity / equity * (1 - drawdown)
            projections["drawdown"] = projected_dd
            check("portfolio_drawdown", projected_dd < policy.max_drawdown or reducing, "known peak-based drawdown; exits remain possible")
        if policy.max_turnover is not None:
            turnover = decimal(snapshot.get("turnover"), nonnegative=True)
            denominator = decimal(snapshot.get("turnover_equity_basis"), positive=True)
            projected_turnover = turnover + qty * price / denominator
            projections["turnover"] = projected_turnover
            check("portfolio_turnover", projected_turnover <= policy.max_turnover or reducing, "gross notional over a trusted fixed equity basis")
        if policy.max_portfolio_volatility is not None:
            volatility = decimal(snapshot.get("projected_portfolio_volatility"), nonnegative=True)
            check("portfolio_volatility_identity", snapshot.get("volatility_basis") == policy.volatility_basis
                  and snapshot.get("order_fingerprint") == portfolio_order_fingerprint(symbol, asset_class, side, qty, price, fees),
                  "vetted volatility projection must bind this exact order and declared return horizon")
            projections["portfolio_volatility"] = volatility
            check("portfolio_volatility", volatility <= policy.max_portfolio_volatility or reducing,
                  "no volatility or covariance estimate is invented by this gate")
    except (ValueError, TypeError, KeyError, ArithmeticError):
        check("portfolio_complete_finite_data", False, "missing or inconsistent portfolio inputs block configured limits")
        return result()
    return result(reducing)
