"""Offline cost arithmetic, never an alpha forecast or an order authorization.

Execution prices are modeled relative to the midpoint at each endpoint. Spread
inputs are EACH SIDE's premium/markdown, not a full quoted spread. Spread and
additional adverse slippage add on that side; fees apply to its execution
notional. This convention must not include the same execution cost twice.
This is a separate scenario, not a reproduction of ReplayEngine execution:
ReplayEngine's full-spread input and bid/ask-relative compounded slippage cannot
be passed directly as this module's per-side midpoint-additive inputs.

Monthly overhead is constant for the supplied scenario and accrues linearly
over the actual seconds in each UTC calendar month. The interval is [start,end),
not a count of exchange sessions or an assumed 30-day month. This is economic
attribution, not an invoice, a cash debit, or an instruction to keep trading.

Every cost must be explicitly supplied, including zero. None or an omitted
component stays unavailable. Inputs use Decimal/string/integer, never binary
floats. Arithmetic uses the isolated financial context (256 digits); outputs
have up to 36 decimal places. Required price hurdles and overhead round upward;
descriptive returns round half-even. No broker tick/fee rounding is inferred.

The PnL helper accepts an already-net replay PnL and subtracts ONLY overhead.
The caller must attest completeness and inclusion of execution costs, and must
use that replay's exact period. Optional replay-period metadata is checked for
equality; the helper cannot authenticate a caller's missing source record.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_EVEN

from atlas.domain import decimal, financial_context


OVERHEAD_CATEGORIES = ("tool", "data", "model", "server")
EXECUTION_COSTS = (
    "buy_fee_bps", "sell_fee_bps", "entry_spread_bps", "exit_spread_bps",
    "entry_slippage_bps", "exit_slippage_bps",
)
MAX_MONTHLY_COMPONENT_USD = Decimal("1000000")
MAX_PERIOD_SECONDS = 366 * 10 * 86400
MAX_TIMESTAMP = 4102444800  # 2100-01-01 UTC; bounded datetime/calendar work.
_ONE = Decimal(1)
_BPS = Decimal(10000)
_QUANTUM = Decimal("1e-36")


class EconomicsError(ValueError):
    """Invalid scenario inputs; missing costs instead return unavailable."""


def _amount(value, name, *, low=None, high=None, high_exclusive=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise EconomicsError(f"{name}: decimal string, integer or Decimal required")
    try:
        result = decimal(value)
    except ValueError as exc:
        raise EconomicsError(f"{name}: invalid finite bounded decimal") from exc
    if low is not None and result < low:
        raise EconomicsError(f"{name}: below permitted range")
    if high is not None and (result >= high if high_exclusive else result > high):
        raise EconomicsError(f"{name}: above permitted range")
    return result


def _capital(value):
    result = _amount(value, "capital_usd", low=0, high=100)
    if result is None or result == 0:
        raise EconomicsError("capital_usd: explicit positive amount at most 100 required")
    return result


def _render(value, *, upward=False):
    if value is None:
        return None
    rounded = value.quantize(_QUANTUM, rounding=ROUND_CEILING if upward else ROUND_HALF_EVEN)
    if not rounded:
        return "0"
    return format(rounded, "f").rstrip("0").rstrip(".")


def _base(kind):
    return {
        "schema_version": 1, "kind": kind, "available": True,
        "status": "available", "unavailable_reasons": [], "unknown_inputs": [],
        "evidence_class": "OFFLINE_COST_SCENARIO", "alpha_forecast": False,
        "profitability_established": False, "live_approved": False,
        "order_authority": False, "allocation_authority": False,
        "arithmetic": "Decimal precision 256; outputs at 36 decimal places",
    }


def _unavailable(result, reasons, unknown=()):
    result.update(available=False, status="unavailable", unavailable_reasons=list(reasons),
                  unknown_inputs=list(unknown))
    return result


@financial_context
def execution_break_even(*, buy_fee_bps=None, sell_fee_bps=None,
                         entry_spread_bps=None, exit_spread_bps=None,
                         entry_slippage_bps=None, exit_slippage_bps=None,
                         allocation_usd="100"):
    """Return a midpoint PRICE hurdle and separate allocated-budget loss.

    Each bps input must be >=0 and <10000; each side's spread+slippage must
    also be <10000. Allocation is a hypothetical all-in buy budget in (0,100].
    The execution loss return divides by this budget, not an account balance.
    Fee-only hurdle=(1+buy_fee)/(1-sell_fee)-1. With per-side price adjustments
    e and x, midpoint hurdle=(1+e)(1+buy_fee)/((1-x)(1-sell_fee))-1.
    """
    supplied = dict(zip(EXECUTION_COSTS, (
        buy_fee_bps, sell_fee_bps, entry_spread_bps, exit_spread_bps,
        entry_slippage_bps, exit_slippage_bps,
    )))
    costs = {name: _amount(value, name, low=0, high=10000, high_exclusive=True)
             for name, value in supplied.items()}
    capital = _capital(allocation_usd)
    for side in ("entry", "exit"):
        pair = (costs[f"{side}_spread_bps"], costs[f"{side}_slippage_bps"])
        if all(v is not None for v in pair) and sum(pair) >= 10000:
            raise EconomicsError(f"{side}: spread plus slippage must be below 10000 bps")
    result = _base("execution_break_even")
    result.update(
        inputs_bps={k: _render(v) for k, v in costs.items()}, allocation_usd=_render(capital),
        cost_units="basis points; each spread is a per-side midpoint premium/markdown",
        return_units="fractions, not percentages; price and allocated-budget returns are separate",
        loss_return_denominator="allocation_usd; not whole-account capital",
        fee_only_price_gain_fraction=None, midpoint_price_gain_fraction=None,
        entry_execution_notional_usd=None, entry_fee_usd=None,
        flat_midpoint_sale_notional_usd=None, flat_midpoint_exit_fee_usd=None,
        flat_midpoint_ending_cash_usd=None, flat_midpoint_net_pnl_usd=None,
        flat_midpoint_allocated_budget_return_fraction=None,
        assumptions=[
            "Full hypothetical entry and exit; maker execution is not implied.",
            "Fees in quote USD on execution notional; no external cash flows.",
            "Spread and additional slippage add on each side and are each included once.",
            "Not ReplayEngine venue-profile units: no direct full-spread/compounded-slippage mapping.",
            "Continuous quantities; broker minimums, increments and fee rounding unmodeled.",
            "No tax, extra commission, fixed overhead or funding cost included here.",
            "Required gain is arithmetic, not a forecast or evidence a target will be reached.",
            "Price hurdle rounds upward; descriptive allocated-budget figures round half-even.",
        ],
    )
    unknown = [name for name, value in costs.items() if value is None]
    if unknown:
        return _unavailable(result, ["execution_costs_unknown"], unknown)
    fb, fs = costs["buy_fee_bps"] / _BPS, costs["sell_fee_bps"] / _BPS
    entry = (costs["entry_spread_bps"] + costs["entry_slippage_bps"]) / _BPS
    exit_ = (costs["exit_spread_bps"] + costs["exit_slippage_bps"]) / _BPS
    entry_notional = capital / (_ONE + fb)
    flat_sale = entry_notional * (_ONE - exit_) / (_ONE + entry)
    ending = flat_sale * (_ONE - fs)
    result.update(
        fee_only_price_gain_fraction=_render((_ONE + fb) / (_ONE - fs) - _ONE, upward=True),
        midpoint_price_gain_fraction=_render(
            (_ONE + entry) * (_ONE + fb) / ((_ONE - exit_) * (_ONE - fs)) - _ONE,
            upward=True),
        entry_execution_notional_usd=_render(entry_notional),
        entry_fee_usd=_render(entry_notional * fb),
        flat_midpoint_sale_notional_usd=_render(flat_sale),
        flat_midpoint_exit_fee_usd=_render(flat_sale * fs),
        flat_midpoint_ending_cash_usd=_render(ending),
        flat_midpoint_net_pnl_usd=_render(ending - capital),
        flat_midpoint_allocated_budget_return_fraction=_render(ending / capital - _ONE),
    )
    return result


def _time(value, name):
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP:
        raise EconomicsError(f"{name}: bounded integer UTC Unix seconds required")
    return value


def _period(start, end):
    start, end = _time(start, "period_start"), _time(end, "period_end")
    if not 0 < end - start <= MAX_PERIOD_SECONDS:
        raise EconomicsError("period: positive duration of at most ten leap-year lengths required")
    segments = []
    cursor = start
    while cursor < end:
        date = datetime.fromtimestamp(cursor, timezone.utc)
        month_start = datetime(date.year, date.month, 1, tzinfo=timezone.utc)
        next_month = datetime(date.year + (date.month == 12), date.month % 12 + 1,
                              1, tzinfo=timezone.utc)
        boundary = int(next_month.timestamp())
        stop = min(end, boundary)
        segments.append({"month": date.strftime("%Y-%m"), "start": cursor, "end": stop,
                         "covered_seconds": stop - cursor,
                         "calendar_month_seconds": boundary - int(month_start.timestamp())})
        cursor = stop
    return start, end, segments


def _overhead(monthly_costs_usd, period_start, period_end, initial_capital_usd):
    capital = _capital(initial_capital_usd)
    start, end, segments = _period(period_start, period_end)
    if monthly_costs_usd is None:
        monthly_costs_usd = {}
    if not isinstance(monthly_costs_usd, dict) or set(monthly_costs_usd) - set(OVERHEAD_CATEGORIES):
        raise EconomicsError("monthly_costs_usd: only tool, data, model, server keys allowed")
    costs = {name: _amount(monthly_costs_usd.get(name), name, low=0,
                           high=MAX_MONTHLY_COMPONENT_USD) for name in OVERHEAD_CATEGORIES}
    result = _base("fixed_overhead_hurdle")
    result.update(
        initial_capital_usd=_render(capital), monthly_costs_usd={k: _render(v) for k, v in costs.items()},
        monthly_total_usd=None, prorated_overhead_usd=None,
        required_account_pnl_usd=None, required_account_return_fraction=None,
        period_start=start, period_end=end, elapsed_seconds=end - start,
        proration_segments=segments,
        period_basis="half-open [start,end), actual seconds per UTC calendar month",
        hurdle_basis="additional account PnL needed to pay fixed overhead, after execution costs",
        overhead_cash_debit_performed=False,
        assumptions=[
            "Each monthly component is constant for this entire explicitly selected scenario.",
            "Linear calendar proration is economic attribution, not actual subscription billing.",
            "All costs, including zero, must be supplied; missing does not mean free.",
            "Account return divides by initial capital; it is not the traded asset's price return.",
            "Overhead and required account return round upward to 36 decimal places.",
        ],
    )
    unknown = [name for name, value in costs.items() if value is None]
    if unknown:
        return _unavailable(result, ["fixed_overhead_unknown"], unknown), None, capital
    total = sum(costs.values(), Decimal(0))
    overhead = sum((total * Decimal(s["covered_seconds"]) / s["calendar_month_seconds"]
                    for s in segments), Decimal(0))
    result.update(monthly_total_usd=_render(total),
                  prorated_overhead_usd=_render(overhead, upward=True),
                  required_account_pnl_usd=_render(overhead, upward=True),
                  required_account_return_fraction=_render(overhead / capital, upward=True))
    return result, overhead, capital


@financial_context
def fixed_overhead_hurdle(monthly_costs_usd, *, period_start, period_end,
                          initial_capital_usd="100"):
    """Fixed tool/data/model/server expense as an additional account-cash hurdle."""
    return _overhead(monthly_costs_usd, period_start, period_end, initial_capital_usd)[0]


@financial_context
def overhead_adjusted_pnl(net_replay_pnl_usd, *, monthly_costs_usd,
                          period_start, period_end, initial_capital_usd="100",
                          replay_complete=None, execution_costs_already_included=None,
                          replay_period_start=None, replay_period_end=None):
    """Subtract only fixed overhead from already-net PnL over the SAME interval.

    Completeness and inclusion of execution costs are explicit caller attestations,
    not inferred from a number. False/None preserves unavailable adjusted results.
    Supplied replay period metadata must contain both endpoints and match exactly.
    Negative replay PnL is valid down to -initial capital; operating overhead may
    take the economic result below that amount without debiting a replay account.
    """
    for name, value in (("replay_complete", replay_complete),
                        ("execution_costs_already_included", execution_costs_already_included)):
        if value is not None and type(value) is not bool:
            raise EconomicsError(f"{name}: explicit boolean or None required")
    overhead_result, overhead, capital = _overhead(
        monthly_costs_usd, period_start, period_end, initial_capital_usd)
    if (replay_period_start is None) != (replay_period_end is None):
        raise EconomicsError("replay period: both endpoints required")
    if replay_period_start is not None:
        rs, re, _ = _period(replay_period_start, replay_period_end)
        if (rs, re) != (period_start, period_end):
            raise EconomicsError("replay period differs from overhead attribution period")
    pnl = _amount(net_replay_pnl_usd, "net_replay_pnl_usd", low=-capital,
                  high=Decimal("1000000000000"))
    result = _base("overhead_adjusted_pnl")
    result.update(
        initial_capital_usd=_render(capital), net_replay_pnl_usd=_render(pnl),
        replay_complete=replay_complete,
        execution_costs_already_included=execution_costs_already_included,
        replay_period_metadata_checked=replay_period_start is not None,
        period_start=period_start, period_end=period_end, fixed_overhead=overhead_result,
        adjusted_net_pnl_usd=None, adjusted_account_return_fraction=None,
        execution_costs_subtracted_again_usd="0", overhead_cash_debit_performed=False,
        assumptions=[
            "Input PnL already includes all replay execution fees, spreads and slippage.",
            "Only prorated fixed overhead is subtracted; no existing replay field is modified.",
            "Caller attests a complete PnL and identical replay/overhead period.",
            "Economic PnL preserves the input valuation convention; open inventory is not realized.",
            "Adjusted PnL is not a withdrawable cash balance, forecast or allocation authority.",
        ],
    )
    reasons, unknown = list(overhead_result["unavailable_reasons"]), list(overhead_result["unknown_inputs"])
    if pnl is None:
        reasons.append("net_replay_pnl_unknown")
        unknown.append("net_replay_pnl_usd")
    if replay_complete is not True:
        reasons.append("replay_incomplete" if replay_complete is False else "replay_completeness_unknown")
    if execution_costs_already_included is not True:
        reasons.append("execution_costs_not_included" if execution_costs_already_included is False
                       else "execution_cost_inclusion_unknown")
    if reasons:
        return _unavailable(result, reasons, unknown)
    result.update(adjusted_net_pnl_usd=_render(pnl - overhead),
                  adjusted_account_return_fraction=_render((pnl - overhead) / capital))
    return result
