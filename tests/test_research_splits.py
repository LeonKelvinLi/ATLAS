import copy

import pytest

from atlas.research.market import DatasetError
from atlas.research.splits import timestamp_folds,validate_fold


def samples():
    return [{"symbol":"XYZ","event_time":1000+i*10,"information_available_time":1001+i*10,
             "label_end_time":1010+i*10,"label_available_time":1012+i*10} for i in range(100)]


def make_folds(rows,**kw):
    return timestamp_folds(rows,train_seconds=200,validation_seconds=100,test_seconds=100,embargo_seconds=20,**kw)


def test_anchored_and_rolling_timestamp_windows_purge_actual_labels():
    rows=samples();rows[18]["label_end_time"]=1300;rows[18]["label_available_time"]=1301
    anchored=make_folds(rows);rolling=make_folds(rows,mode="rolling")
    assert anchored and len(anchored)==len(rolling)
    assert 18 not in anchored[0]["train_indices"]
    assert anchored[-1]["ranges"]["train"]["start"]==1000
    assert rolling[-1]["ranges"]["train"]["start"]>1000
    for fold in anchored+rolling:assert validate_fold(rows,fold)
    assert anchored[0]["purged"]["train"]["unresolved_at_window_end"]>=2


def test_irregular_times_and_late_labels_are_not_row_count_purges():
    rows=samples();del rows[10:13]
    rows[15]["information_available_time"]=1199
    rows[15]["label_end_time"]=1250;rows[15]["label_available_time"]=1251
    folds=make_folds(rows)
    assert 15 not in folds[0]["train_indices"]
    assert all(validate_fold(rows,f) for f in folds)


def test_sealed_holdout_targets_are_never_accessed():
    rows=samples()
    for row in rows:
        if row["event_time"]>=1700:
            row["label_end_time"]="sealed";row["label_available_time"]="sealed"
    folds=make_folds(rows,holdout_start=1700)
    assert folds and all(f["ranges"]["test"]["end_exclusive"]<=1700 for f in folds)
    assert all(validate_fold(rows,f,holdout_start=1700) for f in folds)
    assert all(rows[i]["event_time"]<1700 for f in folds for kind in ["train","validation","test"] for i in f[kind+"_indices"])


def test_labels_not_published_before_validation_start_do_not_enter_training():
    rows=samples();rows[5]["label_available_time"]=1300
    fold=make_folds(rows)[0]
    assert 5 not in fold["train_indices"]
    tampered=copy.deepcopy(fold);tampered["train_indices"].append(5)
    with pytest.raises(DatasetError,match="unresolved"):validate_fold(rows,tampered)


@pytest.mark.parametrize("kw",[{"mode":"random"},{"step_seconds":50},{"step_seconds":True},{"embargo_seconds":-1}])
def test_invalid_split_configuration_rejected(kw):
    args=dict(train_seconds=200,validation_seconds=100,test_seconds=100,embargo_seconds=20);args.update(kw)
    with pytest.raises(ValueError):timestamp_folds(samples(),**args)


def test_duplicate_out_of_order_and_future_feature_chronology_rejected():
    rows=samples();rows.insert(1,dict(rows[0]))
    with pytest.raises(DatasetError,match="duplicate"):make_folds(rows)
    rows=samples();rows[0],rows[1]=rows[1],rows[0]
    with pytest.raises(DatasetError,match="chronologically"):make_folds(rows)
    rows=samples();rows[0]["information_available_time"]=rows[0]["label_end_time"]
    with pytest.raises(DatasetError,match="chronology"):make_folds(rows)


def test_missing_label_publication_is_an_explicit_assumption():
    rows=samples()
    for row in rows:row.pop("label_available_time")
    folds=make_folds(rows)
    assert folds[0]["warnings"] and "assumed" in folds[0]["warnings"][0]
