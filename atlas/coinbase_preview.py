"""Fee-scenario arithmetic for a $100 Coinbase simulation; cannot place orders."""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import time

from atlas.brokers.coinbase_public import (CoinbasePublicError, MAX_AGE_SECONDS, PRODUCT_ID,
                                          amount, clock_value, validate_book, validate_product)
from atlas.domain import financial_context

ALLOCATION_USD = Decimal("100")
MAX_SPREAD_BPS = Decimal("100")
CONFIG_KEYS = {"schema", "product_id", "allocation_usd", "order_budget_usd", "maker_fee_bps", "taker_fee_bps"}


def validate_config(config):
    if (not isinstance(config, dict) or set(config) != CONFIG_KEYS
            or type(config["schema"]) is not int or config["schema"] != 1 or config["product_id"] != PRODUCT_ID):
        raise CoinbasePublicError("coinbase_preview_config_invalid")
    allocation, budget = amount(config["allocation_usd"]), amount(config["order_budget_usd"])
    maker, taker = amount(config["maker_fee_bps"], positive=False), amount(config["taker_fee_bps"], positive=False)
    if allocation != ALLOCATION_USD or budget > ALLOCATION_USD:
        raise CoinbasePublicError("coinbase_preview_100_usd_cap_required")
    if maker > 1000 or taker > 1000:
        raise CoinbasePublicError("coinbase_preview_fee_scenario_out_of_range")
    return {"schema": 1, "product_id": PRODUCT_ID, "allocation_usd": "100",
            "order_budget_usd": format(budget, "f"), "maker_fee_bps": format(maker, "f"),
            "taker_fee_bps": format(taker, "f")}


def _floor(value, quantum):
    return (value / quantum).to_integral_value(rounding=ROUND_FLOOR) * quantum


def _ceil(value, quantum):
    return (value / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum


def _metric(value):
    """Readable derived metrics; calculations and order grids remain exact."""
    return format(value.quantize(Decimal("0.000000000001")), "f")


def _cost(quantity, levels):
    remaining, notional, used = quantity, Decimal(0), []
    for price, size in levels:
        take = min(remaining, size)
        if take > 0:
            notional += take * price
            used.append({"price": format(price, "f"), "quantity": format(take, "f")})
            remaining -= take
        if remaining == 0:
            return notional, used
    raise CoinbasePublicError("coinbase_preview_insufficient_depth")


def _candidate(*, product, levels, budget, entry_bps, exit_bps, best_bid, best_ask, mode):
    base_step, quote_step, price_step = (amount(product[name]) for name in ("base_increment", "quote_increment", "price_increment"))
    max_base, max_quote = amount(product["base_max_size"]), amount(product["quote_max_size"])
    depth = sum(size for _, size in levels)
    high = int(_floor(min(max_base, depth, budget / levels[0][0]), base_step) / base_step)
    low = 0
    rate = entry_bps / 10000
    # Binary search on integer base increments finds the largest affordable
    # quantity, including upward quote-increment rounding of the fee reserve.
    while low < high:
        units = (low + high + 1) // 2
        _, used = _cost(units * base_step, levels)
        limit_notional = units * base_step * amount(used[-1]["price"])
        limit_all_in = limit_notional + _ceil(limit_notional * rate, quote_step)
        if limit_all_in <= budget and limit_notional <= max_quote:
            low = units
        else:
            high = units - 1
    quantity = low * base_step
    if quantity == 0:
        return {"available": False, "reason": "budget_below_one_base_increment"}
    notional, used = _cost(quantity, levels)
    if quantity < amount(product["base_min_size"]) or notional < amount(product["quote_min_size"]):
        return {"available": False, "reason": "budget_below_product_minimum"}
    fee = _ceil(notional * rate, quote_step)
    all_in = notional + fee
    limit_notional = quantity * amount(used[-1]["price"])
    limit_fee = _ceil(limit_notional * rate, quote_step)
    limit_all_in = limit_notional + limit_fee
    if not 0 < all_in <= limit_all_in <= budget <= ALLOCATION_USD:
        raise CoinbasePublicError("coinbase_preview_budget_invariant_failed")
    average = notional / quantity
    # This exit-price threshold includes an extra quote increment when a fee
    # applies, covering the possible upward rounding of a future exit fee.
    exit_rate = exit_bps / 10000
    exit_cushion = quote_step if exit_rate > 0 else Decimal(0)
    break_even = _ceil((limit_all_in + exit_cushion) / (quantity * (1 - exit_rate)), price_step)
    exit_notional = quantity * break_even
    exit_fee = _ceil(exit_notional * exit_rate, quote_step)
    if exit_notional - exit_fee < limit_all_in:
        raise CoinbasePublicError("coinbase_preview_break_even_invariant_failed")
    immediate_exit_notional = quantity * best_bid
    immediate_exit_fee = _ceil(immediate_exit_notional * exit_rate, quote_step)
    result = {
        "available": True, "mode": mode, "side": "buy", "product_id": PRODUCT_ID,
        "quantity_btc": format(quantity, "f"), "entry_notional_usd": format(notional, "f"),
        "entry_vwap_usd": _metric(average), "entry_fee_reserve_usd": format(fee, "f"),
        "entry_all_in_usd": format(all_in, "f"),
        "limit_worst_case_notional_usd": format(limit_notional, "f"),
        "limit_worst_case_fee_reserve_usd": format(limit_fee, "f"),
        "limit_worst_case_all_in_usd": format(limit_all_in, "f"),
        "unused_order_budget_usd": format(budget - limit_all_in, "f"),
        "unused_simulated_allocation_usd": format(ALLOCATION_USD - limit_all_in, "f"),
        "entry_fee_scenario_bps": format(entry_bps, "f"), "exit_taker_fee_scenario_bps": format(exit_bps, "f"),
        "conservative_exit_break_even_price_usd": format(break_even, "f"),
        "break_even_entry_basis": "worst_case_full_fill_at_entry_limit_with_fee_reserve",
        "break_even_rise_from_current_bid_bps": _metric((break_even / best_bid - 1) * 10000),
        "hypothetical_loss_exiting_at_current_bid_usd": format(all_in - immediate_exit_notional + immediate_exit_fee, "f"),
        "exit_depth_verified": False, "exit_fee_rounding_cushion_usd": format(exit_cushion, "f"),
        "fill_probability": None, "fill_verified": False, "order_submitted": False,
    }
    if mode == "maker_post_only_limit_scenario":
        result.update({"limit_price_usd": format(levels[0][0], "f"), "post_only": True,
                       "execution_assumption": "eventual_full_fill_at_passive_limit_unknown"})
    else:
        result.update({"depth_levels_used": used,
                       "marketable_limit_price_usd": used[-1]["price"],
                       "slippage_from_best_ask_bps": _metric((average / best_ask - 1) * 10000),
                       "execution_assumption": "displayed_ask_depth_remains_available_unknown"})
    return result


@financial_context
def build_preview(snapshot, config, *, now=None):
    config = validate_config(config)
    now = clock_value(time.time() if now is None else now)
    if not isinstance(snapshot, dict) or snapshot.get("public_only") is not True:
        raise CoinbasePublicError("coinbase_preview_snapshot_invalid")
    observed = amount(snapshot.get("observed_at"))
    product_observed = amount(snapshot.get("product_observed_at"))
    if not product_observed <= observed <= now or now - product_observed > MAX_AGE_SECONDS:
        raise CoinbasePublicError("coinbase_preview_snapshot_stale_or_future")
    if snapshot.get("transport_provenance") not in {"verified_tls", "injected_test_transport"}:
        raise CoinbasePublicError("coinbase_preview_provenance_unknown")
    product = validate_product(snapshot.get("product"))
    book = validate_book({"pricebook": snapshot.get("pricebook")}, now=now)
    price_step, base_step = amount(product["price_increment"]), amount(product["base_increment"])
    for side in ("bids", "asks"):
        for level in book[side]:
            if amount(level["price"]) % price_step or amount(level["size"]) % base_step:
                raise CoinbasePublicError("coinbase_preview_book_increment_invalid")
    best_bid, best_ask = amount(book["bids"][0]["price"]), amount(book["asks"][0]["price"])
    spread = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 10000
    if spread > MAX_SPREAD_BPS:
        raise CoinbasePublicError("coinbase_preview_spread_exceeds_100_bps")
    maker_bps, taker_bps, budget = (amount(config[name], positive=False) for name in
                                   ("maker_fee_bps", "taker_fee_bps", "order_budget_usd"))
    common = {"product": product, "budget": budget, "exit_bps": taker_bps,
              "best_bid": best_bid, "best_ask": best_ask}
    maker = _candidate(**common, levels=[(best_bid, amount(product["base_max_size"]))],
                       entry_bps=maker_bps, mode="maker_post_only_limit_scenario")
    if product["post_only"]:
        taker = {"available": False, "reason": "product_post_only"}
    else:
        taker = _candidate(**common, levels=[(amount(row["price"]), amount(row["size"])) for row in book["asks"]],
                           entry_bps=taker_bps, mode="taker_displayed_depth_scenario")
    comparison = None
    if maker["available"] and taker["available"]:
        maker_unit = amount(maker["entry_all_in_usd"]) / amount(maker["quantity_btc"])
        taker_unit = amount(taker["entry_all_in_usd"]) / amount(taker["quantity_btc"])
        comparison = {"maker_entry_all_in_per_btc_usd": _metric(maker_unit),
                      "taker_entry_all_in_per_btc_usd": _metric(taker_unit),
                      "maker_cost_advantage_bps": _metric((taker_unit / maker_unit - 1) * 10000),
                      "maker_fill_probability_unknown": True,
                      "candidates_are_alternatives_not_combined_allocations": True}
    return {"schema": 1, "mode": "research_dry_run", "broker": "coinbase_advanced_public",
            "config": config, "generated_at": format(now, "f"), "market_snapshot": snapshot,
            "fee_basis": "explicit_input_scenario_not_api_verified_account_tier", "actual_account_fee_tier": None,
            "fee_reserve_rounding": "up_to_quote_increment", "spread_bps": _metric(spread),
            "maker_candidate": maker, "taker_candidate": taker, "comparison": comparison,
            "allocation_is_simulated_not_reserved_funds": True, "account_connected": False,
            "account_permissions_verified": False, "execution_enabled": False, "automatic_trading_enabled": False,
            "orders_submitted": 0, "account_mutations": 0,
            "limitations": ["Public product flags do not verify account-specific trading eligibility.",
                            "A public book is a point-in-time observation; no future fill is established.",
                            "Fee inputs are scenarios; actual fees and tax effects are unknown.",
                            "Exit depth and market movement are not modeled; break-even is not a forecast.",
                            "Each candidate uses at most the same $100 simulated allocation; they are alternatives.",
                            "This cost comparison does not validate a trading strategy or profitability."]}
