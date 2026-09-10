"""Deterministic attempts to disprove research results, not investment forecasts."""

from datetime import datetime, timezone
import math
import statistics

import numpy as np


VERSION = "research-diagnostics-1"


def finite_series(values):
    values = list(values)
    if any(isinstance(x, bool) for x in values):
        raise ValueError("booleans are not observations")
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("observations must be finite numeric values") from None
    if array.ndim != 1 or not np.isfinite(array).all():
        raise ValueError("observations must be a finite one-dimensional series")
    return array


def return_series(curve):
    if not curve:
        return []
    times = [row.get("timestamp", row.get("event_time")) for row in curve]
    if any(type(t) is not int or not 0 <= t <= 253402300799 for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("equity observations require unique chronological integer times")
    values = finite_series([row["equity"] for row in curve])
    if np.any(values <= 0):
        raise ValueError("positive equity required for percentage returns")
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            result = values[1:] / values[:-1] - 1
    except FloatingPointError:
        raise ValueError("equity return exceeds supported numeric range") from None
    if np.any(result <= -1):
        raise ValueError("equity return underflows supported numeric range")
    return finite_series(result).tolist()


def drawdown_analysis(curve, *, evidence_class):
    if not curve:
        return {"available": False, "evidence_class": evidence_class, "reason": "equity history missing"}
    return_series(curve)
    equity = finite_series([row["equity"] for row in curve])
    times = [row.get("timestamp", row.get("event_time")) for row in curve]
    peaks = np.maximum.accumulate(equity)
    drawdowns = 1 - equity / peaks
    underwater, began, last_peak = [], None, times[0]
    for t, dd in zip(times, drawdowns):
        if dd > 1e-12:
            if began is None:
                began = last_peak
        elif began is not None:
            underwater.append(t - began)
            began = None
            last_peak = t
        else:
            last_peak = t
    if began is not None:
        underwater.append(times[-1] - began)
    elapsed = times[-1] - times[0]
    weighted = sum(b - a for a, b, dd in zip(times, times[1:], drawdowns) if dd > 1e-12)
    return {"available": True, "evidence_class": evidence_class,
            "max_drawdown": float(max(drawdowns)), "average_drawdown": float(np.mean(drawdowns)),
            "time_underwater_fraction": weighted / elapsed if elapsed else None,
            "underwater_observation_fraction": float(np.mean(drawdowns > 1e-12)),
            "observed_underwater_seconds": weighted,
            "time_fraction_basis": "elapsed seconds using left-continuous observed drawdown state; intrainterval path unknown",
            "longest_observed_underwater_seconds": max(underwater, default=0),
            "duration_basis": "last observed peak to recovery or sample end; open spells are censored",
            "open_drawdown": bool(drawdowns[-1] > 1e-12),
            "observations": len(curve), "basis": "observed valuation events; intrabar extremes unavailable"}


def path_metrics(curve, *, evidence_class, periods_per_year=None):
    returns = finite_series(return_series(curve))
    values = finite_series([row["equity"] for row in curve])
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        try:
            total = float(values[-1] / values[0] - 1) if len(values) else None
        except FloatingPointError:
            raise ValueError("total return exceeds supported numeric range") from None
    result = {"evidence_class": evidence_class, "net_return": total, "return_observations": len(returns),
              "annualized_return": None, "cagr": None, "volatility": None,
              "sharpe": None, "sortino": None, "calmar": None,
              "annualization_available": False, "risk_free_rate_assumption": 0.0}
    result.update(drawdown_analysis(curve, evidence_class=evidence_class))
    times = [row.get("timestamp", row.get("event_time")) for row in curve]
    steps = [b - a for a, b in zip(times, times[1:])]
    if periods_per_year is not None:
        if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, (int, float)) or not math.isfinite(periods_per_year) or not 0 < periods_per_year <= 365.25 * 86400:
            raise ValueError("annualization convention must be explicit and positive")
    if len(returns) >= 30 and steps and len(set(steps)) == 1 and periods_per_year:
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                std = float(np.std(returns, ddof=1))
                downside = float(np.sqrt(np.mean(np.minimum(returns, 0) ** 2)))
                log_growth = float(np.log1p(returns).sum()) * periods_per_year / len(returns)
        except FloatingPointError:
            raise ValueError("annualized moments exceed supported numeric range") from None
        growth = math.expm1(log_growth) if -700 < log_growth < 700 else None
        result.update(annualization_available=True, annualized_return=growth, cagr=growth,
                      volatility=std * math.sqrt(periods_per_year),
                      sharpe=float(np.mean(returns)) / std * math.sqrt(periods_per_year) if std else None,
                      sortino=float(np.mean(returns)) / downside * math.sqrt(periods_per_year) if downside else None,
                      calmar=growth / result["max_drawdown"] if growth is not None and result["max_drawdown"] else None)
        if any(value is not None and not math.isfinite(value) for key, value in result.items()
               if key in {"annualized_return", "cagr", "volatility", "sharpe", "sortino", "calmar"}):
            raise ValueError("annualized metrics exceed supported numeric range")
    result["annualization_limit"] = "Requires 30 regular observations and explicit periods/year; not a power guarantee."
    months = {}
    for before, after in zip(curve, curve[1:]):
        timestamp = after.get("timestamp", after.get("event_time"))
        key = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m")
        # Sum logs rather than multiply a potentially overflowing path.
        months[key] = months.get(key, 0.0) + math.log(float(after["equity"])) - math.log(float(before["equity"]))
    if any(not -700 < value < 700 for value in months.values()):
        raise ValueError("monthly return exceeds supported numeric range")
    monthly = {key: math.expm1(value) for key, value in months.items()}
    result.update(monthly_returns=monthly, best_month=max(monthly.values(), default=None),
                  worst_month=min(monthly.values(), default=None),
                  monthly_basis="observed returns grouped by UTC valuation month; partial months retained")
    return result


def _bootstrap_settings(seed, resamples, block_size, observations, variants=1):
    if any(type(value) is not int for value in (seed, resamples, block_size)):
        raise ValueError("bootstrap settings must be integers")
    if not 0 <= seed < 2 ** 64 or not 100 <= resamples <= 5000 or not 1 <= block_size <= 100:
        raise ValueError("bootstrap computation outside bounded settings")
    if observations * resamples * variants > 20_000_000:
        raise ValueError("bootstrap workload exceeds bounded research budget")


def block_bootstrap(returns, *, seed=0, resamples=500, block_size=5, evidence_class="SYNTHETIC",
                    drawdown_threshold=.20, ruin_equity_fraction=.5):
    """Circular moving-block bootstrap; conditional stress distribution only.

    Contiguous blocks preserve some serial dependence. Stationarity and block
    choice remain assumptions; these are not calibrated future loss odds.
    """
    x = finite_series(returns)
    _bootstrap_settings(seed, resamples, block_size, len(x))
    drawdown_threshold, ruin_equity_fraction = finite_series([drawdown_threshold, ruin_equity_fraction])
    if not 0 < drawdown_threshold < 1 or not 0 < ruin_equity_fraction < 1:
        raise ValueError("explicit risk thresholds must lie between zero and one")
    if np.any(x <= -1):
        raise ValueError("returns cannot destroy more than all unlevered equity")
    if len(x) < max(40, 8 * block_size):
        return {"available": False, "evidence_class": evidence_class,
                "reason": "fewer than 40 returns or eight blocks; uncertainty unavailable"}
    rng = np.random.default_rng(seed)
    totals, drawdowns, means, underwater = [], [], [], []
    for _ in range(resamples):
        starts = rng.integers(0, len(x), size=math.ceil(len(x) / block_size))
        indices = np.concatenate([(start + np.arange(block_size)) % len(x) for start in starts])[:len(x)]
        sample = x[indices]
        log_path = np.concatenate(([0.0], np.cumsum(np.log1p(sample))))
        if not np.isfinite(log_path).all() or np.any(np.abs(log_path) >= 700):
            return {"available": False, "evidence_class": evidence_class,
                    "reason": "resampled capital path exceeds supported numeric range; no scenario was silently discarded"}
        dd = 1 - np.exp(log_path - np.maximum.accumulate(log_path))
        totals.append(float(np.expm1(log_path[-1])))
        drawdowns.append(float(max(dd)))
        means.append(float(np.mean(sample)))
        underwater.append(float(np.mean(dd > 1e-12)))
    percentiles = lambda a: {str(p): float(np.percentile(a, p)) for p in (5, 25, 50, 75, 95)}
    return {"available": True, "evidence_class": evidence_class, "method": "circular_moving_block_bootstrap",
            "seed": seed, "resamples": resamples, "block_size": block_size, "observations": len(x),
            "return_percentiles": percentiles(totals), "drawdown_percentiles": percentiles(drawdowns),
            "mean_return_interval_95": [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))],
            "conditional_loss_fraction": float(np.mean(np.asarray(totals) < 0)),
            "conditional_drawdown_exceedance_fraction": float(np.mean(np.asarray(drawdowns) > drawdown_threshold)),
            "drawdown_threshold": float(drawdown_threshold),
            "conditional_terminal_ruin_fraction": float(np.mean(1 + np.asarray(totals) < ruin_equity_fraction)),
            "ruin_equity_fraction": float(ruin_equity_fraction), "time_underwater_percentiles": percentiles(underwater),
            "limitations": ["Empirical resampling conditional on these observations, not future probabilities.",
                            "Stationarity, block length and omitted market regimes affect coverage.",
                            "Terminal capital threshold differs from first-passage ruin."]}


def multiple_testing(differentials, *, attempts, seed=0, resamples=500, block_size=5, evidence_class="SYNTHETIC"):
    """Joint centered block-bootstrap maximum-mean diagnostic versus a baseline.

    This is a White-style concept diagnostic, not a certified implementation
    of a named test or a deflated Sharpe estimate. Complete attempted-family
    coverage is required before interpreting family selection protection.
    """
    if type(attempts) is not int or attempts < len(differentials) or attempts < 1:
        raise ValueError("attempt count must cover all supplied variants")
    _bootstrap_settings(seed, resamples, block_size, 0)
    if not differentials:
        return {"available": False, "evidence_class": evidence_class, "attempted_variants": attempts}
    keys = sorted(differentials)
    columns = [finite_series(differentials[key]) for key in keys]
    if len({len(x) for x in columns}) != 1:
        raise ValueError("joint comparisons require the exact same observations")
    n = len(columns[0])
    _bootstrap_settings(seed, resamples, block_size, n, len(columns))
    if n < max(40, 8 * block_size):
        return {"available": False, "evidence_class": evidence_class, "attempted_variants": attempts,
                "reason": "insufficient dependent observations", "deflated_sharpe": None,
                "probabilistic_sharpe": None}
    matrix = np.column_stack(columns)
    # Positive common scaling leaves maximum-mean exceedance p-values
    # unchanged and keeps extreme but finite input from overflowing sums.
    scale = float(np.max(np.abs(matrix)))
    if scale:
        matrix = matrix / scale
    observed = np.mean(matrix, axis=0)
    centered = matrix - observed
    rng = np.random.default_rng(seed)
    max_means = []
    for _ in range(resamples):
        starts = rng.integers(0, n, size=math.ceil(n / block_size))
        indices = np.concatenate([(s + np.arange(block_size)) % n for s in starts])[:n]
        max_means.append(float(np.max(np.mean(centered[indices], axis=0))))
    pvalues = {key: (1 + sum(value >= observed[i] for value in max_means)) / (resamples + 1)
               for i, key in enumerate(keys)}
    return {"available": True, "evidence_class": evidence_class, "attempted_variants": attempts,
            "included_variants": len(keys), "complete_family": attempts == len(keys),
            "selection_adjusted_diagnostic_pvalues": pvalues,
            "any_positive_variant": bool(np.any(observed > 0)),
            "nominal_expected_false_positives_at_5pct": attempts * .05,
            "deflated_sharpe": None, "probabilistic_sharpe": None,
            "method": "joint_centered_block_maximum_mean_excess_return",
            "limitations": ["Valid only under declared stationarity/dependence assumptions and an ex ante comparison family.",
                            "Missing failed/previous variants prevent a complete historical data-snooping correction.",
                            "No independent-trade or calibrated-normal Sharpe assumption is manufactured."]}


def correlation_report(series, *, evidence_class):
    keys = sorted(series)
    result = {}
    for i, left in enumerate(keys):
        a = finite_series(series[left])
        for right in keys[i + 1:]:
            b = finite_series(series[right])
            if len(a) != len(b):
                raise ValueError("correlations require aligned observations")
            scaled_a = a / np.max(np.abs(a)) if len(a) and np.max(np.abs(a)) else a
            scaled_b = b / np.max(np.abs(b)) if len(b) and np.max(np.abs(b)) else b
            value = float(np.corrcoef(scaled_a, scaled_b)[0, 1]) if len(a) >= 30 and np.std(scaled_a) > 0 and np.std(scaled_b) > 0 else None
            result[f"{left}:{right}"] = value
    return {"evidence_class": evidence_class, "correlations": result,
            "limitation": "Descriptive aligned correlation is not evidence for an optimized ensemble."}


def cost_sensitivity(runs, *, evidence_class):
    """Runs keyed by additional fee bps per side, keeping spread/slippage fixed."""
    rows = []
    normalized = [(float(finite_series([cost])[0]), report) for cost, report in runs.items()]
    if any(cost < 0 for cost, _ in normalized) or len({cost for cost, _ in normalized}) != len(normalized):
        raise ValueError("cost axis requires unique nonnegative finite additional fees")
    assumptions, base_fees = [], []
    for cost, report in sorted(normalized, key=lambda item: item[0]):
        curve = report["equity_curve"]
        net = path_metrics(curve, evidence_class=evidence_class)["net_return"]
        turnover = report.get("metrics", {}).get("turnover")
        if report.get("execution_model"):
            assumptions.append({key: value for key, value in report["execution_model"].items() if key not in {"fee_bps", "name"}})
            base_fees.append(float(finite_series([report["execution_model"]["fee_bps"]])[0]) - cost)
        rows.append({"additional_fee_bps_each_side": cost, "bps_each_side": cost, "net_return": net, "turnover": turnover,
                     "evidence_class": evidence_class})
    if assumptions and any(value != assumptions[0] for value in assumptions):
        raise ValueError("cost grid must keep spread, slippage, latency and other execution assumptions fixed")
    if base_fees and (base_fees[0] < 0 or any(not math.isclose(value, base_fees[0], rel_tol=1e-12, abs_tol=1e-12) for value in base_fees)):
        raise ValueError("cost axis must add the declared fees to one unchanged base fee")
    positive = [row["bps_each_side"] for row in rows if row["net_return"] is not None and row["net_return"] > 0]
    nonpositive = [row["bps_each_side"] for row in rows if row["net_return"] is not None and row["net_return"] <= 0]
    lower = max(positive) if positive else None
    upper = min((cost for cost in nonpositive if lower is None or cost > lower), default=None)
    ordered = [row["net_return"] for row in rows]
    complete = bool(rows) and all(value is not None for value in ordered)
    monotone = all(b <= a + 1e-12 for a, b in zip(ordered, ordered[1:])) if complete else None
    return {"evidence_class": evidence_class, "runs": rows,
            "available": complete, "cost_axis": "additional_fee_bps_each_side",
            "execution_assumptions_verified": len(assumptions) == len(rows) and bool(rows),
            "break_even_bps_bracket": [lower, upper] if monotone and lower is not None else None,
            "monotonic": monotone, "profitable_at_zero": bool(rows and rows[0]["bps_each_side"] == 0 and rows[0]["net_return"] is not None and rows[0]["net_return"] > 0),
            "survives_20bps": any(row["bps_each_side"] == 20 and row["net_return"] is not None and row["net_return"] > 0 for row in rows),
            "basis": "additional fees above base execution model; modeled/observed spread and slippage retained; observed grid bracket only, not an exact break-even root"}


def concentration_attacks(trades, curve, *, evidence_class):
    pnl = []
    for row in trades:
        value = row.get("net_pnl", row.get("realized_pnl"))
        if value is not None:
            pnl.append(float(finite_series([value])[0]))
    finite_series(pnl)
    months = path_metrics(curve, evidence_class=evidence_class)["monthly_returns"]
    retained = sorted(months.items(), key=lambda item: item[1])[:-1] if months else []
    return {"evidence_class": evidence_class, "closed_trade_count": len(pnl),
            "net_pnl_without_best_five_closed_trades": sum(sorted(pnl)[:-5]) if len(pnl) > 5 else None,
            "net_pnl_without_best_closed_trade": sum(pnl) - max(pnl) if len(pnl) > 1 else None,
            "compounded_return_without_best_observed_month": math.prod(1 + value for _, value in retained) - 1 if retained else None,
            "best_symbol_removed": None,
            "limitations": ["Post-hoc concentration diagnostics, not executable alternative paths.",
                            "Open inventory and changed capital paths are not reconstructed by deleting closed trades."]}


def robustness_report(variants, *, base_return, evidence_class):
    base_return = float(finite_series([base_return])[0])
    rows = [{"name": name, "net_return": value, "evidence_class": evidence_class} for name, value in sorted(variants.items())]
    finite_series([row["net_return"] for row in rows])
    positive_fraction = sum(row["net_return"] > 0 for row in rows) / len(rows) if rows else None
    return {"evidence_class": evidence_class, "variants": rows,
            "robustness_score": positive_fraction,
            "score_definition": "fraction of predeclared perturbations with positive net marked return; not a success probability",
            "fragile": bool(base_return > 0 and any(row["net_return"] <= 0 for row in rows)),
            "parameter_stability": min((row["net_return"] for row in rows), default=None),
            "limitations": ["Absolute positivity alone does not establish baseline superiority.",
                            "Perturbations are counted as research variants, not independent confirmations."]}


def strategy_scorecard(*, strategy, metrics, costs, robustness, monte_carlo, baseline_excess,
                       quality, evidence_class, attempted_variants, thesis=None):
    attacks = []
    if type(attempted_variants) is not int or attempted_variants < 1:
        raise ValueError("attempted variants must be a positive integer")
    net = metrics.get("net_return")
    if net is not None:
        net = float(finite_series([net])[0])
    if not quality.get("alpha_validation_eligible", False):
        attacks.append("Point-in-time/source/universe/corporate-action evidence is incomplete.")
    if net is None:
        attacks.append("Net marked return is unavailable.")
    elif net <= 0:
        attacks.append("Net marked return is nonpositive.")
    if not baseline_excess or any(value is None or float(finite_series([value])[0]) <= 0 for value in baseline_excess.values()):
        attacks.append("Does not improve on every declared baseline; cash-only superiority is insufficient.")
    if not costs.get("survives_20bps"):
        attacks.append("Does not demonstrate survival at 20 additional fee bps each side, retaining base spread and slippage.")
    if robustness.get("fragile"):
        attacks.append("At least one predeclared perturbation reverses profitability.")
    if not monte_carlo.get("available"):
        attacks.append("Too few observations for the configured dependent bootstrap.")
    if not thesis:
        attacks.append("No complete economic/mechanism/failure-condition hypothesis is recorded.")
    status = "REJECTED" if net is not None and net <= 0 else "EXPERIMENTAL"
    # Generated data, ordinary inspected evaluation windows and descriptive
    # bootstraps never become promotion authority. A vetted future protocol
    # must establish untouched data and family-level significance first.
    return {"strategy": strategy, "evidence_class": evidence_class, "research_status": status, "status": status,
            "metrics": metrics, "cost_sensitivity": costs, "robustness": robustness,
            "monte_carlo": monte_carlo, "baseline_improvement": baseline_excess,
            "experiment_variant_count": attempted_variants, "sealed_holdout_status": "NOT_EVALUATED",
            "deflated_sharpe": None, "probabilistic_sharpe": None, "thesis": thesis,
            "red_team": {"findings": attacks, "selection_concern": f"{attempted_variants} declared variants; earlier/unrecorded research may increase this denominator.",
                         "could_be_beta_or_disguised_baseline": True},
            "promotion_blockers": ["No sealed final evaluation and no prospective paper/shadow evidence.",
                                   "Research status is never an investment recommendation or live permission."],
            "metric_evidence_class": evidence_class, "live_approved": False}
