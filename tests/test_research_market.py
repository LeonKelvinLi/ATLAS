import copy
from datetime import datetime, timezone
import json

import pytest

from atlas.research.market import DatasetError, load_dataset, observation_keys, quality_report, validate_dataset

NOW = 1735776000


def dataset(n=5):
    bars = []
    for i in range(n):
        event = 1735689600 + (i+1)*60
        bars.append({"symbol":"BTC-USD","asset_class":"crypto","venue":"test-venue",
                     "event_time":event,"interval_seconds":60,"information_available_time":event+1,
                     "ingestion_time":event+2,"timezone":"UTC","session":"24/7","session_date":"2025-01-01",
                     "open":"100","high":"102","low":"99","close":"101","volume":"10",
                     "revision":0,"source_id":"generated-v1"})
    return {"schema_version":"1.0","dataset_id":"fixture-v1",
            "source":{"source_id":"generated-v1","kind":"synthetic","description":"Deterministic software fixture"},
            "asset_class":"crypto","symbols":["BTC-USD"],"venue":"test-venue",
            "price_adjustment":"unadjusted","universe":{"selection_method":"fixed fixture","survivorship_bias":"unknown"},
            "availability_basis":"synthetic","revision_policy":"synthetic","volume_unit":"base_asset","currency":"USD","bars":bars}


def test_canonical_hash_is_content_based_and_metadata_preserved():
    raw = dataset()
    one = validate_dataset(raw,now=NOW)
    two = validate_dataset(copy.deepcopy(raw),now=NOW+10)
    assert one["dataset_hash"] == two["dataset_hash"]
    assert one["bars"][0]["close"] == "101"
    assert one["descriptor"]["price_adjustment"] == "unadjusted"
    assert one["evidence_class"] == "SYNTHETIC"
    assert one["quality"]["provider_authenticity_verified"] is False
    assert one["observation_keys"]==observation_keys(one)
    raw["bars"][0]["close"] = "100.5"
    assert validate_dataset(raw,now=NOW)["dataset_hash"] != one["dataset_hash"]


@pytest.mark.parametrize("field,value",[
    ("event_time",True),("event_time",-1),("event_time",1.5),("interval_seconds",False),
    ("interval_seconds",0),("information_available_time",1735689600),("ingestion_time",NOW+1),
    ("close","NaN"),("open","Infinity"),("volume",-1),("high","100"),("low","101.5"),
    ("revision",True),("revision",1),("timezone","America/New_York"),
    ("session","regular"),("session_date","2025-01-02"),("asset_class","fx_binary"),
    ("source_id","different"),("venue","different"),("symbol","ETH-USD"),
])
def test_rejects_invalid_bars_without_repair(field,value):
    raw=dataset();raw["bars"][0][field]=value
    with pytest.raises(DatasetError): validate_dataset(raw,now=NOW)
    assert quality_report(raw,now=NOW)["valid"] is False


def test_missing_timestamp_or_unknown_key_never_assumed():
    raw=dataset();del raw["bars"][0]["information_available_time"]
    with pytest.raises(DatasetError): validate_dataset(raw,now=NOW)
    raw=dataset();raw["bars"][0]["timestamp"]=raw["bars"][0]["event_time"]
    with pytest.raises(DatasetError): validate_dataset(raw,now=NOW)


def test_missing_bars_are_disclosed_and_never_filled():
    raw=dataset();del raw["bars"][2]
    result=validate_dataset(raw,now=NOW)
    assert len(result["bars"]) == 4
    assert result["quality"]["gaps"][0]["missing_bars"] == 1
    assert "missing_expected_bars" in result["quality"]["blockers"]


def test_revision_must_follow_original_publication_and_is_preserved():
    raw=dataset()
    revision={**raw["bars"][0],"revision":1,"information_available_time":1735689999,
              "ingestion_time":1735690000,"close":"100.25","revision_reason":"provider correction"}
    raw["bars"].append(revision)
    result=validate_dataset(raw,now=NOW)
    assert len(result["bars"]) == 6
    assert result["quality"]["revision_count"] == 1
    raw["bars"].append(copy.deepcopy(revision))
    with pytest.raises(DatasetError):validate_dataset(raw,now=NOW)


def test_unsorted_publications_are_rejected():
    raw=dataset();raw["bars"][0],raw["bars"][1]=raw["bars"][1],raw["bars"][0]
    with pytest.raises(DatasetError,match="ordered"):validate_dataset(raw,now=NOW)


def test_claimed_real_source_cannot_authenticate_itself():
    raw=dataset();raw["source"]["kind"]="real"
    raw["availability_basis"]="observed";raw["revision_policy"]="as_observed"
    raw["universe"]["survivorship_bias"]="mitigated"
    result=validate_dataset(raw,now=NOW)
    assert result["evidence_class"] == "HISTORICAL_UNVERIFIED"
    assert result["quality"]["alpha_validation_eligible"] is False
    assert "provider_authenticity_not_verified" in result["quality"]["blockers"]


def test_observation_hash_survives_dataset_and_source_aliases():
    raw=dataset();before=validate_dataset(raw,now=NOW)
    raw["dataset_id"]="renamed";raw["source"].update(source_id="renamed-source",kind="real",description="renamed source")
    for row in raw["bars"]:
        row["source_id"]="renamed-source";row["ingestion_time"]+=10
    after=validate_dataset(raw,now=NOW)
    assert after["dataset_hash"]!=before["dataset_hash"]
    assert after["observation_hash"]==before["observation_hash"]
    raw["bars"][0]["close"]="100.25"
    assert validate_dataset(raw,now=NOW)["observation_hash"]!=before["observation_hash"]
    assert observation_keys(validate_dataset(raw,now=NOW))==observation_keys(before)
    subset={**before,"bars":before["bars"][2:]}
    assert set(observation_keys(subset)) < set(observation_keys(before))


def test_quote_pair_and_spread_are_explicit():
    raw=dataset();raw["bars"][0]["bid"]="100"
    with pytest.raises(DatasetError): validate_dataset(raw,now=NOW)
    raw["bars"][0]["ask"]="99"
    with pytest.raises(DatasetError,match="crossed"): validate_dataset(raw,now=NOW)
    raw["bars"][0]["ask"]="101"
    assert validate_dataset(raw,now=NOW)["quality"]["spread_observations"]==1


def test_volume_units_are_explicit_propagated_and_unknown_never_assumed():
    raw=dataset()
    result=validate_dataset(raw,now=NOW)
    assert all(row["volume_unit"]=="base_asset" for row in result["bars"])
    raw["volume_unit"]="unknown"
    result=validate_dataset(raw,now=NOW)
    assert "volume_unit_unknown_no_fill_capacity_assumption" in result["quality"]["blockers"]
    raw["volume_unit"]="quote_currency"
    with pytest.raises(DatasetError,match="volume unit"):validate_dataset(raw,now=NOW)
    raw=dataset();raw["bars"][0]["volume_unit"]="shares"
    with pytest.raises(DatasetError,match="volume unit"):validate_dataset(raw,now=NOW)


def test_currency_is_explicit_and_foreign_prices_are_not_usd():
    raw=dataset();del raw["currency"]
    with pytest.raises(DatasetError):validate_dataset(raw,now=NOW)
    raw=dataset();raw["currency"]="EUR"
    result=validate_dataset(raw,now=NOW)
    assert all(row["currency"]=="EUR" for row in result["bars"])
    assert "non_usd_currency_requires_explicit_fx_accounting" in result["quality"]["blockers"]
    raw["bars"][0]["currency"]="USD"
    with pytest.raises(DatasetError,match="currency"):validate_dataset(raw,now=NOW)


def equity_dataset():
    raw=dataset(1)
    opening=int(datetime(2025,1,2,14,30,tzinfo=timezone.utc).timestamp())
    closing=opening+23400
    raw.update(asset_class="equity",symbols=["XYZ"],venue="XNYS",volume_unit="shares",
               calendar={"calendar_id":"operator-schedule","timezone":"America/New_York","version":"v1","source_id":"schedule-source",
                         "sessions":[{"session_date":"2025-01-02","open_time":opening,"close_time":closing}]})
    raw["bars"][0].update(symbol="XYZ",asset_class="equity",venue="XNYS",session="regular",session_date="2025-01-02",
                         event_time=opening+60,information_available_time=opening+61,ingestion_time=opening+62)
    return raw,closing+60


def test_equity_requires_explicit_regular_calendar_bounds():
    raw,now=equity_dataset()
    result=validate_dataset(raw,now=now)
    assert "calendar_authenticity_not_verified" in result["quality"]["blockers"]
    bad=copy.deepcopy(raw);del bad["calendar"]
    with pytest.raises(DatasetError,match="calendar"):validate_dataset(bad,now=now)
    bad=copy.deepcopy(raw);bad["bars"][0]["event_time"]-=60
    with pytest.raises(DatasetError,match="session boundary"):validate_dataset(bad,now=now)


def test_equity_declared_weekend_and_wrong_dst_bounds_rejected():
    raw,now=equity_dataset()
    raw["calendar"]["sessions"][0]["open_time"]+=3600
    with pytest.raises(DatasetError,match="bounds"):validate_dataset(raw,now=now)


def test_missing_entire_declared_equity_session_is_reported():
    raw,now=equity_dataset()
    first=raw["calendar"]["sessions"][0]
    for offset in (86400,4*86400):
        opening=first["open_time"]+offset
        day=datetime.fromtimestamp(opening,timezone.utc).date().isoformat()
        raw["calendar"]["sessions"].append({"session_date":day,"open_time":opening,"close_time":opening+23400})
    final=raw["calendar"]["sessions"][-1]
    bar={**raw["bars"][0],"event_time":final["open_time"]+60,"information_available_time":final["open_time"]+61,
         "ingestion_time":final["open_time"]+62,"session_date":final["session_date"]}
    raw["bars"].append(bar)
    result=validate_dataset(raw,now=bar["ingestion_time"]+1)
    full_session=next(g for g in result["quality"]["gaps"] if g.get("session_date")=="2025-01-03")
    assert full_session["missing_bars"]==390


def test_strict_json_duplicate_and_nonfinite_and_size_limit(tmp_path,monkeypatch):
    p=tmp_path/"bad.json"
    p.write_text('{"bars": [], "bars": []}')
    with pytest.raises(DatasetError,match="duplicate"):load_dataset(p,now=NOW)
    p.write_text('{"x":NaN}')
    with pytest.raises(DatasetError,match="nonfinite"):load_dataset(p,now=NOW)
    p.write_text(json.dumps(dataset()))
    assert load_dataset(p,now=NOW)["quality"]["valid"]
    monkeypatch.setattr("atlas.research.market.MAX_DATASET_BYTES",10)
    with pytest.raises(DatasetError,match="maximum size"):load_dataset(p,now=NOW)
