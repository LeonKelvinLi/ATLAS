"""Configured storage must not depend on the service working directory."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
PATH_KEYS = ("ATLAS_STATE_DIR", "ATLAS_MODEL_DIR", "ATLAS_ENV_FILE", "ATLAS_BACKUP_DIR")


def child_env(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in PATH_KEYS}
    env["PYTHONPATH"] = str(PROJECT)
    env.update(overrides)
    return env


def test_resolving_config_creates_nothing_and_ignores_cwd(tmp_path):
    state = tmp_path / "not-created" / "state"
    result = subprocess.run(
        [sys.executable, str(PROJECT / "runtime_paths.py")],
        cwd=tmp_path, env=child_env(ATLAS_STATE_DIR=str(state)),
        capture_output=True, text=True, check=True,
    )
    paths = json.loads(result.stdout)
    assert paths["state"] == str(state)
    assert paths["models"] == str(state / "models")
    assert paths["logs"] == str(state / "logs")
    assert paths["env_file"] == str(state / ".env")
    assert paths["backup"] is None
    assert not list(tmp_path.iterdir())


def test_env_file_does_not_change_child_paths_and_preserves_value_precedence(tmp_path):
    env_file = tmp_path / "broker.env"
    env_file.write_text(
        "".join(f"{key}=relative-not-a-process-setting\n" for key in PATH_KEYS)
        + "ATLAS_FIXTURE_VALUE=first\nATLAS_FIXTURE_VALUE=second\n"
        + "ATLAS_FIXTURE_OVERRIDE=from-file\n"
    )
    env = child_env(ATLAS_ENV_FILE=str(env_file), ATLAS_FIXTURE_OVERRIDE="from-process")
    env.pop("ATLAS_FIXTURE_VALUE", None)
    code = """
import json
import os
import subprocess
import sys
import runtime_paths as paths
import server  # loads the selected fixture, never starts the MCP server
import run_once
import live_h2_runner
run_once._load_env()
live_h2_runner._load_env()
assert os.environ['ATLAS_FIXTURE_VALUE'] == 'first'
assert os.environ['ATLAS_FIXTURE_OVERRIDE'] == 'from-process'
assert 'ATLAS_STATE_DIR' not in os.environ
assert 'ATLAS_MODEL_DIR' not in os.environ
assert 'ATLAS_BACKUP_DIR' not in os.environ
child = subprocess.run([sys.executable, str(paths.PROJECT_DIR / 'runtime_paths.py')],
                       capture_output=True, text=True, check=True)
resolved = json.loads(child.stdout)
assert resolved['state'] == str(paths.STATE_DIR)
assert resolved['env_file'] == str(paths.ENV_FILE)
assert resolved['backup'] is None
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == [env_file]


@pytest.mark.parametrize("key", PATH_KEYS)
@pytest.mark.parametrize("bad_path", ["relative/path", ""])
def test_invalid_override_fails_before_creating_state(tmp_path, key, bad_path):
    result = subprocess.run(
        [sys.executable, str(PROJECT / "runtime_paths.py")], cwd=tmp_path,
        env=child_env(**{key: bad_path}), capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert f"{key} must be an absolute path" in result.stderr
    assert not list(tmp_path.iterdir())


def test_databases_are_created_in_external_state_with_unrelated_cwd(tmp_path):
    state = tmp_path / "volume" / "atlas"
    work = tmp_path / "cwd"
    work.mkdir()
    code = """
import json
import journal
import storage
with storage.open_db() as conn:
    assert conn.execute('SELECT count(*) FROM datasets').fetchone()[0] == 0
conn = journal.open_journal()
try:
    assert conn.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
finally:
    conn.close()
print(json.dumps([str(storage.DB_PATH), str(journal.DB_PATH)]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=work,
        env=child_env(ATLAS_STATE_DIR=str(state)),
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == [str(state / "market.duckdb"), str(state / "journal.db")]
    assert sorted(p.name for p in state.iterdir()) == ["journal.db", "market.duckdb"]
    assert not list(work.iterdir())


def test_backup_requires_a_destination_and_excludes_credentials(tmp_path):
    from backup_state import run_backup

    state = tmp_path / "state"
    state.mkdir()
    (state / ".env").write_text("private credential fixture")
    (state / "journal.db").write_bytes(b"legacy journal bytes")
    with pytest.raises(ValueError, match="explicitly"):
        run_backup(state, None)
    with pytest.raises(ValueError, match="differ"):
        run_backup(state, state)
    dest = tmp_path / "backup"
    result = run_backup(state, dest)
    assert result["copied"] == ["journal.db"]
    assert not (dest / ".env").exists()
    assert not result["failed"]


def test_selfcheck_reports_unconfigured_backup_and_empty_database(tmp_path, monkeypatch):
    import selfcheck
    from storage import open_db

    monkeypatch.setattr(selfcheck, "BACKUP_DIR", None)
    monkeypatch.setattr(selfcheck, "STATE_DIR", tmp_path)
    open_db(tmp_path / "market.duckdb").close()
    report = selfcheck.Report()
    selfcheck.check_backup(report)
    selfcheck.check_database(report)
    assert report.exit_code == 1
    assert {check for level, check, detail in report.rows} == {
        "backup", "candles", "payout snapshots",
    }
    assert all("crash" not in detail for _, _, detail in report.rows)


def test_selfcheck_uses_environment_credentials_without_exposing_them(tmp_path, monkeypatch):
    import selfcheck

    env_file = tmp_path / "fixture.env"
    env_file.write_text("IQ_ALLOW_REAL=1\nIQ_DEFAULT_BALANCE=REAL\n")
    monkeypatch.setattr(selfcheck, "ENV_FILE", env_file)
    monkeypatch.setenv("IQ_EMAIL", "fixture@example.invalid")
    monkeypatch.setenv("IQ_PASSWORD", "fixture-password")
    monkeypatch.setenv("IQ_ALLOW_REAL", "0")
    monkeypatch.setenv("IQ_DEFAULT_BALANCE", "PRACTICE")
    report = selfcheck.Report()
    selfcheck.check_safety(report)
    assert report.exit_code == 0
    assert "fixture-password" not in repr(report.rows)
    assert "fixture@example.invalid" not in repr(report.rows)


def test_selfcheck_matches_first_duplicate_env_value(tmp_path, monkeypatch):
    import selfcheck

    env_file = tmp_path / "fixture.env"
    env_file.write_text("IQ_ALLOW_REAL=1\nIQ_ALLOW_REAL=0\n"
                        "IQ_DEFAULT_BALANCE=REAL\nIQ_DEFAULT_BALANCE=PRACTICE\n")
    monkeypatch.setattr(selfcheck, "ENV_FILE", env_file)
    monkeypatch.delenv("IQ_ALLOW_REAL", raising=False)
    monkeypatch.delenv("IQ_DEFAULT_BALANCE", raising=False)
    report = selfcheck.Report()
    selfcheck.check_safety(report)
    failed = {check for level, check, _ in report.rows if level == "FAIL"}
    assert {"safety IQ_ALLOW_REAL", "safety balance mode"} <= failed


def test_backup_preserves_uncheckpointed_wal_with_uri_characters(tmp_path):
    from backup_state import run_backup

    state = tmp_path / "state #% with spaces"
    state.mkdir()
    writer = sqlite3.connect(state / "journal.db")
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE events (value TEXT)")
        writer.execute("INSERT INTO events VALUES ('committed only in WAL')")
        writer.commit()
        assert (state / "journal.db-wal").stat().st_size > 0
        dest = tmp_path / "backup"
        result = run_backup(state, dest)
        assert result["copied"] == ["journal.db"]
        assert not result["failed"]
        with sqlite3.connect(dest / "journal.db") as restored:
            assert restored.execute("SELECT value FROM events").fetchall() == [
                ("committed only in WAL",),
            ]
    finally:
        writer.close()
