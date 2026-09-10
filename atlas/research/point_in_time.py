"""Versioned causal feature definitions over explicitly published observations."""

from dataclasses import dataclass
import hashlib
import json
import math
import statistics

from atlas.research.market import DatasetError, identifier, timestamp


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def as_of_rows(bars, decision_time, symbol=None):
    """Latest published revision of each completed event, never future values.

    Acquisition time is retained as provenance. Historical public availability
    is the decision cutoff; claiming an old publication time in a file does not
    authenticate that claim (the dataset quality gate remains blocked).
    """
    timestamp(decision_time, "decision_time")
    if symbol is not None:
        identifier(symbol, "symbol")
    selected, seen = {}, set()
    for row in bars:
        if not isinstance(row, dict):
            raise DatasetError("bar must be an object")
        if symbol is not None and row.get("symbol") != symbol:
            continue
        event = timestamp(row.get("event_time"), "event_time")
        available = timestamp(row.get("information_available_time"), "information_available_time")
        if available < event:
            raise DatasetError("bar cannot be known before it is complete")
        # Do not evaluate prices or other feature inputs from a future revision.
        if event > decision_time or available > decision_time:
            continue
        revision = row.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise DatasetError("invalid revision")
        identifier(row.get("symbol"), "symbol")
        key = (row["symbol"], event)
        unique = (*key, revision)
        if unique in seen:
            raise DatasetError("duplicate revision cannot be silently resolved")
        seen.add(unique)
        previous = selected.get(key)
        if previous and ((revision > previous["revision"]) != (available > previous["information_available_time"])):
            raise DatasetError("revision order conflicts with publication order")
        if previous is None or revision > previous["revision"]:
            selected[key] = dict(row)
    return sorted(selected.values(), key=lambda r:(r["event_time"],r["symbol"]))


@dataclass(frozen=True)
class FeatureRegistry:
    """Small interpretable feature set; every value identifies exact inputs.

    Values are research descriptors, never calibrated probabilities. All rolling
    features reset across missing intervals and session dates. Definitions are
    immutable; future extensions need a new registry version and causality tests.
    """
    lookback: int = 20
    version: str = "1.0.0"

    def __post_init__(self):
        if isinstance(self.lookback, bool) or not isinstance(self.lookback, int) or not 2 <= self.lookback <= 10_000:
            raise ValueError("lookback must be an integer in 2..10000")
        if self.version != "1.0.0":
            raise ValueError("unknown feature registry version")

    def definitions(self):
        descriptions = {
            "return_1": (2,["close"],"fractional_return","close[t] / close[t-1] - 1"),
            "momentum": (self.lookback+1,["close"],"fractional_return",f"close[t] / close[t-{self.lookback}] - 1"),
            "volatility": (self.lookback+1,["close"],"unannualized_return_standard_deviation","sample standard deviation of trailing simple one-bar returns"),
            "atr": (self.lookback+1,["high","low","close"],"price","mean trailing true range including prior close; not Wilder smoothing"),
            "ma_distance": (self.lookback+1,["close"],"fractional_price_distance","mean of prior lookback closes / current close - 1"),
            "volume_zscore": (self.lookback+1,["volume"],"standard_deviations","current volume minus prior mean divided by prior sample standard deviation"),
            "spread": (1,["bid","ask"],"fraction_of_mid","(ask-bid) / ((ask+bid)/2) at publication; not a future executable quote"),
            "drawdown": (self.lookback,["close"],"fractional_drawdown","1 - current close / trailing maximum close"),
        }
        return [{"feature_id":key,"version":self.version,"lookback_observations":n,"source_fields":fields,
                 "asset_classes":["equity","crypto"],"units":units,"definition":description,
                 "earliest_availability":"maximum information_available_time of every input revision",
                 "warmup_policy":"all_required_contiguous_same_session_observations",
                 "missing_policy":"unavailable_no_imputation","session_policy":"reset_at_session_date_change",
                 "gap_policy":"reset_after_missing_or_changed_interval","causality":"past_and_current_completed_published_bars_only"}
                for key,(n,fields,units,description) in descriptions.items()]

    def snapshot(self, bars, *, symbol, decision_time):
        known = as_of_rows(bars, decision_time, symbol)
        segment = []
        for row in known:
            if row.get("asset_class") not in {"equity","crypto"}:
                raise DatasetError("feature asset class unsupported")
            interval = row.get("interval_seconds")
            if isinstance(interval,bool) or not isinstance(interval,int) or interval <= 0:
                raise DatasetError("feature inputs require an explicit interval")
            if not isinstance(row.get("session_date"),str) or not isinstance(row.get("session"),str):
                raise DatasetError("feature inputs require an explicit session")
            if segment:
                previous = segment[-1]
                if (row["event_time"]-previous["event_time"] != interval
                        or previous["interval_seconds"] != interval
                        or (row["session"],row["session_date"]) != (previous["session"],previous["session_date"])
                        or row.get("venue") != previous.get("venue")):
                    segment = []
            segment.append(row)
        definitions = self.definitions()
        values, availability, lineage = {}, {}, {}
        for definition in definitions:
            feature = definition["feature_id"]
            count = definition["lookback_observations"]
            rows = segment[-count:]
            values[feature], availability[feature] = None, None
            status = "insufficient_contiguous_history" if len(rows) < count else "available"
            numeric = []
            if status == "available":
                for row in rows:
                    item = {}
                    for field in definition["source_fields"]:
                        raw = row.get(field)
                        if raw is None:
                            status = "missing_input"
                            break
                        if isinstance(raw,bool): raise DatasetError("boolean feature input")
                        try:
                            value = float(raw)
                        except (ValueError,TypeError,OverflowError) as exc:
                            raise DatasetError("invalid feature input") from exc
                        if not math.isfinite(value) or (value < 0 if field == "volume" else value <= 0):
                            raise DatasetError("nonfinite/nonpositive feature input")
                        item[field] = value
                    if status != "available": break
                    numeric.append(item)
            if status == "available":
                closes = [row.get("close") for row in numeric]
                if feature in {"return_1","momentum"}: value = closes[-1]/closes[0]-1
                elif feature == "volatility": value = statistics.stdev([b/a-1 for a,b in zip(closes,closes[1:])])
                elif feature == "atr":
                    value = statistics.mean(max(b["high"]-b["low"],abs(b["high"]-a["close"]),abs(b["low"]-a["close"]))
                                            for a,b in zip(numeric,numeric[1:]))
                elif feature == "ma_distance": value = statistics.mean(closes[:-1])/closes[-1]-1
                elif feature == "volume_zscore":
                    volumes = [row["volume"] for row in numeric]
                    std = statistics.stdev(volumes[:-1])
                    value = (volumes[-1]-statistics.mean(volumes[:-1]))/std if std else None
                    if value is None: status = "zero_reference_variance"
                elif feature == "spread":
                    bid,ask = numeric[-1]["bid"],numeric[-1]["ask"]
                    if bid > ask: raise DatasetError("crossed feature quote")
                    value = (ask-bid)/((ask+bid)/2)
                else: value = 1-closes[-1]/max(closes)
                if value is not None and not math.isfinite(value):
                    raise DatasetError("feature calculation is nonfinite")
                values[feature] = value
                if value is not None:
                    availability[feature] = max(row["information_available_time"] for row in rows)
            input_keys = [{"symbol":r["symbol"],"event_time":r["event_time"],"revision":r["revision"],
                           "information_available_time":r["information_available_time"],"source_id":r.get("source_id")}
                          for r in rows]
            feature_inputs = [{**key,**{field:r.get(field) for field in definition["source_fields"]}}
                              for key,r in zip(input_keys,rows)]
            lineage[feature] = {"input_hash":_hash(feature_inputs),"input_keys":input_keys,
                                "observations":len(rows),"required_observations":count,"status":status}
        result = {"symbol":symbol,"decision_time":decision_time,"values":values,"availability":availability,
                  "lineage":lineage,"definitions_hash":_hash(definitions),"known_rows":len(known),
                  "contiguous_session_rows":len(segment),"feature_registry_version":self.version,
                  "limitations":["Publication metadata and source authenticity require separate verification.",
                                 "Features reset at session dates and gaps; unknown values are not zero.",
                                 "No target, forecast probability, or trading approval is inferred."]}
        assert_causal_snapshot(result)
        return result


def assert_causal_snapshot(snapshot):
    """Reject an injected future feature or future lineage input."""
    decision = timestamp(snapshot["decision_time"], "decision_time")
    for feature, value in snapshot["values"].items():
        inputs = snapshot["lineage"][feature]["input_keys"]
        if any(timestamp(row["event_time"]) > decision or timestamp(row["information_available_time"]) > decision for row in inputs):
            raise DatasetError("future data found in feature lineage")
        available = snapshot["availability"][feature]
        if value is not None and (available is None or timestamp(available) > decision
                                  or not inputs or available != max(row["information_available_time"] for row in inputs)):
            raise DatasetError("feature availability is inconsistent with its lineage")
    return True
