"""Bounded research workload and sample planning, never performance evidence.

Counts are upper bounds: unavailable inputs can reduce executed work. A campaign
receipt cannot certify the number of hypotheses tried elsewhere or make reused
historical data unseen. The existing validation gate still checks actual data.
"""

import json

from atlas.research.multiasset_strategies import normalize_spec
from atlas.research.multiasset_validation import _thresholds


DAY = 86400
MAX_PLAN_SECONDS = 20 * 366 * DAY
_FIXED_FIELDS = {
    "version": "multiasset-campaign-v1",
    "initial_capital_usd": "100",
    "objective": "maximum_validation_CAGR_subject_to_fixed_numeric_constraints",
    "allocation": "only_prior_completed_verified_OOS_survivors; never_current_test_ranking",
    "account_scope": "local_synthetic_100_USD_only",
    "live_approved": False,
    "risk_fractions": [".0025", ".005", ".01"],
    "cost_multipliers": ["1", "2"],
}
_REQUIRED_FIELDS = set(_FIXED_FIELDS) | {
    "strategy_specs", "train_seconds", "validation_seconds", "test_seconds",
    "embargo_seconds", "label_horizon_seconds", "max_folds", "thresholds",
    "bootstrap_resamples", "seed", "latency_stress_seconds",
}
_OPTIONAL_FIELDS = {"research_purpose", "max_replay_evaluations"}
REPLAY_STAGES = (
    "train", "validation", "test", "double_cost", "latency", "entry_delay", "downtime"
)


def _native_json(value):
    """Reject coercions that would make a copied plan differ from its input."""
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("campaign plan JSON object keys must be strings")
        for item in value.values():
            _native_json(item)
    elif type(value) is list:
        for item in value:
            _native_json(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("campaign plan requires native JSON values")


def validate_campaign_plan(plan):
    """Validate the complete execution contract without I/O or normalization.

    Returns the same workload/sample receipt as ``campaign_preflight``. Existing
    optional purpose/budget defaults and stricter threshold configurations are
    preserved. Timing limits share the existing bounded split-duration domain;
    seed/bootstrap bounds match the downstream diagnostics. Nothing here grants
    qualification or changes the frozen hypothesis or validation definitions.
    """
    if type(plan) is not dict:
        raise ValueError("campaign plan must be a mapping")
    try:
        _native_json(plan)
        json.dumps(plan, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("campaign plan requires finite native JSON") from None
    missing = _REQUIRED_FIELDS - set(plan)
    unknown = set(plan) - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
    if missing:
        raise ValueError("campaign plan missing fields: " + ", ".join(sorted(missing)))
    if unknown:
        raise ValueError("campaign plan unknown fields: " + ", ".join(sorted(unknown)))
    for key, expected in _FIXED_FIELDS.items():
        if type(plan[key]) is not type(expected) or plan[key] != expected:
            raise ValueError("fixed campaign field required: " + key + "; synthetic capital and no live authority")
    for key, lower in (("embargo_seconds", 0), ("label_horizon_seconds", 1),
                       ("latency_stress_seconds", 0)):
        if type(plan[key]) is not int or not lower <= plan[key] <= MAX_PLAN_SECONDS:
            raise ValueError(f"{key} must be integer seconds from {lower} through {MAX_PLAN_SECONDS}")
    if type(plan["seed"]) is not int or not 0 <= plan["seed"] < 2 ** 64:
        raise ValueError("seed must be an integer from 0 through 2**64 - 1")
    if type(plan["bootstrap_resamples"]) is not int or not 100 <= plan["bootstrap_resamples"] <= 5000:
        raise ValueError("bootstrap_resamples must be an integer from 100 through 5000")
    try:
        return campaign_preflight(plan)
    except (TypeError, KeyError, OverflowError):
        raise ValueError("campaign plan contains invalid execution fields") from None


def campaign_preflight(plan):
    """Reject excessive work and impossible qualification plans before any run.

    Qualification requires a conservative elapsed-day planning minimum in all
    three splits. This is not a guarantee of 60 exchange sessions or 30 completed
    trades, let alone statistical power. Short diagnostic fixtures remain valid.
    """
    if not isinstance(plan, dict):
        raise ValueError("campaign plan must be a mapping")
    purpose = plan.get("research_purpose", "diagnostic")
    if purpose not in {"diagnostic", "qualification"}:
        raise ValueError("explicit diagnostic or qualification purpose required")
    specs = plan.get("strategy_specs")
    if not isinstance(specs, list) or not 1 <= len(specs) <= 100:
        raise ValueError("bounded hypothesis list required")
    identities = [normalize_spec(spec)["strategy_id"] for spec in specs]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate hypotheses")
    if plan.get("risk_fractions") != [".0025", ".005", ".01"]:
        raise ValueError("fixed three-risk research grid required")
    folds = plan.get("max_folds")
    if type(folds) is not int or not 1 <= folds <= 10:
        raise ValueError("fold limit must be an integer from 1 through 10")
    budget = plan.get("max_replay_evaluations", 5000)
    if type(budget) is not int or not 1 <= budget <= 21000:
        raise ValueError("replay budget must be an integer from 1 through 21000")
    variants = len(identities) * 3
    windows = variants * folds
    # Benchmarks and the cash ledger are included: five per fold plus one final.
    strategy_replays = windows * len(REPLAY_STAGES)
    benchmark_replays = 5 * folds
    total_replays = strategy_replays + benchmark_replays + 1
    if total_replays > budget:
        raise ValueError(f"declared replay workload {total_replays} exceeds budget {budget}")
    thresholds = _thresholds(plan.get("thresholds"))
    required_days = thresholds["min_daily_observations"]
    splits, warnings = {}, []
    for split in ("train", "validation", "test"):
        seconds = plan.get(split + "_seconds")
        if type(seconds) is not int or not 0 < seconds <= MAX_PLAN_SECONDS:
            raise ValueError("positive bounded integer split seconds required")
        # With unknown start phase, a half-open interval may touch an extra UTC
        # date. This is an optimistic ceiling, never a session/trade estimate.
        maximum_dates = (seconds - 1 + DAY - 1) // DAY + 1
        sufficient_elapsed = seconds >= required_days * DAY
        splits[split] = {
            "elapsed_seconds": seconds,
            "maximum_utc_dates": maximum_dates,
            "required_daily_observations": required_days,
            "elapsed_planning_minimum_met": sufficient_elapsed,
            "actual_session_count": None,
            "actual_completed_trades": None,
        }
        if maximum_dates < required_days:
            warnings.append(split + "_cannot_supply_required_daily_observations")
        if purpose == "qualification" and not sufficient_elapsed:
            raise ValueError(f"{split} is shorter than the qualification planning minimum of {required_days} elapsed days")
    return {
        "schema_version": 1, "research_purpose": purpose,
        "hypotheses": len(identities), "hypothesis_risk_variants": variants,
        "maximum_candidate_windows": windows,
        "replay_stages": list(REPLAY_STAGES),
        "maximum_strategy_replays": strategy_replays,
        "maximum_benchmark_replays": benchmark_replays,
        "maximum_cash_ledger_replays": 1,
        "maximum_total_replays": total_replays,
        "max_replay_evaluations": budget,
        "splits": splits, "warnings": warnings,
        "outside_campaign_attempt_history": "unknown",
        "complete_historical_multiple_testing_correction": False,
        "qualification_established": False, "live_approved": False,
        "limitations": [
            "Folds, risk variants and stress reruns are dependent comparisons, not independent trades.",
            "Elapsed days do not guarantee complete exchange sessions, completed trades or statistical power.",
            "Previously inspected history remains retrospective even when a new plan is frozen.",
            "Replay counts bound runs, not CPU time, bar count, bootstrap resamples or total memory.",
        ],
    }
