"""Exact offline cost hurdles; invented scenarios are not return forecasts."""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
import json

import pytest

from atlas.research.economics import (
    EconomicsError, execution_break_even, fixed_overhead_hurdle, overhead_adjusted_pnl,
)


D = Decimal
ZERO_EXECUTION = {name: "0" for name in (
    "buy_fee_bps", "sell_fee_bps", "entry_spread_bps", "exit_spread_bps",
    "entry_slippage_bps", "exit_slippage_bps",
)}
ZERO_OVERHEAD = {name: "0" for name in ("tool", "data", "model", "server")}


def ts(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp())


PERIOD = {"period_start": ts("2026-01-01"), "period_end": ts("2026-02-01")}


def economics(**changes):
    return execution_break_even(**{**ZERO_EXECUTION, **changes})


def adjusted(pnl="5", **changes):
    return overhead_adjusted_pnl(pnl, **{
        "monthly_costs_usd": {**ZERO_OVERHEAD, "tool": "1"}, **PERIOD,
        "replay_complete": True, "execution_costs_already_included": True, **changes,
    })


def test_zero_execution_costs_are_explicit_and_return_no_gain_or_loss():
    r = economics()
    assert r["available"] and r["midpoint_price_gain_fraction"] == "0"
    assert r["entry_execution_notional_usd"] == r["flat_midpoint_ending_cash_usd"] == "100"
    assert r["flat_midpoint_net_pnl_usd"] == r["flat_midpoint_allocated_budget_return_fraction"] == "0"
    assert not r["alpha_forecast"] and not r["live_approved"] and not r["order_authority"]
    json.dumps(r, allow_nan=False)


def test_two_taker_fees_use_sell_denominator_and_all_in_buy_budget():
    r = economics(buy_fee_bps="120", sell_fee_bps="120")
    assert D(r["midpoint_price_gain_fraction"]) == D("0.024291497975708502024291497975708503")
    assert D(r["entry_execution_notional_usd"]) < 100
    assert D(r["entry_execution_notional_usd"]) + D(r["entry_fee_usd"]) == 100
    assert D(r["flat_midpoint_net_pnl_usd"]) == D("-2.371541501976284584980237154150197628")
    assert D(r["flat_midpoint_allocated_budget_return_fraction"]) != -D(r["midpoint_price_gain_fraction"])


def test_high_sell_fee_cannot_be_approximated_as_fee_sum():
    assert economics(sell_fee_bps="9000")["midpoint_price_gain_fraction"] == "9"
    assert economics(buy_fee_bps="1000")["midpoint_price_gain_fraction"] == "0.1"
    r = economics(buy_fee_bps="1000", sell_fee_bps="9000")
    assert r["midpoint_price_gain_fraction"] == "10"
    assert D(r["flat_midpoint_ending_cash_usd"]) < 10


def test_per_side_spread_and_additional_slippage_are_included_once():
    r = economics(buy_fee_bps="120", sell_fee_bps="120", entry_spread_bps="5",
                  exit_spread_bps="5", entry_slippage_bps="5", exit_slippage_bps="5")
    assert D(r["midpoint_price_gain_fraction"]) == D("0.026342131605289500026342131605289501")
    assert r["fee_only_price_gain_fraction"] == economics(buy_fee_bps="120", sell_fee_bps="120")["midpoint_price_gain_fraction"]
    # Entry-only adjustment is paid by receiving less inventory, not charged a second time as a fee.
    r = economics(entry_spread_bps="1000")
    assert r["midpoint_price_gain_fraction"] == "0.1"
    assert r["entry_fee_usd"] == r["flat_midpoint_exit_fee_usd"] == "0"
    assert D(r["flat_midpoint_ending_cash_usd"]) == D("90.909090909090909090909090909090909091")


def test_allocation_changes_dollars_but_not_price_hurdle():
    a = economics(buy_fee_bps="120", sell_fee_bps="120")
    b = economics(buy_fee_bps="120", sell_fee_bps="120", allocation_usd="10")
    assert a["midpoint_price_gain_fraction"] == b["midpoint_price_gain_fraction"]
    assert a["flat_midpoint_allocated_budget_return_fraction"] == b["flat_midpoint_allocated_budget_return_fraction"]
    assert "flat_midpoint_account_return_fraction" not in b
    assert b["loss_return_denominator"] == "allocation_usd; not whole-account capital"
    with localcontext() as ctx:
        ctx.prec = 100
        assert abs(D(a["flat_midpoint_net_pnl_usd"]) - 10 * D(b["flat_midpoint_net_pnl_usd"])) < D("1e-34")
        # A $10 allocation within a $100 account incurs one tenth the budget return.
        assert abs(D(b["flat_midpoint_net_pnl_usd"]) / 100
                   - D(b["flat_midpoint_allocated_budget_return_fraction"]) / 10) < D("1e-36")


def test_missing_execution_cost_is_unavailable_and_does_not_mask_invalid_other_input():
    r = execution_break_even(buy_fee_bps="120", sell_fee_bps="120")
    assert not r["available"] and "entry_spread_bps" in r["unknown_inputs"]
    assert r["midpoint_price_gain_fraction"] is r["fee_only_price_gain_fraction"] is None
    assert r["flat_midpoint_ending_cash_usd"] is None
    with pytest.raises(EconomicsError):
        execution_break_even(buy_fee_bps="NaN")


@pytest.mark.parametrize("field", list(ZERO_EXECUTION))
@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "-Infinity", "10000", True, .1, "1e100"])
def test_execution_rejects_invalid_nonfinite_or_unbounded_inputs(field, value):
    with pytest.raises(EconomicsError):
        economics(**{field: value})


@pytest.mark.parametrize("side", ["entry", "exit"])
def test_combined_side_price_adjustment_must_stay_below_one(side):
    with pytest.raises(EconomicsError):
        economics(**{f"{side}_spread_bps": "6000", f"{side}_slippage_bps": "4000"})


@pytest.mark.parametrize("capital", [None, "0", "-1", "100.01", "NaN", True, 100.0])
def test_fixed_hypothetical_capital_is_positive_and_at_most_100(capital):
    with pytest.raises(EconomicsError):
        economics(allocation_usd=capital)


def test_one_dollar_monthly_cost_requires_one_percent_account_return_not_price_forecast():
    r = fixed_overhead_hurdle({**ZERO_OVERHEAD, "tool": "1"}, **PERIOD)
    assert r["monthly_total_usd"] == r["prorated_overhead_usd"] == r["required_account_pnl_usd"] == "1"
    assert r["required_account_return_fraction"] == "0.01"
    assert not r["overhead_cash_debit_performed"] and not r["alpha_forecast"]


def test_all_four_fixed_components_and_zero_costs():
    r = fixed_overhead_hurdle(dict(tool="1", data="2", model="3", server="4"), **PERIOD)
    assert r["monthly_total_usd"] == r["prorated_overhead_usd"] == "10"
    assert r["required_account_return_fraction"] == "0.1"
    assert fixed_overhead_hurdle(ZERO_OVERHEAD, **PERIOD)["required_account_return_fraction"] == "0"


def test_half_leap_february_and_year_boundary_use_actual_utc_calendar_seconds():
    r = fixed_overhead_hurdle({**ZERO_OVERHEAD, "data": "29"},
                             period_start=ts("2024-02-01"), period_end=ts("2024-02-15T12:00:00"))
    assert r["prorated_overhead_usd"] == "14.5"
    assert r["proration_segments"][0]["calendar_month_seconds"] == 29 * 86400
    r = fixed_overhead_hurdle({**ZERO_OVERHEAD, "data": "31"},
                             period_start=ts("2025-12-31"), period_end=ts("2026-01-02"))
    assert r["prorated_overhead_usd"] == "2"
    assert [s["month"] for s in r["proration_segments"]] == ["2025-12", "2026-01"]


def test_proration_interval_partition_preserves_cost_with_only_declared_display_rounding():
    costs = {**ZERO_OVERHEAD, "model": "1"}
    start, split, end = ts("2026-01-15"), ts("2026-02-10"), ts("2026-03-04")
    with localcontext() as ctx:
        ctx.prec = 100
        amounts = [D(fixed_overhead_hurdle(costs, period_start=a, period_end=b)["prorated_overhead_usd"])
                   for a, b in [(start, end), (start, split), (split, end)]]
        assert abs(amounts[0] - amounts[1] - amounts[2]) <= D("1e-36")
    r = fixed_overhead_hurdle(costs, period_start=start, period_end=end)
    assert sum(s["covered_seconds"] for s in r["proration_segments"]) == end - start


@pytest.mark.parametrize("costs", [None, {}, {"tool": "1"}, {**ZERO_OVERHEAD, "server": None}])
def test_unknown_fixed_costs_never_become_zero(costs):
    r = fixed_overhead_hurdle(costs, **PERIOD)
    assert not r["available"] and r["prorated_overhead_usd"] is None
    assert r["required_account_return_fraction"] is None and r["unknown_inputs"]
    assert not adjusted(monthly_costs_usd=costs)["available"]


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "1000000.01", False, 1.0])
def test_fixed_component_range_and_decimal_type_are_validated(value):
    with pytest.raises(EconomicsError):
        fixed_overhead_hurdle({**ZERO_OVERHEAD, "data": value}, **PERIOD)


def test_typo_or_wrong_cost_container_is_not_silently_ignored():
    for costs in ({**ZERO_OVERHEAD, "tax": "1"}, ["0"]):
        with pytest.raises(EconomicsError):
            fixed_overhead_hurdle(costs, **PERIOD)


@pytest.mark.parametrize("start,end", [(0, 0), (2, 1), (-1, 1), (True, 2), (0.0, 1),
                                       (0, 4102444801), (0, 366 * 11 * 86400)])
def test_invalid_or_ambiguous_intervals_are_rejected(start, end):
    with pytest.raises(EconomicsError):
        fixed_overhead_hurdle(ZERO_OVERHEAD, period_start=start, period_end=end)


def test_existing_net_pnl_only_subtracts_overhead_and_preserves_losses():
    # $5 is already net of execution; there is no input or path to charge those fees again.
    r = adjusted()
    assert r["net_replay_pnl_usd"] == "5" and r["adjusted_net_pnl_usd"] == "4"
    assert r["adjusted_account_return_fraction"] == "0.04"
    assert r["execution_costs_subtracted_again_usd"] == "0"
    assert adjusted("-2")["adjusted_net_pnl_usd"] == "-3"
    assert adjusted("-100")["adjusted_net_pnl_usd"] == "-101"
    assert adjusted(monthly_costs_usd=ZERO_OVERHEAD)["adjusted_net_pnl_usd"] == "5"
    assert not r["overhead_cash_debit_performed"]


@pytest.mark.parametrize("changes,reason", [
    ({"replay_complete": False}, "replay_incomplete"),
    ({"replay_complete": None}, "replay_completeness_unknown"),
    ({"execution_costs_already_included": False}, "execution_costs_not_included"),
    ({"execution_costs_already_included": None}, "execution_cost_inclusion_unknown"),
])
def test_incomplete_unknown_or_gross_replay_is_not_promoted(changes, reason):
    r = adjusted(**changes)
    assert not r["available"] and reason in r["unavailable_reasons"]
    assert r["adjusted_net_pnl_usd"] is r["adjusted_account_return_fraction"] is None
    assert r["net_replay_pnl_usd"] == "5"


def test_unknown_replay_pnl_and_missing_attestations_stay_unavailable():
    r = adjusted(None)
    assert not r["available"] and r["net_replay_pnl_usd"] is None
    assert "net_replay_pnl_unknown" in r["unavailable_reasons"]
    r = overhead_adjusted_pnl("5", monthly_costs_usd=ZERO_OVERHEAD, **PERIOD)
    assert not r["available"] and len(r["unavailable_reasons"]) == 2


def test_replay_period_cannot_mismatch_overhead_period_or_omit_one_endpoint():
    r = adjusted(replay_period_start=PERIOD["period_start"], replay_period_end=PERIOD["period_end"])
    assert r["available"] and r["replay_period_metadata_checked"]
    for changes in ({"replay_period_start": PERIOD["period_start"]},
                    {"replay_period_start": PERIOD["period_start"] + 1, "replay_period_end": PERIOD["period_end"]}):
        with pytest.raises(EconomicsError):
            adjusted(**changes)


@pytest.mark.parametrize("pnl", ["NaN", "Infinity", "-100.01", "1000000000000.01", True, 5.0])
def test_invalid_replay_pnl_cannot_supply_adjusted_returns(pnl):
    with pytest.raises(EconomicsError):
        adjusted(pnl)


def test_boolean_attestations_are_strict():
    with pytest.raises(EconomicsError):
        adjusted(replay_complete="true")
    with pytest.raises(EconomicsError):
        adjusted(execution_costs_already_included=1)


def test_financial_results_ignore_callers_decimal_context_and_are_json_safe():
    expected = economics(buy_fee_bps="120", sell_fee_bps="120")
    expected_overhead = adjusted()
    with localcontext() as ctx:
        ctx.prec = 3
        ctx.rounding = ROUND_DOWN
        assert economics(buy_fee_bps="120", sell_fee_bps="120") == expected
        assert adjusted() == expected_overhead
    json.dumps(expected_overhead, allow_nan=False)
