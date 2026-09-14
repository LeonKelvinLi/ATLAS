"""Offline CLI integration: immutable registration, intake and data-only reads.

The normal CLI uses its real-clock code path with time.time monkeypatched. An
explicitly injected archive is tested separately so test evidence cannot be
mistaken for a production receipt or independently authenticated market data.
"""

import argparse
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import urllib.request

import pytest

import atlas.research.data_archive as archive
import atlas.research.multiasset_lab as lab
import atlas.research.multiasset_validation as validation
import atlas.research.replay as replay
from atlas.research.data_contract_v1 import VERSION, default_contract
import scripts.atlas_data as cli


NOW = int(datetime(2026, 9, 13, tzinfo=timezone.utc).timestamp())
START = NOW - 180 * 86400
REPO = Path(__file__).resolve().parents[1]


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def offline_data_only(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        pytest.fail("data CLI invoked network, strategy evaluation or execution")

    monkeypatch.setattr(archive.time, "time", lambda: NOW)
    for target, names in (
        (socket.socket, ("connect", "connect_ex")),
        (socket, ("create_connection", "getaddrinfo")),
        (urllib.request, ("urlopen",)),
        (lab, ("run_campaign", "evaluate", "signals_for", "benchmark", "metrics_for")),
        (validation, ("metrics_for", "stress_diagnostics", "rank_candidates", "gate_result")),
        (replay, ("run_replay",)),
    ):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr(replay.ReplayEngine, "__init__", forbidden)
    # Tests which exercise collect-once explicitly replace this with a fake.
    monkeypatch.setattr(cli, "acquire_once", forbidden)
    yield
    assert attempts == []


def run(capsys, *arguments):
    assert cli.main([str(value) for value in arguments]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def refused(capsys, *arguments):
    with pytest.raises(SystemExit) as error:
        cli.main([str(value) for value in arguments])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "error:" in output.err
    return output.err


@pytest.fixture
def root(tmp_path, capsys):
    path = tmp_path / "observations"
    run(capsys, "init", "--archive", path, "--generation", "cli-v1", "--research-start", iso(START))
    return path


def disk_bytes(root):
    return {path.name: path.read_bytes() for path in root.iterdir()}


def stored_json(root, table):
    assert table in {"manifest", "strategy_freeze"}
    with sqlite3.connect((root / "archive.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        return json.loads(db.execute(f"SELECT body FROM {table}").fetchone()[0])


def bundle(*, event=NOW-300, received=NOW, kind="historical_backfill"):
    raw = json.dumps({"fixture": True, "event": event, "note": "exact raw receipt — retained"}).encode()
    row = {
        "contract_version": VERSION, "record_type": "bar", "symbol": "BTC-USD", "asset_class": "crypto",
        "venue": "coinbase-exchange", "source_id": "coinbase-exchange-candles", "event_time": event,
        "publication_time": None, "available_time": received, "ingestion_time": received,
        "revision": 0, "previous_observation_hash": None, "raw_capture_sha256": hashlib.sha256(raw).hexdigest(),
        "acquisition_kind": kind, "revision_basis": "locally_observed_versions", "interval_seconds": 300,
        "session": None, "payload": {"open": "100", "high": "100", "low": "100", "close": "100",
        "volume": "1", "volume_unit": "unknown", "currency": "USD", "price_adjustment": "unadjusted",
        "corporate_actions_status": "not_applicable"},
    }
    capture = {"role": "bars", "request_url": "https://api.exchange.coinbase.com/products/BTC-USD/candles",
               "received_at": received, "body_base64": base64.b64encode(raw).decode("ascii")}
    return {"records": [row], "captures": [capture], "calendar_coverage": []}


def write_bundle(tmp_path, value=None):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(bundle() if value is None else value))
    return path


def test_init_creates_exact_contract_with_system_clock_and_private_storage(tmp_path, capsys):
    path = tmp_path / "generation"
    result = run(capsys, "init", "--archive", path, "--generation", "cli-v1", "--research-start", iso(START))
    assert result["record_count"] == result["batch_count"] == result["orders_submitted"] == 0
    assert result["execution_enabled"] is result["qualification_established"] is False
    manifest = stored_json(path, "manifest")
    assert manifest["clock_basis"] == "system_utc"
    assert manifest["contract"] == default_contract("cli-v1", START, NOW)
    assert path.stat().st_mode & 0o777 == 0o700
    assert (path / "archive.sqlite3").stat().st_mode & 0o777 == 0o600
    assert set(disk_bytes(path)) == {"archive.sqlite3"}


@pytest.mark.parametrize("value", [
    "2026-09-13", "2026-09-13T00:00:00", "2026-09-13 00:00:00Z",
    "2026-09-13T00:00:00.001Z", "2026-09-13T00:00:00.000000001Z",
    "2026-09-13T00:00:00.000000000Z", "2026-09-13T00:00:00+00:00:01",
    "2026-09-13T00:00:00z", "2026-02-30T00:00:00Z", "2026-09-13T24:00:00Z",
    "1970-01-01T00:00:00Z", "1969-12-31T23:59:59Z", "1789257600", "NaN",
])
def test_invalid_timestamp_refused_before_archive_creation(tmp_path, capsys, value):
    path = tmp_path / "not-created"
    refused(capsys, "init", "--archive", path, "--generation", "invalid-time", "--research-start", value)
    assert not path.exists()


def test_explicit_offset_is_normalized_without_local_timezone_assumption():
    assert cli.utc_seconds("2026-09-13T01:00:00+01:00") == NOW
    assert cli.utc_seconds("2026-09-12T17:00:00-07:00") == NOW
    assert cli.utc_seconds("2026-09-13T00:00:00Z") == NOW
    with pytest.raises(argparse.ArgumentTypeError):
        cli.utc_seconds(None)


def test_init_refuses_existing_archive_and_nonempty_destination(root, tmp_path, capsys):
    before = disk_bytes(root)
    refused(capsys, "init", "--archive", root, "--generation", "replacement", "--research-start", iso(START))
    assert disk_bytes(root) == before
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "sentinel").write_bytes(b"owner data")
    refused(capsys, "init", "--archive", occupied, "--generation", "replacement", "--research-start", iso(START))
    assert disk_bytes(occupied) == {"sentinel": b"owner data"}


@pytest.mark.parametrize("command", ["verify", "contract", "coverage"])
def test_read_commands_preserve_all_archive_bytes_and_do_not_qualify(root, tmp_path, capsys, command):
    path = write_bundle(tmp_path)
    run(capsys, "import-backfill", "--archive", root, "--input", path)
    before = disk_bytes(root)
    result = run(capsys, command, "--archive", root)
    assert disk_bytes(root) == before
    if command == "contract":
        assert result == default_contract("cli-v1", START, NOW)
    else:
        summary = result if command == "verify" else result["archive"]
        assert summary["record_count"] == 1
        assert summary["integrity_verified"] is True
        assert summary["provider_authenticity_verified"] is summary["qualification_established"] is False
        assert summary["execution_enabled"] is False and summary["orders_submitted"] == 0
        if command == "coverage":
            coverage = result["coverage"]
            assert coverage["mode"] == "data_quality_only"
            assert coverage["qualification_established"] is coverage["alpha_validation_eligible"] is False
            assert coverage["live_approved"] is coverage["holdout_prices_exposed"] is False
            assert coverage["actual_orders_submitted"] == 0
            assert coverage["blockers"]
            assert '"payload"' not in json.dumps(coverage)
            assert '"close"' not in json.dumps(coverage)


@pytest.mark.parametrize("command", ["verify", "contract", "coverage"])
def test_read_commands_do_not_create_missing_archives(tmp_path, capsys, command):
    path = tmp_path / "absent"
    refused(capsys, command, "--archive", path)
    assert not path.exists()


def test_import_base64_preserves_raw_bytes_and_receipt_but_stamps_actual_ingestion(root, tmp_path, capsys):
    value = bundle(event=NOW-600, received=NOW-200)
    original = deepcopy(value)
    path = write_bundle(tmp_path, value)
    source = path.read_bytes()
    decoded = cli.import_bundle(path)
    raw = base64.b64decode(value["captures"][0]["body_base64"])
    assert decoded["captures"][0]["body"] == raw
    assert "body_base64" not in decoded["captures"][0]
    result = run(capsys, "import-backfill", "--archive", root, "--input", path)
    assert result["record_count"] == result["captures_retained"] == 1
    assert result["acquisition_kinds"] == {"historical_backfill": 1, "test": 0, "prospective_observation": 0}
    saved = archive.read_bucket(root, bucket="validation", as_of=NOW)[0]
    assert saved["available_time"] == NOW-200 and saved["ingestion_time"] == NOW
    assert saved["publication_time"] is None and saved["payload"]["volume_unit"] == "unknown"
    assert archive.read_bucket(root, bucket="validation", as_of=NOW-100) == []
    with sqlite3.connect((root / "archive.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("SELECT body FROM captures").fetchone()[0] == raw
    assert value == original and path.read_bytes() == source


@pytest.mark.parametrize("injected", [False, True])
def test_import_caller_prospective_label_never_promotes_even_with_test_clock(tmp_path, capsys, injected):
    path = tmp_path / "labels"
    if injected:
        archive.init_archive(path, default_contract("clock-label", START, NOW), clock=lambda: NOW)
    else:
        run(capsys, "init", "--archive", path, "--generation", "clock-label", "--research-start", iso(START))
    source = write_bundle(tmp_path, bundle(event=NOW, kind="prospective_observation"))
    result = run(capsys, "import-backfill", "--archive", path, "--input", source)
    expected = "test" if injected else "historical_backfill"
    assert result["acquisition_kinds"][expected] == 1
    assert result["acquisition_kinds"]["prospective_observation"] == 0
    assert result["qualification_established"] is False
    assert archive.read_bucket(path, bucket="validation", as_of=NOW)[0]["acquisition_kind"] == expected
    assert stored_json(path, "manifest")["clock_basis"] == ("injected_test_clock" if injected else "system_utc")


@pytest.mark.parametrize("raw", [b"", b"{", b"\xff", b"null", b"[]",
    b'{"records":[],"records":[],"captures":[],"calendar_coverage":[]}',
    b'{"records":NaN,"captures":[],"calendar_coverage":[]}',
    b'{"records":Infinity,"captures":[],"calendar_coverage":[]}'])
def test_malformed_json_refused_without_archive_mutation(root, tmp_path, capsys, raw):
    path = tmp_path / "malformed.json"
    path.write_bytes(raw)
    before = disk_bytes(root)
    refused(capsys, "import-backfill", "--archive", root, "--input", path)
    assert disk_bytes(root) == before


@pytest.mark.parametrize("problem", ["extra", "missing", "empty_captures", "too_many_captures", "capture_extra",
    "capture_not_object", "bad_base64", "nonstring_base64", "records_not_list", "calendar_not_list",
    "raw_hash_mismatch", "future_receipt", "bad_price"])
def test_bad_backfill_shape_or_linkage_refused_atomically(root, tmp_path, capsys, problem):
    value = bundle()
    if problem == "extra":
        value["caller_authorized"] = True
    elif problem == "missing":
        del value["calendar_coverage"]
    elif problem == "empty_captures":
        value["captures"] = []
    elif problem == "too_many_captures":
        value["captures"] *= 32
    elif problem == "capture_extra":
        value["captures"][0]["body"] = "unexpected"
    elif problem == "capture_not_object":
        value["captures"] = ["unexpected"]
    elif problem == "bad_base64":
        value["captures"][0]["body_base64"] = "invalid!base64"
    elif problem == "nonstring_base64":
        value["captures"][0]["body_base64"] = 123
    elif problem == "records_not_list":
        value["records"] = {}
    elif problem == "calendar_not_list":
        value["calendar_coverage"] = {}
    elif problem == "raw_hash_mismatch":
        value["records"][0]["raw_capture_sha256"] = "a" * 64
    elif problem == "future_receipt":
        value["captures"][0]["received_at"] = NOW + 1
    else:
        value["records"][0]["payload"]["close"] = "not-a-price"
    path = write_bundle(tmp_path, value)
    before = disk_bytes(root)
    refused(capsys, "import-backfill", "--archive", root, "--input", path)
    assert disk_bytes(root) == before


@pytest.mark.parametrize("problem", ["relative", "checkout", "symlink", "symlink_parent", "directory", "fifo", "missing"])
def test_import_requires_explicit_external_regular_file(root, tmp_path, capsys, problem):
    real = write_bundle(tmp_path)
    path = tmp_path / "selected"
    if problem == "relative":
        path = Path("input.json")
    elif problem == "checkout":
        path = REPO / "scripts" / "atlas_data.py"
    elif problem == "symlink":
        path.symlink_to(real)
    elif problem == "symlink_parent":
        path.symlink_to(tmp_path, target_is_directory=True)
        path /= real.name
    elif problem == "directory":
        path.mkdir()
    elif problem == "fifo":
        os.mkfifo(path)
    before = disk_bytes(root)
    refused(capsys, "import-backfill", "--archive", root, "--input", path)
    assert disk_bytes(root) == before


def test_bundle_read_bound_and_fixed_errors_do_not_echo_imported_text(root, tmp_path, capsys, monkeypatch):
    path = write_bundle(tmp_path)
    monkeypatch.setattr(cli, "MAX_BATCH_BYTES", 32)
    before = disk_bytes(root)
    refused(capsys, "import-backfill", "--archive", root, "--input", path)
    assert disk_bytes(root) == before
    path.write_text('{"DO_NOT_ECHO_THIS_IMPORT":1,"DO_NOT_ECHO_THIS_IMPORT":2}')
    monkeypatch.setattr(cli, "MAX_BATCH_BYTES", 1024)
    error = refused(capsys, "import-backfill", "--archive", root, "--input", path)
    assert "DO_NOT_ECHO_THIS_IMPORT" not in error


def test_freeze_hashes_exact_specification_bytes_without_running_it(root, tmp_path, capsys):
    raw = b'{"prepared_strategy":"unexecuted","threshold":1}\n'
    path = tmp_path / "strategy.json"
    path.write_bytes(raw)
    expected = {"strategy_sha256": hashlib.sha256(raw).hexdigest(), "frozen_at": NOW}
    assert run(capsys, "freeze-strategy", "--archive", root, "--specification", path) == expected
    assert stored_json(root, "strategy_freeze") == expected
    assert path.read_bytes() == raw
    summary = run(capsys, "verify", "--archive", root)
    assert summary["strategy_freeze"] == expected
    assert summary["qualification_established"] is False and summary["orders_submitted"] == 0
    before = disk_bytes(root)
    path.write_bytes(raw + b" ")
    refused(capsys, "freeze-strategy", "--archive", root, "--specification", path)
    assert disk_bytes(root) == before


@pytest.mark.parametrize("content", [b"", b" \n\t ", b"x" * 1_000_001])
def test_freeze_refuses_empty_or_oversize_specification(root, tmp_path, capsys, content):
    path = tmp_path / "specification"
    path.write_bytes(content)
    before = disk_bytes(root)
    refused(capsys, "freeze-strategy", "--archive", root, "--specification", path)
    assert disk_bytes(root) == before


def test_freeze_refuses_symlink_and_late_registration(root, tmp_path, capsys, monkeypatch):
    path = tmp_path / "specification"
    path.write_bytes(b"a prepared specification")
    linked = tmp_path / "specification-link"
    linked.symlink_to(path)
    before = disk_bytes(root)
    refused(capsys, "freeze-strategy", "--archive", root, "--specification", linked)
    monkeypatch.setattr(archive.time, "time", lambda: NOW+600)
    refused(capsys, "freeze-strategy", "--archive", root, "--specification", path)
    assert disk_bytes(root) == before


@pytest.mark.parametrize("provider", ["coinbase", "alpaca"])
def test_collect_once_routes_one_explicit_request_to_fake_acquisition(root, tmp_path, capsys, monkeypatch, provider):
    calls = []
    result = {"fake_acquisition": True, "qualification_established": False, "orders_submitted": 0}

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(cli, "acquire_once", fake)
    monkeypatch.setenv("COINBASE_API_KEY", "must-not-be-used")
    key_path = tmp_path / "explicit-key-not-read-by-fake"
    arguments = ["collect-once", "--archive", root, "--provider", provider, "--start", iso(NOW-300), "--end", iso(NOW)]
    if provider == "alpaca":
        arguments += ["--key-file", key_path]
    before = disk_bytes(root)
    assert run(capsys, *arguments) == result
    assert calls == [((str(root),), {"provider": provider, "start": NOW-300, "end": NOW,
                                    "key_file": str(key_path) if provider == "alpaca" else None})]
    assert disk_bytes(root) == before
    assert not key_path.exists()


def test_coinbase_key_argument_refused_before_any_acquisition_or_key_read(root, tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "acquire_once", lambda *args, **kwargs: calls.append(True))
    path = tmp_path / "does-not-exist.key"
    before = disk_bytes(root)
    refused(capsys, "collect-once", "--archive", root, "--provider", "coinbase", "--start", iso(NOW-300),
            "--end", iso(NOW), "--key-file", path)
    assert calls == [] and disk_bytes(root) == before and not path.exists()


@pytest.mark.parametrize("extra", [
    ["--clock", "test"], ["--now", "2026-09-13T00:00:00Z"], ["--execute"], ["--live"], ["--promote"],
])
def test_cli_exposes_no_clock_override_promotion_or_execution_flag(root, capsys, extra):
    before = disk_bytes(root)
    refused(capsys, "verify", "--archive", root, *extra)
    assert disk_bytes(root) == before


def test_provider_failure_body_not_reflected_by_cli(root, capsys, monkeypatch):
    def fake(*args, **kwargs):
        raise ValueError("PRIVATE_PROVIDER_BODY_SENTINEL")

    monkeypatch.setattr(cli, "acquire_once", fake)
    error = refused(capsys, "collect-once", "--archive", root, "--provider", "coinbase", "--start", iso(NOW-300),
                    "--end", iso(NOW))
    assert "PRIVATE_PROVIDER_BODY_SENTINEL" not in error
