"""Timestamp-based train/validation/test windows with outcome-resolution purge.

No fitting, file access, holdout access or inference. These are scheduling
contracts: labels and their publication timestamps must come from the dataset.
"""

from atlas.research.market import DatasetError, timestamp


def _duration(value, name, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be {'nonnegative' if allow_zero else 'positive'} integer seconds")
    return value


def timestamp_folds(samples, *, train_seconds, validation_seconds, test_seconds,
                    step_seconds=None, mode="anchored", embargo_seconds=0,
                    holdout_start=None):
    """Create complete, chronologically ordered expanding or rolling folds.

    Each sample requires event_time, information_available_time, label_end_time;
    optional label_available_time must be >= label_end_time. If omitted, the
    explicit assumption is that the label was published when the outcome ended.
    Features and labels must both be known strictly before their window ends.
    Embargo is unobserved time between training/validation and validation/test.
    Holdout rows are excluded before even forming windows or reading labels.
    """
    train_seconds = _duration(train_seconds,"train_seconds")
    validation_seconds = _duration(validation_seconds,"validation_seconds")
    test_seconds = _duration(test_seconds,"test_seconds")
    step_seconds = test_seconds if step_seconds is None else _duration(step_seconds,"step_seconds")
    embargo_seconds = _duration(embargo_seconds,"embargo_seconds",allow_zero=True)
    if step_seconds < test_seconds:
        raise ValueError("test windows must not overlap; step_seconds must be >= test_seconds")
    if mode not in {"anchored","rolling"}:
        raise ValueError("mode must be anchored or rolling")
    if holdout_start is not None: timestamp(holdout_start,"holdout_start")
    rows, previous, seen = [], -1, set()
    assumptions = set()
    for index, sample in enumerate(samples):
        if not isinstance(sample,dict): raise DatasetError("sample must be an object")
        event = timestamp(sample.get("event_time"),"event_time")
        if event < previous: raise DatasetError("samples must be chronologically ordered; no silent sorting")
        previous = event
        # No target access for the sealed suffix, including malformed targets.
        if holdout_start is not None and event >= holdout_start: continue
        available = timestamp(sample.get("information_available_time"),"information_available_time")
        if holdout_start is not None and available >= holdout_start: continue
        label_end = timestamp(sample.get("label_end_time"),"label_end_time")
        label_available = timestamp(sample.get("label_available_time",label_end),"label_available_time")
        if not event <= available < label_end <= label_available:
            raise DatasetError("sample feature/outcome/publication chronology is invalid")
        key = sample.get("sample_id",(sample.get("symbol"),event))
        try:
            if key in seen: raise DatasetError("duplicate sample identity")
            seen.add(key)
        except TypeError as exc: raise DatasetError("invalid sample identity") from exc
        if "label_available_time" not in sample:
            assumptions.add("label_available_time omitted: outcome end is explicitly assumed to equal label publication")
        rows.append({"index":index,"event":event,"available":available,"label_end":label_end,"label_available":label_available})
    if not rows: return []
    beginning = rows[0]["event"]
    data_end = max(r["event"] for r in rows)+1
    if holdout_start is not None: data_end = min(data_end,holdout_start)
    training_end = beginning+train_seconds
    folds = []
    while True:
        validation_start = training_end+embargo_seconds
        validation_end = validation_start+validation_seconds
        test_start = validation_end+embargo_seconds
        test_end = test_start+test_seconds
        if test_end > data_end: break
        training_start = beginning if mode == "anchored" else training_end-train_seconds
        bounds = {"train":(training_start,training_end),"validation":(validation_start,validation_end),"test":(test_start,test_end)}
        fold = {"fold_id":len(folds),"mode":mode,"embargo_seconds":embargo_seconds,"holdout_accessed":False,
                "ranges":{name:{"start":start,"end_exclusive":end} for name,(start,end) in bounds.items()},
                "purged":{},"warnings":sorted(assumptions)}
        for name,(start,end) in bounds.items():
            candidates = [r for r in rows if start <= r["event"] < end]
            usable = [r for r in candidates if r["available"] < end and r["label_available"] < end]
            fold[f"{name}_indices"] = [r["index"] for r in usable]
            fold["purged"][name] = {"candidate_count":len(candidates),"kept":len(usable),
                                     "unresolved_at_window_end":len(candidates)-len(usable)}
        if all(fold[f"{name}_indices"] for name in bounds):
            folds.append(fold)
        training_end += step_seconds
    return folds


def validate_fold(samples, fold, *, holdout_start=None):
    """Independently reject tampered overlapping or future-label fold indices."""
    previous_end, seen = None, set()
    for name in ("train","validation","test"):
        bounds = fold["ranges"][name]
        start,end = timestamp(bounds["start"]),timestamp(bounds["end_exclusive"])
        if end <= start or (previous_end is not None and start < previous_end+fold["embargo_seconds"]):
            raise DatasetError("invalid fold chronology or embargo")
        if holdout_start is not None and end > holdout_start:
            raise DatasetError("fold enters sealed holdout")
        previous_end = end
        for index in fold[f"{name}_indices"]:
            if isinstance(index,bool) or not isinstance(index,int) or index < 0 or index >= len(samples) or index in seen:
                raise DatasetError("duplicate or invalid fold index")
            seen.add(index)
            row = samples[index]
            if not (start <= row["event_time"] < end and row["information_available_time"] < end
                    and row.get("label_available_time",row["label_end_time"]) < end):
                raise DatasetError("fold contains future information or an unresolved label")
    return True
