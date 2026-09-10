"""Inspect fixture configuration only; no broker, host checks or credentials."""

import json

import pytest

import selfcheck


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(selfcheck, "ENV_FILE", tmp_path / "fixture.env")
    monkeypatch.setattr(selfcheck, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setenv("IQ_EMAIL", "fixture@example.invalid")
    monkeypatch.setenv("IQ_PASSWORD", "fixture-secret-only")
    monkeypatch.delenv("IQ_ALLOW_REAL", raising=False)
    monkeypatch.delenv("IQ_DEFAULT_BALANCE", raising=False)


@pytest.mark.parametrize("mode,amount,allowed,valid", [
    ("paper", None, "0", True),
    ("paper", None, "1", True),
    ("paper", 2, "0", False),
    ("practice", None, "0", True),
    ("practice", 2.5, "0", True),
    ("real", 2.5, "1", True),
    ("real", None, "1", False),
    ("real", 2.5, "0", False),
    ("real", float("nan"), "1", False),
])
def test_selected_mode_configuration_is_checked_without_exporting_secrets(
        monkeypatch, mode, amount, allowed, valid):
    monkeypatch.setenv("IQ_ALLOW_REAL", allowed)
    report = selfcheck.Report()
    selfcheck.check_safety(report, mode=mode, amount=amount)
    assert (report.exit_code == 0) is valid
    assert "fixture-secret-only" not in repr(report.rows)
    assert "fixture@example.invalid" not in repr(report.rows)
    assert not selfcheck.LOGS_DIR.exists()


def test_real_configuration_can_come_from_selected_file_without_changing_process_env(monkeypatch):
    import os

    selfcheck.ENV_FILE.write_text("IQ_ALLOW_REAL=1\n")
    report = selfcheck.Report()
    selfcheck.check_safety(report, mode="real", amount=2)
    assert report.exit_code == 0
    assert "IQ_ALLOW_REAL" not in os.environ


def test_selfcheck_refuses_pending_real_submission_without_clearing_it(monkeypatch):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    marker = selfcheck.LOGS_DIR / "real" / "order_submission_pending.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"fixture": "unresolved"}')
    report = selfcheck.Report()
    selfcheck.check_safety(report, mode="real", amount=2)
    assert report.exit_code == 2
    assert any("unresolved REAL submission" in detail for _, _, detail in report.rows)
    assert marker.read_text() == '{"fixture": "unresolved"}'


def test_real_backup_requires_real_records(monkeypatch, tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "logs_live_h2.jsonl").write_text("historical fixture")
    monkeypatch.setattr(selfcheck, "BACKUP_DIR", backup)
    report = selfcheck.Report()
    selfcheck.check_backup(report, mode="real")
    assert report.exit_code == 2
    real_log = backup / "logs" / "real" / "live_h2.jsonl"
    real_log.parent.mkdir(parents=True)
    real_log.write_text("real fixture")
    report = selfcheck.Report()
    selfcheck.check_backup(report, mode="real")
    assert report.exit_code == 0


def test_cli_forwards_selected_mode_without_running_external_checks(monkeypatch, capsys):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    for name in ("check_tasks", "check_disk", "check_secrets", "check_models",
                 "check_database", "check_git", "check_tests"):
        monkeypatch.setattr(selfcheck, name, lambda _rep: None)
    backup_modes = []
    monkeypatch.setattr(selfcheck, "check_backup", lambda _rep, mode: backup_modes.append(mode))
    assert selfcheck.main(["--json", "--skip-tests", "--mode", "real", "--amount", "2"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "real"
    assert backup_modes == ["real"]
    assert any(row["check"] == "order amount" and row["detail"] == "2 account-currency units"
               for row in result["rows"])
