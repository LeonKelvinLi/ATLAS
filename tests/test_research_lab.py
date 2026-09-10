"""Synthetic integration tests; never a market alpha test or broker operation."""

import copy
import hashlib
import json
from pathlib import Path
import socket

import pytest

from atlas.research import lab
from atlas.research.market import load_dataset,observation_keys,validate_dataset
from atlas.research.store import HoldoutAccessError,ResearchStore,ResearchStoreError,content_hash


PLAN={"campaign":"lab-engineering-test","lookbacks":[10],"primary_lookback":10,
      "bootstrap_resamples":100,"seed":7}
SOURCE={"git_commit":"a"*40,"source_tree_sha256":"b"*64,"lock_sha256":"c"*64,
        "runtime":{"python":"test-fixture"},"git_dirty":True,"source_files":{"synthetic-fixture":"d"*64}}


@pytest.fixture(autouse=True)
def isolated_engineering_identity(monkeypatch):
    # Unit fixtures isolate source identity from concurrent development edits;
    # real CLI reproduction must separately use the actual source identity.
    monkeypatch.setattr(lab,"source_identity",lambda:copy.deepcopy(SOURCE))
    monkeypatch.setattr(lab,"_file_hash",lambda path:hashlib.sha256(Path(path).name.encode()).hexdigest())
    def refuse(*_args,**_kwargs):raise AssertionError("research lab tests must never connect to a network")
    monkeypatch.setattr(socket.socket,"connect",refuse)
    monkeypatch.setattr(socket.socket,"connect_ex",refuse)


@pytest.fixture
def inputs(tmp_path):
    path=tmp_path/"synthetic.json"
    path.write_text(json.dumps(lab.synthetic_dataset(120)),encoding="utf-8")
    return path,tmp_path/"research"


@pytest.fixture
def completed(inputs):
    path,root=inputs
    result=lab.run_research(dataset_path=path,store_path=root,config=PLAN)
    with ResearchStore(root) as store:record=store.get(result["experiment_id"])
    return path,root,result,record


def test_end_to_end_synthetic_gauntlet_reproduces_and_never_promotes(completed):
    path,root,result,record=completed
    artifacts=record["artifacts"]
    assert result["evidence_class"]=="SYNTHETIC"
    assert result["status"] in {"EXPERIMENTAL","REJECTED"}
    assert result["trade_submitted"] is False
    assert artifacts["scorecard"]["status"] not in {"PROMISING","PAPER-CANDIDATE","SHADOW-CANDIDATE"}
    assert artifacts["data_quality"]["alpha_validation_eligible"] is False
    assert artifacts["shadow_results"]["available"] is False
    assert artifacts["equity_curve"] and artifacts["feature_lineage"]
    assert artifacts["evaluated_variant_count"]==record["manifest"]["evaluated_variant_count"]
    assert len(artifacts["trial_summaries"])==artifacts["evaluated_variant_count"]
    assert artifacts["baseline_comparison"]["evidence_class"]=="SYNTHETIC"
    for row in artifacts["feature_lineage"]:
        assert set(row["values"])==set(row["availability"])==set(row["input_hashes"])
        assert all(len(value)==64 for value in row["input_hashes"].values())
        assert all(value is None or value<=row["decision_time"] for value in row["availability"].values())
    assert record["manifest"]["dataset"]["observation_keys"]==observation_keys(load_dataset(path,now=1788998400))
    with ResearchStore(root) as store:before=store.counts(PLAN["campaign"])
    reproduced=lab.reproduce(experiment_id=result["experiment_id"],store_path=root,dataset_path=path)
    assert reproduced["reproduced"] is True
    assert reproduced["new_attempt_recorded"] is False
    with ResearchStore(root) as store:assert store.counts(PLAN["campaign"])==before
    assert Path(result["export"]).is_dir()
    json.dumps(artifacts,allow_nan=False)


def test_candidate_feature_decisions_are_unchanged_by_future_price_mutation():
    raw=lab.synthetic_dataset(120)
    before=validate_dataset(raw,now=1788998400)
    cutoff=before["bars"][79]["information_available_time"]
    after_raw=copy.deepcopy(raw)
    for row in after_raw["bars"][80:]:
        for key in ("open","high","low","close"):row[key]=str(float(row[key])*2)
    after=validate_dataset(after_raw,now=1788998400)
    trace_before=lab.feature_trace(before,[10])[10]
    trace_after=lab.feature_trace(after,[10])[10]
    assert [r for r in trace_before if r["decision_time"]<=cutoff]==[r for r in trace_after if r["decision_time"]<=cutoff]
    plan=lab.normalize_plan(PLAN);definition=lab.strategy_definition("momentum",10,plan)
    args={"symbols":before["symbols"],"start":before["bars"][0]["information_available_time"],"end":cutoff+1,"interval":60}
    assert lab.decisions_for(trace_before,definition,**args)==lab.decisions_for(trace_after,definition,**args)


def test_buy_hold_waits_for_each_symbols_first_known_quote():
    rows=lab._baseline_decisions("buy_hold",[],{},["A","B"],100,1000,60,7,entry_times={"A":100,"B":113})
    assert {r["symbol"]:r["decision_time"] for r in rows}=={"A":100,"B":113}


def test_interleaved_same_clock_publication_batch_has_one_snapshot_per_symbol():
    raw=lab.synthetic_dataset(2);raw["symbols"].append("SECOND-USD")
    first,second=raw["bars"]
    raw["bars"]=[first,{**first,"symbol":"SECOND-USD"},second]
    publication=second["event_time"]+1
    for row in raw["bars"]:
        row["information_available_time"]=publication;row["ingestion_time"]=publication+1
    normalized=validate_dataset(raw,now=1788998400)
    trace=lab.feature_trace(normalized,[10])[10]
    assert len(trace)==2
    assert len({(row["symbol"],row["decision_time"]) for row in trace})==len(trace)


def test_asynchronous_two_symbol_benchmark_does_not_drop_late_symbol(inputs,monkeypatch):
    path,root=inputs
    raw=json.loads(path.read_text());raw["symbols"].append("SECOND-USD")
    second=[]
    for row in raw["bars"]:
        second.append({**row,"symbol":"SECOND-USD","information_available_time":row["information_available_time"]+10,
                       "ingestion_time":row["ingestion_time"]+10})
    raw["bars"]=sorted(raw["bars"]+second,key=lambda r:r["information_available_time"])
    path.write_text(json.dumps(raw))
    run=lab.run_replay;captured=[]
    def capture(bars,decisions,**kwargs):
        result=run(bars,decisions,**kwargs)
        if decisions and all(r["strategy_id"]=="buy_hold" for r in decisions):captured.append(result)
        return result
    monkeypatch.setattr(lab,"run_replay",capture)
    lab.run_research(dataset_path=path,store_path=root,config=PLAN)
    assert len(captured)==1
    benchmark=captured[0]
    assert {r["symbol"] for r in benchmark["orders"] if r["status"] in {"filled","partially_filled","accepted"}}==set(raw["symbols"])
    assert not any(r.get("reason")=="missing_or_stale_market_data" for r in benchmark["orders"])


@pytest.mark.parametrize("mode",["renamed_dataset","source_alias","venue_alias","changed_price","overlapping_subset"])
def test_sealed_overlap_is_blocked_before_feature_evaluation(inputs,monkeypatch,mode):
    path,root=inputs
    original=load_dataset(path,now=1788998400)
    with ResearchStore(root) as store:
        store.seal_holdout(original["observation_hash"],"e"*64,"f"*64,observation_keys=observation_keys(original))
    raw=json.loads(path.read_text())
    if mode=="renamed_dataset":raw["dataset_id"]="renamed-fixture"
    elif mode=="source_alias":
        raw["source"].update(source_id="aliased-source",kind="real")
        for row in raw["bars"]:row["source_id"]="aliased-source"
    elif mode=="venue_alias":
        raw["venue"]="aliased-venue"
        for row in raw["bars"]:row["venue"]="aliased-venue"
    elif mode=="changed_price":
        raw["bars"][0]["close"]="100.01"
    else:raw["bars"]=raw["bars"][10:]
    path.write_text(json.dumps(raw))
    def fail(*_args,**_kwargs):raise AssertionError("sealed features were accessed")
    monkeypatch.setattr(lab,"feature_trace",fail)
    with pytest.raises(HoldoutAccessError):lab.run_research(dataset_path=path,store_path=root,config=PLAN)
    with ResearchStore(root) as store:assert store.counts(PLAN["campaign"])["attempts"]==0


@pytest.mark.parametrize("config",[{"arbitrary_code":"print('bad')"},{"role":"holdout"},{"role":"live"},
                                   {"seed":True},{"lookbacks":[10,10]},{"bootstrap_resamples":99}])
def test_unknown_settings_and_live_or_holdout_roles_rejected(config):
    with pytest.raises(ValueError):lab.normalize_plan({**PLAN,**config})


def test_source_and_dataset_mismatch_block_reproduction_before_features(completed,monkeypatch):
    path,root,result,_=completed
    def fail(*_args,**_kwargs):raise AssertionError("mismatched evidence evaluated")
    monkeypatch.setattr(lab,"feature_trace",fail)
    monkeypatch.setattr(lab,"source_identity",lambda:{**SOURCE,"source_tree_sha256":"0"*64})
    with pytest.raises(ResearchStoreError,match="identity mismatch"):
        lab.reproduce(experiment_id=result["experiment_id"],store_path=root,dataset_path=path)
    monkeypatch.setattr(lab,"source_identity",lambda:copy.deepcopy(SOURCE))
    raw=json.loads(path.read_text());raw["bars"][0]["close"]="100.01";path.write_text(json.dumps(raw))
    with pytest.raises(ResearchStoreError,match="dataset"):
        lab.reproduce(experiment_id=result["experiment_id"],store_path=root,dataset_path=path)


def test_failed_attempt_is_durable_and_remains_in_multiplicity_denominator(inputs,monkeypatch):
    path,root=inputs
    def fail(*_args,**_kwargs):raise RuntimeError("injected feature evaluation failure")
    monkeypatch.setattr(lab,"feature_trace",fail)
    with pytest.raises(RuntimeError,match="injected"):
        lab.run_research(dataset_path=path,store_path=root,config=PLAN)
    with ResearchStore(root) as store:
        counts=store.counts(PLAN["campaign"])
        assert counts["attempts"]==1 and counts["failed"]==1 and counts["reserved"]==0
        assert counts["planned_variants"]>1
        assert counts["evaluated_variants"] is None
        row=store.list()[0]
        assert row["failure_code"]=="research_evaluation_failed"
