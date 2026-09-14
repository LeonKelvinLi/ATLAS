"""Bounded OS-process crash drills with fake market transport, never broker I/O.

These exercise process termination and SQLite recovery, not hardware power loss,
storage-controller durability, a host reboot, or an installed service manager.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from atlas.collection.archive import verify_collection_archive
from atlas.collection.config import validate_config
from atlas.collection.runtime import init_runtime
from atlas.collection.state import connection, snapshot


NOW = 1789398000  # 2026-09-14 15:00 UTC; injected clocks never prove live evidence.
REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signal and flock crash drills")

WORKER = r'''
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys

sys.path.insert(0, sys.argv[1])
from atlas.collection.archive import acquire_collection_once
from atlas.collection.primitives import HTTPResponse, _encoded
from atlas.collection.runtime import run


def refuse_network(*args, **kwargs):
    raise AssertionError("process fixture forbids provider connections")


socket.socket.connect = refuse_network
socket.create_connection = refuse_network
config = json.loads(Path(sys.argv[2]).read_text())
mode, now = sys.argv[3], int(sys.argv[4])
clock = lambda: now


def acquire(root, **kwargs):
    def transport(url, headers, **limits):
        if "/candles?" in url:
            rows = [[stamp,100,100,100,100,1]
                    for stamp in range(kwargs["start"], kwargs["end"], 300)]
            return HTTPResponse(200, _encoded(rows))
        return HTTPResponse(200, _encoded({"sequence":1,
            "bids":[["100","1",1]],"asks":[["101","1",1]]}))
    result = acquire_collection_once(root, transport=transport, **kwargs)
    if mode == "kill_after_archive_commit":
        print("READY_ARCHIVE_COMMITTED", flush=True)
        signal.pause()
        raise AssertionError("fixture expected SIGKILL")
    return result


def log(kind, stamp, **fields):
    if mode == "wait_for_sigterm" and kind in {"request_succeeded", "reconnected"}:
        print("READY_SIGTERM", flush=True)


try:
    if mode in {"exit_hot_state", "exit_hot_archive"}:
        if mode == "exit_hot_state":
            path = Path(config["state_dir"]) / "state.sqlite3"
        else:
            path = next((Path(config["archive_dir"]) / "days").glob("*/archive.sqlite3"))
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA cache_size=1")
        db.execute("BEGIN IMMEDIATE")
        if mode == "exit_hot_state":
            value = json.loads(db.execute("SELECT body FROM metadata WHERE id=1").fetchone()[0])
            value["uncommitted_process_payload"] = "x" * 262144
            db.execute("UPDATE metadata SET body=? WHERE id=1", (json.dumps(value),))
        else:
            position = db.execute("SELECT coalesce(max(id),0)+1 FROM batches").fetchone()[0]
            db.execute("INSERT INTO batches VALUES (?,?,?)", (position, b"uncommitted" * 26214, "a" * 64))
        journal = Path(str(path) + "-journal")
        if not journal.exists() or journal.read_bytes()[:8] != bytes.fromhex("d9d505f920a163d7"):
            raise AssertionError("fixture failed to produce a durable hot rollback journal")
        os._exit(73)
    result = run(config, once=mode != "wait_for_sigterm", clock=clock, acquire=acquire, log=log)
    print(json.dumps({"ok":True,"result":result}), flush=True)
except Exception as exc:
    print(json.dumps({"ok":False,"error_type":type(exc).__name__}), flush=True)
    sys.exit(1)
'''


@pytest.fixture
def process_fixture(tmp_path):
    root = tmp_path.resolve()
    state = root / "state"
    config = validate_config({"version": 1, "mode": "data_collection", "live_trading": False,
        "state_dir": str(state), "archive_dir": str(state / "archives" / "process-v1"),
        "generation_id": "process-v1", "research_start": NOW-179*86400,
        "providers": ["coinbase"], "alpaca_key_file": None})
    init_runtime(config, clock=lambda: NOW)
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    script = root / "process_fixture.py"
    script.write_text(WORKER)
    return config, script, config_path


def command(fixture, mode, *, now=NOW):
    _, script, config_path = fixture
    return [sys.executable, str(script), str(REPO), str(config_path), mode, str(now)]


def run_child(fixture, mode="once", *, now=NOW):
    return subprocess.run(command(fixture, mode, now=now), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=10, check=False)


@contextmanager
def running_child(fixture, mode):
    process = subprocess.Popen(command(fixture, mode), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def wait_ready(process, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        readable, _, _ = select.select([process.stdout], [], [], max(0, deadline-time.monotonic()))
        if readable:
            line = process.stdout.readline()
            if line.strip() == expected:
                return
            if not line:
                break
    pytest.fail("bounded child process did not reach its expected checkpoint")


def events(config):
    with connection(config["state_dir"]) as db:
        return [row[0] for row in db.execute("SELECT kind FROM events ORDER BY id")]


def test_real_os_exit_hot_state_journal_is_rolled_back_before_restart(process_fixture):
    config, _, _ = process_fixture
    crashed = run_child(process_fixture, "exit_hot_state")
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    journal = Path(config["state_dir"]) / "state.sqlite3-journal"
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
    restarted = run_child(process_fixture)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    assert not journal.exists()
    assert "uncommitted_process_payload" not in snapshot(config)["metadata"]
    with connection(config["state_dir"]) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_real_os_exit_hot_archive_transaction_preserves_committed_observations(process_fixture):
    config, _, _ = process_fixture
    assert run_child(process_fixture).returncode == 0
    before = verify_collection_archive(config["archive_dir"], clock=lambda: NOW)
    crashed = run_child(process_fixture, "exit_hot_archive")
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    journals = list((Path(config["archive_dir"]) / "days").glob("*/archive.sqlite3-journal"))
    assert len(journals) == 1
    assert journals[0].read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
    restarted = run_child(process_fixture)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    assert not journals[0].exists()
    assert verify_collection_archive(config["archive_dir"], clock=lambda: NOW) == before


def test_sigkill_after_archive_commit_releases_lock_and_replays_without_duplicate_rows(process_fixture):
    config, _, _ = process_fixture
    with running_child(process_fixture, "kill_after_archive_commit") as process:
        wait_ready(process, "READY_ARCHIVE_COMMITTED")
        competing = run_child(process_fixture)
        assert competing.returncode == 1
        before = verify_collection_archive(config["archive_dir"], clock=lambda: NOW)
        assert before["record_count"] == 4
        assert snapshot(config)["providers"]["coinbase"]["last_success_at"] is None
        process.send_signal(signal.SIGKILL)
        process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL
    restarted = run_child(process_fixture)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    after = verify_collection_archive(config["archive_dir"], clock=lambda: NOW)
    assert after["record_count"] == before["record_count"]
    assert after["batch_count"] == before["batch_count"] + 1
    assert after["acquisition_kinds"] == before["acquisition_kinds"]
    assert events(config).count("unclean_restart") == 1
    assert snapshot(config)["providers"]["coinbase"]["last_success_at"] == NOW


def test_sigkill_outage_restart_retains_durable_gap_and_historical_recovery(process_fixture):
    config, _, _ = process_fixture
    with running_child(process_fixture, "kill_after_archive_commit") as process:
        wait_ready(process, "READY_ARCHIVE_COMMITTED")
        process.kill()
        process.communicate(timeout=5)
    restarted = run_child(process_fixture, now=NOW+3600)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    with connection(config["state_dir"]) as db:
        gaps = db.execute("SELECT start,end,reason,resolved_at FROM gaps WHERE reason='collection_outage'").fetchall()
        assert gaps == [(NOW, NOW+3600, "collection_outage", NOW+3600)]
        attempts = [json.loads(row[0]) for row in db.execute("SELECT body FROM events WHERE kind='request_started' ORDER BY id")]
    assert attempts[-1]["recovery"] is True
    kinds = events(config)
    assert kinds.count("unclean_restart") == 1 and "gap_filled_historically" in kinds
    archive = verify_collection_archive(config["archive_dir"], clock=lambda: NOW+3600)
    assert archive["acquisition_kinds"]["prospective_observation"] == 0
    assert archive["acquisition_kinds"]["test"] == archive["record_count"]


def test_sigterm_flushes_checkpoint_and_records_clean_shutdown(process_fixture):
    config, _, _ = process_fixture
    with running_child(process_fixture, "wait_for_sigterm") as process:
        wait_ready(process, "READY_SIGTERM")
        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=5)
        assert process.returncode == 0, output + error
    state = snapshot(config)
    assert state["metadata"]["running"] is False
    assert state["providers"]["coinbase"]["last_success_at"] == NOW
    assert events(config)[-1] == "stopped"
    restarted = run_child(process_fixture)
    assert restarted.returncode == 0
    assert "unclean_restart" not in events(config)


@pytest.mark.parametrize("target", ["state", "archive", "incomplete_restore"])
def test_corrupt_or_incomplete_state_refuses_child_start_and_preserves_evidence(process_fixture, target):
    config, _, _ = process_fixture
    assert run_child(process_fixture).returncode == 0
    if target == "state":
        path = Path(config["state_dir"]) / "state.sqlite3"
    elif target == "archive":
        path = next((Path(config["archive_dir"]) / "days").glob("*/archive.sqlite3"))
    else:
        path = Path(config["state_dir"]) / ".restore-incomplete"
    corrupted = b"INCOMPLETE_PROCESS_FIXTURE_DO_NOT_REINITIALIZE"
    path.write_bytes(corrupted)
    path.chmod(0o600)
    restarted = run_child(process_fixture)
    assert restarted.returncode == 1
    assert path.read_bytes() == corrupted
    assert corrupted.decode() not in restarted.stdout + restarted.stderr
