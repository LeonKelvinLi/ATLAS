"""Offline process/restart evidence for the dedicated collector."""

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

import pytest

from atlas.collection.config import CollectionError, load_config, validate_config
from atlas.collection.runtime import backoff_seconds, expected_events, init_runtime, run
from atlas.collection.state import connection, file_lock, metadata, save_metadata, snapshot


NOW = int(datetime(2026, 9, 14, 15, tzinfo=timezone.utc).timestamp())


class Clock:
    def __init__(self, value=NOW):
        self.value = value
    def __call__(self):
        return self.value


class Stop:
    def __init__(self, clock):
        self.clock, self.stopped = clock, False
    def is_set(self):
        return self.stopped
    def set(self):
        self.stopped = True
    def wait(self, seconds):
        self.clock.value += seconds
        return self.stopped


def config_for(tmp_path, *, providers=None):
    state = tmp_path.resolve() / "state"
    providers = ["coinbase"] if providers is None else providers
    return validate_config({"version": 1, "mode": "data_collection", "live_trading": False,
        "state_dir": str(state), "archive_dir": str(state / "archives" / "v1"),
        "generation_id": "v1", "research_start": NOW - 179 * 86400,
        "providers": providers, "alpaca_key_file": str(tmp_path.resolve() / "secret.json") if "alpaca" in providers else None})


def fake_result(clock, kwargs, *, missing=()):
    rows = [{"symbol": "BTC-USD", "record_type": "bar", "event_time": t}
            for t in range(kwargs["start"] + 300, kwargs["end"] + 1, 300) if t not in missing]
    rows.append({"symbol": "BTC-USD", "record_type": "quote", "event_time": clock.value})
    return {"collection_complete": True, "collection_observations": rows,
            "collection_calendar_coverage": [], "appended_records": len(rows)}


def initialized(tmp_path):
    config, clock = config_for(tmp_path), Clock()
    init_runtime(config, clock=clock)
    return config, clock


def test_explicit_init_preserves_registration_and_refuses_existing(tmp_path):
    config, clock = initialized(tmp_path)
    before = snapshot(config)
    with pytest.raises(CollectionError, match="new_state"):
        init_runtime(config, clock=clock)
    assert snapshot(config) == before
    assert before["metadata"]["registered_at"] == NOW
    assert before["metadata"]["test_clock"] is True


def test_installer_empty_private_directory_is_allowed(tmp_path):
    config, clock = config_for(tmp_path), Clock()
    Path(config["state_dir"]).mkdir(mode=0o700)
    init_runtime(config, clock=clock)
    assert snapshot(config)["metadata"]["registered_at"] == NOW


@pytest.mark.parametrize("change", [
    {"mode": "live"}, {"live_trading": True}, {"live_trading": "false"},
    {"order_endpoint": "anything"}, {"poll_seconds": True}, {"poll_seconds": 1},
    {"overlap_seconds": 901}, {"max_backoff_seconds": 10}, {"providers": []},
    {"providers": ["coinbase", "coinbase"]}, {"providers": ["iqoption"]},
    {"generation_id": "../escape"}, {"research_start": True}, {"research_start": NOW + 1},
    {"alpaca_key_file": "/tmp/unneeded.json"}, {"archive_dir": "/tmp/other"},
])
def test_configuration_refuses_execution_or_ambiguous_inputs(tmp_path, change):
    config = config_for(tmp_path)
    with pytest.raises((CollectionError, ValueError)):
        validate_config({**config, **change})


@pytest.mark.parametrize("name", ["LIVE_TRADING", "ATLAS_LIVE_TRADING_ENABLED", "ATLAS_LIVE_AUTO_ENABLED"])
def test_execution_environment_is_independent_refusal(tmp_path, monkeypatch, name):
    config = config_for(tmp_path)
    monkeypatch.setenv(name, "true")
    with pytest.raises(CollectionError, match="execution_environment"):
        validate_config(config)


def test_duplicate_json_key_private_config_refused(tmp_path):
    path = tmp_path.resolve() / "config.json"
    path.write_text('{"version":1,"version":1}')
    path.chmod(0o600)
    with pytest.raises(CollectionError, match="json_invalid"):
        load_config(str(path))


def test_secret_inside_backup_tree_refused(tmp_path):
    config = config_for(tmp_path, providers=["alpaca"])
    config["alpaca_key_file"] = str(Path(config["state_dir"]) / "secret.json")
    with pytest.raises(CollectionError, match="secret_inside"):
        validate_config(config)


def test_retry_backoff_is_bounded_and_respects_server_minimum():
    values = [backoff_seconds(n, poll_seconds=60, maximum=3600) for n in range(1, 30)]
    assert values[:4] == [60, 120, 240, 480]
    assert values[-1] == 3600
    assert backoff_seconds(1, poll_seconds=60, maximum=3600, retry_after=7200) == 7200
    assert backoff_seconds(1, poll_seconds=60, maximum=3600, retry_after="secret") == 60


def test_request_failure_retry_checkpoint_and_resume(tmp_path):
    config, clock = initialized(tmp_path)
    def refused(*args, **kwargs):
        return {"collection_complete": False, "error": "SHOULD_NOT_BE_LOGGED", "retry_after_seconds": 120}
    logs = []
    run(config, once=True, clock=clock, acquire=refused, log=lambda *args, **kwargs: logs.append((args, kwargs)))
    state = snapshot(config)["providers"]["coinbase"]
    assert state["failures"] == 1
    assert state["next_attempt_at"] == NOW + 120
    assert state["last_success_at"] is None
    assert "SHOULD_NOT_BE_LOGGED" not in json.dumps(logs)
    called = []
    def success(*args, **kwargs):
        called.append(kwargs)
        return fake_result(clock, kwargs)
    clock.value += 60
    assert run(config, once=True, clock=clock, acquire=success, log=lambda *a, **k: None)["attempts"] == 0
    clock.value += 60
    run(config, once=True, clock=clock, acquire=success, log=lambda *a, **k: None)
    state = snapshot(config)["providers"]["coinbase"]
    assert len(called) == 1 and state["last_success_at"] == clock.value and state["failures"] == 0


def test_zero_capture_http_failure_remains_diagnosable_after_recovery(tmp_path, monkeypatch):
    from atlas.collection.archive import acquire_collection_once
    from atlas.research.data_collector import DataCollectorError
    from atlas.research import data_collector
    config, clock = initialized(tmp_path)
    def unavailable(*args, **kwargs):
        raise DataCollectorError("collector_http_refused", status=503, retry_after_seconds=120)
    monkeypatch.setattr(data_collector, "_exchange_get", unavailable)
    logs = []
    run(config, once=True, clock=clock, acquire=acquire_collection_once,
        log=lambda *a, **k: logs.append((a, k)))
    failed = snapshot(config)["providers"]["coinbase"]
    assert failed["last_error"] == "collector_http_refused"
    assert failed["next_attempt_at"] == NOW + 120
    assert not list((Path(config["archive_dir"]) / "days").iterdir())
    with connection(config["state_dir"]) as db:
        before = db.execute("SELECT body FROM events WHERE kind='request_failed'").fetchone()[0]
    diagnostic = json.loads(before)
    assert diagnostic["error"] == "collector_http_refused"
    assert diagnostic["request_role"] == "candles"
    assert diagnostic["http_status"] == 503
    assert diagnostic["retry_after_seconds"] == diagnostic["retry_in_seconds"] == 120
    assert diagnostic["duration_seconds"] == 0
    assert next(k for a, k in logs if a[0] == "request_failed")["error"] == diagnostic["error"]
    clock.value += 120
    run(config, once=True, clock=clock, acquire=lambda *a, **k: fake_result(clock, k),
        log=lambda *a, **k: None)
    assert snapshot(config)["providers"]["coinbase"]["last_error"] is None
    with connection(config["state_dir"]) as db:
        assert db.execute("SELECT body FROM events WHERE kind='request_failed'").fetchone()[0] == before
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='reconnected'").fetchone()[0] == 1


@pytest.mark.parametrize("unsafe", ["PRIVATE_TOKEN", {"PRIVATE_TOKEN": 1}, ["PRIVATE_TOKEN"], True, 1.5])
def test_failure_diagnostics_never_persist_arbitrary_values(tmp_path, unsafe):
    config, clock = initialized(tmp_path)
    logs = []
    run(config, once=True, clock=clock,
        acquire=lambda *a, **k: {"collection_complete": False, "error": unsafe,
            "http_status": unsafe, "request_role": unsafe, "retry_after_seconds": unsafe},
        log=lambda *a, **k: logs.append((a, k)))
    with connection(config["state_dir"]) as db:
        row = json.loads(db.execute("SELECT body FROM events WHERE kind='request_failed'").fetchone()[0])
    assert row["error"] == "collector_request_or_response_invalid"
    assert row["http_status"] is row["request_role"] is row["retry_after_seconds"] is None
    assert row["retry_in_seconds"] == 60
    assert "PRIVATE_TOKEN" not in json.dumps([row, logs, snapshot(config)])


def test_outage_live_first_then_old_data_is_explicit_backfill(tmp_path):
    config, clock = initialized(tmp_path)
    calls = []
    def success(*args, **kwargs):
        calls.append(kwargs)
        return fake_result(clock, kwargs)
    clock.value += 3 * 86400
    run(config, once=True, clock=clock, acquire=success, log=lambda *a, **k: None)
    assert [call["acquisition_mode"] for call in calls] == ["outage_backfill", "outage_backfill"]
    assert calls[1]["start"] == NOW
    assert calls[1]["end"] - calls[1]["start"] == 86400
    state = snapshot(config)
    assert state["providers"]["coinbase"]["recovery_cursor"] == NOW + 86400
    assert state["gap_count"] == 1


def test_missing_mature_bar_recorded_and_later_resolution_preserves_incident(tmp_path):
    config, clock = initialized(tmp_path)
    missing = NOW - 300
    run(config, once=True, clock=clock,
        acquire=lambda *a, **k: fake_result(clock, k, missing={missing}), log=lambda *a, **k: None)
    assert snapshot(config)["unresolved_gaps"] == 1
    clock.value += 60
    run(config, once=True, clock=clock, acquire=lambda *a, **k: fake_result(clock, k), log=lambda *a, **k: None)
    state = snapshot(config)
    assert state["gap_count"] == 1 and state["unresolved_gaps"] == 0


def test_process_lock_prevents_second_writer(tmp_path):
    config, clock = initialized(tmp_path)
    with file_lock(config["state_dir"], ".collector.lock"):
        with pytest.raises(CollectionError, match="lock_unavailable"):
            run(config, once=True, clock=clock, acquire=lambda *a, **k: None)


def test_snapshot_lock_prevents_writes_during_backup(tmp_path):
    config, clock = initialized(tmp_path)
    with file_lock(config["state_dir"], ".snapshot.lock"):
        with pytest.raises(CollectionError, match="lock_unavailable"):
            run(config, once=True, clock=clock, acquire=lambda *a, **k: None)


def test_restore_marker_forces_historical_recovery_once(tmp_path):
    config, clock = initialized(tmp_path)
    clock.value += 3600
    marker = Path(config["state_dir"]) / "restore-marker.json"
    marker.write_text(json.dumps({"version": 1, "restored_at": clock.value,
        "backup_id": "atlas-backup-test", "manifest_sha256": "a" * 64, "collection_must_record_gap": True}))
    marker.chmod(0o600)
    calls = []
    def collect(*a, **k):
        calls.append(k)
        return fake_result(clock, k)
    run(config, once=True, clock=clock, acquire=collect, log=lambda *a, **k: None)
    assert calls[-1]["acquisition_mode"] == "outage_backfill"
    with connection(config["state_dir"]) as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='restore_discontinuity'").fetchone()[0] == 1
    clock.value += 60
    run(config, once=True, clock=clock, acquire=collect, log=lambda *a, **k: None)
    with connection(config["state_dir"]) as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='restore_discontinuity'").fetchone()[0] == 1


def test_incomplete_restore_refuses_start(tmp_path):
    config, clock = initialized(tmp_path)
    (Path(config["state_dir"]) / ".restore-incomplete").write_text("incomplete")
    with pytest.raises(CollectionError, match="incomplete_restore"):
        run(config, once=True, clock=clock, acquire=lambda *a, **k: None)


def test_corrupt_state_is_retained_without_reinitializing(tmp_path):
    config, clock = initialized(tmp_path)
    path = Path(config["state_dir"]) / "state.sqlite3"
    path.write_bytes(b"corrupt")
    with pytest.raises(CollectionError):
        run(config, once=True, clock=clock, acquire=lambda *a, **k: None)
    assert path.read_bytes() == b"corrupt"


def test_clock_regression_refuses_before_network(tmp_path):
    config, clock = initialized(tmp_path)
    clock.value -= 60
    with pytest.raises(CollectionError, match="clock_regressed"):
        run(config, once=True, clock=clock, acquire=lambda *a, **k: pytest.fail("network"))


def test_missing_intervals_calendar_holiday_and_early_close():
    start = NOW // 86400 * 86400
    expected, known = expected_events("alpaca", start, start + 86400, [])
    assert not known and all(not values for values in expected.values())
    date = "2026-09-14"
    coverage = [{"start_date": "2026-09-13", "end_date": date, "sessions": []}]
    assert expected_events("alpaca", start, NOW, coverage)[1] is True
    coverage[0]["sessions"] = [{"session_date": date, "open_time": NOW, "close_time": NOW + 900}]
    expected, known = expected_events("alpaca", NOW, NOW + 1800, coverage)
    assert known and expected["SPY"] == {NOW + 300, NOW + 600, NOW + 900}


def test_600_bounded_cycles_with_faults_and_restarts(tmp_path):
    config, clock = config_for(tmp_path), Clock()
    config["poll_seconds"] = 30
    init_runtime(config, clock=clock)
    count = 0
    def collect(*args, **kwargs):
        nonlocal count
        count += 1
        if count % 50 == 0:
            return {"collection_complete": False, "retry_after_seconds": 60}
        return fake_result(clock, kwargs)
    result = run(config, clock=clock, acquire=collect, stop_event=Stop(clock), max_cycles=600, log=lambda *a, **k: None)
    assert result["cycles"] == 600 and result["attempts"] >= 580
    state = snapshot(config)
    assert state["metadata"]["running"] is False
    assert state["providers"]["coinbase"]["last_success_at"] >= clock.value - 120
    assert state["event_count"] >= 1160
    assert (Path(config["state_dir"]) / "state.sqlite3").stat().st_size < 1024**2


def test_real_sigterm_fake_foreground_process(tmp_path):
    # This child is an explicit bounded fake process; no worker/service installed.
    config = config_for(tmp_path)
    config["research_start"] = int(__import__("time").time()) // 300 * 300 - 179 * 86400
    path = tmp_path.resolve() / "fake-process.py"
    source_root = str(Path(__file__).resolve().parents[1])
    path.write_text("import sys,time,json\n"
        + "sys.path.insert(0," + repr(source_root) + ")\n"
        + "from atlas.collection.runtime import init_runtime,run\n"
        + "config=" + repr(config) + "\n"
        + "init_runtime(config,clock=time.time)\n"
        + "def fake(*a,**k):\n return {'collection_complete':False,'retry_after_seconds':60}\n"
        + "run(config,clock=time.time,acquire=fake)\n")
    process = subprocess.Popen([sys.executable, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([process.stdout], [], [], 10)[0], "child failed to start"
        assert '"event": "started"' in process.stdout.readline()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert '"event": "stopped"' in stdout
        assert snapshot(config)["metadata"]["running"] is False
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_reconnect_recent_bars_cannot_be_promoted_to_prospective(tmp_path, monkeypatch):
    """Run the real archive/runtime boundary against a fake pinned HTTP getter."""
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit
    import time
    from atlas.research import data_collector
    config, clock = config_for(tmp_path), Clock()
    monkeypatch.setattr(time, "time", clock)
    def get(url, headers, **options):
        query = parse_qs(urlsplit(url).query)
        if "candles" in url:
            start = int(datetime.fromisoformat(query["start"][0].replace("Z", "+00:00")).timestamp())
            end = int(datetime.fromisoformat(query["end"][0].replace("Z", "+00:00")).timestamp())
            body = [[stamp, 99, 102, 100, 101, 12] for stamp in range(start, end, 300)]
        else:
            body = {"sequence": clock.value, "bids": [["100", "1", 1]],
                "asks": [["101", "1", 1]],
                "time": datetime.fromtimestamp(clock.value, timezone.utc).isoformat()}
        return SimpleNamespace(status=200, body=json.dumps(body).encode())
    monkeypatch.setattr(data_collector, "_exchange_get", get)
    init_runtime(config)
    run(config, once=True, log=lambda *a, **k: None)
    clock.value += 1800
    run(config, once=True, log=lambda *a, **k: None)
    clock.value += 60
    run(config, once=True, log=lambda *a, **k: None)
    rows = []
    for path in Path(config["archive_dir"]).glob("days/*/archive.sqlite3"):
        with sqlite3.connect(path) as db:
            rows.extend(json.loads(body) for (body,) in db.execute("SELECT body FROM observations"))
    reconnect = [row for row in rows if row["event_time"] == NOW + 1800]
    assert {row["record_type"] for row in reconnect} == {"bar", "quote"}
    assert all(row["acquisition_kind"] == "historical_backfill" for row in reconnect)
    next_quote = [row for row in rows if row["record_type"] == "quote" and row["event_time"] == NOW + 1860]
    assert len(next_quote) == 1 and next_quote[0]["acquisition_kind"] == "prospective_observation"


@pytest.mark.parametrize("provider", ["alpaca", "coinbase"])
@pytest.mark.parametrize("offset,expected_end,receipt", [
    (-0.5, NOW - 300, NOW),
    (0, NOW, NOW),
    (0.5, NOW, NOW + 1),
])
def test_request_window_boundary_uses_completed_time_with_ceil_receipts(
        tmp_path, provider, offset, expected_end, receipt):
    """Exercise real runtime/archive/provider validation with only HTTP faked."""
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit
    from atlas.collection.archive import acquire_collection_once

    config = config_for(tmp_path, providers=[provider])
    clock = Clock(NOW - 120)
    init_runtime(config, clock=clock)
    clock.value = NOW + offset
    requests, results = [], []

    def get(url, headers, **options):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        requests.append(parsed.path)
        if parsed.path.endswith("/calendar"):
            body = [{"date": "2026-09-14", "open": "09:30", "close": "16:00"}]
        elif parsed.path.endswith("/quotes/latest"):
            body = {"quotes": {}}
        elif parsed.path.endswith("/book"):
            body = {"sequence": 1, "bids": [["100", "1", 1]], "asks": [["101", "1", 1]],
                    "time": datetime.fromtimestamp(int(clock.value), timezone.utc).isoformat()}
        else:
            start, end = (int(datetime.fromisoformat(query[key][0].replace("Z", "+00:00")).timestamp())
                          for key in ("start", "end"))
            if provider == "alpaca":
                # The Alpaca request expresses its exclusive end as end - 1.
                end += 1
                bars = [{"t": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                         "o": 100, "h": 102, "l": 99, "c": 101, "v": 12}
                        for t in range(start, end, 300)]
                body = {"bars": {symbol: bars for symbol in ("SPY", "QQQ", "GLD", "USO")},
                        "next_page_token": None}
            else:
                body = [[t, 99, 102, 100, 101, 12] for t in range(start, end, 300)]
            assert end == expected_end
        return SimpleNamespace(status=200, body=json.dumps(body).encode())

    def acquire(root, **kwargs):
        # An injected transport requires no credential file and remains TEST.
        kwargs["key_file"] = None
        result = acquire_collection_once(root, transport=get, **kwargs)
        results.append(result)
        return result

    run(config, once=True, clock=clock, acquire=acquire, log=lambda *a, **k: None)
    assert len(results) == 1 and results[0]["collection_complete"] is True, results
    assert len(requests) == (3 if provider == "alpaca" else 2)
    state = snapshot(config)
    assert state["providers"][provider]["failures"] == 0
    assert state["providers"][provider]["last_success_at"] == receipt
    with connection(config["state_dir"]) as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind IN ('request_failed','reconnected')").fetchone()[0] == 0
    rows = []
    for path in Path(config["archive_dir"]).glob("days/*/archive.sqlite3"):
        with sqlite3.connect(path) as db:
            rows.extend(json.loads(body) for (body,) in db.execute("SELECT body FROM observations"))
    assert rows and all(row["acquisition_kind"] == "test" for row in rows)
    assert {row["ingestion_time"] for row in rows} == {receipt}
    assert max(row["event_time"] for row in rows if row["record_type"] == "bar") == expected_end


def test_sqlite_crash_rollback_precedes_readonly_verification(tmp_path):
    config, clock = initialized(tmp_path)
    path = Path(config["state_dir"]) / "state.sqlite3"
    code = ("import sqlite3,os\n"
        + "db=sqlite3.connect(" + repr(str(path)) + ")\n"
        + "db.execute('PRAGMA cache_size=1')\n"
        + "db.execute('BEGIN IMMEDIATE')\n"
        + "db.execute(\"UPDATE providers SET body=?\",('x'*100000,))\n"
        + "os._exit(9)\n")
    assert subprocess.run([sys.executable, "-c", code], timeout=10).returncode == 9
    assert Path(str(path) + "-journal").exists()
    run(config, once=True, clock=clock, acquire=lambda *a, **k: fake_result(clock, k), log=lambda *a, **k: None)
    assert snapshot(config)["providers"]["coinbase"]["last_success_at"] == NOW


def test_runtime_shutdown_on_disk_full_preserves_checkpoint(tmp_path, monkeypatch):
    config, clock = initialized(tmp_path)
    from types import SimpleNamespace
    monkeypatch.setattr("atlas.collection.runtime.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(CollectionError, match="disk_reserve"):
        run(config, once=True, clock=clock, acquire=lambda *a, **k: pytest.fail("should not acquire"), log=lambda *a, **k: None)
    assert snapshot(config)["metadata"]["running"] is False


def test_wrong_contract_refuses_before_acquisition(tmp_path):
    config, clock = initialized(tmp_path)
    from atlas.collection.archive import verify_collection_archive
    def wrong(*args, **kwargs):
        return {**verify_collection_archive(*args, **kwargs), "contract_sha256": "f" * 64}
    with pytest.raises(CollectionError, match="registration_mismatch"):
        run(config, once=True, clock=clock, acquire=lambda *a, **k: pytest.fail("acquisition"), archive_verify=wrong)


def test_state_can_relocate_without_rewriting_registration(tmp_path):
    config, clock = initialized(tmp_path)
    target = tmp_path.resolve() / "relocated"
    Path(config["state_dir"]).rename(target)
    relocated = {**config, "state_dir": str(target), "archive_dir": str(target / "archives" / "v1"), "poll_seconds": 30}
    assert snapshot(relocated)["metadata"]["registered_at"] == NOW
    run(relocated, once=True, clock=clock, acquire=lambda *a, **k: fake_result(clock, k), log=lambda *a, **k: None)
