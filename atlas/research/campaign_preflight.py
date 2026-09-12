"""Bounded research workload and sample planning, never performance evidence.

Counts are upper bounds: unavailable inputs can reduce executed work. A campaign
receipt cannot certify the number of hypotheses tried elsewhere or make reused
historical data unseen. The existing validation gate still checks actual data.
"""

from atlas.research.multiasset_strategies import normalize_spec
from atlas.research.multiasset_validation import _thresholds


DAY = 86400
REPLAY_STAGES = (
    "train", "validation", "test", "double_cost", "latency", "entry_delay", "downtime"
)


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
        if type(seconds) is not int or not 0 < seconds <= 20 * 366 * DAY:
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
