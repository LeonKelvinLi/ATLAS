import copy
import json
import os
import sqlite3

import pytest

from atlas.research.health import HealthStore,HealthStoreError,assess_health,historical_tail_risk

SCHEMA="a"*64


def report(**kw):
    args={"evidence_class":"TEST","reference_schema_hash":SCHEMA,"current_schema_hash":SCHEMA}
    args.update(kw)
    reference=[{"momentum":float(i%10),"volatility":1.0} for i in range(100)]
    current=[{"momentum":float(i%10),"volatility":1.0} for i in range(30)]
    return assess_health(reference,current,**args)


def test_identical_distributions_are_descriptively_healthy_without_significance():
    result=report()
    assert result["status"]=="HEALTHY"
    assert result["features"]["momentum"]["ks_distance"]==0
    assert result["features"]["momentum"]["standardized_mean_shift"]==0
    assert result["statistical_significance"] is None
    assert result["live_approved"] is False


def test_large_shift_and_constant_reference_change_degrade():
    result=assess_health([{"a":1.0}]*100,[{"a":2.0}]*30,evidence_class="TEST",reference_schema_hash=SCHEMA,current_schema_hash=SCHEMA)
    assert result["status"]=="DEGRADED"
    assert result["features"]["a"]["ks_distance"]==1
    assert result["features"]["a"]["standardized_mean_shift"] is None
    assert result["features"]["a"]["constant_reference_changed"] is True


def test_small_or_missing_sample_watches_and_does_not_invent_statistics():
    result=assess_health([{"a":1.0}]*99,[{"a":1.0}]*29,evidence_class="TEST",reference_schema_hash=SCHEMA,current_schema_hash=SCHEMA)
    assert result["status"]=="WATCH"
    assert result["features"]["a"]["ks_distance"] is None
    result=assess_health([{"a":1.0}]*100,[{"a":None}]*30,evidence_class="TEST",reference_schema_hash=SCHEMA,current_schema_hash=SCHEMA)
    assert result["status"]=="WATCH"
    assert result["features"]["a"]["current_count"]==0


@pytest.mark.parametrize("kw",[{"stale":True},{"data_quality_ok":False},{"data_quality_ok":None},
                                {"current_schema_hash":"b"*64},{"reference_schema_hash":"missing"},
                                {"evidence_class":"LIVE_PROFITABLE"}])
def test_schema_staleness_and_quality_disable(kw):
    assert report(**kw)["status"]=="DISABLED"


@pytest.mark.parametrize("bad",[float("nan"),float("inf"),True,"NaN"])
def test_nonfinite_boolean_input_disables(bad):
    result=assess_health([{"a":1.0}]*100,[{"a":bad}]*30,evidence_class="TEST",reference_schema_hash=SCHEMA,current_schema_hash=SCHEMA)
    assert result["status"]=="DISABLED"
    json.dumps(result,allow_nan=False)


def test_persistent_disable_survives_restart_and_new_healthy_report(tmp_path):
    path=tmp_path/"health.db"
    with HealthStore(path,clock=lambda:1000) as store:
        assert store.get("momentum") is None
        disabled=store.record("momentum",report(stale=True),100)
        assert disabled["status"]=="DISABLED"
    with HealthStore(path,clock=lambda:1000) as store:
        result=store.record("momentum",report(),200)
        assert result["status"]=="DISABLED"
        assert result["assessed_status"]=="HEALTHY"
        assert store.get("momentum")["sticky_disabled"]


def test_monotonic_time_and_exact_retry_preserve_history(tmp_path):
    with HealthStore(tmp_path/"health.db",clock=lambda:1000) as store:
        assessed=report();first=store.record("momentum",assessed,100)
        assert store.record("momentum",copy.deepcopy(assessed),100)==first
        with pytest.raises(HealthStoreError,match="monotonically"):store.record("momentum",assessed,99)
        with pytest.raises(HealthStoreError,match="monotonically"):store.record("momentum",report(stale=True),100)
        with pytest.raises(HealthStoreError,match="future"):store.record("momentum",assessed,1001)
        assert store.get("momentum")==first


def test_other_connection_cannot_clear_disabled_status(tmp_path):
    path=tmp_path/"health.db"
    with HealthStore(path,clock=lambda:1000) as first,HealthStore(path,clock=lambda:1000) as second:
        assert second.get("momentum") is None
        first.record("momentum",report(stale=True),100)
        assert second.record("momentum",report(),101)["status"]=="DISABLED"


def test_existing_empty_partial_or_corrupted_state_is_not_reset(tmp_path):
    empty=tmp_path/"empty.db";empty.touch();empty.chmod(0o600)
    with pytest.raises(HealthStoreError,match="empty"):HealthStore(empty)
    partial=tmp_path/"partial.db"
    c=sqlite3.connect(partial);c.execute("CREATE TABLE health(x TEXT)");c.commit();c.close();partial.chmod(0o600)
    before=partial.read_bytes()
    with pytest.raises(HealthStoreError):HealthStore(partial)
    assert partial.read_bytes()==before
    path=tmp_path/"valid.db"
    with HealthStore(path) as store:store.record("momentum",report(stale=True),100)
    c=sqlite3.connect(path);c.execute("DELETE FROM health");c.commit();c.close()
    with pytest.raises(HealthStoreError):HealthStore(path)


def test_deleted_history_and_tampered_status_are_detected(tmp_path):
    path=tmp_path/"history.db"
    with HealthStore(path) as store:store.record("momentum",report(stale=True),100)
    c=sqlite3.connect(path);c.execute("DELETE FROM events");c.commit();c.close()
    with pytest.raises(HealthStoreError):HealthStore(path)


def test_symlink_or_repository_state_rejected(tmp_path):
    path=tmp_path/"health.db"
    with HealthStore(path):pass
    alias=tmp_path/"alias.db";alias.symlink_to(path)
    with pytest.raises(HealthStoreError):HealthStore(alias)
    from pathlib import Path
    with pytest.raises(HealthStoreError,match="outside"):HealthStore(Path(__file__).resolve().parents[1]/"unsafe-health.db")


def test_commit_directory_failure_is_unknown_and_disables_reuse(tmp_path,monkeypatch):
    path=tmp_path/"health.db"
    with HealthStore(path) as store:
        def fail(_):raise OSError("injected durability failure")
        monkeypatch.setattr(store,"_sync",fail)
        with pytest.raises(HealthStoreError,match="uncertain"):store.record("momentum",report(stale=True),100)
        with pytest.raises(HealthStoreError,match="unavailable"):store.record("momentum",report(),101)
    with HealthStore(path) as store:
        assert store.get("momentum")["status"]=="DISABLED"


def test_historical_daily_tail_risk_sample_floor_and_loss_sign():
    assert historical_tail_risk([0.01]*249)["var"] is None
    result=historical_tail_risk([0.01]*230+[-0.02]*15+[-0.10]*5)
    assert result["var"]==0.02
    assert result["expected_shortfall"]>result["var"]
    assert result["daily_observations"]==250
    assert result["independence_verified"] is False
    with pytest.raises(ValueError):historical_tail_risk([True]*250)
