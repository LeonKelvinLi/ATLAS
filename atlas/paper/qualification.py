"""Offline evidence assessment, never strategy evaluation or execution authority.

Only caller-supplied JSON is inspected. Digests prove internal consistency, not
broker origin, point-in-time provenance, or an independently frozen experiment.
No passing field in this report can authorize paper or live orders.
"""

from copy import deepcopy
import re
import statistics

from atlas.collection.primitives import DEFAULT_THRESHOLDS
from atlas.research import diagnostics
from atlas.research.health import historical_tail_risk
from atlas.research.market import timestamp
from atlas.research.multiasset_validation import gate_result, metrics_for
from atlas.research.splits import validate_fold
from atlas.research.store import canonical, content_hash, validate_manifest


RESEARCH_REVIEWS = (
    "point_in_time", "leakage_and_holdout", "attempt_registry",
    "independence_and_power", "regime_coverage", "benchmark_attribution",
    "fees_and_cashflows", "execution_stress", "concentration_and_tails",
)
INFRASTRUCTURE_CHECKS = (
    "account_and_paper_origin", "durable_intent_and_reservation",
    "ambiguous_submission_recovery", "partial_fill_and_cancel_races",
    "restart_reconciliation", "single_writer_and_signal_deduplication",
    "inventory_and_cash_reconciliation", "risk_halts_and_kill_switch",
    "audit_integrity_and_redaction",
)
STRESS_SCENARIOS = ("double_cost", "latency_600s", "entry_delay_60s", "one_hour_downtime")
PROVENANCE_REASONS = {
    "evidence_not_verified_out_of_sample", "provenance_verified_required",
    "point_in_time_verified_required", "out_of_sample_required",
    "session_calendar_verification_required",
}
CLASSES = {"TEST", "SYNTHETIC", "HISTORICAL_UNVERIFIED", "HISTORICAL_OUT_OF_SAMPLE", "PAPER", "SHADOW"}
NONPROSPECTIVE = {"TEST", "SYNTHETIC", "HISTORICAL_UNVERIFIED", "HISTORICAL_OUT_OF_SAMPLE"}


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("full lowercase SHA256 required")
    return value


def _status(failures, pending):
    return "FAIL" if failures else "PARTIAL" if pending else "PASS"


def _fold_definition(value):
    if not isinstance(value, dict) or set(value) != {"ranges", "embargo_seconds"}:
        raise ValueError("fold definition requires ranges and embargo_seconds")
    if type(value["embargo_seconds"]) is not int or value["embargo_seconds"] < 0:
        raise ValueError("invalid embargo")
    if set(value["ranges"]) != {"train", "validation", "test"}:
        raise ValueError("explicit train, validation and test ranges required")
    validate_fold([], {**value, **{name + "_indices": [] for name in ("train", "validation", "test")}})
    return deepcopy(value)


def build_policy(*, registered_at, evaluation_start, evaluation_end, manifest_sha256,
                 strategy_sha256, data_contract_sha256, risk_policy_sha256, walk_forward_plan=()):
    """Describe a proposed declaration; creation does not independently freeze it.

    Times are integer UTC seconds, evaluation_end exclusive. The original study
    remains authoritative; the additional items are evidence-review requirements.
    No thresholds can be supplied or weakened through this interface.
    """
    times = [timestamp(t) for t in (registered_at, evaluation_start, evaluation_end)]
    if not times[0] < times[1] < times[2]:
        raise ValueError("policy must be declared before its evaluation window")
    if not isinstance(walk_forward_plan, (list, tuple)) or len(walk_forward_plan) > 10:
        raise ValueError("at most ten predeclared folds")
    folds, previous_end = [], -1
    for item in walk_forward_plan:
        fold = _fold_definition(item)
        start, end = (fold["ranges"]["test"][k] for k in ("start", "end_exclusive"))
        if not registered_at < start or start < previous_end:
            raise ValueError("predeclared nonoverlapping chronological test folds required")
        previous_end = end
        folds.append(fold)
    body = {"schema_version": 1, "scope": "paper_evidence_review_only",
            "registered_at": registered_at, "evaluation_start": evaluation_start,
            "evaluation_end": evaluation_end, "manifest_sha256": _hash(manifest_sha256),
            "strategy_sha256": _hash(strategy_sha256), "data_contract_sha256": _hash(data_contract_sha256),
            "risk_policy_sha256": _hash(risk_policy_sha256), "walk_forward_plan": folds,
            "thresholds": dict(DEFAULT_THRESHOLDS), "research_reviews": list(RESEARCH_REVIEWS),
            "infrastructure_checks": list(INFRASTRUCTURE_CHECKS), "stress_scenarios": list(STRESS_SCENARIOS),
            "live_approved": False, "activation_authorized": False}
    return {**body, "policy_sha256": content_hash(body)}


def _policy(value):
    canonical(value, maximum=1_000_000)
    fields = ("registered_at", "evaluation_start", "evaluation_end", "manifest_sha256",
              "strategy_sha256", "data_contract_sha256", "risk_policy_sha256", "walk_forward_plan")
    try:
        expected = build_policy(**{field: value[field] for field in fields})
    except (KeyError, TypeError):
        raise ValueError("complete qualification policy required") from None
    if value != expected:
        raise ValueError("qualification policy hash, scope or frozen criteria changed")
    return expected


def _reviews(records, names, policy_sha256):
    if not isinstance(records, dict) or set(records) - set(names):
        raise ValueError("unknown evidence review check")
    rows, failures, pending = {}, [], []
    for name in names:
        record = records.get(name)
        if record is None:
            pending.append(name + "_missing")
            rows[name] = {"status": "PARTIAL", "reason": "evidence_record_missing"}
            continue
        if not isinstance(record, dict):
            raise ValueError("review must be a hash-bound evidence record, not a boolean")
        required = {"status", "policy_sha256", "evidence_class", "evidence_refs", "note", "sha256"}
        if set(record) != required or record["status"] not in {"PASS", "PARTIAL", "FAIL"}:
            raise ValueError("invalid review record schema")
        body = {k: v for k, v in record.items() if k != "sha256"}
        _hash(record["sha256"])
        if record["evidence_class"] not in CLASSES or not isinstance(record["note"], str) or not record["note"].strip():
            raise ValueError("review requires an evidence class and explanation")
        refs = record["evidence_refs"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 100 or len(set(refs)) != len(refs):
            raise ValueError("unique bounded evidence references required")
        for ref in refs:
            _hash(ref)
        mismatch = record["policy_sha256"] != policy_sha256 or content_hash(body) != record["sha256"]
        status = "FAIL" if mismatch else record["status"]
        rows[name] = {"status": status, "evidence_class": record["evidence_class"],
                      "record_sha256": record["sha256"], "evidence_refs": refs,
                      "origin_authenticated": False}
        if status == "FAIL":
            failures.append(name + ("_identity_mismatch" if mismatch else "_failed"))
        elif status == "PARTIAL":
            pending.append(name + "_incomplete")
    return {"status": _status(failures, pending), "failures": failures, "pending": pending, "checks": rows,
            "basis": "supplied review records; their references and conclusions are not independently authenticated"}


def _numeric(report, calendar, session_days):
    metrics = metrics_for(report, calendar=calendar, session_days=session_days)
    # Never forward caller provenance attestations to the research permission gate.
    gate = gate_result(metrics)
    reasons = [r for r in gate["reasons"] if r not in PROVENANCE_REASONS]
    failures = [r for r in reasons if r.endswith("_threshold_failed")]
    pending = [r for r in reasons if r not in failures]
    return metrics, {"status": _status(failures, pending), "failures": failures, "pending": pending}, gate


def _walk_forward(records, policy, calendar, session_days):
    plan = policy["walk_forward_plan"]
    if not isinstance(records, list) or len(records) > 10:
        raise ValueError("bounded fold report list required")
    failures, pending, rows = [], [], []
    if len(records) > len(plan):
        failures.append("undeclared_walk_forward_results")
    if len(records) < len(plan) or len(plan) < 2:
        pending.append("multiple_predeclared_completed_folds_required_for_stability_review")
    for i, record in enumerate(records[:len(plan)]):
        if not isinstance(record, dict) or set(record) != {"fold", "samples", "selected_at", "selection_split", "report"}:
            raise ValueError("complete fold, sample chronology, selection and report required")
        fold = record["fold"]
        definition = {k: fold[k] for k in ("ranges", "embargo_seconds")}
        if definition != plan[i]:
            failures.append(f"fold_{i}_plan_mismatch")
            continue
        if not isinstance(record["samples"], list) or len(record["samples"]) > 10000:
            raise ValueError("bounded explicit fold sample metadata required")
        validate_fold(record["samples"], fold)
        if any(not fold[name + "_indices"] for name in ("train", "validation", "test")):
            pending.append(f"fold_{i}_sample_metadata_missing")
        test = definition["ranges"]["test"]
        selected = timestamp(record["selected_at"])
        if (record["selection_split"] not in {"train", "validation"}
                or not definition["ranges"]["validation"]["end_exclusive"] <= selected < test["start"]):
            failures.append(f"fold_{i}_selection_leakage")
        metrics, numeric, _ = _numeric(record["report"], calendar, session_days)
        if (metrics["evaluated_start"] is None or metrics["evaluated_end"] is None):
            pending.append(f"fold_{i}_valuation_window_missing")
        elif not test["start"] <= metrics["evaluated_start"] <= metrics["evaluated_end"] < test["end_exclusive"]:
            failures.append(f"fold_{i}_report_outside_test")
        if numeric["status"] == "FAIL":
            failures.append(f"fold_{i}_numeric_threshold_failed")
        elif numeric["status"] == "PARTIAL":
            pending.append(f"fold_{i}_numeric_evidence_incomplete")
        rows.append({"fold": i, "evidence_class": metrics["evidence_class"], "numeric": numeric,
                     "net_return": metrics["net_return"], "max_drawdown": metrics["max_drawdown"],
                     "sharpe": metrics["sharpe"], "report_sha256": content_hash(record["report"])})
    returns = [r["net_return"] for r in rows if r["net_return"] is not None]
    return {"status": _status(failures, pending), "failures": failures, "pending": pending, "folds": rows,
            "positive_fold_fraction": sum(r > 0 for r in returns) / len(returns) if returns else None,
            "worst_fold_return": min(returns, default=None), "folds_are_independent": False,
            "basis": "chronology and numerical floors only; no test ranking or pooling to manufacture independent trials"}


def assess_qualification(*, policy, report=None, manifest=None, evidence=None, infrastructure=None,
                         walk_forward=None, reviews=None, cost_runs=None, stress_reports=None,
                         calendar="UTC_24_7", session_days=None):
    """Check explicit records without accessing a path, broker, evaluator or holdout.

    Report uses the existing metrics_for replay-report schema. Evidence is an
    inventory, not a credential: policy/report/manifest hashes and lineage_classes.
    Reviews use the exact schema documented in docs/paper-qualification.md.
    """
    bundle = {"report": report, "manifest": manifest, "evidence": evidence, "infrastructure": infrastructure,
              "walk_forward": walk_forward, "reviews": reviews, "cost_runs": cost_runs,
              "stress_reports": stress_reports, "calendar": calendar, "session_days": session_days}
    canonical(bundle)  # Bounded JSON; rejects secrets, nonfinite values and arbitrary objects.
    policy = _policy(policy)
    identity, failures, pending = policy["policy_sha256"], [], []
    engineering = _reviews({} if infrastructure is None else infrastructure, INFRASTRUCTURE_CHECKS, identity)
    research = _reviews({} if reviews is None else reviews, RESEARCH_REVIEWS, identity)
    failures.extend("infrastructure_" + reason for reason in engineering["failures"])
    pending.extend("infrastructure_" + reason for reason in engineering["pending"])
    failures.extend(research["failures"])
    pending.extend(research["pending"])
    pending.append("external_origin_and_predeclaration_authentication_required")
    lineage = []
    if manifest is None:
        pending.append("experiment_manifest_missing")
    else:
        validate_manifest(manifest)
        if (content_hash(manifest) != policy["manifest_sha256"]
                or manifest["strategy"]["implementation_sha256"] != policy["strategy_sha256"]):
            failures.append("experiment_manifest_identity_mismatch")
    if evidence is None:
        pending.append("evidence_inventory_missing")
    else:
        if not isinstance(evidence, dict) or set(evidence) != {"policy_sha256", "manifest_sha256", "report_sha256", "lineage_classes"}:
            raise ValueError("explicit hash inventory required; provenance booleans are not accepted")
        lineage = evidence["lineage_classes"]
        if not isinstance(lineage, list) or not lineage or any(label not in CLASSES for label in lineage):
            raise ValueError("explicit known lineage evidence classes required")
        for name in ("policy_sha256", "manifest_sha256", "report_sha256"):
            _hash(evidence[name])
        if (evidence["policy_sha256"] != identity or evidence["manifest_sha256"] != policy["manifest_sha256"]
                or report is None or evidence["report_sha256"] != content_hash(report)):
            failures.append("evidence_inventory_identity_mismatch")
    metrics, numeric, gate, calculated = None, {"status": "PARTIAL", "failures": [], "pending": ["report_missing"]}, None, {}
    if report is not None:
        metrics, numeric, gate = _numeric(report, calendar, session_days)
        label = metrics["evidence_class"]
        if label not in CLASSES:
            failures.append("report_evidence_class_unknown")
        if manifest is not None and manifest["evidence_kind"] != label:
            failures.append("report_manifest_evidence_class_mismatch")
        if lineage and label not in lineage:
            failures.append("report_lineage_evidence_class_mismatch")
        if label in {"PAPER", "SHADOW"} and any(item in NONPROSPECTIVE for item in lineage):
            failures.append("nonprospective_evidence_cannot_be_relabelled")
        if label not in {"PAPER", "SHADOW"}:
            pending.append("prospective_paper_or_shadow_evidence_required")
        if metrics["evaluated_start"] is not None and not (policy["evaluation_start"] <= metrics["evaluated_start"]
                <= metrics["evaluated_end"] < policy["evaluation_end"]):
            failures.append("report_outside_predeclared_window")
        if report.get("cash_flows"):
            failures.append("cashflow_adjusted_return_accounting_required")
        trips = metrics["round_trips"] or []
        pnl = [row["net_pnl"] for row in trips]
        wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
        calculated = {
            "win_rate": len(wins) / len(pnl) if pnl else None,
            "mean_win": statistics.mean(wins) if wins else None,
            "mean_loss": statistics.mean(losses) if losses else None,
            "win_loss_ratio": statistics.mean(wins) / -statistics.mean(losses) if wins and losses else None,
            "independent_observations": None, "independence_verified": False,
            "concentration": diagnostics.concentration_attacks(trips, metrics["daily_curve"], evidence_class=label),
            "historical_daily_tail_risk": historical_tail_risk(metrics["daily_returns"]),
            "deflated_sharpe": None, "probabilistic_sharpe": None,
            "regime_results": None,
        }
        if cost_runs is not None:
            if not isinstance(cost_runs, dict) or len(cost_runs) > 20:
                raise ValueError("at most twenty supplied cost runs")
            for run in cost_runs.values():
                summary = metrics_for(run, calendar=calendar, session_days=session_days)
                if (summary["evidence_class"] != label or any(summary[field] != metrics[field]
                        for field in ("evaluated_start", "evaluated_end", "daily_observations"))):
                    raise ValueError("cost comparisons require matching evidence class and valuation windows")
            calculated["cost_sensitivity"] = diagnostics.cost_sensitivity(cost_runs, evidence_class=label)
            if not calculated["cost_sensitivity"]["execution_assumptions_verified"]:
                pending.append("cost_execution_assumptions_unverified")
        if stress_reports is not None:
            if not isinstance(stress_reports, dict) or set(stress_reports) - set(STRESS_SCENARIOS):
                raise ValueError("only predeclared stress scenarios accepted")
            stress = {}
            for name, item in stress_reports.items():
                summary = metrics_for(item, calendar=calendar, session_days=session_days)
                if (summary["evidence_class"] != label or any(summary[field] != metrics[field]
                        for field in ("evaluated_start", "evaluated_end", "daily_observations"))):
                    raise ValueError("stress comparisons require matching evidence class and valuation windows")
                if summary["net_return"] is not None:
                    stress[name] = summary["net_return"]
            if metrics["net_return"] is not None:
                calculated["execution_robustness"] = diagnostics.robustness_report(stress, base_return=metrics["net_return"], evidence_class=label)
                if calculated["execution_robustness"]["fragile"]:
                    pending.append("observed_stress_fragility_requires_review")
            calculated["stress_assumptions_authenticated"] = False
        if not cost_runs:
            pending.append("cost_sensitivity_reports_missing")
        if set(stress_reports or {}) != set(STRESS_SCENARIOS):
            pending.append("execution_stress_reports_incomplete")
    folds = _walk_forward([] if walk_forward is None else walk_forward, policy, calendar, session_days)
    if metrics and any(r["evidence_class"] in {"TEST", "SYNTHETIC"} for r in folds["folds"]) and metrics["evidence_class"] in {"PAPER", "SHADOW"}:
        failures.append("synthetic_folds_cannot_establish_paper_qualification")
    failures.extend(numeric["failures"] + folds["failures"])
    pending.extend(numeric["pending"] + folds["pending"])
    return {"schema_version": 1, "policy_sha256": identity, "input_sha256": content_hash(bundle),
            "scope": "offline_paper_evidence_assessment", "status": _status(failures, pending),
            "failures": sorted(set(failures)), "pending": sorted(set(pending)),
            "infrastructure": {"engineering": engineering,
                               "authenticated_broker_readiness": "FAIL" if engineering["status"] == "FAIL" else "PARTIAL",
                               "broker_readiness_reason": "no authenticated broker acceptance or origin is established by this assessor"},
            "strategy": {"status": _status([r for r in failures if not r.startswith("infrastructure_")],
                                             [r for r in pending if not r.startswith("infrastructure_")]),
                         "numeric": numeric, "research_reviews": research, "walk_forward": folds,
                         "metrics": metrics, "diagnostics": calculated, "existing_research_gate": gate},
            "origin_authenticated": False, "prospective_qualified": False,
            "paper_execution_authorized": False, "activation_authorized": False, "live_approved": False,
            "limitations": ["Self-consistent hashes and supplied PASS reviews do not authenticate evidence or independent predeclaration.",
                            "Numerical thresholds retain the frozen multiasset floors; added diagnostics do not rewrite the frozen study.",
                            "Round trips, daily observations and walk-forward folds are not established independent samples.",
                            "No sealed evaluator is run; no dataset, model, broker, credential, file or service is opened.",
                            "Legacy binary-payout forward-test acceptance is separate from equity/spot qualification."]}
