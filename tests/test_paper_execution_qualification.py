"""Invented records only; a passing test never proves a real paper account works."""

from copy import deepcopy

import pytest

from atlas.paper.qualification import (
    INFRASTRUCTURE_CHECKS, RESEARCH_REVIEWS, STRESS_SCENARIOS,
    assess_qualification, build_policy,
)
from atlas.research.store import content_hash


DAY = 86400
START = 20000 * DAY


def report(*, start=START, label="TEST", days=75):
    cash, curve, fills = 100.0, [], []
    for day in range(days):
        when = start + day * DAY
        if day < 60:
            index, side = day // 2, "sell" if day % 2 else "buy"
            pnl = (1.0 if index % 3 else -.25) if side == "sell" else 0.0
            cash += pnl
            fills.append({"fill_id": f"{side}-{index}", "fill_time": when, "strategy_id": "fixture",
                          "strategy_version": "1", "symbol": "SPY", "side": side,
                          "quantity": "1", "fill_price": "1", "realized_pnl": str(pnl)})
        curve.append({"timestamp": when, "equity": str(cash)})
    return {"evidence_class": label, "complete": True, "initial_cash": "100", "equity_curve": curve,
            "fills": fills, "portfolio": {"equity": str(cash), "stale_symbols": []},
            "execution_quality": {"fees": "0.20", "spread_cost": "0.10", "slippage_cost": "0.10",
                                  "implementation_shortfall": "0.25"}}


def manifest(label="TEST"):
    return {"schema_version": 1, "campaign": "paper-qualification-fixture", "evidence_kind": label,
            "source_identity": {"git_commit": "a" * 40, "source_tree_sha256": "b" * 64,
                                "lock_sha256": "c" * 64, "runtime": {"python": "fixture-only"}},
            "dataset": {"content_sha256": "d" * 64, "partition_sha256": "e" * 64, "role": "evaluation"},
            "strategy": {"strategy_id": "fixture", "version": "1", "implementation_sha256": "f" * 64,
                         "parameters": {"lookback": 20}},
            "features": {"version": "1", "definitions_sha256": "1" * 64},
            "execution": {"version": "1", "implementation_sha256": "2" * 64,
                          "assumptions": {"fees": "fixture-model"}},
            "split": {"training": [1, 10], "validation": [11, 15], "test": [16, 20]},
            "seed": 0, "evaluated_variant_count": 1}


def inputs(label="TEST", plan=()):
    source, observations = manifest(label), report(label=label)
    policy = build_policy(registered_at=START - 500 * DAY, evaluation_start=START,
                          evaluation_end=START + 80 * DAY, manifest_sha256=content_hash(source),
                          strategy_sha256="f" * 64, data_contract_sha256="3" * 64,
                          risk_policy_sha256="4" * 64, walk_forward_plan=plan)
    evidence = {"policy_sha256": policy["policy_sha256"], "manifest_sha256": content_hash(source),
                "report_sha256": content_hash(observations), "lineage_classes": [label]}
    return {"policy": policy, "report": observations, "manifest": source, "evidence": evidence}


def reviews(names, policy, label="TEST"):
    result = {}
    for name in names:
        body = {"status": "PASS", "policy_sha256": policy["policy_sha256"], "evidence_class": label,
                "evidence_refs": [content_hash({"fixture": name})], "note": "Invented fixture review only."}
        result[name] = {**body, "sha256": content_hash(body)}
    return result


def refresh_inventory(data):
    data["evidence"]["report_sha256"] = content_hash(data["report"])


def fold_fixture(start):
    ranges = {"train": {"start": start - 160 * DAY, "end_exclusive": start - 90 * DAY},
              "validation": {"start": start - 89 * DAY, "end_exclusive": start - DAY},
              "test": {"start": start, "end_exclusive": start + 75 * DAY}}
    definition = {"ranges": ranges, "embargo_seconds": 300}
    fold = {**definition, "train_indices": [0], "validation_indices": [1], "test_indices": [2]}
    samples = [{"event_time": window["start"], "information_available_time": window["start"],
                "label_end_time": window["start"] + 60, "label_available_time": window["start"] + 60}
               for window in ranges.values()]
    record = {"fold": fold, "samples": samples, "selected_at": start - 100,
              "selection_split": "validation", "report": report(start=start)}
    return definition, record


def test_missing_evidence_is_partial_and_unknown_is_not_zero():
    result = assess_qualification(policy=inputs()["policy"])
    assert result["status"] == "PARTIAL"
    assert result["strategy"]["metrics"] is None
    assert "report_missing" in result["pending"]
    assert "experiment_manifest_missing" in result["pending"]
    assert result["infrastructure"]["authenticated_broker_readiness"] == "PARTIAL"


def test_numeric_success_is_separate_from_infrastructure_and_prospective_qualification():
    data = inputs()
    data["infrastructure"] = reviews(INFRASTRUCTURE_CHECKS, data["policy"])
    result = assess_qualification(**data)
    assert result["strategy"]["numeric"]["status"] == "PASS"
    assert result["infrastructure"]["engineering"]["status"] == "PASS"
    assert result["status"] == "PARTIAL"
    assert not result["strategy"]["existing_research_gate"]["eligible"]
    assert not result["prospective_qualified"]
    assert not result["paper_execution_authorized"]
    assert not result["live_approved"]
    assert result["strategy"]["metrics"]["total_costs"] == pytest.approx(.4)
    diagnostics = result["strategy"]["diagnostics"]
    assert diagnostics["win_rate"] == pytest.approx(20 / 30)
    assert diagnostics["independent_observations"] is None
    assert diagnostics["historical_daily_tail_risk"]["var"] is None


@pytest.mark.parametrize("field,value", [("live_approved", True), ("activation_authorized", True),
                                        ("thresholds", {"min_completed_round_trips": 1})])
def test_even_rehashed_policy_cannot_change_frozen_scope_or_criteria(field, value):
    data = inputs()
    data["policy"][field] = value
    data["policy"]["policy_sha256"] = content_hash({k: v for k, v in data["policy"].items() if k != "policy_sha256"})
    with pytest.raises(ValueError, match="frozen criteria changed"):
        assess_qualification(**data)


def test_forged_provenance_booleans_are_not_a_supported_inventory():
    data = inputs("PAPER")
    data["evidence"]["provenance_verified"] = True
    with pytest.raises(ValueError, match="booleans"):
        assess_qualification(**data)


def test_self_consistent_paper_claims_still_cannot_authenticate_origin():
    data = inputs("PAPER")  # Deliberately forged fixture labels, testing the authority boundary.
    data["infrastructure"] = reviews(INFRASTRUCTURE_CHECKS, data["policy"], "PAPER")
    data["reviews"] = reviews(RESEARCH_REVIEWS, data["policy"], "PAPER")
    result = assess_qualification(**data)
    assert result["strategy"]["research_reviews"]["status"] == "PASS"
    assert "external_origin_and_predeclaration_authentication_required" in result["pending"]
    assert not result["origin_authenticated"]
    assert not result["prospective_qualified"]
    assert not result["activation_authorized"]


@pytest.mark.parametrize("ancestor", ["TEST", "SYNTHETIC", "HISTORICAL_UNVERIFIED", "HISTORICAL_OUT_OF_SAMPLE"])
def test_nonprospective_lineage_cannot_be_promoted_by_relabelling(ancestor):
    data = inputs("PAPER")
    data["evidence"]["lineage_classes"].append(ancestor)
    result = assess_qualification(**data)
    assert result["status"] == "FAIL"
    assert "nonprospective_evidence_cannot_be_relabelled" in result["failures"]


def test_changed_observations_and_manifest_break_evidence_binding():
    data = inputs()
    data["report"]["execution_quality"]["fees"] = "9"
    data["manifest"]["strategy"]["parameters"]["lookback"] = 10
    result = assess_qualification(**data)
    assert "evidence_inventory_identity_mismatch" in result["failures"]
    assert "experiment_manifest_identity_mismatch" in result["failures"]


def test_observed_loss_fails_but_unknown_cost_is_partial():
    data = inputs()
    data["report"]["equity_curve"][-1]["equity"] = "80"
    data["report"]["portfolio"]["equity"] = "80"
    refresh_inventory(data)
    result = assess_qualification(**data)
    assert result["strategy"]["numeric"]["status"] == "FAIL"
    assert "net_return_threshold_failed" in result["failures"]
    assert "max_drawdown_threshold_failed" in result["failures"]
    data = inputs()
    del data["report"]["execution_quality"]["fees"]
    refresh_inventory(data)
    result = assess_qualification(**data)
    assert result["strategy"]["numeric"]["status"] == "PARTIAL"
    assert "execution_costs_unavailable" in result["pending"]


def test_partial_exits_cannot_manufacture_thirty_round_trips():
    data = inputs()
    fills = data["report"]["fills"]
    fills[0]["quantity"] = "30"
    data["report"]["fills"] = [fills[0]] + [item for item in fills if item["side"] == "sell"]
    refresh_inventory(data)
    result = assess_qualification(**data)
    assert result["strategy"]["metrics"]["completed_round_trips"] == 1
    assert "completed_round_trips_threshold_failed" in result["failures"]


def test_missing_days_and_stale_valuations_do_not_pass():
    data = inputs()
    del data["report"]["equity_curve"][10]
    data["report"]["portfolio"]["stale_symbols"] = ["SPY"]
    refresh_inventory(data)
    result = assess_qualification(**data)
    assert result["strategy"]["numeric"]["status"] == "PARTIAL"
    assert "daily_calendar_incomplete" in result["pending"]
    assert "terminal_valuation_freshness_unverified" in result["pending"]


def test_two_predeclared_folds_measure_stability_without_claiming_independence():
    first, a = fold_fixture(START - 180 * DAY)
    second, b = fold_fixture(START - 90 * DAY)
    data = inputs(plan=[first, second])
    data["walk_forward"] = [a, b]
    result = assess_qualification(**data)
    assert result["strategy"]["walk_forward"]["status"] == "PASS"
    assert result["strategy"]["walk_forward"]["positive_fold_fraction"] == 1
    assert not result["strategy"]["walk_forward"]["folds_are_independent"]
    assert not result["prospective_qualified"]


def test_test_window_selection_and_future_labels_are_rejected():
    definition, record = fold_fixture(START - 90 * DAY)
    data = inputs(plan=[definition])
    data["walk_forward"] = [record]
    record["selected_at"] = definition["ranges"]["test"]["start"]
    result = assess_qualification(**data)
    assert "fold_0_selection_leakage" in result["failures"]
    record["samples"][0]["label_available_time"] = definition["ranges"]["validation"]["start"]
    with pytest.raises(ValueError, match="future information"):
        assess_qualification(**data)


def test_overlapping_or_unregistered_fold_plans_fail_before_assessment():
    definition, _ = fold_fixture(START - 90 * DAY)
    with pytest.raises(ValueError, match="nonoverlapping"):
        inputs(plan=[definition, deepcopy(definition)])
    definition, _ = fold_fixture(START - 600 * DAY)
    with pytest.raises(ValueError, match="predeclared"):
        inputs(plan=[definition])


def test_review_tampering_and_explicit_engineering_failures_fail_without_hiding_numeric_pass():
    data = inputs()
    checks = reviews(INFRASTRUCTURE_CHECKS, data["policy"])
    checks[INFRASTRUCTURE_CHECKS[0]]["note"] = "Changed after hashing"
    data["infrastructure"] = checks
    result = assess_qualification(**data)
    assert result["status"] == "FAIL"
    assert result["strategy"]["numeric"]["status"] == "PASS"
    assert result["infrastructure"]["engineering"]["status"] == "FAIL"


def test_only_supplied_stress_results_are_summarized_and_never_generated():
    data = inputs()
    data["stress_reports"] = {name: report() for name in STRESS_SCENARIOS}
    data["stress_reports"]["double_cost"]["equity_curve"][-1]["equity"] = "99"
    data["stress_reports"]["double_cost"]["portfolio"]["equity"] = "99"
    result = assess_qualification(**data)
    robustness = result["strategy"]["diagnostics"]["execution_robustness"]
    assert robustness["fragile"] is True
    assert robustness["robustness_score"] == .75
    assert not result["strategy"]["diagnostics"]["stress_assumptions_authenticated"]
    assert "observed_stress_fragility_requires_review" in result["pending"]


def test_cost_sensitivity_preserves_assumptions_and_uses_only_observed_grid():
    data = inputs()
    base, costly = report(), report()
    base["execution_model"] = {"fee_bps": 1, "spread_bps": 5, "slippage_bps": 5}
    costly["execution_model"] = {"fee_bps": 21, "spread_bps": 5, "slippage_bps": 5}
    costly["equity_curve"][-1]["equity"] = "109"
    costly["portfolio"]["equity"] = "109"
    data["cost_runs"] = {"0": base, "20": costly}
    result = assess_qualification(**data)
    sensitivity = result["strategy"]["diagnostics"]["cost_sensitivity"]
    assert sensitivity["execution_assumptions_verified"] is True
    assert sensitivity["survives_20bps"] is True
    assert sensitivity["break_even_bps_bracket"] == [20, None]
    assert not result["activation_authorized"]
    costly["execution_model"]["slippage_bps"] = 1
    with pytest.raises(ValueError, match="execution assumptions fixed"):
        assess_qualification(**data)


@pytest.mark.parametrize("section", ["cost_runs", "stress_reports"])
def test_comparisons_from_a_different_evaluation_window_are_rejected(section):
    data = inputs()
    data[section] = {"0" if section == "cost_runs" else "double_cost": report(start=START + DAY)}
    with pytest.raises(ValueError, match="valuation windows"):
        assess_qualification(**data)


def test_a_single_intraday_day_does_not_make_annualized_statistics_available():
    data = inputs()
    data["report"]["fills"] = []
    data["report"]["equity_curve"] = [{"timestamp": START + i * 60, "equity": str(100 + i / 100)}
                                     for i in range(100)]
    data["report"]["portfolio"]["equity"] = "100.99"
    refresh_inventory(data)
    result = assess_qualification(**data)
    metrics = result["strategy"]["metrics"]
    assert metrics["daily_observations"] == 1
    assert metrics["sharpe"] is None
    assert metrics["sortino"] is None
    assert result["status"] == "FAIL"


def test_cashflows_require_explicit_adjusted_accounting_and_secret_fields_are_rejected():
    data = inputs()
    data["report"]["cash_flows"] = [{"amount": "10", "timestamp": START}]
    refresh_inventory(data)
    assert "cashflow_adjusted_return_accounting_required" in assess_qualification(**data)["failures"]
    data = inputs()
    data["evidence"]["api_key"] = "invented-do-not-log"
    with pytest.raises(ValueError, match="credentials"):
        assess_qualification(**data)


def test_assessment_does_not_mutate_supplied_evidence():
    data = inputs()
    before = deepcopy(data)
    assess_qualification(**data)
    assert data == before
