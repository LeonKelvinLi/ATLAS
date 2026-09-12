import copy
from datetime import datetime, time, timezone
from decimal import Decimal, Inexact, localcontext
from zoneinfo import ZoneInfo

import pytest

from atlas.research.causal_screener import MAX_INPUT_BARS, ScreenerError, normalize_spec, scan_dataset


START = 1735689600


def payload(prices=(105, 104, 103, 102, 101, 100), *, volumes=None, interval=900):
    volumes = volumes if volumes is not None else [10] * (len(prices) - 1) + [30]
    bars = []
    for i, (price, volume) in enumerate(zip(prices, volumes)):
        event = START + (i + 1) * interval
        bars.append({"symbol": "BTC-USD", "asset_class": "crypto", "venue": "fixture",
                     "event_time": event, "interval_seconds": interval, "information_available_time": event + 1,
                     "ingestion_time": event + 2, "timezone": "UTC", "session": "24/7",
                     "session_date": datetime.fromtimestamp(event - 1, timezone.utc).date().isoformat(),
                     "open": str(price), "high": str(Decimal(price) + 1), "low": str(Decimal(price) - 1),
                     "close": str(price), "volume": str(volume), "revision": 0, "source_id": "fixture-v1"})
    return {"schema_version": "1.0", "dataset_id": "fixture-v1",
            "source": {"source_id": "fixture-v1", "kind": "test", "description": "Deterministic software fixture"},
            "asset_class": "crypto", "symbols": ["BTC-USD"], "venue": "fixture", "price_adjustment": "unadjusted",
            "universe": {"selection_method": "fixed fixture", "survivorship_bias": "unknown"},
            "availability_basis": "synthetic", "revision_policy": "synthetic", "volume_unit": "base_asset",
            "currency": "USD", "bars": bars}


def spec(**kwargs):
    return {"symbol": "BTC-USD", "venue": "fixture", "asset_class": "crypto", "currency": "USD",
            "rsi_length": 3, "volume_lookback": 3, **kwargs}


def scan(data, settings=None, *, as_of=None, now=None):
    end = max(row["ingestion_time"] for row in data["bars"])
    return scan_dataset(data, settings or spec(), as_of=end if as_of is None else as_of,
                        now=end if now is None else now)


def revise(row, *, price, published, ingested=None):
    return {**row, "revision": row["revision"] + 1, "close": str(price), "open": str(price),
            "high": str(price + 1), "low": str(price - 1), "information_available_time": published,
            "ingestion_time": published if ingested is None else ingested}


def test_three_times_prior_volume_matches_and_preserves_input():
    data = payload()
    before = copy.deepcopy(data)
    result = scan(data)
    assert result["status"] == "AVAILABLE" and result["matched"] is True
    assert result["rsi"] == "0" and result["volume_ratio"] == "3"
    assert result["prior_volume_mean"] == "10"
    assert result["spec"]["rsi_method"] == "simple_trailing_gain_share"
    assert result["source_evidence"]["evidence_class"] == "TEST"
    assert result["trading_eligible"] is False and data == before


@pytest.mark.parametrize("volume,expected", [(20, False), ("29.999999999999999999", False), (30, True), (31, True)])
def test_two_hundred_percent_increase_means_three_times_baseline(volume, expected):
    result = scan(payload(volumes=[10, 10, 10, 10, 10, volume]))
    assert result["matched"] is expected
    assert result["prior_volume_mean"] == "10"  # The spike is excluded from its own baseline.


def test_rsi_threshold_is_strict_and_uses_exact_cross_products():
    result = scan(payload(prices=(10, 13, 6)), spec(rsi_length=2, volume_lookback=2))
    assert result["rsi"] == "30"
    assert result["rsi_condition_met"] is False and result["matched"] is False


def test_future_bars_do_not_change_full_earlier_observation_or_hashes():
    full = payload(prices=(110, 109, 108, 107, 106, 105, 300, 400))
    earlier = {**full, "bars": full["bars"][:6]}
    cutoff = earlier["bars"][-1]["ingestion_time"]
    assert scan(full, as_of=cutoff) == scan(earlier, as_of=cutoff)


def test_future_revision_is_excluded_then_changes_only_later_as_of():
    data = payload()
    cutoff = data["bars"][-1]["ingestion_time"]
    earlier = scan(data)
    data["bars"].append(revise(data["bars"][-2], price=150, published=cutoff + 100))
    assert scan(data, as_of=cutoff) == earlier
    later = scan(data)
    assert later["matched"] is False
    assert later["feature_inputs_sha256"] != earlier["feature_inputs_sha256"]
    assert later["feature_information_available_time"] == cutoff + 100
    assert later["feature_observed_time"] == cutoff + 100
    assert later["observation"]["information_available_time"] < later["feature_information_available_time"]
    assert any(row["revision"] == 1 for row in later["feature_inputs"])
    assert earlier["matched"] is True


def test_publication_and_ingestion_are_both_required():
    data = payload()
    final = data["bars"][-1]
    cutoff = final["event_time"] + 10
    final["information_available_time"] = cutoff + 1
    final["ingestion_time"] = cutoff + 2
    prefix = {**data, "bars": data["bars"][:-1]}
    assert scan(data, as_of=cutoff) == scan(prefix, as_of=cutoff, now=cutoff)
    final["information_available_time"] = final["event_time"]
    assert scan(data, as_of=cutoff) == scan(prefix, as_of=cutoff, now=cutoff)


def test_historical_download_cannot_create_a_prior_observation():
    data = payload()
    cutoff = data["bars"][-1]["ingestion_time"]
    for row in data["bars"]:
        row["ingestion_time"] = cutoff + 100
    result = scan(data, as_of=cutoff)
    assert result["unavailable_reason"] == "no_closed_observed_bars_as_of"
    assert result["matched"] is None


def test_visible_revision_without_observed_original_is_unavailable():
    data = payload()
    cutoff = data["bars"][-1]["ingestion_time"]
    original = data["bars"][-2]
    original["ingestion_time"] = cutoff + 100
    data["bars"].append(revise(original, price=150, published=cutoff + 50))
    result = scan(data, as_of=cutoff + 50)
    assert result["unavailable_reason"] == "revision_history_not_observed_as_of"


@pytest.mark.parametrize("kind,label", [("real", "HISTORICAL_UNVERIFIED"), ("unknown", "HISTORICAL_UNVERIFIED"),
                                       ("synthetic", "SYNTHETIC"), ("test", "TEST")])
def test_source_labels_cannot_grant_validation_eligibility(kind, label):
    data = payload()
    data["source"]["kind"] = kind
    result = scan(data)
    assert result["source_evidence"]["evidence_class"] == label
    assert result["source_evidence"]["provider_authenticity_verified"] is False
    assert result["source_evidence"]["alpha_validation_eligible"] is False


def test_unknown_volume_is_unavailable_instead_of_zero_or_false():
    data = payload()
    data["volume_unit"] = "unknown"
    result = scan(data)
    assert result["unavailable_reason"] == "volume_unit_unknown"
    assert result["volume_ratio"] is None and result["matched"] is None


def test_warmup_needs_rsi_changes_and_prior_volume_observations():
    result = scan(payload(prices=(5, 4, 3)))
    assert result["unavailable_reason"] == "insufficient_contiguous_history"
    assert result["required_contiguous_bars"] == 4 and result["contiguous_bars_available"] == 3


def test_recent_gap_resets_warmup_without_repairing_bars():
    data = payload()
    del data["bars"][-2]
    before = copy.deepcopy(data)
    result = scan(data)
    assert result["unavailable_reason"] == "insufficient_contiguous_history"
    assert result["history_boundary"] == "gap" and result["contiguous_bars_available"] == 1
    assert data == before


def test_old_gap_does_not_contaminate_sufficient_recent_history():
    data = payload(prices=(10, 9, 8, 7, 6, 5, 4, 3))
    del data["bars"][1]
    assert scan(data)["matched"] is True


def test_undefined_flat_rsi_and_zero_volume_baseline_are_unavailable():
    flat = scan(payload(prices=(5, 5, 5, 5)))
    zero = scan(payload(volumes=(0, 0, 0, 0, 0, 30)))
    assert flat["unavailable_reason"] == "rsi_zero_total_price_change"
    assert zero["unavailable_reason"] == "zero_prior_volume_baseline"
    assert flat["matched"] is zero["matched"] is None


@pytest.mark.parametrize("interval", [60, 300, 900])
def test_native_timeframes_preserve_last_bar_lineage(interval):
    data = payload(interval=interval)
    result = scan(data, spec(timeframe_seconds=interval))
    assert result["matched"] is True
    assert result["feature_inputs"][-1]["event_time"] == data["bars"][-1]["event_time"]


def test_resampling_uses_complete_causal_buckets():
    data = payload(prices=tuple(range(160, 100, -1)), volumes=[10] * 45 + [30] * 15, interval=60)
    result = scan(data, spec(rsi_length=2, volume_lookback=2))
    assert result["matched"] is True and result["volume_ratio"] == "3"
    cutoff = data["bars"][44]["ingestion_time"]
    prefix = {**data, "bars": data["bars"][:45]}
    assert scan(data, spec(rsi_length=2, volume_lookback=2), as_of=cutoff) == scan(prefix, spec(rsi_length=2, volume_lookback=2))
    del data["bars"][-5]
    result = scan(data, spec(rsi_length=2, volume_lookback=2))
    assert result["unavailable_reason"] == "latest_completed_bucket_has_missing_source_bars"
    assert result["matched"] is None


def test_partial_next_bucket_keeps_dated_last_complete_observation():
    data = payload(prices=tuple(range(160, 110, -1)), volumes=[10] * 30 + [30] * 20, interval=60)
    result = scan(data, spec(rsi_length=2, volume_lookback=2))
    assert result["status"] == "AVAILABLE" and result["matched"] is True
    assert result["observation"]["event_time"] == START + 45 * 60
    assert result["observation"]["latest_source_event_time"] == START + 50 * 60
    assert result["observation"]["age_seconds"] == 302


def test_resampling_work_limit_is_explicit_without_large_computation():
    data = payload(prices=[100] * 1400, interval=60)
    result = scan(data, spec(timeframe_seconds=86400))
    assert result["unavailable_reason"] == "aggregation_work_limit"


def test_unsupported_finer_resampling_does_not_fake_native_bars():
    result = scan(payload(interval=60), spec(timeframe_seconds=300))
    assert result["unavailable_reason"] == "sub_15m_resampling_not_supported_use_native_bars"


def test_other_declared_symbols_never_enter_the_features():
    data = payload()
    baseline = scan(data)
    data["symbols"].append("ETH-USD")
    data["bars"] = sorted(data["bars"] + [{**row, "symbol": "ETH-USD", "volume": "999999"} for row in data["bars"]],
                          key=lambda row: row["information_available_time"])
    assert scan(data) == baseline


@pytest.mark.parametrize("field,value", [("venue", "other"), ("currency", "EUR"), ("volume_unit", "unknown"),
                                        ("symbol", "ETH-USD"), ("information_available_time", START)])
def test_inconsistent_bar_contracts_are_rejected(field, value):
    data = payload()
    data["bars"][2][field] = value
    with pytest.raises(ScreenerError, match="dataset_invalid"):
        scan(data)


@pytest.mark.parametrize("settings", [spec(venue="other"), spec(currency="EUR"), spec(symbol="ETH-USD")])
def test_spec_must_match_dataset_instrument(settings):
    with pytest.raises(ScreenerError, match="instrument_contract_mismatch"):
        scan(payload(), settings)


@pytest.mark.parametrize("change", [{"rsi_method": "Wilder"}, {"asset_class": "futures"}, {"rsi_length": True},
                                    {"rsi_length": 257}, {"volume_lookback": 1}, {"minimum_volume_ratio": "2"},
                                    {"minimum_volume_ratio": 3}, {"rsi_below": "NaN"}, {"rsi_below": "100"},
                                    {"timeframe_seconds": 0}, {"execute": True}])
def test_invalid_or_unbounded_specs_are_rejected(change):
    with pytest.raises(ScreenerError):
        normalize_spec(spec(**change))


def test_specs_are_normalized_without_mutation_and_bind_parameters():
    settings = spec(minimum_volume_ratio="3.00")
    before = copy.deepcopy(settings)
    one = scan(payload(), settings)
    assert settings == before and one["spec"]["minimum_volume_ratio"] == "3"
    assert one["spec_sha256"] == scan(payload())["spec_sha256"]
    assert one["spec_sha256"] != scan(payload(), spec(rsi_below="25"))["spec_sha256"]


def test_financial_math_ignores_ambient_decimal_precision_and_traps():
    data = payload(prices=(10, 12, 9, 11, 10, 7))
    expected = scan(data)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert scan(data) == expected


def test_bounds_and_invalid_clocks_fail_without_work_or_network():
    data = payload()
    data["bars"] = data["bars"][:1] * (MAX_INPUT_BARS + 1)
    with pytest.raises(ScreenerError, match="bounded"):
        scan(data)
    with pytest.raises(ScreenerError, match="clock_invalid"):
        scan(payload(), as_of=True)
    with pytest.raises(ScreenerError, match="clock_invalid"):
        scan(payload(), as_of=START + 10000, now=START + 9999)


def equity(days, *, count=None):
    data = payload()
    data.update(asset_class="equity", symbols=["SPY"], volume_unit="shares", bars=[])
    sessions = []
    for day_index, day in enumerate(days):
        date = datetime.strptime(day, "%Y-%m-%d").date()
        opening = int(datetime.combine(date, time(9, 30), ZoneInfo("America/New_York")).timestamp())
        closing = opening + 23400
        sessions.append({"session_date": day, "open_time": opening, "close_time": closing})
        for i in range(26 if count is None else count):
            row = payload(prices=(100 - day_index,), volumes=[30 if day_index == len(days) - 1 else 10])["bars"][0]
            event = opening + (i + 1) * 900
            row.update(symbol="SPY", asset_class="equity", session="regular", session_date=day,
                       event_time=event, information_available_time=event + 1, ingestion_time=event + 2)
            data["bars"].append(row)
    data["calendar"] = {"calendar_id": "fixture", "timezone": "America/New_York", "version": "1",
                        "source_id": "fixture-v1", "sessions": sessions}
    return data


def equity_spec(**kwargs):
    return spec(symbol="SPY", asset_class="equity", rsi_length=2, volume_lookback=2, **kwargs)


def test_equity_intraday_resets_at_the_session_boundary():
    data = equity(["2025-01-02", "2025-01-03"], count=2)
    result = scan(data, equity_spec())
    assert result["unavailable_reason"] == "insufficient_contiguous_history"
    assert result["history_boundary"] == "session_boundary"


def test_equity_daily_uses_explicit_adjacent_sessions_and_detects_missing_session():
    data = equity(["2025-01-02", "2025-01-03", "2025-01-06"])
    settings = equity_spec(timeframe_seconds=86400)
    result = scan(data, settings)
    assert result["matched"] is True and result["volume_ratio"] == "3"
    data["bars"] = [row for row in data["bars"] if row["session_date"] != "2025-01-03"]
    result = scan(data, settings)
    assert result["unavailable_reason"] == "insufficient_contiguous_history"
    assert result["history_boundary"] == "gap"


def test_equity_eight_hour_contract_is_explicitly_unavailable():
    result = scan(equity(["2025-01-02"]), equity_spec(timeframe_seconds=28800))
    assert result["unavailable_reason"] == "equity_8h_exceeds_regular_session"


def test_future_equity_calendar_and_bars_do_not_change_earlier_snapshot():
    data = equity(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"])
    earlier = copy.deepcopy(data)
    earlier["bars"] = [row for row in earlier["bars"] if row["session_date"] != "2025-01-07"]
    earlier["calendar"]["sessions"].pop()
    cutoff = earlier["bars"][-1]["ingestion_time"]
    settings = equity_spec(timeframe_seconds=86400)
    assert scan(data, settings, as_of=cutoff) == scan(earlier, settings)
