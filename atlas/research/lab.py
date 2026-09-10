"""A bounded, reproducible offline gauntlet. This module has no broker authority.

All variants are predeclared and reserved before feature computation. Evaluation
is an ordinary inspected development window; it never becomes a sealed holdout.
The primary hypothesis is chosen in the plan, never by the reported returns.
"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import subprocess
import tempfile
import time
import uuid

import numpy as np

from atlas.research import diagnostics as diag
from atlas.research.market import load_dataset, observation_keys
from atlas.research.point_in_time import FeatureRegistry, as_of_rows
from atlas.research.replay import ExecutionModel, run_replay
from atlas.research.splits import timestamp_folds
from atlas.research.store import ResearchStore, ResearchStoreError, content_hash

VERSION = "research-lab-1"
PROJECT = Path(__file__).resolve().parents[2]
_IMPORTED_RESEARCH_SOURCES = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in set(PROJECT.glob("*.py")) | set((PROJECT / "atlas").rglob("*.py"))}
FAMILIES = ("momentum", "mean_reversion", "moving_average", "volatility_scaled")
DEFAULTS = {"campaign": "research-development", "seed": 0, "initial_cash": "10000",
            "lookbacks": [10, 20], "primary_family": "momentum", "primary_lookback": 10,
            "position_weight": 0.4, "threshold": 0.002, "rebalance_bars": 5,
            "volatility_target_per_bar": 0.001, "bootstrap_resamples": 200, "block_size": 5,
            "periods_per_year": None, "role": "development"}


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_identity():
    paths = sorted(set(PROJECT.glob("*.py")) | set((PROJECT / "atlas").rglob("*.py")))
    if set(paths) != set(_IMPORTED_RESEARCH_SOURCES):
        raise ResearchStoreError("source file inventory changed since import; restart the offline command")
    for path, expected in _IMPORTED_RESEARCH_SOURCES.items():
        if not path.is_file() or _file_hash(path) != expected:
            raise ResearchStoreError("research source changed since import; restart the offline command")
    if any(path.is_symlink() for path in paths):
        raise ResearchStoreError("research source cannot contain symbolic links")
    files = {str(path.relative_to(PROJECT)): _file_hash(path) for path in paths}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT, check=True,
                            capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT, check=True,
                                capture_output=True, text=True).stdout)
    runtime = {"python": platform.python_version(), "implementation": platform.python_implementation(),
               "system": platform.system(), "machine": platform.machine(),
               **{name: importlib.metadata.version(name) for name in ("numpy", "scipy", "pandas")}}
    return {"git_commit": commit, "source_tree_sha256": content_hash(files),
            "lock_sha256": _file_hash(PROJECT / "uv.lock"), "runtime": runtime, "git_dirty": dirty,
            "source_files": files}


def normalize_plan(config):
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError("unknown research settings; arbitrary strategy code and execution settings are unsupported")
    plan = {**DEFAULTS, **config}
    for key, low, high in (("seed", 0, 2**32-1), ("rebalance_bars", 1, 100),
                           ("bootstrap_resamples", 100, 2000), ("block_size", 1, 100)):
        if type(plan[key]) is not int or not low <= plan[key] <= high:
            raise ValueError("invalid bounded research setting: " + key)
    lookbacks = plan["lookbacks"]
    if (not isinstance(lookbacks, list) or not 1 <= len(lookbacks) <= 3
            or any(type(n) is not int or not 3 <= n <= 100 for n in lookbacks)
            or len(set(lookbacks)) != len(lookbacks)):
        raise ValueError("one through three distinct lookbacks in 3..100 are required")
    if plan["primary_family"] not in FAMILIES or plan["primary_lookback"] not in lookbacks:
        raise ValueError("primary hypothesis must be a predeclared family/lookback")
    if plan["role"] not in {"development", "validation", "evaluation"}:
        raise ValueError("ordinary research cannot select or reveal a sealed holdout")
    for key in ("position_weight", "threshold", "volatility_target_per_bar"):
        val = plan[key]
        if type(val) not in (int, float) or not math.isfinite(val) or not 0 < val <= 1:
            raise ValueError("finite positive research fractions required")
    ppy = plan["periods_per_year"]
    if ppy is not None and (type(ppy) not in (int, float) or not math.isfinite(ppy) or not 0 < ppy <= 100_000_000):
        raise ValueError("explicit positive annualization convention required")
    cash = Decimal(str(plan["initial_cash"]))
    if not cash.is_finite() or not 0 < cash <= 1_000_000_000:
        raise ValueError("bounded positive initial cash required")
    plan["initial_cash"] = str(cash)
    plan["lookbacks"] = sorted(lookbacks)
    return plan


def strategy_definition(family, lookback, plan):
    mechanisms = {
        "momentum": ("Delayed adjustment to new information can cause continuation.", "Choppy prices, reversal and costs can erase the effect."),
        "mean_reversion": ("Temporary inventory pressure can produce short-horizon reversal.", "Persistent trends and adverse selection can overwhelm reversal."),
        "moving_average": ("A price above its trailing mean is a simple trend exposure hypothesis.", "It may merely reproduce market beta and lose through whipsaw."),
        "volatility_scaled": ("Scaling positive momentum by trailing volatility may stabilize exposure.", "Volatility estimates lag shocks and may reduce profitable exposure."),
    }
    economic, failure = mechanisms[family]
    return {"strategy_id": f"{family}_{lookback}", "version": VERSION,
            "implementation_sha256": _file_hash(__file__), "family": family,
            "parameters": {"lookback": lookback, "threshold": plan["threshold"],
                           "position_weight": plan["position_weight"], "rebalance_bars": plan["rebalance_bars"],
                           "volatility_target_per_bar": plan["volatility_target_per_bar"]},
            "thesis": {"classification": "HYPOTHESIS", "economic_mechanism": economic,
                       "behavioral_or_structural_source": "Unverified hypothesis, not inferred from a profitable curve.",
                       "failure_conditions": failure, "capacity": "Unknown; bar participation is a model assumption.",
                       "why_not_arbitraged_away": "Unknown; requires independent evidence.",
                       "risk_premia_or_beta_alternative": "Plausible; compare cash and exposure-matched market returns.",
                       "predictive_probability": None}}


def _split(dataset):
    times = sorted({b["information_available_time"] for b in dataset["bars"] if b["revision"] == 0})
    if len(times) < 100 or len(dataset["bars"]) > 2000 or len(dataset["symbols"]) > 4:
        raise ValueError("research gauntlet requires 100+ publication events, at most 2000 bars and four symbols")
    train_end, test_start = times[len(times)*2//5], times[len(times)*3//5]
    return {"training": {"start": times[0], "end_exclusive": train_end},
            "validation": {"start": train_end, "end_exclusive": test_start},
            "test": {"start": test_start, "end_exclusive": times[-1]+1},
            "selection": "Primary parameters fixed in plan; no model fitting or selection on these windows.",
            "holdout": "NOT_EVALUATED", "evaluation_role": "inspected development evaluation, not final holdout"}


def _trials(plan, schedules=None):
    candidates = [{"name": f"{f}_{n}", "kind": "candidate", "family": f, "lookback": n}
                  for f in FAMILIES for n in plan["lookbacks"]]
    trials = candidates + ([{"name": n, "kind": "baseline"} for n in
                          ("cash", "buy_hold", "exposure_matched_buy_hold", "conditional_random", "equal_weight_ensemble")]
                         + [{"name": f"cost_{n}", "kind": "cost", "fee_bps": n} for n in (0, 1, 2, 5, 10, 20)]
                         + [{"name": f"scenario_{n}", "kind": "scenario", "scenario": n}
                            for n in ("optimistic", "pessimistic", "stress")]
                         + [{"name": n, "kind": "perturbation"} for n in
                            ("lookback_minus_one", "lookback_plus_one", "threshold_half", "threshold_double",
                             "one_bar_delay", "two_bar_delay", "slower_rebalance", "start_shift", "end_shift")])
    for mode, folds in (schedules or {}).items():
        trials.extend({"name": f"fold_{mode}_{fold['fold_id']}", "kind": "fold", "fold": fold}
                      for fold in folds[:3])
    return trials


def feature_trace(dataset, lookbacks):
    """Compact hash-linked inputs; exact rows/definitions remain reproducible."""
    traces = {}
    for n in sorted(set(lookbacks)):
        registry = FeatureRegistry(n)
        records = []
        for t, symbol in sorted({(bar["information_available_time"], bar["symbol"]) for bar in dataset["bars"]}):
            snapshot = registry.snapshot(dataset["bars"], symbol=symbol, decision_time=t)
            records.append({"symbol": symbol, "decision_time": t, "values": snapshot["values"],
                            "availability": snapshot["availability"],
                            "input_hashes": {k: v["input_hash"] for k, v in snapshot["lineage"].items()},
                            "definitions_hash": snapshot["definitions_hash"],
                            "known_price": as_of_rows(dataset["bars"], t, symbol)[-1]["close"]})
        traces[n] = records
    return traces


def decisions_for(trace, definition, *, symbols, start, end, interval, delay=0):
    p, family = definition["parameters"], definition["family"]
    result, counts = [], {}
    for row in trace:
        t, symbol, values = row["decision_time"], row["symbol"], row["values"]
        if not start <= t < end:
            continue
        count = counts.get(symbol, 0); counts[symbol] = count + 1
        if count % p["rebalance_bars"]:
            continue
        momentum, distance, vol = values["momentum"], values["ma_distance"], values["volatility"]
        if momentum is None or distance is None or vol is None:
            continue
        active = (momentum > p["threshold"] if family in {"momentum", "volatility_scaled"}
                  else momentum < -p["threshold"] if family == "mean_reversion" else distance < -p["threshold"])
        weight = p["position_weight"] / len(symbols) if active else 0.0
        if family == "volatility_scaled" and active:
            weight *= min(1.0, p["volatility_target_per_bar"] / vol) if vol > 0 else 0.0
        decision_time = t + delay
        if decision_time >= end:
            continue
        identity = {"strategy": definition, "symbol": symbol, "time": t,
                    "inputs": row["input_hashes"], "weight": weight, "delay": delay}
        result.append({"signal_id": content_hash(identity), "strategy_id": definition["strategy_id"],
                       "strategy_version": VERSION, "symbol": symbol, "decision_time": decision_time,
                       "expires_at": min(decision_time + interval*p["rebalance_bars"], end+interval),
                       "target_weight": str(weight), "feature_decision_time": t,
                       "feature_input_hash": content_hash(row["input_hashes"])})
    return result


def _baseline_decisions(name, primary, candidates, symbols, start, end, interval, seed, *, entry_times=None):
    if name == "cash":
        return []
    if name == "equal_weight_ensemble":
        groups = {}
        for rows in candidates.values():
            for row in rows:
                groups.setdefault((row["decision_time"], row["symbol"]), []).append(float(row["target_weight"]))
        targets = [(t, symbol, sum(weights)/len(candidates)) for (t, symbol), weights in sorted(groups.items())]
    elif name == "conditional_random":
        rng = np.random.default_rng(seed)
        targets = []
        for symbol in symbols:
            rows = [r for r in primary if r["symbol"] == symbol]
            # Circular rotation preserves most intent transitions; measured fills/turnover can differ.
            weights = np.roll([float(r["target_weight"]) for r in rows], int(rng.integers(0, max(1, len(rows)))))
            targets.extend((r["decision_time"], symbol, float(w)) for r, w in zip(rows, weights))
    else:
        weight = (sum(float(r["target_weight"]) for r in primary)/len(primary)*len(symbols)
                  if name == "exposure_matched_buy_hold" and primary else 0.0 if name == "exposure_matched_buy_hold" else 1.0)
        targets = [((entry_times or {}).get(symbol, start), symbol, weight/len(symbols)) for symbol in symbols]
    return [{"signal_id": content_hash([name, t, symbol, w, seed]), "strategy_id": name,
             "strategy_version": VERSION, "symbol": symbol, "decision_time": t,
             "expires_at": end+interval if name in {"buy_hold", "exposure_matched_buy_hold"} else min(t+5*interval, end+interval),
             "target_weight": str(w)} for t, symbol, w in sorted(targets)]


def _aligned_returns(report, timeline, initial_cash):
    cursor, equity, values = 0, float(initial_cash), []
    curve = report["equity_curve"]
    for t in timeline:
        while cursor < len(curve) and curve[cursor]["timestamp"] <= t:
            equity = float(curve[cursor]["equity"]); cursor += 1
        values.append({"timestamp": t, "equity": equity})
    return diag.return_series(values)


def _folds(dataset):
    originals = sorted((b for b in dataset["bars"] if b["revision"] == 0), key=lambda b: (b["event_time"], b["symbol"]))
    samples = []
    for symbol in dataset["symbols"]:
        rows = [b for b in originals if b["symbol"] == symbol]
        for a, b in zip(rows, rows[1:]):
            if a["information_available_time"] < b["event_time"]:
                samples.append({"symbol": symbol, "event_time": a["event_time"],
                                "information_available_time": a["information_available_time"],
                                "label_end_time": b["event_time"], "label_available_time": b["information_available_time"]})
    samples.sort(key=lambda x: (x["event_time"], x["symbol"]))
    span = originals[-1]["event_time"]-originals[0]["event_time"]
    interval = originals[0]["interval_seconds"]
    return {mode: timestamp_folds(samples, train_seconds=max(1, span*3//10), validation_seconds=max(1, span//10),
                                  test_seconds=max(1, span//10), mode=mode, embargo_seconds=interval)
            for mode in ("anchored", "rolling")}


def _regimes(trace, report, start):
    # Only the feature snapshot strictly before the return interval's end is used.
    rows = [r for r in trace if r["decision_time"] >= start]
    curve = report["equity_curve"]
    buckets = {}
    for before, after in zip(curve, curve[1:]):
        known = [r for r in rows if r["decision_time"] <= before["timestamp"]]
        if not known:
            continue
        v = known[-1]["values"]
        if v["momentum"] is None or v["volatility"] is None:
            continue
        regime = ("bull" if v["momentum"] > .01 else "bear" if v["momentum"] < -.01 else "sideways")
        regime += ":" + ("high_volatility" if v["volatility"] > .01 else "lower_volatility")
        buckets.setdefault(regime, []).append(float(after["equity"])/float(before["equity"])-1)
    return {"buckets": {k: {"observations": len(v), "compounded_interval_return": math.prod(1+x for x in v)-1}
                        for k, v in sorted(buckets.items())},
            "definition": "prior primary-symbol momentum +/-1%; trailing per-bar volatility 1%; fixed ex ante thresholds",
            "limitation": "Disjoint selected return intervals are descriptive; no fitted regime classifier or crisis label."}


def evaluate(dataset, plan, manifest):
    """Pure deterministic execution after attempt reservation; no files/network."""
    split, label = manifest["split"], dataset["evidence_class"]
    start, end = split["test"]["start"], split["test"]["end_exclusive"]
    interval = dataset["bars"][0]["interval_seconds"]
    if len({b["interval_seconds"] for b in dataset["bars"]}) != 1:
        raise ValueError("gauntlet requires a common declared interval")
    traces = feature_trace(dataset, plan["lookbacks"]+[plan["primary_lookback"]-1, plan["primary_lookback"]+1])
    bars = [b for b in dataset["bars"] if start <= b["information_available_time"] < end]
    entry_times = {symbol: min((b["information_available_time"] for b in bars if b["symbol"] == symbol), default=None)
                   for symbol in dataset["symbols"]}
    if any(t is None for t in entry_times.values()):
        raise ValueError("evaluation window does not cover every declared symbol")
    definitions = {t["name"]: strategy_definition(t["family"], t["lookback"], plan)
                   for t in manifest["trial_plan"] if t["kind"] == "candidate"}
    candidate_decisions = {name: decisions_for(traces[d["parameters"]["lookback"]], d,
                           symbols=dataset["symbols"], start=start, end=end, interval=interval)
                           for name, d in definitions.items()}
    primary_name = f"{plan['primary_family']}_{plan['primary_lookback']}"
    definition, primary = definitions[primary_name], candidate_decisions[primary_name]
    normal = ExecutionModel.scenario("normal")
    reports = {}
    for trial in manifest["trial_plan"]:
        name, kind = trial["name"], trial["kind"]
        model, decisions, selected_bars = normal, primary, bars
        if kind == "candidate":
            decisions = candidate_decisions[name]
        elif kind == "baseline":
            decisions = _baseline_decisions(name, primary, candidate_decisions, dataset["symbols"], start, end, interval, plan["seed"], entry_times=entry_times)
        elif kind == "cost":
            model = replace(normal, fee_bps=normal.fee_bps + Decimal(trial["fee_bps"]))
        elif kind == "scenario":
            model = ExecutionModel.scenario(trial["scenario"])
        elif kind == "fold":
            bounds = trial["fold"]["ranges"]["test"]
            local_start, local_end = bounds["start"], bounds["end_exclusive"]
            selected_bars = [b for b in dataset["bars"] if local_start <= b["information_available_time"] < local_end]
            decisions = decisions_for(traces[plan["primary_lookback"]], definition, symbols=dataset["symbols"],
                                      start=local_start, end=local_end, interval=interval)
        else:
            changed = json.loads(json.dumps(definition)); p = changed["parameters"]
            local_start, local_end, delay = start, end, 0
            if name == "lookback_minus_one": p["lookback"] -= 1
            elif name == "lookback_plus_one": p["lookback"] += 1
            elif name == "threshold_half": p["threshold"] /= 2
            elif name == "threshold_double": p["threshold"] *= 2
            elif name == "one_bar_delay": delay = interval
            elif name == "two_bar_delay": delay = 2*interval
            elif name == "slower_rebalance": p["rebalance_bars"] *= 2
            elif name == "start_shift": local_start += interval*5
            elif name == "end_shift": local_end -= interval*5
            decisions = decisions_for(traces[p["lookback"]], changed, symbols=dataset["symbols"],
                                      start=local_start, end=local_end, interval=interval, delay=delay)
            selected_bars = [b for b in bars if local_start <= b["information_available_time"] < local_end]
        reports[name] = run_replay(selected_bars, decisions, model=model, initial_cash=plan["initial_cash"], evidence_class=label)
    report = reports[primary_name]
    timeline = sorted({b["information_available_time"] for b in bars})
    # Different-window trials cannot be forced into the common comparison sample.
    common_names = [t["name"] for t in manifest["trial_plan"] if t["kind"] != "fold" and t["name"] not in {"start_shift", "end_shift"}]
    series = {name: _aligned_returns(reports[name], timeline, plan["initial_cash"]) for name in common_names}
    metrics = {**report["metrics"], **diag.path_metrics(report["equity_curve"], evidence_class=label,
                                                       periods_per_year=plan["periods_per_year"])}
    costs = diag.cost_sensitivity({t["fee_bps"]: reports[t["name"]] for t in manifest["trial_plan"] if t["kind"] == "cost"}, evidence_class=label)
    costs["axis"] = "additional fee bps each side above normal base fee; observed/minimum modeled spread and slippage held fixed"
    perturb = {t["name"]: float(reports[t["name"]]["metrics"]["net_return"]) for t in manifest["trial_plan"] if t["kind"] == "perturbation"}
    robustness = diag.robustness_report(perturb, base_return=metrics["net_return"], evidence_class=label)
    robustness["concentration_attacks"] = diag.concentration_attacks(report["trades"], report["equity_curve"], evidence_class=label)
    mc = diag.block_bootstrap(series[primary_name], seed=plan["seed"], resamples=plan["bootstrap_resamples"],
                              block_size=plan["block_size"], evidence_class=label)
    baselines = {t["name"]: {"net_return": float(reports[t["name"]]["metrics"]["net_return"]),
                            "turnover": reports[t["name"]]["metrics"]["turnover"], "evidence_class": label}
                 for t in manifest["trial_plan"] if t["kind"] == "baseline"}
    baseline_excess = {k: metrics["net_return"]-v["net_return"] for k, v in baselines.items()}
    scorecard = diag.strategy_scorecard(strategy=definition, metrics=metrics, costs=costs, robustness=robustness,
                  monte_carlo=mc, baseline_excess=baseline_excess, quality=dataset["quality"], evidence_class=label,
                  attempted_variants=len(reports), thesis=definition["thesis"])
    scorecard["status"] = scorecard["research_status"]
    multiple = diag.multiple_testing({k: [a-b for a, b in zip(v, series["buy_hold"])] for k, v in series.items()},
                  attempts=len(reports), seed=plan["seed"], resamples=plan["bootstrap_resamples"], block_size=plan["block_size"], evidence_class=label)
    multiple["scope"] = "Aligned current-run comparisons only; other-window trials and previous/unpublished attempts are not fully corrected."
    for diagnostic in (mc, multiple):
        diagnostic["sampling_basis"] = "union of evaluation publication timestamps, including revisions; irregular or multi-symbol times are event returns, not equal-duration calendar returns"
    primary_trace = traces[plan["primary_lookback"]]
    from atlas.research.health import assess_health
    features = lambda rows: [row["values"] for row in rows]
    definitions_hash = content_hash(FeatureRegistry(plan["primary_lookback"]).definitions())
    health = assess_health(features([r for r in primary_trace if r["decision_time"] < start]),
                           features([r for r in primary_trace if r["decision_time"] >= start]), evidence_class=label,
                           reference_schema_hash=definitions_hash, current_schema_hash=definitions_hash,
                           data_quality_ok=dataset["quality"]["alpha_validation_eligible"])
    regimes = _regimes([r for r in primary_trace if r["symbol"] == dataset["symbols"][0]], report, start)
    regimes["evidence_class"] = label
    portfolio = {**report["portfolio"], "evidence_class": label, "portfolio_volatility": metrics["volatility"],
                 "historical_var": None, "expected_shortfall": None,
                 "tail_risk_limit": "Requires a separately reviewed daily-return horizon and adequate independent regime coverage."}
    summaries = {name: {"metrics": {**r["metrics"], **diag.path_metrics(r["equity_curve"], evidence_class=label)},
                       "execution_quality": r["execution_quality"], "run_id": r["run_id"],
                       "curve_sha256": content_hash(r["equity_curve"])} for name, r in reports.items()}
    candidate_scorecards = {name: {"strategy": d, "evidence_class": label,
                            "status": "REJECTED" if float(reports[name]["metrics"]["net_return"]) <= 0 else "EXPERIMENTAL",
                            "metrics": summaries[name]["metrics"], "live_approved": False,
                            "gauntlet_applied": name == primary_name,
                            "qualification": "Only the primary predeclared hypothesis receives the complete gauntlet; no test-window winner is selected."}
                           for name, d in definitions.items()}
    return {"schema_version": 1, "evidence_class": label, "evaluated_variant_count": len(reports),
            "data_quality": dataset["quality"], "scorecard": scorecard, "metrics": metrics,
            "strategy_health": health, "feature_drift": health, "regime_performance": regimes,
            "robustness": robustness, "cost_sensitivity": costs,
            "feature_definitions": {str(n): FeatureRegistry(n).definitions() for n in sorted(traces)},
            "feature_lineage": primary_trace, "portfolio_exposures": portfolio,
            "drawdown_analysis": diag.drawdown_analysis(report["equity_curve"], evidence_class=label),
            "monte_carlo": mc, "baseline_comparison": {"evidence_class": label, "baselines": baselines,
                 "net_excess": baseline_excess, "comparison_window": split["test"],
                 "first_published_entry_time_by_symbol": entry_times,
                 "matching_limits": "Conditional random uses rotated observed intent weights; exposure control uses average decision weight. Realized exposure/turnover are not exactly matched; controls are post-hoc diagnostics."},
            "strategy_thesis": definition["thesis"], "execution_quality": report["execution_quality"],
            "shadow_results": {"available": False, "evidence_class": label,
                 "reason": "No prospective feed or running shadow service; replay is retrospective engineering evidence."},
            "research_anomalies": {"evidence_class": label, "findings": scorecard["red_team"]["findings"],
                 "provider_authenticity_verified": False, "frozen_legacy_evidence_recertified": False},
            "multiple_testing": multiple, "candidate_correlations": diag.correlation_report(
                 {name: series[name] for name in candidate_decisions}, evidence_class=label),
            "candidate_scorecards": candidate_scorecards, "strategy_definitions": definitions,
            "trial_summaries": summaries, "walk_forward_schedules": manifest["walk_forward_schedules"],
            "walk_forward_results": {t["name"]: {"fold": t["fold"], **summaries[t["name"]]} for t in manifest["trial_plan"] if t["kind"] == "fold"},
            "walk_forward_limit": "Fixed predeclared rules evaluated from fresh cash within test time ranges; no model fitting. Purged indices are label/fitting diagnostics; replay retains in-range market marks and decisions. Anchored and rolling contexts do not imply distinct fitted models. Overlapping modes are not independent evidence.",
            "equity_curve": report["equity_curve"], "orders": report["orders"], "fills": report["fills"],
            "trades": report["trades"], "replay_events": report["events"], "trade_submitted": False,
            "limitations": report["limitations"]}


def _manifest(dataset, plan):
    split = _split(dataset)
    definition = strategy_definition(plan["primary_family"], plan["primary_lookback"], plan)
    schedules = _folds(dataset)
    trials = _trials(plan, schedules)
    return {"schema_version": 1, "campaign": plan["campaign"], "evidence_kind": dataset["evidence_class"],
            "source_identity": source_identity(), "dataset": {"content_sha256": dataset["observation_hash"],
                "metadata_sha256": dataset["dataset_hash"], "partition_sha256": content_hash(split), "role": plan["role"],
                "observation_keys": observation_keys(dataset), "descriptor": dataset["descriptor"]},
            "strategy": definition, "features": {"version": FeatureRegistry().version,
                "definitions_sha256": content_hash({str(n): FeatureRegistry(n).definitions() for n in
                    sorted(set(plan["lookbacks"]+[plan["primary_lookback"]-1, plan["primary_lookback"]+1]))})},
            "execution": {"version": VERSION, "implementation_sha256": _file_hash(PROJECT / "atlas/research/replay.py"),
                "assumptions": ExecutionModel.scenario("normal").public()}, "split": split, "seed": plan["seed"],
            "plan": plan, "trial_plan": trials, "walk_forward_schedules": schedules, "evaluated_variant_count": len(trials)}


def run_research(*, dataset_path, store_path, config):
    plan = normalize_plan(config)
    dataset = load_dataset(dataset_path, now=int(time.time()))
    manifest = _manifest(dataset, plan)
    with ResearchStore(store_path) as store:
        store.assert_development_access(manifest["dataset"]["content_sha256"], manifest["dataset"]["partition_sha256"],
                                        observation_keys=manifest["dataset"]["observation_keys"])
        record = store.reserve(manifest, request_id="run_"+uuid.uuid4().hex)
        experiment_id = record["experiment_id"]
        try:
            artifacts = evaluate(dataset, plan, manifest)
            _assert_source_unchanged(manifest["source_identity"])
            store.complete(experiment_id, artifacts)
        except BaseException:
            store.fail(experiment_id, "research_evaluation_failed")
            raise
        record = store.get(experiment_id)
        exported = store.export(experiment_id)
        return {"experiment_id": experiment_id, "evidence_class": dataset["evidence_class"],
                "status": artifacts["scorecard"]["status"], "result_hash": record["result_hash"],
                "export": str(exported), "attempts": store.counts(plan["campaign"]),
                "metrics": artifacts["metrics"], "trade_submitted": False}


def reproduce(*, experiment_id, store_path, dataset_path):
    with ResearchStore(store_path) as store:
        record = store.get(experiment_id)
        if record["status"] != "completed":
            raise ResearchStoreError("only completed immutable experiments can be reproduced")
        manifest = record["manifest"]
        dataset = load_dataset(dataset_path, now=int(time.time()))
        expected = manifest["source_identity"]
        actual = source_identity()
        for key in ("source_tree_sha256", "lock_sha256", "runtime"):
            if actual[key] != expected[key]:
                raise ResearchStoreError("reproduction identity mismatch: " + key)
        if dataset["dataset_hash"] != manifest["dataset"]["metadata_sha256"] or dataset["observation_hash"] != manifest["dataset"]["content_sha256"]:
            raise ResearchStoreError("reproduction dataset content or metadata mismatch")
        store.assert_development_access(dataset["observation_hash"], manifest["dataset"]["partition_sha256"],
                                        observation_keys=manifest["dataset"]["observation_keys"])
        artifacts = evaluate(dataset, normalize_plan(manifest["plan"]), manifest)
        _assert_source_unchanged(actual)
        matches = content_hash(artifacts) == content_hash(record["artifacts"])
        return {"experiment_id": experiment_id, "reproduced": matches, "comparison": "exact canonical artifact SHA256",
                "original_commit": expected["git_commit"], "current_commit": actual["git_commit"],
                "identical_source_bytes": True, "commit_changed": expected["git_commit"] != actual["git_commit"],
                "evidence_class": manifest["evidence_kind"], "new_attempt_recorded": False, "trade_submitted": False}


def _assert_source_unchanged(expected):
    actual = source_identity()
    for key in ("source_tree_sha256", "lock_sha256", "runtime"):
        if actual[key] != expected[key]:
            raise ResearchStoreError("source changed during research; run cannot establish immutable lineage")


def synthetic_dataset(count=240):
    """Generated intraday fixture, deliberately labelled as non-market evidence."""
    bars, beginning, previous = [], 1788220800, 100.0
    for i in range(count):
        event = beginning+(i+1)*60
        close = 100 + .015*i + 1.4*math.sin(i/9) + .3*math.sin(i/2.7)
        bars.append({"symbol": "DEMO-USD", "asset_class": "crypto", "venue": "generated",
                     "event_time": event, "interval_seconds": 60, "information_available_time": event+1,
                     "ingestion_time": event+2, "timezone": "UTC", "session": "24/7",
                     "session_date": datetime.fromtimestamp(event-1, timezone.utc).date().isoformat(),
                     "open": f"{previous:.8f}", "high": f"{max(previous, close)+.1:.8f}",
                     "low": f"{min(previous, close)-.1:.8f}", "close": f"{close:.8f}",
                     "volume": str(500 + i % 17*10), "revision": 0, "source_id": "generated-v1"})
        previous = close
    return {"schema_version": "1.0", "dataset_id": "synthetic-gauntlet-v1",
            "source": {"source_id": "generated-v1", "kind": "synthetic", "description": "Deterministic software fixture; no market evidence"},
            "asset_class": "crypto", "symbols": ["DEMO-USD"], "venue": "generated", "volume_unit": "base_asset", "currency": "USD",
            "price_adjustment": "unadjusted", "universe": {"selection_method": "fixed generated fixture", "survivorship_bias": "unknown"},
            "availability_basis": "synthetic", "revision_policy": "synthetic", "bars": bars}


def synthetic_demo(*, store_path=None):
    if store_path is None:
        with tempfile.TemporaryDirectory(prefix="atlas-research-demo-") as temp:
            result = synthetic_demo(store_path=temp)
            result["temporary_outputs_removed"] = True
            result.pop("dataset_path", None); result.pop("export", None)
            return result
    original = Path(store_path).expanduser()
    if not original.is_absolute() or original.is_symlink():
        raise ResearchStoreError("research demo store must be an absolute nonsymlink external directory")
    root = original.resolve()
    with ResearchStore(root):
        pass  # Validate/create an explicit external store before writing the fixture.
    path = root / "synthetic-dataset.json"
    encoded = json.dumps(synthetic_dataset(), sort_keys=True, indent=2, allow_nan=False)+"\n"
    if path.is_symlink() or (path.exists() and path.read_text() != encoded):
        raise ResearchStoreError("synthetic fixture path already has different content")
    if not path.exists():
        with path.open("x") as handle:
            handle.write(encoded)
    result = run_research(dataset_path=path, store_path=root, config={"campaign": "synthetic-engineering"})
    result["reproduction"] = reproduce(experiment_id=result["experiment_id"], store_path=root, dataset_path=path)
    result["dataset_path"] = str(path)
    return result
