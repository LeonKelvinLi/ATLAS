"""Health reports distinguish request liveness, source freshness and coverage."""

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas.collection.health import market_state, report
from atlas.collection.runtime import init_runtime, run
from atlas.collection.state import connection, metadata, provider_state, save_metadata, save_provider
from test_collection_runtime import Clock, NOW, config_for, fake_result, initialized


def active(config, now, *, providers=None):
    with connection(config["state_dir"], write=True) as db:
        meta = metadata(db)
        meta.update(running=True, heartbeat_at=now, continuous_since=now - 60)
        save_metadata(db, meta)
        for name in config["providers"]:
            item = provider_state(db, name)
            symbols = ["BTC-USD"] if name == "coinbase" else ["SPY", "QQQ", "GLD", "USO"]
            item.update(last_attempt_at=now, last_success_at=now, last_observation_at=now,
                last_bar_event=now // 300 * 300, last_quote_event=now,
                last_by_symbol={symbol: {"bar": now // 300 * 300, "quote": now} for symbol in symbols})
            if name == "alpaca":
                item["calendar_coverage"] = [{"start_date": "2026-09-14", "end_date": "2026-09-14",
                    "sessions": [{"session_date": "2026-09-14", "open_time": NOW - 5400, "close_time": NOW + 18000}]}]
            save_provider(db, name, item)


def test_empty_registered_state_is_unhealthy_and_unknown_is_not_zero(tmp_path):
    config, clock = initialized(tmp_path)
    result = report(config, now=clock.value)
    assert result["status"] == "UNHEALTHY"
    assert result["providers"]["coinbase"]["last_successful_observation"] is None
    assert result["providers"]["coinbase"]["observation_age_seconds"] is None
    assert result["prospective_collection"]["continuous_duration_seconds"] is None
    assert result["archive"]["records"] == 0 and result["archive"]["integrity"] == "PASS"
    assert result["trading"]["live_trading"] == "DISABLED"
    assert result["qualification_established"] is False


def test_fresh_requests_and_source_data_are_healthy(tmp_path):
    config, clock = initialized(tmp_path)
    active(config, clock.value)
    result = report(config, now=clock.value)
    assert result["status"] == "HEALTHY" and result["blockers"] == []
    assert result["prospective_collection"]["continuous_duration_seconds"] == 60


def test_http_success_without_new_observations_does_not_claim_freshness(tmp_path):
    config, clock = initialized(tmp_path)
    active(config, clock.value)
    with connection(config["state_dir"], write=True) as db:
        item = provider_state(db, "coinbase")
        item["last_by_symbol"]["BTC-USD"] = {"bar": NOW - 3600, "quote": NOW - 3600}
        item["last_observation_at"] = NOW - 3600
        save_provider(db, "coinbase", item)
    result = report(config, now=clock.value)
    assert result["providers"]["coinbase"]["last_successful_request"] == NOW
    assert result["providers"]["coinbase"]["observation_age_seconds"] == 3600
    assert "coinbase:BTC-USD_bar_missing_or_stale" in result["blockers"]
    assert result["status"] == "UNHEALTHY"


def test_dead_heartbeat_does_not_grow_continuous_duration(tmp_path):
    config, clock = initialized(tmp_path)
    active(config, clock.value)
    result = report(config, now=clock.value + 3600)
    assert "collector_heartbeat_stale" in result["blockers"]
    assert result["prospective_collection"]["continuous_duration_seconds"] is None


def test_quote_freshness_is_per_symbol_not_provider_max(tmp_path):
    config, clock = config_for(tmp_path, providers=["alpaca"]), Clock()
    init_runtime(config, clock=clock)
    active(config, clock.value)
    with connection(config["state_dir"], write=True) as db:
        item = provider_state(db, "alpaca")
        item["last_by_symbol"]["GLD"]["quote"] -= 3600
        save_provider(db, "alpaca", item)
    result = report(config, now=clock.value)
    assert result["status"] == "UNHEALTHY"
    assert "alpaca:GLD_quote_missing_or_stale" in result["blockers"]
    assert "alpaca:SPY_quote_missing_or_stale" not in result["blockers"]


def test_verified_closed_market_does_not_report_stale_equities(tmp_path):
    config, clock = config_for(tmp_path, providers=["alpaca"]), Clock()
    init_runtime(config, clock=clock)
    active(config, clock.value)
    with connection(config["state_dir"], write=True) as db:
        item = provider_state(db, "alpaca")
        item["calendar_coverage"][0]["sessions"] = []
        item["last_by_symbol"] = {}
        save_provider(db, "alpaca", item)
    result = report(config, now=clock.value)
    assert result["providers"]["alpaca"]["market"] == "closed"
    assert result["status"] == "HEALTHY"


def test_missing_current_calendar_is_unknown_not_implicitly_closed(tmp_path):
    config, clock = config_for(tmp_path, providers=["alpaca"]), Clock()
    init_runtime(config, clock=clock)
    active(config, clock.value)
    with connection(config["state_dir"], write=True) as db:
        item = provider_state(db, "alpaca")
        item["calendar_coverage"] = []
        save_provider(db, "alpaca", item)
    result = report(config, now=clock.value)
    assert result["providers"]["alpaca"]["market"] == "unknown"
    assert "alpaca:market_calendar_unknown" in result["blockers"]


def test_calendar_early_close_and_dst_use_recorded_utc_sessions():
    close = int(datetime(2026, 11, 27, 18, tzinfo=timezone.utc).timestamp())
    coverage = [{"start_date": "2026-11-27", "end_date": "2026-11-27", "sessions": [
        {"session_date": "2026-11-27", "open_time": close - 12600, "close_time": close}]}]
    assert market_state("alpaca", close - 1, coverage) == "open"
    assert market_state("alpaca", close + 901, coverage) == "closed"
    assert market_state("alpaca", close + 86400, coverage) == "unknown"


def test_disk_reserve_failure_is_unhealthy(tmp_path):
    config, clock = initialized(tmp_path)
    active(config, clock.value)
    result = report(config, now=clock.value, disk_usage=lambda _: SimpleNamespace(used=10**9, free=0))
    assert "disk_reserve_exhausted" in result["blockers"]
    assert result["disk"]["remaining_bytes"] == 0


def test_corrupt_archive_is_unhealthy_without_repair(tmp_path):
    config, clock = initialized(tmp_path)
    active(config, clock.value)
    def fail(*a, **k):
        raise ValueError("secret must never appear")
    result = report(config, now=clock.value, archive_verify=fail)
    assert "archive_integrity_failed" in result["blockers"]
    assert result["archive"]["records"] is None
    assert "secret" not in json.dumps(result)


def test_quick_health_never_asserts_full_integrity(tmp_path):
    config, clock = initialized(tmp_path)
    active(config, clock.value)
    result = report(config, now=clock.value, verify=False)
    assert result["archive"]["integrity"] == "NOT_CHECKED"


def test_health_never_calls_acquisition_or_execution(tmp_path, monkeypatch):
    config, clock = initialized(tmp_path)
    import atlas.collection.archive as archive
    monkeypatch.setattr(archive, "acquire_collection_once", lambda *a, **k: pytest.fail("health attempted acquisition"))
    result = report(config, now=clock.value)
    assert result["trading"]["execution_enabled"] is False


def test_session_open_does_not_require_unfinished_first_bar(tmp_path):
    config, clock = config_for(tmp_path, providers=["alpaca"]), Clock()
    init_runtime(config, clock=clock)
    active(config, clock.value)
    with connection(config["state_dir"], write=True) as db:
        item = provider_state(db, "alpaca")
        item["calendar_coverage"][0]["sessions"][0]["open_time"] = NOW
        for values in item["last_by_symbol"].values():
            values["bar"] = None
        save_provider(db, "alpaca", item)
    result = report(config, now=clock.value)
    assert result["status"] == "HEALTHY"


def test_incomplete_restore_health_fails_even_if_prior_state_is_healthy(tmp_path):
    config, clock = initialized(tmp_path)
    active(config, clock.value)
    (Path(config["state_dir"]) / ".restore-incomplete").write_text("partial")
    assert "collector_incomplete_restore" in report(config, now=clock.value)["blockers"]
