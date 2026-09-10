"""Bounded research views and explicit offline CLI; no execution authority."""

import json
from pathlib import Path

from atlas.research.store import ResearchStore, ResearchStoreError, canonical

_SECTIONS = {
    "atlas_get_data_quality": "data_quality",
    "atlas_get_strategy_scorecard": "scorecard",
    "atlas_get_strategy_health": "strategy_health",
    "atlas_get_strategy_regime_performance": "regime_performance",
    "atlas_get_strategy_robustness": "robustness",
    "atlas_get_strategy_cost_sensitivity": "cost_sensitivity",
    "atlas_get_feature_definitions": "feature_definitions",
    "atlas_get_feature_drift": "feature_drift",
    "atlas_get_portfolio_exposures": "portfolio_exposures",
    "atlas_get_drawdown_analysis": "drawdown_analysis",
    "atlas_get_monte_carlo": "monte_carlo",
    "atlas_get_baseline_comparison": "baseline_comparison",
    "atlas_explain_strategy": "strategy_thesis",
    "atlas_get_execution_quality": "execution_quality",
    "atlas_get_shadow_results": "shadow_results",
    "atlas_find_research_anomalies": "research_anomalies",
}


def _bounded(result):
    canonical(result, maximum=128_000)
    return result


def experiment_view(store, experiment_id):
    record = store.get(experiment_id)
    artifacts = record.pop("artifacts")
    record["artifact_names"] = sorted(artifacts or {})
    record["trade_submitted"] = False
    return _bounded(record)


def read_report(store, experiment_id, section):
    if section not in _SECTIONS.values():
        raise ResearchStoreError("unsupported bounded research projection")
    record = store.get(experiment_id)
    report = (record["artifacts"] or {}).get(section)
    return _bounded({"available": report is not None, "experiment_id": experiment_id,
                     "manifest_hash": record["manifest_hash"], "result_hash": record["result_hash"],
                     "evidence_kind": record["manifest"]["evidence_kind"], "section": section, "report": report,
                     "reason": None if report is not None else "no stored evidence for requested report",
                     "trade_submitted": False})


def list_experiments(store, limit=20):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ResearchStoreError("research query limit must be 1 through 50")
    records = store.list(limit)
    return _bounded({"experiments": [{"experiment_id": row["experiment_id"], "status": row["status"],
                                      "campaign": row["manifest"]["campaign"], "manifest_hash": row["manifest_hash"],
                                      "result_hash": row["result_hash"], "evidence_kind": row["manifest"]["evidence_kind"],
                                      "created_at": row["created_at"]} for row in records],
                     "limit": limit, "window_may_be_truncated": len(records) == limit, "trade_submitted": False})


def compare_experiments(store, experiment_ids):
    if not isinstance(experiment_ids, list) or not 2 <= len(experiment_ids) <= 10 or len(set(experiment_ids)) != len(experiment_ids):
        raise ResearchStoreError("comparison requires 2 through 10 distinct experiment identifiers")
    return _bounded({"experiments": [read_report(store, experiment_id, "scorecard") for experiment_id in experiment_ids],
                     "comparison": "stored scorecards; no inferred matched-window or statistical superiority",
                     "trade_submitted": False})


def rank_candidates(store, limit=20):
    ids = [row["experiment_id"] for row in list_experiments(store, limit)["experiments"]]
    reports = [read_report(store, experiment_id, "scorecard") for experiment_id in ids]
    # No new numerical weights or promotion threshold comes from the AI caller.
    order = {"SHADOW-CANDIDATE": 0, "PAPER-CANDIDATE": 1, "PROMISING": 2, "EXPERIMENTAL": 3, "REJECTED": 4}
    def status(row):
        report = row.get("report")
        return report.get("status") if isinstance(report, dict) else None
    reports.sort(key=lambda row: order.get(status(row), 5))
    return _bounded({"candidates": reports, "ranking_basis": "stored research status; no investment recommendation",
                     "window_limit": limit, "trade_submitted": False})


def register_research_tools(mcp, store, safe):
    """Register reads only. No experiment execution, path, code, or holdout access."""
    @mcp.tool()
    def atlas_get_experiment(experiment_id: str) -> dict:
        """Read a hash-verified saved experiment manifest and artifact inventory."""
        return safe(experiment_view, store, experiment_id)

    @mcp.tool()
    def atlas_list_experiments(limit: int = 20) -> dict:
        """Read a bounded attempt list, including failed and unfinished experiments."""
        return safe(list_experiments, store, limit)

    @mcp.tool()
    def atlas_compare_experiments(experiment_ids: list[str]) -> dict:
        """Compare saved scorecards without rerunning research or accessing holdout data."""
        return safe(compare_experiments, store, experiment_ids)

    @mcp.tool()
    def atlas_rank_research_candidates(limit: int = 20) -> dict:
        """Order saved research statuses; this cannot promote a strategy or trade."""
        return safe(rank_candidates, store, limit)

    for name, section in _SECTIONS.items():
        def bind(selected):
            def read(experiment_id: str) -> dict:
                return safe(read_report, store, experiment_id, selected)
            return read
        mcp.tool(name=name, description="Read saved " + section.replace("_", " ") + "; absent evidence remains unavailable.")(bind(section))


def add_research_parser(commands):
    parser = commands.add_parser("research", help="offline immutable experiment research; never broker execution")
    sub = parser.add_subparsers(dest="research_command", required=True)
    demo = sub.add_parser("demo", help="generated synthetic engineering example only")
    demo.add_argument("--store", help="optional explicit external result directory")
    run = sub.add_parser("run", help="run a reviewed research plan on explicit external data")
    run.add_argument("--dataset", required=True)
    run.add_argument("--store", required=True)
    run.add_argument("--config", required=True)
    reproduce = sub.add_parser("reproduce", help="verify code/data identity before reproducing a saved experiment")
    reproduce.add_argument("--experiment-id", required=True)
    reproduce.add_argument("--store", required=True)
    reproduce.add_argument("--dataset", required=True)
    inspect = sub.add_parser("inspect", help="read a stored immutable manifest and artifact list")
    inspect.add_argument("--experiment-id", required=True)
    inspect.add_argument("--store", required=True)
    inventory = sub.add_parser("inventory", help="read recorded attempts without rerunning experiments")
    inventory.add_argument("--store", required=True)
    inventory.add_argument("--limit", type=int, default=20)


def research_command(args):
    from atlas.config import _json_file, external_path
    def checked_store(value):
        external_path(value)
        original = Path(value).expanduser()
        if original.is_symlink():
            raise ResearchStoreError("research store must not be a symlink")
        return original
    if args.research_command == "demo":
        from atlas.research.lab import synthetic_demo
        store = checked_store(args.store) if args.store else None
        return synthetic_demo(store_path=store)
    if args.research_command == "run":
        from atlas.research.lab import run_research
        config = _json_file(args.config, max_bytes=1_000_000)
        if not isinstance(config, dict):
            raise ResearchStoreError("research plan must be a JSON object")
        return run_research(dataset_path=external_path(args.dataset), store_path=checked_store(args.store), config=config)
    if args.research_command == "reproduce":
        from atlas.research.lab import reproduce
        return reproduce(experiment_id=args.experiment_id, store_path=checked_store(args.store), dataset_path=external_path(args.dataset))
    root = checked_store(args.store)
    if not (root / "research.db").is_file():
        raise ResearchStoreError("research inspection requires an existing store")
    with ResearchStore(root) as store:
        if args.research_command == "inspect":
            return experiment_view(store, args.experiment_id)
        return list_experiments(store, args.limit)
