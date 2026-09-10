"""Backup tool test - copies what exists, reports what doesn't."""

from backup_state import run_backup


def test_run_backup_copies_existing_and_reports_missing(tmp_path):
    project = tmp_path / "proj"
    (project / "logs").mkdir(parents=True)
    (project / "logs" / "live_h2.jsonl").write_text('{"ts": 1}\n')
    (project / "journal.db").write_bytes(b"sqlite-ish")
    dest = tmp_path / "backup"

    result = run_backup(project, dest)

    assert "logs/live_h2.jsonl" in result["copied"]
    assert "journal.db" in result["copied"]
    assert "logs/live_h2_heartbeat.jsonl" in result["missing"]
    assert ".env" not in result["missing"]  # credentials are outside backup scope
    assert (dest / "logs_live_h2.jsonl").read_text() == '{"ts": 1}\n'
    assert (dest / "journal.db").exists()
    assert result["bytes"] > 0
