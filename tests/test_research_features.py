import copy

import pytest

from atlas.research.market import DatasetError,validate_dataset
from atlas.research.point_in_time import FeatureRegistry,as_of_rows,assert_causal_snapshot
from test_research_market import dataset,NOW


def rows():
    raw=dataset(30)
    for i,row in enumerate(raw["bars"]):
        row.update(open=str(100+i),high=str(102+i),low=str(99+i),close=str(101+i),volume=str(10+i),
                   bid=str(100+i),ask=str(102+i))
    return validate_dataset(raw,now=NOW)["bars"]


def test_prefix_and_future_mutation_do_not_change_past_features():
    bars=rows();decision=bars[24]["information_available_time"]
    registry=FeatureRegistry()
    before=registry.snapshot(bars[:25],symbol="BTC-USD",decision_time=decision)
    bars[-1]["close"]="not read before publication"
    after=registry.snapshot(bars,symbol="BTC-USD",decision_time=decision)
    assert after==before
    assert before["values"]["momentum"]==pytest.approx(125/105-1)
    assert before["values"]["atr"]==3
    assert before["values"]["drawdown"]==0
    assert before["values"]["spread"]==pytest.approx(2/125)


def test_late_revision_enters_only_after_publication():
    bars=rows();decision=bars[-1]["information_available_time"]
    revision={**bars[-1],"revision":1,"close":"128","information_available_time":decision+10,"ingestion_time":decision+20}
    registry=FeatureRegistry()
    before=registry.snapshot(bars,symbol="BTC-USD",decision_time=decision)
    bars.append(revision)
    assert registry.snapshot(bars,symbol="BTC-USD",decision_time=decision)==before
    after=registry.snapshot(bars,symbol="BTC-USD",decision_time=decision+10)
    assert after["values"]["return_1"] != before["values"]["return_1"]
    assert after["availability"]["return_1"]==decision+10
    assert after["lineage"]["return_1"]["input_keys"][-1]["revision"]==1


def test_duplicate_and_inconsistent_revision_rejected():
    bars=rows();bars.append(dict(bars[-1]))
    with pytest.raises(DatasetError,match="duplicate"):as_of_rows(bars,NOW)
    bars=rows();bars.append({**bars[-1],"revision":1,"information_available_time":bars[-1]["information_available_time"]})
    with pytest.raises(DatasetError,match="revision order"):as_of_rows(bars,NOW)


def test_gap_and_session_reset_rolling_history():
    bars=rows();del bars[20]
    snapshot=FeatureRegistry().snapshot(bars,symbol="BTC-USD",decision_time=NOW)
    assert snapshot["contiguous_session_rows"]==9
    assert snapshot["values"]["momentum"] is None
    assert snapshot["values"]["return_1"] is not None
    bars=rows();bars[-1]["session_date"]="2025-01-02"
    snapshot=FeatureRegistry().snapshot(bars,symbol="BTC-USD",decision_time=NOW)
    assert snapshot["contiguous_session_rows"]==1
    assert snapshot["values"]["return_1"] is None


def test_zero_volume_variance_and_missing_quotes_are_unknown():
    bars=rows()
    for row in bars:
        row["volume"]="0";row.pop("bid");row.pop("ask")
    snapshot=FeatureRegistry().snapshot(bars,symbol="BTC-USD",decision_time=NOW)
    assert snapshot["values"]["volume_zscore"] is None
    assert snapshot["lineage"]["volume_zscore"]["status"]=="zero_reference_variance"
    assert snapshot["values"]["spread"] is None


def test_future_injected_value_or_lineage_rejected():
    snapshot=FeatureRegistry().snapshot(rows(),symbol="BTC-USD",decision_time=NOW)
    tampered=copy.deepcopy(snapshot);tampered["availability"]["momentum"]=NOW+1
    with pytest.raises(DatasetError):assert_causal_snapshot(tampered)
    tampered=copy.deepcopy(snapshot);tampered["lineage"]["momentum"]["input_keys"][0]["information_available_time"]=NOW+1
    with pytest.raises(DatasetError):assert_causal_snapshot(tampered)


def test_registry_definitions_bind_lookback_and_empty_history():
    a=FeatureRegistry(lookback=20);b=FeatureRegistry(lookback=10)
    sa=a.snapshot([],symbol="BTC-USD",decision_time=NOW)
    sb=b.snapshot([],symbol="BTC-USD",decision_time=NOW)
    assert sa["definitions_hash"]!=sb["definitions_hash"]
    assert all(v is None for v in sa["values"].values())
    assert len(a.definitions())==8
    assert all(d["missing_policy"]=="unavailable_no_imputation" for d in a.definitions())
