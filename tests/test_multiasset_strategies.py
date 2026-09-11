"""Synthetic causal bars for the bounded multiasset strategy library."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import socket
from zoneinfo import ZoneInfo

import pytest

from atlas.research.multiasset_strategies import (FAMILIES, StrategyError, StrategyUnavailable, _features, _target,
                                                 aggregate_bars, normalize_spec, signals_for, strategy_catalog)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("pure strategies cannot contact network"))


def bar(event, *, symbol="BTC-USD", interval=900, close="100", volume="100", revision=0, available=None):
    value = Decimal(str(close))
    asset = "crypto" if symbol == "BTC-USD" else "equity"
    day = datetime.fromtimestamp(event - 1, timezone.utc).date().isoformat()
    return {"symbol": symbol, "asset_class": asset, "venue": "fixture", "event_time": event,
            "information_available_time": event if available is None else available,
            "ingestion_time": event if available is None else available, "interval_seconds": interval,
            "open": str(value), "high": str(value + Decimal("0.05")), "low": str(value - Decimal("0.05")),
            "close": str(value), "volume": volume, "revision": revision, "session_date": day,
            "currency": "USD", "volume_unit": "base_asset" if asset == "crypto" else "shares",
            "session": "24/7" if asset == "crypto" else "regular", "source_id": "fixture"}


def crypto(count=40, *, interval=900, prices=None):
    start = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    return [bar(start + (index + 1) * interval, interval=interval,
                close=str(100 + (index % 3) * .1) if prices is None else str(prices[index])) for index in range(count)]


def equity(day="2026-09-01", *, symbol="SPY", interval=900, count=None, prices=None):
    opening = int(datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=ZoneInfo("America/New_York")).timestamp())
    count = 23400 // interval if count is None else count
    return [bar(opening + (index + 1) * interval, symbol=symbol, interval=interval,
                close=str(100 + index * .1) if prices is None else str(prices[index])) for index in range(count)]


def spec(family="btc_breakout", variant="donchian", **extra):
    return normalize_spec({"family": family, "variant": variant, "lookback": 5, **extra})


def calendar(days, *, early=()):
    sessions = []
    for day in sorted(days):
        opening = int(datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=ZoneInfo("America/New_York")).timestamp())
        sessions.append({"session_date": day, "open_time": opening, "close_time": opening + (12600 if day in early else 23400)})
    return {"sessions": sessions}


def test_fixed_catalog_is_bounded_unique_canonical_and_all_families_covered():
    rows = strategy_catalog()
    assert len(rows) == 59 and len({row["strategy_id"] for row in rows}) == 59
    assert {row["family"] for row in rows} == set(FAMILIES)
    for row in rows:
        assert normalize_spec(row) == row
        assert normalize_spec({"strategy_id": row["strategy_id"]}) == row
    assert {r["variant"] for r in rows if r["family"] == "equity_mean_reversion"} == set(FAMILIES["equity_mean_reversion"])
    assert all(row["symbols"] == ["GLD", "USO"] for row in rows if row["family"] == "commodity_proxy_trend")


@pytest.mark.parametrize("changes", [
    {"unknown": True}, {"family": "real_trading"}, {"variant": "future_labels"}, {"symbols": ["ETH-USD"]},
    {"symbols": [{"bad": 1}]}, {"symbols": []}, {"lookback": 10000}, {"lookback": True},
    {"z_threshold": "10"}, {"z_threshold": 2}, {"atr_multiple": "NaN"}, {"timeframe_seconds": 60},
    {"strategy_id": "forged"}, {"strategy_version": "later"}, {"position_weight": "100"}, {"fee_bps": "99999"},
])
def test_unsupported_parameter_search_and_cost_or_allocation_inputs_refuse(changes):
    with pytest.raises(StrategyError):
        normalize_spec({"family": "btc_breakout", "variant": "donchian", **changes})


def test_crypto_aggregation_requires_complete_contiguous_buckets_and_max_availability():
    rows = crypto(30, interval=60)
    rows[5].update(information_available_time=rows[14]["event_time"] + 7, ingestion_time=rows[14]["event_time"] + 7)
    combined = aggregate_bars(rows, 900)
    assert len(combined) == 2
    first = combined[0]
    assert first["event_time"] == rows[14]["event_time"]
    assert first["information_available_time"] == rows[14]["event_time"] + 7
    assert Decimal(first["open"]) == Decimal(rows[0]["open"]) and Decimal(first["close"]) == Decimal(rows[14]["close"])
    assert Decimal(first["volume"]) == 1500 and len(first["source_inputs"]) == 15
    assert aggregate_bars(rows[:14], 900) == []
    assert aggregate_bars(rows[:5] + rows[6:15], 900) == []


def test_equity_anchor_respects_new_york_dst_without_crossing_sessions():
    for day in ("2026-01-05", "2026-07-06"):
        rows = equity(day, interval=900)
        hour = aggregate_bars(rows, 3600)
        assert len(hour) == 6
        expected = int(datetime.fromisoformat(day + "T10:30:00").replace(tzinfo=ZoneInfo("America/New_York")).timestamp())
        assert hour[0]["event_time"] == expected
        assert hour[-1]["event_time"] < rows[-1]["event_time"]
        assert all(row["interval_seconds"] == 3600 for row in hour)
    two_days = equity("2026-09-01") + equity("2026-09-02")
    four_hours = aggregate_bars(two_days, 14400)
    assert len(four_hours) == 2 and all(len(row["source_inputs"]) == 16 for row in four_hours)
    assert len({row["session_date"] for row in four_hours}) == 2


def test_eight_hour_equity_bars_are_explicitly_unavailable_not_fabricated():
    with pytest.raises(StrategyUnavailable, match="8h_exceeds"):
        aggregate_bars(equity(symbol="GLD"), 28800)
    with pytest.raises(StrategyUnavailable, match="8h_exceeds"):
        signals_for(equity(symbol="GLD"), spec("commodity_proxy_trend", "momentum", timeframe_seconds=28800))


def test_daily_equity_uses_complete_390_minutes_and_partial_day_is_unavailable():
    daily = aggregate_bars(equity(symbol="GLD"), 86400)
    assert len(daily) == 1
    assert daily[0]["timeframe_seconds"] == 86400 and daily[0]["interval_seconds"] == 23400
    assert daily[0]["regular_session_daily"] and len(daily[0]["source_inputs"]) == 26
    assert aggregate_bars(equity(symbol="GLD", count=14), 86400) == []
    assert aggregate_bars(equity(symbol="GLD")[:-1], 86400) == []


def test_explicit_early_close_calendar_forms_true_daily_and_intraday_bars():
    day = "2026-11-27"
    rows = equity(day, symbol="GLD", count=14)
    cal = calendar([day], early=[day])
    daily = aggregate_bars(rows, 86400, calendar=cal)
    hourly = aggregate_bars(rows, 3600, calendar=cal)
    assert len(daily) == 1 and daily[0]["interval_seconds"] == 12600
    assert daily[0]["explicit_session_calendar"] and len(daily[0]["source_inputs"]) == 14
    assert len(hourly) == 3
    assert aggregate_bars(rows, 86400) == []
    assert aggregate_bars(rows, 14400, calendar=cal) == []


def test_short_session_partial_does_not_make_other_four_hour_sessions_unavailable():
    first, early, last = "2026-11-25", "2026-11-27", "2026-11-30"
    rows = equity(first, symbol="GLD") + equity(early, symbol="GLD", count=14) + equity(last, symbol="GLD")
    output = aggregate_bars(rows, 14400, calendar=calendar([first, early, last], early=[early]))
    assert len(output) == 2 and [row["session_date"] for row in output] == [first, last]


def test_explicit_calendar_detects_a_missing_whole_trading_session():
    settings = spec("commodity_proxy_trend", "momentum", timeframe_seconds=3600)
    rows = equity("2026-09-01", symbol="GLD") + equity("2026-09-03", symbol="GLD")
    cal = calendar(["2026-09-01", "2026-09-02", "2026-09-03"])
    output = signals_for(rows, settings, calendar=cal)
    assert output[6]["metadata"]["atr"] is None
    # Explicitly declaring only the actual listed sessions is a separate
    # calendar contract; absence is never silently inferred to be a holiday.
    contiguous = signals_for(rows, settings, calendar=calendar(["2026-09-01", "2026-09-03"]))
    assert contiguous[6]["metadata"]["atr"] is not None
    unverified = signals_for(rows, settings)
    assert unverified[6]["metadata"]["atr"] is None


def test_early_close_to_next_listed_session_retains_valid_trend_history():
    first, second = "2026-11-27", "2026-11-30"
    rows = equity(first, symbol="GLD", count=14) + equity(second, symbol="GLD")
    output = signals_for(rows, spec("commodity_proxy_trend", "momentum"), calendar=calendar([first, second], early=[first]))
    assert output[5]["metadata"]["atr"] is not None


@pytest.mark.parametrize("value", [{}, {"sessions": []}, {"sessions": [{"session_date": "bad", "open_time": 1, "close_time": 2}]}])
def test_supplied_invalid_calendar_cannot_fall_back_to_guessed_session(value):
    with pytest.raises(StrategyError, match="calendar_invalid"):
        aggregate_bars(equity(), 3600, calendar=value)


def test_revisions_only_publish_new_aggregate_revision_when_available():
    rows = crypto(15, interval=60)
    revised = deepcopy(rows[2])
    revised.update(close="90", open="90", low="89", high="91", revision=1,
                   information_available_time=rows[-1]["event_time"] + 10, ingestion_time=rows[-1]["event_time"] + 10)
    original = aggregate_bars(rows, 900)
    updated = aggregate_bars(rows + [revised], 900)
    assert updated[:1] == original
    assert updated[1]["revision"] == 1 and updated[1]["low"] == "89"
    assert updated[1]["information_available_time"] == revised["information_available_time"]
    assert updated[1]["source_inputs_sha256"] != original[0]["source_inputs_sha256"]


def test_source_hash_identifies_actual_ohlcv_values_not_only_row_timestamps():
    rows = crypto(15, interval=60)
    original = aggregate_bars(rows, 900)
    changed = deepcopy(rows)
    changed[0]["volume"] = "101"
    updated = aggregate_bars(changed, 900)
    assert original[0]["source_inputs_sha256"] != updated[0]["source_inputs_sha256"]


def test_unknown_volume_and_non_usd_currency_are_preserved_never_upgraded():
    rows = crypto(15, interval=60)
    for row in rows:
        row["volume_unit"] = "unknown"
    result = aggregate_bars(rows, 900)
    assert result[0]["volume_unit"] == "unknown"
    with pytest.raises(StrategyUnavailable, match="explicit_usd_and_volume_units"):
        signals_for(rows, spec())
    for row in rows:
        row.update(volume_unit="base_asset", currency="EUR")
    assert aggregate_bars(rows, 900)[0]["currency"] == "EUR"
    with pytest.raises(StrategyUnavailable):
        signals_for(rows, spec())


def test_missing_units_stay_unknown_and_mixed_units_cannot_be_summed():
    rows = crypto(15, interval=60)
    for row in rows:
        del row["currency"]
        del row["volume_unit"]
    aggregated = aggregate_bars(rows, 900)[0]
    assert aggregated["currency"] == aggregated["volume_unit"] == "unknown"
    rows[0]["volume_unit"] = "base_asset"
    with pytest.raises(StrategyError):
        aggregate_bars(rows, 900)


def test_signal_prefix_invariance_and_future_labels_never_enter_features():
    rows = crypto(40)
    settings = spec()
    prefix = signals_for(rows[:25], settings)
    augmented = deepcopy(rows)
    for row in augmented:
        row.update(future_return="999", profitability_label=True, next_day_high="999999")
    full = signals_for(augmented, settings)
    cutoff = rows[24]["information_available_time"]
    assert [row for row in full if row["decision_time"] <= cutoff] == prefix
    assert "future_return" not in str(full) and "next_day_high" not in str(full)
    assert all(row["metadata"]["feature_information_available_time"] <= row["decision_time"] for row in full)
    assert all(row["metadata"]["same_event_fill_allowed"] is False for row in full)
    assert all(row["expires_at"] > row["decision_time"] for row in full)


def test_late_historical_revision_does_not_rewrite_past_or_create_old_trade():
    rows = crypto(25)
    revised = deepcopy(rows[16])
    revised.update(open="101", high="101.05", low="100.95", close="101", revision=1,
                   information_available_time=rows[20]["event_time"] + 1, ingestion_time=rows[20]["event_time"] + 1)
    without = signals_for(rows, spec())
    with_revision = signals_for(rows + [revised], spec())
    cut = revised["information_available_time"]
    assert [r for r in with_revision if r["decision_time"] < cut] == [r for r in without if r["decision_time"] < cut]
    assert not any(r["decision_time"] == cut for r in with_revision)
    later = [r for r in with_revision if r["decision_time"] > cut]
    assert any(any(x["information_available_time"] == cut for x in r["metadata"]["feature_inputs"]) for r in later)


def test_delayed_batch_produces_one_current_decision_not_historical_orders():
    rows = crypto(12)
    now = rows[-1]["event_time"] + 10
    for row in rows:
        row.update(information_available_time=now, ingestion_time=now)
    output = signals_for(rows, spec())
    assert len(output) == 1 and output[0]["decision_time"] == now
    assert output[0]["metadata"]["feature_event_time"] == rows[-1]["event_time"]


def test_breakout_requires_prior_level_volume_confirmation_and_has_fakeout_exit():
    prices = [100, 100.1, 100, 100.1, 100, 100.1, 102, 100]
    rows = crypto(len(prices), prices=prices)
    rows[6]["volume"] = "300"
    output = signals_for(rows, spec())
    assert output[6]["direction"] == "long" and output[6]["metadata"]["reason"] == "breakout_entry"
    assert output[7]["direction"] == "flat" and output[7]["metadata"]["reason"] == "breakout_fakeout_exit"
    no_volume = deepcopy(rows)
    no_volume[6]["volume"] = "10"
    assert signals_for(no_volume, spec())[6]["direction"] == "flat"
    assert output[6]["metadata"]["features"]["prior_high"] < 102


@pytest.mark.parametrize("variant", FAMILIES["equity_mean_reversion"])
def test_all_mean_reversion_variants_produce_only_causal_long_flat_descriptors(variant):
    prices = [100, 101, 99, 101, 99, 100, 98, 101, 100, 99, 100, 101, 99, 100, 98, 101, 100, 99, 100, 101, 99, 100, 98, 101, 100, 99]
    output = signals_for(equity(prices=prices), spec("equity_mean_reversion", variant))
    assert len(output) == 26 and {r["target_weight"] for r in output} <= {"0", "1"}
    assert any(r["metadata"]["atr"] is not None for r in output)
    assert all(r["metadata"]["instrument_kind"] == "equity_etf" for r in output)


@pytest.mark.parametrize("variant", FAMILIES["commodity_proxy_trend"])
def test_all_proxy_trend_variants_keep_etf_identity_and_cross_session_history(variant):
    rows = []
    for offset in range(4):
        day = (datetime(2026, 9, 1) + timedelta(days=offset)).date().isoformat()
        rows += equity(day, symbol="GLD", prices=[100 + offset * 3 + i * .1 for i in range(26)])
    output = signals_for(rows, spec("commodity_proxy_trend", variant, timeframe_seconds=3600),
                         calendar=calendar(sorted({row["session_date"] for row in rows})))
    assert len(output) == 24
    assert all(r["metadata"]["instrument_kind"] == "commodity_proxy_etf" and not r["metadata"]["actual_futures_contract"] for r in output)
    assert output[-1]["metadata"]["atr"] is not None


def test_regime_labels_are_causal_and_crash_forces_flat():
    rising = crypto(10, prices=[100 + i * .2 for i in range(10)])
    assert signals_for(rising, spec())[-1]["metadata"]["regime"] == "trend"
    ranging = crypto(10, prices=[100, 101, 99, 100, 101, 99, 100, 101, 99, 100])
    assert signals_for(ranging, spec())[-1]["metadata"]["regime"] == "range"
    crash = deepcopy(rising)
    crash[-1].update(open="90", high="90.05", low="89.95", close="90")
    last = signals_for(crash, spec())[-1]
    assert last["metadata"]["regime"] == "crash" and last["direction"] == "flat"
    wide = deepcopy(ranging)
    for row in wide:
        row.update(high="105", low="95")
    assert signals_for(wide, spec())[-1]["metadata"]["regime"] == "high_volatility"


def test_missing_intervals_reset_feature_warmup_and_mean_reversion_resets_daily():
    rows = crypto(20)
    missing = signals_for(rows[:10] + rows[11:], spec())
    first_after_gap = next(r for r in missing if r["decision_time"] == rows[11]["event_time"])
    assert first_after_gap["metadata"]["atr"] is None and first_after_gap["direction"] == "flat"
    equity_rows = equity("2026-09-01") + equity("2026-09-02")
    daily = signals_for(equity_rows, spec("equity_mean_reversion", "zscore"))
    assert daily[26]["metadata"]["atr"] is None


def test_opening_range_does_not_redefine_missing_opening_bars_as_later_bars():
    rows = equity()[2:]
    output = signals_for(rows, spec("equity_mean_reversion", "opening_range"))
    assert all(not row["metadata"]["features"]["opening_complete"] for row in output if row["metadata"]["features"])


@pytest.mark.parametrize("mutation", ["nan", "invalid_ohlc", "duplicate", "revision_without_original", "before_complete", "mixed_interval", "wrong_session"])
def test_bad_bar_contracts_fail_closed(mutation):
    rows = crypto(15)
    if mutation == "nan": rows[0]["close"] = "NaN"
    elif mutation == "invalid_ohlc": rows[0]["high"] = "1"
    elif mutation == "duplicate": rows.append(deepcopy(rows[0]))
    elif mutation == "revision_without_original": rows[0]["revision"] = 1
    elif mutation == "before_complete": rows[0]["information_available_time"] -= 1
    elif mutation == "mixed_interval": rows[0]["interval_seconds"] = 60
    else: rows[0]["session_date"] = "2020-01-01"
    with pytest.raises(StrategyError):
        aggregate_bars(rows, 900)


def test_inputs_and_catalog_results_are_independent_mutation_safe_values():
    rows, settings = crypto(), spec()
    before = deepcopy((rows, settings))
    signals_for(rows, settings)
    assert (rows, settings) == before
    catalog = strategy_catalog()
    catalog[0]["symbols"].append("EVIL")
    assert "EVIL" not in strategy_catalog()[0]["symbols"]
