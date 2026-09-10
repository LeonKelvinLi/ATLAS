"""Offline descriptive metrics and matched, causal baseline experiments.

No models, credentials, broker sessions, inferred fills, or performance claims.
Synthetic/test results remain labeled synthetic/test throughout the API.
"""

import math
import random
import statistics

from atlas.strategies import Baseline, finite, validate_bars
from atlas.validation import chronological_folds, validate_chronology

EVIDENCE_KINDS = {"backtest", "walk_forward", "holdout", "paper", "practice", "live", "synthetic", "test"}
MIN_RATIO_OBSERVATIONS = 30


def performance_report(trades, *, initial_capital=None, equity_curve=None,
                       evidence_kind="backtest", periods_per_year=None,
                       fee_slippage_assumptions=None):
    """Summarize supplied records, without treating missing costs as zero.

    Each trade supplies net_pnl, or gross_pnl + fees + slippage (all monetary
    units). net_pnl is taken as explicitly net, never netted a second time.
    Equity rows require timestamp/equity. Annual ratios require at least 30
    equally spaced returns and an explicit periods_per_year convention; this
    is a reporting floor, not a claim of statistical power or independence.
    """
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError("unknown evidence class")
    capital = finite(initial_capital, positive=True) if initial_capital is not None else None
    warnings = []
    pnls, fees, slippages = [], [], []
    records = list(trades)
    complete = True
    for trade in records:
        fee = finite(trade["fees"]) if trade.get("fees") is not None else None
        slip = finite(trade["slippage"]) if trade.get("slippage") is not None else None
        if fee is not None and fee < 0 or slip is not None and slip < 0:
            raise ValueError("costs must be nonnegative")
        fees.append(fee)
        slippages.append(slip)
        if trade.get("net_pnl") is not None:
            pnls.append(finite(trade["net_pnl"]))
        elif trade.get("gross_pnl") is not None and fee is not None and slip is not None:
            pnls.append(finite(trade["gross_pnl"]) - fee - slip)
        else:
            complete = False
    if not complete:
        warnings.append("Incomplete trade P&L/cost records; trade profitability metrics are unavailable.")
        pnls = []
    n = len(records)
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    total = sum(pnls) if pnls else None
    curve = [{"timestamp": row["timestamp"], "equity": finite(row["equity"])}
             for row in (equity_curve or [])]
    if any(row["equity"] < 0 for row in curve):
        raise ValueError("equity cannot be negative")
    validate_chronology([r["timestamp"] for r in curve])
    returns = ([b["equity"] / a["equity"] - 1 for a, b in zip(curve, curve[1:])]
               if all(row["equity"] > 0 for row in curve[:-1]) else [])
    peak, max_dd = capital or (curve[0]["equity"] if curve else None), None
    if curve:
        max_dd = 0.0
        for row in curve:
            peak = max(peak, row["equity"])
            max_dd = max(max_dd or 0.0, 1 - row["equity"] / peak) if peak else None
    total_return = ((curve[-1]["equity"] / capital - 1) if curve and capital else
                    (total / capital if total is not None and capital else None))
    annualized_return = annual_vol = sharpe = sortino = None
    gaps = [b["timestamp"] - a["timestamp"] for a, b in zip(curve, curve[1:])]
    if periods_per_year is not None:
        frequency = finite(periods_per_year, positive=True)
        if len(returns) >= MIN_RATIO_OBSERVATIONS and len(set(gaps)) == 1 and curve[-1]["equity"] > 0:
            mean = statistics.mean(returns)
            std = statistics.stdev(returns)
            annual_vol = std * math.sqrt(frequency)
            sharpe = mean / std * math.sqrt(frequency) if std > 0 else None
            downside = math.sqrt(sum(min(x, 0) ** 2 for x in returns) / len(returns))
            sortino = mean / downside * math.sqrt(frequency) if downside > 0 else None
            # Equity path endpoints define the observed number of periods.
            exponent = math.log(curve[-1]["equity"] / curve[0]["equity"]) * frequency / len(returns)
            annualized_return = math.expm1(exponent) if -700 < exponent < 700 else None
            warnings.append("Annualization assumes stable equally spaced returns and zero risk-free rate; serial dependence can invalidate ratios.")
        else:
            warnings.append("Annualized ratios require >=30 equally spaced returns; insufficient or irregular observations.")
    else:
        warnings.append("Annualization convention absent; annualized return, volatility, Sharpe and Sortino are unavailable.")
    if n < 30:
        warnings.append("Fewer than 30 trades: descriptive results cannot establish a reliable edge; 30 is not a power guarantee.")
    if not curve:
        warnings.append("Equity path unavailable; drawdown and path-dependent risk cannot be reconstructed from total P&L.")
    if any(x is None for x in fees + slippages):
        warnings.append("Some fees or slippage are unknown; aggregate costs are unavailable.")
    warnings.append("No statistical significance, calibration, execution quality or baseline superiority is inferred from these descriptive metrics.")
    warnings.append("Equity returns assume no deposits or withdrawals; cash-flow-adjusted returns require a separate flow ledger.")
    if evidence_kind in {"synthetic", "test"}:
        warnings.append("Generated/test data validate software behavior, not market performance.")
    return {
        "evidence_kind": evidence_kind, "trade_count": n, "scored_trade_count": len(pnls),
        "net_pnl": total, "total_return": total_return, "annualized_return": annualized_return,
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "average_win": statistics.mean(wins) if wins else None,
        "average_loss": statistics.mean(losses) if losses else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "expectancy_per_trade": statistics.mean(pnls) if pnls else None,
        "max_drawdown": max_dd, "annualized_volatility": annual_vol,
        "sharpe": sharpe, "sortino": sortino, "exposure": None,
        "fees": sum(fees) if fees and all(x is not None for x in fees) else None,
        "slippage": sum(slippages) if slippages and all(x is not None for x in slippages) else None,
        "probability_calibration": None, "prediction_correlation": None,
        "fee_slippage_assumptions": fee_slippage_assumptions,
        "return_observations": len(returns), "warnings": warnings,
    }


def _simulate(rows, start, strategy_id, capital, fee, slip, lookback, seed):
    rule = Baseline(strategy_id, lookback) if strategy_id in {"momentum", "mean_reversion"} else None
    rng = random.Random(seed)
    cash, quantity, entry, exposed = capital, 0.0, None, 0
    trades, curve = [], [{"timestamp": rows[start - 1]["timestamp"], "equity": capital}]

    def close_position(raw_price, bar_timestamp):
        nonlocal cash, quantity, entry
        fill = raw_price * (1 - slip)
        exit_fee = quantity * fill * fee
        cash += quantity * fill - exit_fee
        gross = quantity * (raw_price - entry["raw_price"])
        slippage = quantity * (entry["raw_price"] * slip + raw_price * slip)
        trade_fees = entry["fee"] + exit_fee
        trades.append({"entry_bar_timestamp": entry["timestamp"], "exit_bar_timestamp": bar_timestamp,
                       "gross_pnl": gross, "fees": trade_fees, "slippage": slippage,
                       "net_pnl": gross - trade_fees - slippage})
        quantity, entry = 0.0, None

    for i in range(start, len(rows)):
        # All decisions use the previous closed bar. The current open is used
        # only by the fill simulator, never by the strategy.
        if strategy_id == "buy_and_hold":
            desired = True
        elif strategy_id == "random":
            desired = bool(rng.getrandbits(1))
        else:
            direction, _, _ = rule.evaluate([r["close"] for r in rows[max(0, i - lookback - 1):i]])
            desired = direction == "buy"
        if quantity and not desired:
            close_position(rows[i]["open"], rows[i]["timestamp"])
        elif desired and not quantity:
            raw = rows[i]["open"]
            fill = raw * (1 + slip)
            quantity = cash / (fill * (1 + fee))
            entry_fee = quantity * fill * fee
            cash -= quantity * fill + entry_fee
            entry = {"raw_price": raw, "fee": entry_fee, "timestamp": rows[i]["timestamp"]}
        exposed += int(quantity > 0)
        curve.append({"timestamp": rows[i]["timestamp"], "equity": cash + quantity * rows[i]["close"]})
    if quantity:
        close_position(rows[-1]["close"], rows[-1]["timestamp"])
        curve[-1]["equity"] = cash
    return trades, curve, exposed / (len(rows) - start)


def compare_baselines(bars, *, initial_capital=10000, fee_bps, slippage_bps,
                      train_size=60, test_size=30, gap=1, lookback=20, seed=0,
                      evidence_kind="backtest", now=None):
    """Fixed-rule expanding-window research comparison; no fitting/selection.

    Long/cash only, full cash allocation, next observed open, close valuation,
    final-close liquidation. Same OOS window and costs for every rule. This
    is a simple benchmark simulator, not an equities execution model: no
    spreads beyond specified slippage, dividends, splits, borrow or liquidity.
    Bars must be appropriately adjusted upstream. Explicit costs are required.
    """
    rows = validate_bars(bars, now=now, require_open=True)
    capital = finite(initial_capital, positive=True)
    fee, slip = finite(fee_bps) / 10000, finite(slippage_bps) / 10000
    if not 0 <= fee < 1 or not 0 <= slip < 1:
        raise ValueError("cost assumptions must be between 0 and 10000 bps")
    Baseline("momentum", lookback)
    if train_size < lookback + 1:
        raise ValueError("training/context window must cover the baseline lookback")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    folds = list(chronological_folds(len(rows), train_size=train_size, test_size=test_size, gap=gap))
    if not folds:
        raise ValueError("insufficient observations for a chronological test window")
    if evidence_kind not in {"backtest", "synthetic", "test"}:
        raise ValueError("baseline simulation cannot be labeled paper, live or untouched holdout")
    start = folds[0][1].start
    assumptions = {"fee_bps_each_side": fee_bps, "slippage_bps_each_side": slippage_bps,
                   "entry": "next_observed_bar_open", "final_exit": "last_test_bar_close",
                   "position": "long_or_cash", "allocation": "all_available_cash",
                   "corporate_actions": "must_be_adjusted_upstream"}
    reports, paths = {}, {}
    for key in ("buy_and_hold", "random", "momentum", "mean_reversion"):
        trades, curve, exposure = _simulate(rows, start, key, capital, fee, slip, lookback, seed)
        report = performance_report(trades, initial_capital=capital, equity_curve=curve,
                                    evidence_kind=evidence_kind, fee_slippage_assumptions=assumptions)
        report["exposure"] = exposure
        report["equity_curve"] = curve
        report["trades"] = trades
        reports[key] = report
        paths[key] = [b["equity"] / a["equity"] - 1 for a, b in zip(curve, curve[1:])]
    correlations = {}
    keys = list(paths)
    for i, left in enumerate(keys):
        for right in keys[i + 1:]:
            a, b = paths[left], paths[right]
            correlations[f"{left}:{right}"] = (
                statistics.correlation(a, b)
                if len(a) >= MIN_RATIO_OBSERVATIONS and statistics.stdev(a) > 0 and statistics.stdev(b) > 0 else None
            )
    return {"evidence_kind": evidence_kind, "method": "chronological_fixed_rule_comparison",
            "fitted_models": 0, "untouched_holdout": False, "live_validated": False,
            "folds": [{"train_start": tr.start, "train_stop": tr.stop,
                       "test_start": te.start, "test_stop": te.stop} for tr, te in folds],
            "seed": seed, "reports": reports, "return_correlations": correlations,
            "warnings": ["No hyperparameters or weights were selected; expanding training ranges are context only.",
                "One seeded random path is a control, not a null-distribution significance test.",
                "Different rule exposure is reported, not assumed matched; decision opportunities, window and costs match.",
                "Bar closes understate intrabar drawdown; gaps imply next observed open, not guaranteed immediate execution.",
                "No dividends, spreads beyond assumed slippage, market impact, liquidity limits or provider verification.",
                "Legacy FX binary H2 is not applied to this equity/crypto benchmark."]}
