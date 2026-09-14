"""Descriptive research gates and causal selection; no broker or file access.

Evidence flags are caller-supplied provenance attestations, not authentication.
Passing these gates permits only a proposed paper allocation, never live orders.
"""

from datetime import datetime, timezone
from decimal import Decimal
import math
import statistics

import numpy as np

from atlas.domain import decimal, financial_context
from atlas.collection.primitives import DEFAULT_THRESHOLDS
from atlas.research import diagnostics
from atlas.research.market import timestamp
from atlas.research.splits import timestamp_folds, validate_fold


DAY = 86400
PAPER_EVIDENCE = {"HISTORICAL_OUT_OF_SAMPLE", "PAPER", "SHADOW"}


def _finite(value):
    return float(diagnostics.finite_series([value])[0])


def _optional(value):
    return None if value is None else _finite(value)


def _round_trips(fills):
    if fills is None:
        return None, None
    if not isinstance(fills, list) or len(fills) > 100_000:
        raise ValueError("fills must be a chronological complete list")
    sleeves, completed, seen, previous_time = {}, [], set(), -1
    for fill in fills:
        when = timestamp(fill["fill_time"])
        key = tuple(fill[k] for k in ("strategy_id", "strategy_version", "symbol"))
        if any(not isinstance(k, str) or not k for k in key):
            raise ValueError("fill sleeve identity missing")
        fill_id = fill["fill_id"]
        if not isinstance(fill_id, str) or fill_id in seen or when < previous_time:
            raise ValueError("duplicate or unordered fill history")
        seen.add(fill_id)
        previous_time = when
        quantity = decimal(fill["quantity"], positive=True)
        state = sleeves.setdefault(key, {"quantity": Decimal(0), "pnl": Decimal(0), "start": when})
        if fill["side"] == "buy":
            if state["quantity"] == 0:
                state.update(pnl=Decimal(0), start=when)
            state["quantity"] += quantity
        elif fill["side"] == "sell":
            if quantity > state["quantity"]:
                raise ValueError("fill history sells missing opening inventory")
            state["quantity"] -= quantity
            state["pnl"] += decimal(fill["realized_pnl"])
            if state["quantity"] == 0:
                completed.append({"strategy_id": key[0], "strategy_version": key[1], "symbol": key[2],
                                  "entry_time": state["start"], "exit_time": when,
                                  "net_pnl": float(state["pnl"]), "holding_seconds": when - state["start"]})
        else:
            raise ValueError("unsupported fill side")
    return completed, sum(s["quantity"] > 0 for s in sleeves.values())


def _daily(curve, calendar, session_days):
    if calendar not in {"UTC_24_7", "UTC_SESSION_DAYS"}:
        raise ValueError("explicit UTC_24_7 or UTC_SESSION_DAYS calendar required")
    days = {}
    for row in curve:
        days[row["timestamp"] // DAY * DAY] = row
    observed_days = sorted(days)
    if calendar == "UTC_SESSION_DAYS":
        if not isinstance(session_days, (list, tuple)) or not session_days:
            raise ValueError("session calendar requires explicit UTC-midnight session_days")
        declared = [timestamp(day) for day in session_days]
        if any(day % DAY for day in declared) or any(b <= a for a, b in zip(declared, declared[1:])):
            raise ValueError("session_days must be unique increasing UTC-midnight timestamps")
        if any(day not in set(declared) for day in observed_days):
            raise ValueError("observation outside supplied session calendar")
        expected = [day for day in declared if observed_days and observed_days[0] <= day <= observed_days[-1]]
        annual = 252.0
    else:
        if session_days is not None:
            raise ValueError("session_days only applies to UTC_SESSION_DAYS")
        if observed_days and (observed_days[-1] - observed_days[0]) // DAY > 100_000:
            raise ValueError("daily calendar exceeds bounded research range")
        expected = list(range(observed_days[0], observed_days[-1] + DAY, DAY)) if observed_days else []
        annual = 365.25
    missing = [day for day in expected if day not in days]
    rows = [{"timestamp": day, "equity": days[day]["equity"],
             "last_observation_time": days[day]["timestamp"]} for day in observed_days]
    return rows, missing, annual


def _recovery(curve, initial):
    peak, peak_time, open_since, recovered = initial, None, None, []
    for row in curve:
        value, when = row["equity"], row["timestamp"]
        if peak_time is None:
            peak_time = when
        if value >= peak:
            if open_since is not None:
                recovered.append(when - open_since)
            peak, peak_time, open_since = value, when, None
        elif open_since is None:
            open_since = peak_time
    return {"recovery_seconds": max(recovered, default=None),
            "completed_recoveries": len(recovered),
            "open_drawdown_duration_seconds": curve[-1]["timestamp"] - open_since if open_since is not None else None,
            "recovery_basis": "observed peak to recovery; unfinished drawdown reported separately; intrainterval path unknown"}


@financial_context
def metrics_for(report, *, calendar="UTC_24_7", session_days=None, initial_capital="100"):
    """Summarize a complete ReplayEngine report using a declared $100 capital.

    Daily marks are the last observed mark in each UTC date. Missing dates are
    never filled. Session annualization needs the complete supplied calendar;
    it does not infer holidays from the data. Trade statistics count a sleeve
    only when inventory returns to zero, including all partial exits in it.
    """
    if not isinstance(report, dict) or decimal(initial_capital, positive=True) != 100:
        raise ValueError("multiasset validation requires a report and declared $100 capital")
    initial = 100.0
    source_curve = report.get("equity_curve", [])
    if not isinstance(source_curve, list) or len(source_curve) > 100_000:
        raise ValueError("equity history exceeds bounded research observations")
    curve = [{"timestamp": timestamp(row.get("timestamp", row.get("event_time"))),
              "equity": _finite(row["equity"])} for row in source_curve]
    diagnostics.return_series(curve)  # Reject duplicate/rewound times and invalid capital.
    daily, missing, annual = _daily(curve, calendar, session_days)
    returns = diagnostics.return_series(daily) if daily and not missing else []
    evidence_class = report.get("evidence_class", "UNKNOWN")
    end_equity = _optional(report.get("portfolio", {}).get("equity"))
    if end_equity is None and curve:
        end_equity = curve[-1]["equity"]
    if end_equity is not None and (end_equity <= 0 or (curve and not math.isclose(end_equity, curve[-1]["equity"], rel_tol=1e-10, abs_tol=1e-8))):
        raise ValueError("terminal portfolio and equity observations disagree")
    if report.get("initial_cash") is not None and decimal(report["initial_cash"]) != 100:
        raise ValueError("report capital differs from declared $100")
    reported_net = _optional(report.get("metrics", {}).get("net_return"))
    net = end_equity / initial - 1 if end_equity is not None else None
    if reported_net is not None and net is not None and not math.isclose(reported_net, net, rel_tol=1e-8, abs_tol=1e-8):
        raise ValueError("report net return does not reconcile with declared $100 capital")
    peak, max_dd = initial, 0.0
    for row in curve:
        peak = max(peak, row["equity"])
        max_dd = max(max_dd, 1 - row["equity"] / peak)
    trips, open_sleeves = _round_trips(report.get("fills"))
    if report.get("fills") and (not curve or any(not curve[0]["timestamp"] <= fill["fill_time"] <= curve[-1]["timestamp"] for fill in report["fills"])):
        raise ValueError("fill statistics contain observations outside the equity evaluation window")
    pnl = [row["net_pnl"] for row in trips] if trips is not None else []
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    eq = report.get("execution_quality", {})
    costs = {name: _optional(eq.get(field)) for name, field in (
        ("fees", "fees"), ("spread", "spread_cost"), ("slippage", "slippage_cost"),
        ("implementation_shortfall", "implementation_shortfall"))}
    turnover_notional = None
    if report.get("fills") is not None:
        turnover_notional = sum(_finite(f["quantity"]) * _finite(f["fill_price"]) for f in report["fills"])
    mean_equity = statistics.mean(row["equity"] for row in daily) if daily else None
    result = {
        "evidence_class": evidence_class, "report_complete": report.get("complete") is True,
        "terminal_valuation_fresh": report.get("portfolio", {}).get("stale_symbols") == [],
        "initial_capital_usd": initial, "initial_capital_basis": "caller-declared $100; no deposits, withdrawals or cash interest",
        "evaluated_start": curve[0]["timestamp"] if curve else None,
        "evaluated_end": curve[-1]["timestamp"] if curve else None,
        "net_return": net, "net_pnl": end_equity - initial if end_equity is not None else None,
        "cash_baseline_return": 0.0, "cash_baseline_basis": "uninvested $100 with explicitly zero interest and no cash flows",
        "benchmark_excess_return": None, "max_drawdown": max_dd if curve else None,
        "cagr": None, "sharpe": None, "sortino": None, "calmar": None, "volatility": None,
        "annualization_available": False, "periods_per_year": annual, "risk_free_rate_assumption": 0.0,
        "daily_observations": len(daily), "daily_return_observations": len(returns),
        "daily_curve": daily, "daily_returns": returns, "calendar": calendar,
        "calendar_complete": bool(daily) and not missing, "missing_days": missing,
        "daily_sampling_basis": "last observed mark per UTC day; first/last buckets may be partial; no missing-day forward fill",
        "session_calendar_verified": False if calendar == "UTC_SESSION_DAYS" else None,
        "completed_round_trips": len(trips) if trips is not None else None,
        "round_trips": trips, "open_sleeves": open_sleeves,
        "trade_count_basis": "flat-to-flat strategy/version/symbol sleeve episodes; partial sells are not independent trades",
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "profit_factor_reason": None if losses else "no completed loss denominator",
        "expectancy": statistics.mean(pnl) if pnl else None,
        "expectancy_per_trade": statistics.mean(pnl) if pnl else None,
        "turnover_notional": turnover_notional,
        "turnover": turnover_notional / mean_equity if turnover_notional is not None and mean_equity else None,
        "turnover_basis": "gross fill notional divided by mean observed daily equity",
        "costs": costs, "fees": costs["fees"], "slippage": costs["slippage"],
        "total_costs": sum(costs[k] for k in ("fees", "spread", "slippage")) if all(costs[k] is not None for k in ("fees", "spread", "slippage")) else None,
        "costs_basis": "fees plus spread/slippage estimates; implementation shortfall separately reported, not double counted",
        "worst_day": min(returns, default=None), "worst_month": None, "monthly_returns": {},
        "live_approved": False,
    }
    result.update(_recovery(curve, initial))
    if len(returns) >= 30:
        standard = statistics.stdev(returns)
        downside = math.sqrt(statistics.mean(min(x, 0) ** 2 for x in returns))
        mean = statistics.mean(returns)
        elapsed = curve[-1]["timestamp"] - curve[0]["timestamp"]
        log_growth = math.log1p(net) * (365.25 * DAY / elapsed) if elapsed > 0 else None
        cagr = math.expm1(log_growth) if log_growth is not None and -700 < log_growth < 700 else None
        result.update(cagr=cagr, volatility=standard * math.sqrt(annual),
                      sharpe=mean / standard * math.sqrt(annual) if standard else None,
                      sortino=mean / downside * math.sqrt(annual) if downside else None,
                      calmar=cagr / max_dd if cagr is not None and max_dd > 0 else None,
                      annualization_available=True)
    if returns:
        monthly = {}
        for row, value in zip(daily[1:], returns):
            month = datetime.fromtimestamp(row["timestamp"], timezone.utc).strftime("%Y-%m")
            monthly[month] = monthly.get(month, 0.0) + math.log1p(value)
        result["monthly_returns"] = {key: math.expm1(value) for key, value in monthly.items()}
        result["worst_month"] = min(result["monthly_returns"].values(), default=None)
    result["monthly_basis"] = "compounded regular daily returns by ending UTC month; partial months retained"
    result["annualization_basis"] = "daily-return moments with declared calendar; CAGR uses actual elapsed UTC seconds from first valuation"
    return result


def _thresholds(value):
    if value is None:
        return dict(DEFAULT_THRESHOLDS)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_THRESHOLDS):
        raise ValueError("unknown validation threshold")
    result = {**DEFAULT_THRESHOLDS, **value}
    for name, number in result.items():
        number = _finite(number)
        if name in {"min_completed_round_trips", "min_daily_observations"} and (type(result[name]) is not int):
            raise ValueError("minimum sample thresholds must be integers")
        if (name == "max_drawdown" and not 0 < number <= .10) or (name != "max_drawdown" and number < DEFAULT_THRESHOLDS[name]):
            raise ValueError("thresholds cannot weaken the strict research floor")
    return result


def _metric_reasons(metrics, limits):
    reasons = []
    if metrics.get("report_complete") is not True:
        reasons.append("incomplete_report")
    if metrics.get("calendar_complete") is not True:
        reasons.append("daily_calendar_incomplete")
    if metrics.get("terminal_valuation_fresh") is not True:
        reasons.append("terminal_valuation_freshness_unverified")
    checks = (
        ("completed_round_trips", "min_completed_round_trips", "min"),
        ("daily_observations", "min_daily_observations", "min"),
        ("max_drawdown", "max_drawdown", "max"), ("sharpe", "min_sharpe", "min"),
        ("profit_factor", "min_profit_factor", "min"), ("net_return", "min_net_return", "positive"),
    )
    for name, limit, op in checks:
        try:
            number = _optional(metrics.get(name))
        except ValueError:
            number = None
        if name in {"completed_round_trips", "daily_observations"} and type(metrics.get(name)) is not int:
            number = None
        if name == "max_drawdown" and number is not None and not 0 <= number <= 1:
            number = None
        if number is None:
            reasons.append(name + "_unavailable")
        elif (op == "min" and number < limits[limit]) or (op == "max" and number > limits[limit]) or (op == "positive" and number <= limits[limit]):
            reasons.append(name + "_threshold_failed")
    for name in ("fees", "spread", "slippage"):
        try:
            cost = _optional(metrics.get("costs", {}).get(name))
        except ValueError:
            cost = None
        if cost is None or cost < 0:
            reasons.append("execution_costs_unavailable")
            break
    return reasons


def gate_result(metrics, thresholds=None, evidence=None):
    """Fail closed on missing metrics or unverified provenance; paper only."""
    limits, evidence = _thresholds(thresholds), evidence or {}
    reasons = _metric_reasons(metrics, limits)
    label = evidence.get("evidence_class")
    if label not in PAPER_EVIDENCE or metrics.get("evidence_class") != label:
        reasons.append("evidence_not_verified_out_of_sample")
    for field in ("provenance_verified", "point_in_time_verified", "out_of_sample"):
        if evidence.get(field) is not True:
            reasons.append(field + "_required")
    if metrics.get("calendar") == "UTC_SESSION_DAYS" and evidence.get("session_calendar_verified") is not True:
        reasons.append("session_calendar_verification_required")
    eligible = not reasons
    score = _finite(metrics["sharpe"]) / (1 + 10 * _finite(metrics["max_drawdown"])) if eligible else None
    return {"eligible": eligible, "reasons": reasons, "score": score, "thresholds": limits,
            "allocation_scope": "paper_only", "live_approved": False,
            "evidence_authentication": "caller attestations; this function cannot authenticate market provenance"}


def schedule_folds(samples, *, train_seconds, validation_seconds, test_seconds,
                   embargo_seconds, label_horizon_seconds=None, step_seconds=None,
                   mode="anchored", holdout_start=None):
    """Wrap timestamp_folds, requiring explicit future horizon for unlabeled bars.

    A multiasset timestamp group may contain multiple rows. Indices refer to
    the original input; sealed holdout target fields are never inspected.
    """
    if label_horizon_seconds is not None and (type(label_horizon_seconds) is not int or label_horizon_seconds <= 0):
        raise ValueError("label horizon must be positive integer seconds")
    normalized = []
    for index, source in enumerate(samples):
        row = dict(source) if isinstance(source, dict) else {"event_time": timestamp(source)}
        event = timestamp(row.get("event_time", row.get("timestamp")))
        row["event_time"] = event
        if holdout_start is not None and event >= holdout_start:
            normalized.append({"event_time": event})
            continue
        row.setdefault("information_available_time", event)
        row.setdefault("sample_id", (row.get("symbol", "sample"), event))
        if "label_end_time" not in row:
            if label_horizon_seconds is None:
                raise ValueError("unlabeled input requires explicit label_horizon_seconds")
            row["label_end_time"] = row["information_available_time"] + label_horizon_seconds
            row["label_available_time"] = row["label_end_time"]
        normalized.append(row)
    folds = timestamp_folds(normalized, train_seconds=train_seconds, validation_seconds=validation_seconds,
                            test_seconds=test_seconds, step_seconds=step_seconds, mode=mode,
                            embargo_seconds=embargo_seconds, holdout_start=holdout_start)
    for fold in folds:
        validate_fold(normalized, fold, holdout_start=holdout_start)
        fold["label_horizon_seconds"] = label_horizon_seconds
        fold["purge_basis"] = "feature and outcome publication strictly before each window end"
    return folds


def _candidate_identity(candidate, seen):
    name = candidate.get("candidate_id")
    if not isinstance(name, str) or not name or len(name) > 200 or name in seen:
        raise ValueError("candidate IDs must be unique nonempty strings")
    seen.add(name)
    return name


def rank_candidates(candidates, *, selection_time, thresholds=None):
    """Maximize earlier-split CAGR subject to numerical floors, never test data.

    Numerical qualification here is descriptive, including for synthetic data.
    Final paper promotion still requires gate_result's full evidence checks.
    """
    selection_time = timestamp(selection_time)
    limits = _thresholds(thresholds)
    ranked, seen = [], set()
    for candidate in candidates:
        name = _candidate_identity(candidate, seen)
        if candidate.get("split") not in {"train", "validation"}:
            raise ValueError("only training or validation may select a candidate")
        end = timestamp(candidate["evaluated_end"])
        metrics = candidate["metrics"]
        actual_end = timestamp(metrics["evaluated_end"])
        if actual_end > end or end >= selection_time:
            raise ValueError("candidate contains current or future selection information")
        reasons = _metric_reasons(metrics, limits)
        score = _optional(metrics.get("cagr"))
        if score is None:
            reasons.append("cagr_unavailable")
        if reasons:
            score = None
        ranked.append({"candidate_id": name, "selection_split": candidate["split"],
                       "selected_at": selection_time, "evaluated_end": end,
                       "selection_score": score, "selection_qualified": not reasons,
                       "selection_reasons": reasons,
                       "selection_score_basis": "maximize earlier training/validation CAGR subject to strict numeric gates",
                       "paper_eligible": False, "live_approved": False})
    return sorted(ranked, key=lambda row: (row["selection_score"] is None,
                                          -(row["selection_score"] or 0), row["candidate_id"]))


def pre_cutoff(candidates, *, allocation_time, thresholds=None):
    """Filter a predeclared ranking with *completed earlier* OOS evidence.

    Input order is retained. Test performance never re-ranks candidates. A
    selection must precede that test's first valuation, and its last valuation
    and complete evaluation window must both precede allocation time.
    """
    allocation_time = timestamp(allocation_time)
    eligible, checks, seen = [], [], set()
    for candidate in candidates:
        name = _candidate_identity(candidate, seen)
        selected = timestamp(candidate["selected_at"])
        start, end = timestamp(candidate["evaluated_start"]), timestamp(candidate["evaluated_end"])
        metrics = candidate["metrics"]
        actual_start, actual_end = timestamp(metrics["evaluated_start"]), timestamp(metrics["evaluated_end"])
        if candidate.get("selection_split") not in {"train", "validation"} or not selected < start <= actual_start <= actual_end <= end < allocation_time:
            raise ValueError("paper selection requires pre-test choice and a completed earlier OOS window")
        gate = gate_result(metrics, thresholds, candidate.get("evidence"))
        checks.append({"candidate_id": name, "evaluated_end": end, **gate})
        if gate["eligible"]:
            eligible.append(name)
    return {"allocation_time": allocation_time, "eligible_ids": eligible, "checks": checks,
            "selection_basis": "predeclared input ranking filtered by strictly earlier completed OOS gates",
            "current_test_used_for_selection": False, "live_approved": False}


def _trade_order_monte_carlo(metrics, *, seed, resamples):
    trips = metrics["round_trips"]
    if trips is None or len(trips) < 30:
        return {"available": False, "reason": "at least 30 completed flat-to-flat round trips required",
                "evidence_class": metrics["evidence_class"]}
    if len(trips) * resamples > 20_000_000:
        raise ValueError("trade-order stress exceeds bounded research workload")
    pnls = diagnostics.finite_series([trip["net_pnl"] for trip in trips])
    rng = np.random.default_rng(seed)
    drawdowns, ruins, terminal_returns = [], [], []
    for _ in range(resamples):
        capital, peak, worst, ruined = 100.0, 100.0, 0.0, False
        for pnl in rng.permutation(pnls):
            capital = max(0.0, capital + float(pnl))
            peak = max(peak, capital)
            worst = max(worst, 1 - capital / peak)
            if capital == 0:
                ruined = True
                break
        drawdowns.append(worst)
        ruins.append(ruined)
        terminal_returns.append(capital / 100 - 1)
    percentiles = lambda values: {str(p): float(np.percentile(values, p)) for p in (5, 25, 50, 75, 95)}
    return {"available": True, "evidence_class": metrics["evidence_class"],
            "method": "permutation_of_completed_round_trip_net_dollar_pnl", "seed": seed,
            "resamples": resamples, "completed_round_trips": len(trips), "initial_capital_usd": 100.0,
            "drawdown_percentiles": percentiles(drawdowns), "return_percentiles": percentiles(terminal_returns),
            "conditional_ruin_fraction": sum(ruins) / resamples,
            "limitations": ["Order permutation destroys chronology/dependence; it is not a future probability forecast.",
                            "Fixed observed net dollar P&Ls, no dynamic resizing, open inventory or overlapping sleeve chronology.",
                            "Scenarios stop at zero capital; without ruin total P&L is invariant to ordering."]}


def stress_diagnostics(report, *, candidate_outputs=None, seed=0, resamples=500, block_size=5,
                       calendar="UTC_24_7", session_days=None):
    """Conditional block-bootstrap stress plus supplied, unranked perturbations.

    This never generates, tunes or selects parameters. Future/test variants
    may be described after the fact but are never selection evidence here.
    """
    metrics = metrics_for(report, calendar=calendar, session_days=session_days)
    bootstrap = diagnostics.block_bootstrap(metrics["daily_returns"], seed=seed, resamples=resamples,
        block_size=block_size, evidence_class=metrics["evidence_class"], drawdown_threshold=.10)
    sensitivity = {}
    for name, output in (candidate_outputs or {}).items():
        if not isinstance(name, str) or not name:
            raise ValueError("parameter variant names must be nonempty strings")
        summary = metrics_for(output, calendar=calendar, session_days=session_days)
        if (summary["evaluated_start"], summary["evaluated_end"]) != (metrics["evaluated_start"], metrics["evaluated_end"]):
            raise ValueError("parameter diagnostics require the same evaluation window")
        sensitivity[name] = {key: summary[key] for key in (
            "net_return", "sharpe", "max_drawdown", "profit_factor", "completed_round_trips", "evidence_class")}
    trade_order = _trade_order_monte_carlo(metrics, seed=seed, resamples=resamples)
    return {"block_bootstrap": bootstrap, "monte_carlo": trade_order,
            "parameter_sensitivity": sensitivity, "parameters_optimized": False,
            "selection_eligible": False, "live_approved": False,
            "limitations": ["Conditional empirical block resampling is not a future probability forecast.",
                            "Supplied variants are descriptive only; no test-window parameter optimization or ranking.",
                            "Daily calendar gaps make bootstrap observations unavailable; missing marks are never invented."]}
