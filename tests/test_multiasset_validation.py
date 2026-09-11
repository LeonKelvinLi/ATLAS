"""Causal scheduling and descriptive metric contracts on invented fixtures only."""

from copy import deepcopy
from datetime import datetime, timezone
import math
import statistics

import pytest

from atlas.research.multiasset_validation import (
    DAY, DEFAULT_THRESHOLDS, gate_result, metrics_for, pre_cutoff, rank_candidates,
    schedule_folds, stress_diagnostics,
)
from atlas.research.replay import ExecutionModel, run_replay


START = 20_000 * DAY


def fill(key, when, side, qty="1", pnl="0", **changes):
    return {"fill_id": key, "fill_time": when, "strategy_id": "fixture",
            "strategy_version": "1", "symbol": "BTC-USD", "side": side,
            "quantity": qty, "fill_price": "1", "realized_pnl": pnl, **changes}


def report(*, days=75, evidence="SYNTHETIC", empty_fills=False):
    equity, curve = 100.0, []
    for i in range(days):
        if i:
            equity *= 1 + (.006 if i % 3 else -.002)
        curve.append({"timestamp": START + i * DAY, "equity": str(equity)})
    fills = []
    if not empty_fills:
        for i in range(30):
            fills.extend([fill(f"buy-{i}", START + i * 2 * DAY, "buy"),
                          fill(f"sell-{i}", START + (i * 2 + 1) * DAY, "sell", pnl="1" if i % 3 else "-0.25")])
    return {"evidence_class": evidence, "complete": True, "equity_curve": curve,
            "portfolio": {"equity": str(equity), "stale_symbols": []}, "fills": fills,
            "metrics": {"net_return": str(equity / 100 - 1)},
            "execution_quality": {"fees": "0.40", "spread_cost": "0.20",
                                  "slippage_cost": "0.10", "implementation_shortfall": "0.50"}}


def evidence(**changes):
    # These are intentionally fabricated attestations for gate-unit tests;
    # no fixture data or output is suitable for any real paper allocation.
    return {"evidence_class": "HISTORICAL_OUT_OF_SAMPLE", "provenance_verified": True,
            "point_in_time_verified": True, "out_of_sample": True, **changes}


def qualifying_metrics():
    return metrics_for(report(evidence="HISTORICAL_OUT_OF_SAMPLE"))


def test_metrics_use_daily_year_convention_and_initial_cash_without_benchmark():
    summary = metrics_for(report())
    returns = summary["daily_returns"]
    assert summary["daily_observations"] == 75
    assert summary["periods_per_year"] == 365.25
    assert summary["sharpe"] == pytest.approx(statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(365.25))
    assert summary["volatility"] == pytest.approx(statistics.stdev(returns) * math.sqrt(365.25))
    expected = (1 + summary["net_return"]) ** (365.25 / 74) - 1
    assert summary["cagr"] == pytest.approx(expected)
    assert summary["sortino"] is not None and summary["calmar"] is not None
    assert summary["risk_free_rate_assumption"] == 0
    assert summary["cash_baseline_return"] == 0
    assert summary["benchmark_excess_return"] is None
    assert summary["total_costs"] == pytest.approx(.70)  # Shortfall is not counted twice.


def test_many_intraday_points_cannot_manufacture_annualization_sample_size():
    data = report(days=3, empty_fills=True)
    data["equity_curve"] = [{"timestamp": START + hour * 3600, "equity": str(100 + hour / 100)} for hour in range(72)]
    data["portfolio"]["equity"] = data["equity_curve"][-1]["equity"]
    data["metrics"] = {}
    summary = metrics_for(data)
    assert summary["daily_observations"] == 3
    assert len(summary["daily_returns"]) == 2
    assert not summary["annualization_available"]
    assert summary["sharpe"] is None


def test_crypto_missing_weekend_days_are_not_backfilled_or_annualized():
    data = report()
    data["equity_curve"] = [r for r in data["equity_curve"]
                            if datetime.fromtimestamp(r["timestamp"], timezone.utc).weekday() < 5]
    data["portfolio"]["equity"] = data["equity_curve"][-1]["equity"]
    data["metrics"] = {}
    summary = metrics_for(data)
    assert summary["missing_days"]
    assert not summary["calendar_complete"]
    assert summary["daily_returns"] == []
    assert summary["sharpe"] is None and summary["worst_day"] is None


def test_supplied_equity_session_calendar_is_explicit_and_cannot_hide_a_missing_session():
    data = report()
    data["equity_curve"] = [r for r in data["equity_curve"]
                            if datetime.fromtimestamp(r["timestamp"], timezone.utc).weekday() < 5]
    sessions = [r["timestamp"] for r in data["equity_curve"]]
    data["portfolio"]["equity"] = data["equity_curve"][-1]["equity"]
    data["metrics"] = {}
    summary = metrics_for(data, calendar="UTC_SESSION_DAYS", session_days=sessions)
    assert summary["calendar_complete"]
    assert summary["periods_per_year"] == 252
    assert summary["sharpe"] is not None
    assert summary["session_calendar_verified"] is False
    del data["equity_curve"][8]
    missing = metrics_for(data, calendar="UTC_SESSION_DAYS", session_days=sessions)
    assert missing["missing_days"] == [sessions[8]]
    assert missing["sharpe"] is None


@pytest.mark.parametrize("kwargs", [
    {"calendar": "hourly"}, {"calendar": "UTC_SESSION_DAYS"},
    {"calendar": "UTC_SESSION_DAYS", "session_days": [START + 1]},
    {"calendar": "UTC_SESSION_DAYS", "session_days": [START, START]},
    {"calendar": "UTC_24_7", "session_days": [START]},
])
def test_unknown_or_invalid_calendar_is_rejected(kwargs):
    with pytest.raises(ValueError):
        metrics_for(report(), **kwargs)


def test_partial_sells_and_scale_ins_count_only_one_completed_episode():
    data = report()
    data["fills"] = [fill("b1", START, "buy"), fill("s1", START + 1, "sell", ".25", "-0.10"),
                     fill("b2", START + 2, "buy", ".50"), fill("s2", START + 3, "sell", "1.25", ".40"),
                     fill("b3", START + 4, "buy"), fill("s3", START + 5, "sell", ".50", ".90")]
    summary = metrics_for(data)
    assert summary["completed_round_trips"] == 1
    assert summary["expectancy"] == pytest.approx(.30)
    assert summary["open_sleeves"] == 1
    assert summary["round_trips"][0]["holding_seconds"] == 3
    assert summary["profit_factor"] is None


def test_two_sleeves_do_not_close_each_others_inventory():
    data = report()
    data["fills"] = [fill("a", START, "buy", strategy_id="a"),
                     fill("b", START + 1, "buy", strategy_id="b"),
                     fill("c", START + 2, "sell", pnl="1", strategy_id="b")]
    summary = metrics_for(data)
    assert summary["completed_round_trips"] == 1 and summary["open_sleeves"] == 1
    assert summary["round_trips"][0]["strategy_id"] == "b"


@pytest.mark.parametrize("fills", [
    [fill("a", START, "sell")],
    [fill("a", START, "buy"), fill("a", START + 1, "sell")],
    [fill("a", START + 1, "buy"), fill("b", START, "sell")],
    [fill("a", START, "buy"), fill("b", START + 1, "sell", "1.1")],
])
def test_incomplete_or_duplicated_fill_history_cannot_supply_trade_statistics(fills):
    data = report()
    data["fills"] = fills
    with pytest.raises(ValueError):
        metrics_for(data)


def test_missing_fills_are_unavailable_not_a_manufactured_trade_count():
    data = report()
    del data["fills"]
    data["trades"] = [{"net_pnl": "100"}] * 100
    summary = metrics_for(data)
    assert summary["completed_round_trips"] is None
    assert summary["profit_factor"] is None
    assert summary["turnover"] is None


def test_future_fills_cannot_be_hidden_behind_an_earlier_equity_curve_cutoff():
    data = report()
    data["fills"][-1]["fill_time"] = data["equity_curve"][-1]["timestamp"] + DAY
    with pytest.raises(ValueError, match="outside the equity evaluation window"):
        metrics_for(data)


def test_initial_capital_loss_and_unrecovered_duration_remain_visible():
    data = report(days=3, empty_fills=True)
    data["equity_curve"] = [{"timestamp": START + i * DAY, "equity": str(v)} for i, v in enumerate([99, 98, 97])]
    data["portfolio"]["equity"] = "97"
    data["metrics"] = {}
    summary = metrics_for(data)
    assert summary["net_return"] == pytest.approx(-.03)
    assert summary["max_drawdown"] == pytest.approx(.03)
    assert summary["recovery_seconds"] is None
    assert summary["open_drawdown_duration_seconds"] == 2 * DAY


def test_terminal_or_declared_capital_mismatch_is_rejected():
    data = report()
    data["portfolio"]["equity"] = "9000"
    with pytest.raises(ValueError, match="disagree"):
        metrics_for(data)
    with pytest.raises(ValueError, match="100"):
        metrics_for(report(), initial_capital="10000")
    data = report()
    data["metrics"]["net_return"] = "10"
    with pytest.raises(ValueError, match="reconcile"):
        metrics_for(data)


def test_strict_gate_can_only_propose_paper_with_all_verified_fixture_attestations():
    gate = gate_result(qualifying_metrics(), evidence=evidence())
    assert gate["eligible"] and not gate["reasons"]
    assert gate["score"] > 0
    assert gate["allocation_scope"] == "paper_only" and not gate["live_approved"]


@pytest.mark.parametrize("label", ["SYNTHETIC", "TEST", "HISTORICAL_UNVERIFIED", "HISTORICAL", "UNKNOWN"])
def test_unverified_data_cannot_promote_even_when_metrics_exceed_every_threshold(label):
    metrics = qualifying_metrics()
    metrics["evidence_class"] = label
    gate = gate_result(metrics, evidence=evidence(evidence_class=label))
    assert not gate["eligible"] and gate["score"] is None
    assert "evidence_not_verified_out_of_sample" in gate["reasons"]


@pytest.mark.parametrize("field,value", [
    ("completed_round_trips", 29), ("daily_observations", 59), ("max_drawdown", .10001),
    ("sharpe", 1.19), ("profit_factor", 1.24), ("net_return", 0),
    ("sharpe", None), ("profit_factor", float("nan")),
    ("completed_round_trips", 30.5), ("max_drawdown", -.1),
    ("calendar_complete", False), ("report_complete", False), ("terminal_valuation_fresh", False),
])
def test_each_gate_fails_closed(field, value):
    metrics = qualifying_metrics()
    metrics[field] = value
    assert not gate_result(metrics, evidence=evidence())["eligible"]


@pytest.mark.parametrize("field", ["provenance_verified", "point_in_time_verified", "out_of_sample"])
def test_provenance_attestations_are_exact_true(field):
    assert not gate_result(qualifying_metrics(), evidence=evidence(**{field: "true"}))["eligible"]


@pytest.mark.parametrize("thresholds", [
    {"min_completed_round_trips": 29}, {"min_daily_observations": 59},
    {"max_drawdown": .2}, {"min_sharpe": 1.1}, {"min_profit_factor": 1.2},
    {"min_net_return": -.01}, {"min_daily_observations": True}, {"unknown": 1},
])
def test_threshold_override_cannot_weaken_floor(thresholds):
    with pytest.raises(ValueError):
        gate_result(qualifying_metrics(), thresholds, evidence())


def test_fold_windows_are_chronological_embargoed_and_purge_boundary_labels():
    times = list(range(0, 180))
    folds = schedule_folds(times, train_seconds=40, validation_seconds=20, test_seconds=20,
                           embargo_seconds=5, label_horizon_seconds=4)
    assert len(folds) >= 3
    for fold in folds:
        ranges = fold["ranges"]
        assert ranges["train"]["end_exclusive"] + 5 == ranges["validation"]["start"]
        assert ranges["validation"]["end_exclusive"] + 5 == ranges["test"]["start"]
        for name in ("train", "validation", "test"):
            end = ranges[name]["end_exclusive"]
            assert all(times[i] + 4 < end for i in fold[f"{name}_indices"])
            assert fold["purged"][name]["unresolved_at_window_end"] == 4
    assert all(a["ranges"]["test"]["end_exclusive"] <= b["ranges"]["test"]["start"] for a, b in zip(folds, folds[1:]))


def test_sealed_holdout_targets_are_not_read_and_unlabeled_data_need_horizon():
    samples = [{"event_time": i, "information_available_time": i, "label_end_time": i + 1} for i in range(120)]
    samples += [{"event_time": i, "label_end_time": object()} for i in range(120, 140)]
    folds = schedule_folds(samples, train_seconds=40, validation_seconds=20, test_seconds=20,
                           embargo_seconds=2, holdout_start=120)
    assert all(fold["ranges"]["test"]["end_exclusive"] <= 120 for fold in folds)
    with pytest.raises(ValueError, match="horizon"):
        schedule_folds(list(range(100)), train_seconds=20, validation_seconds=10, test_seconds=10, embargo_seconds=1)
    with pytest.raises(ValueError, match="overlap"):
        schedule_folds(list(range(100)), train_seconds=20, validation_seconds=10, test_seconds=10,
                       step_seconds=5, embargo_seconds=1, label_horizon_seconds=1)


def candidate(name="baseline", **changes):
    metrics = qualifying_metrics()
    return {"candidate_id": name, "split": "validation", "evaluated_end": metrics["evaluated_end"],
            "metrics": metrics, **changes}


def test_tournament_ranks_training_validation_only_and_rejects_future_or_test():
    a, b = candidate("a"), candidate("b")
    b["metrics"]["cagr"] += .1
    a["metrics"]["sharpe"] += 10  # CAGR wins among candidates satisfying all risk floors.
    selection = a["evaluated_end"] + 1
    ranked = rank_candidates([a, b], selection_time=selection)
    assert [r["candidate_id"] for r in ranked] == ["b", "a"]
    assert all(r["paper_eligible"] is False for r in ranked)
    assert all(r["selection_qualified"] is True for r in ranked)
    for bad in [candidate(split="test"), candidate(evaluated_end=selection)]:
        with pytest.raises(ValueError):
            rank_candidates([bad], selection_time=selection)
    masked = candidate(evaluated_end=a["evaluated_end"] - 1)
    with pytest.raises(ValueError):
        rank_candidates([masked], selection_time=selection)


def test_insufficient_training_sample_has_no_winner_even_with_attractive_cagr():
    item = candidate()
    item["metrics"]["daily_observations"] = 59
    item["metrics"]["cagr"] = 999
    ranked = rank_candidates([item], selection_time=item["evaluated_end"] + 1)
    assert ranked[0]["selection_score"] is None
    assert ranked[0]["selection_qualified"] is False
    assert "daily_observations_threshold_failed" in ranked[0]["selection_reasons"]


def test_synthetic_numeric_ranking_never_grants_paper_eligibility():
    item = candidate()
    item["metrics"]["evidence_class"] = "SYNTHETIC"
    ranked = rank_candidates([item], selection_time=item["evaluated_end"] + 1)
    assert ranked[0]["selection_qualified"]
    assert not ranked[0]["paper_eligible"]
    assert not gate_result(item["metrics"], evidence=evidence())["eligible"]


def selected(name="candidate", **changes):
    metrics = qualifying_metrics()
    return {"candidate_id": name, "selection_split": "validation", "selected_at": START - 1,
            "evaluated_start": START, "evaluated_end": metrics["evaluated_end"],
            "metrics": metrics, "evidence": evidence(), **changes}


def test_prior_completed_oos_filters_predeclared_order_without_reranking_test_results():
    first, second = selected("first"), selected("second")
    second["metrics"]["sharpe"] = 100
    allocation = first["evaluated_end"] + 1
    result = pre_cutoff([first, second], allocation_time=allocation)
    assert result["eligible_ids"] == ["first", "second"]
    assert not result["current_test_used_for_selection"]
    assert not result["live_approved"]


@pytest.mark.parametrize("patch", [
    {"selected_at": START}, {"selection_split": "test"},
    {"evaluated_start": START + 1}, {"evaluated_end": START + 10},
])
def test_selection_cannot_use_same_or_future_oos_information(patch):
    with pytest.raises(ValueError):
        pre_cutoff([selected(**patch)], allocation_time=START + 80 * DAY)


def test_unfinished_current_fold_cannot_select_its_own_allocation():
    item = selected()
    with pytest.raises(ValueError):
        pre_cutoff([item], allocation_time=item["evaluated_end"])
    item["evidence"]["evidence_class"] = "SYNTHETIC"
    result = pre_cutoff([item], allocation_time=item["evaluated_end"] + 1)
    assert result["eligible_ids"] == []


def test_stress_is_deterministic_descriptive_and_refuses_mismatched_variant_window():
    base = report()
    alternate = deepcopy(base)
    result = stress_diagnostics(base, candidate_outputs={"fixed_variant": alternate}, seed=19, resamples=100)
    assert result == stress_diagnostics(base, candidate_outputs={"fixed_variant": alternate}, seed=19, resamples=100)
    assert result["block_bootstrap"]["available"]
    assert result["block_bootstrap"]["observations"] == 74
    assert result["monte_carlo"]["method"] == "permutation_of_completed_round_trip_net_dollar_pnl"
    assert result["monte_carlo"]["completed_round_trips"] == 30
    assert result["monte_carlo"]["drawdown_percentiles"]["95"] >= result["monte_carlo"]["drawdown_percentiles"]["5"]
    # With no ruin, permuting fixed dollar outcomes cannot alter their total.
    assert result["monte_carlo"]["return_percentiles"]["5"] == pytest.approx(result["monte_carlo"]["return_percentiles"]["95"])
    assert not result["parameters_optimized"] and not result["selection_eligible"]
    alternate["equity_curve"] = alternate["equity_curve"][:-1]
    alternate["portfolio"]["equity"] = alternate["equity_curve"][-1]["equity"]
    alternate["metrics"] = {}
    with pytest.raises(ValueError, match="same evaluation window"):
        stress_diagnostics(base, candidate_outputs={"future_slice": alternate}, resamples=100)


def test_stress_does_not_impute_missing_daily_returns():
    data = report()
    del data["equity_curve"][20]
    result = stress_diagnostics(data, resamples=100)
    assert result["block_bootstrap"]["available"] is False


def test_trade_order_stress_does_not_count_partial_sells_as_completed_round_trips():
    data = report()
    data["fills"] = [fill("buy", START, "buy", "31")]
    data["fills"] += [fill(f"partial-{i}", START + i + 1, "sell", "1", ".1") for i in range(30)]
    result = stress_diagnostics(data, resamples=100)
    assert result["monte_carlo"]["available"] is False


def test_real_replay_schema_cash_baseline_is_flat_without_manufactured_trades():
    bars = [{"symbol": "BTC-USD", "asset_class": "crypto", "venue": "TEST", "currency": "USD",
             "event_time": START + i * DAY, "interval_seconds": DAY,
             "information_available_time": START + i * DAY, "ingestion_time": START + i * DAY,
             "timezone": "UTC", "session": "synthetic", "session_date": "2024-01-01",
             "open": "100", "high": "100", "low": "100", "close": "100", "volume": "10",
             "volume_unit": "base_asset", "revision": 0} for i in range(75)]
    model = ExecutionModel(fee_bps="10", spread_bps="10", slippage_bps="5", latency_seconds=0,
                           participation_rate="0.5", quantity_increment="0.01", minimum_quantity="0.01")
    data = run_replay(bars, [], model=model, initial_cash="100", evidence_class="SYNTHETIC")
    summary = metrics_for(data)
    assert summary["net_return"] == 0
    assert summary["completed_round_trips"] == 0
    assert summary["cagr"] == 0
    assert summary["sharpe"] is None and summary["profit_factor"] is None
    assert summary["total_costs"] == 0
    assert not gate_result(summary, evidence=evidence())["eligible"]
