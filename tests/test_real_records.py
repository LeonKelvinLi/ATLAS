"""REAL account record isolation with synthetic files and a fake broker only."""

import json
import sys
import types

import pytest

import backup_state
import dashboard
import mission_control as mc
import settle_missing
import status


@pytest.fixture
def record_state(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    real = logs / "real"
    real.mkdir(parents=True)
    monkeypatch.setattr(mc, "LOGS_DIR", logs)
    monkeypatch.setattr(settle_missing, "LOGS_DIR", logs)
    monkeypatch.setattr(settle_missing, "ENV_FILE", tmp_path / "absent.env")
    monkeypatch.delenv("ATLAS_MONITOR_MODE", raising=False)
    monkeypatch.delenv("IQ_ALLOW_REAL", raising=False)
    monkeypatch.setattr(mc, "scheduled_task_state", lambda: {"status": "Running"})
    monkeypatch.setattr(mc, "sidecar_health", lambda now: {})
    monkeypatch.setattr(mc, "candle_freshness", lambda: {"latest_to_ts": 1000})
    monkeypatch.setattr(mc, "universe_split", lambda rows: {})
    monkeypatch.setattr(mc, "label_fidelity", lambda rows: {"settled_orders": len(rows)})
    (logs / "live_h2.jsonl").write_text('{"order_id": 11, "mode": "trade"}\n')
    (real / "live_h2.jsonl").write_text('{"order_id": 22, "mode": "real", "balance_mode": "REAL", "balance_id": 101, "currency": "USD"}\n')
    (logs / "live_h2_heartbeat.jsonl").write_text('{"ts": 900}\n')
    (real / "live_h2_heartbeat.jsonl").write_text('{"ts": 950, "balance_mode": "REAL", "balance_id": 101, "currency": "USD"}\n')
    (real / "supervisor.log").write_text('[2026-07-24T04:00:00Z] runner launched pid=22\n')
    return tmp_path, logs, real


def test_real_status_isolated_without_execution_permission(record_state, monkeypatch):
    _, logs, real = record_state
    monkeypatch.setattr(mc, "journal_counts", lambda: pytest.fail("historical journal must not be read as REAL"))
    view = mc.build_status(now=1000, mode="real")
    assert view["monitor_mode"] == "real"
    assert view["balance_mode"] == "REAL"
    assert view["signals"]["last_signal"]["order_id"] == 22
    assert view["heartbeat"]["last_ts"] == 950
    assert view["supervisor"]["log_path"] == str(real / "supervisor.log")
    assert view["forward_progress"] == {}
    assert view["journal"]["selected"] is False
    assert "excluded" in view["registered_note"]
    assert "shared" in view["shared_state_note"]
    assert mc.observation_paths()["signals"] == logs / "live_h2.jsonl"
    monkeypatch.setenv("ATLAS_MONITOR_MODE", "real")
    assert mc.build_status(now=1000, deep=False)["signals"]["last_signal"]["order_id"] == 22
    assert mc.observation_paths("practice")["signals"] == logs / "live_h2.jsonl"


def test_pending_marker_is_critical_and_read_only(record_state):
    _, _, real = record_state
    marker = real / "order_submission_pending.json"
    marker.write_bytes(b"interrupted incomplete record")
    view = mc.build_status(now=1000, deep=False, mode="real")
    assert view["tier"] == "CRITICAL"
    assert view["pending_submission"]["present"]
    assert any("manual broker history reconciliation" in line for line in view["reasons"])
    assert marker.read_bytes() == b"interrupted incomplete record"


def test_dashboard_payload_uses_same_explicit_scope(record_state, monkeypatch):
    monkeypatch.setattr(dashboard, "build_status", lambda **kwargs: mc.build_status(now=1000, deep=False, **kwargs))
    monkeypatch.setattr(dashboard, "latest_payouts", lambda: [])
    view = dashboard.build_payload(mode="real")
    assert view["status"]["monitor_mode"] == "real"
    assert [row["order_id"] for row in view["signals"]] == [22]
    assert view["heartbeats"][0]["ts"] == 950
    assert "not applicable" in view["verdict_window"]


def test_invalid_monitor_env_fails_before_dashboard_bind_or_status_read(monkeypatch):
    monkeypatch.setenv("ATLAS_MONITOR_MODE", "REAL")
    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", lambda *args: pytest.fail("must not bind"))
    monkeypatch.setattr(status, "build_status", lambda **kwargs: pytest.fail("must not read"))
    with pytest.raises(SystemExit) as exc:
        dashboard.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        status.main([])
    assert exc.value.code == 2


@pytest.fixture
def broker(record_state, monkeypatch):
    class FakeBroker:
        def __init__(self):
            self.mode = "PRACTICE"
            self.calls = []
            self.drift = False
            self.honor_selection = True
            self.balance_id = 101
            self.currency = "USD"
            self.identity_drift = False

        def connect(self):
            self.calls.append("connect")
            return True, None

        def change_balance(self, mode):
            self.calls.append(("change_balance", mode))
            if self.honor_selection:
                self.mode = mode

        def get_balance_mode(self):
            self.calls.append("get_balance_mode")
            return self.mode

        def get_balance_id(self):
            return self.balance_id

        def get_currency(self):
            return self.currency

        def get_position_history_v2(self, *args):
            self.calls.append("history")
            if self.drift:
                self.mode = "PRACTICE"
            if self.identity_drift:
                self.balance_id = 999
            return {"positions": [{"external_id": 22, "close_reason": "win", "pnl": 0.88}]}

        def get_optioninfo(self, *args):
            self.calls.append("optioninfo")
            return {}

        def close(self):
            self.calls.append("close")

        def buy(self, *args):
            pytest.fail("reconciliation must never buy")

    fake = FakeBroker()
    package = types.ModuleType("iqoptionapi")
    stable = types.ModuleType("iqoptionapi.stable_api")
    stable.IQ_Option = lambda *args: fake
    once = types.ModuleType("run_once")
    once._call = lambda fn, *args, timeout=60, **kwargs: fn(*args, **kwargs)
    monkeypatch.setitem(sys.modules, "iqoptionapi", package)
    monkeypatch.setitem(sys.modules, "iqoptionapi.stable_api", stable)
    monkeypatch.setitem(sys.modules, "run_once", once)
    monkeypatch.setenv("IQ_EMAIL", "fixture@example.invalid")
    monkeypatch.setenv("IQ_PASSWORD", "fixture")
    return fake


def test_real_reconciliation_requires_opt_in_before_connection(record_state, broker):
    with pytest.raises(SystemExit) as exc:
        settle_missing.main(["--mode", "real"])
    assert exc.value.code == 2
    assert broker.calls == []


def test_real_reconciliation_selects_account_and_preserves_pending(record_state, broker, monkeypatch, capsys):
    _, logs, real = record_state
    practice_before = (logs / "live_h2.jsonl").read_bytes()
    marker = real / "order_submission_pending.json"
    marker.write_text('{"submission":"unknown"}')
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    assert settle_missing.main(["--mode", "real"]) == 1
    assert broker.calls[:3] == ["connect", ("change_balance", "REAL"), "get_balance_mode"]
    assert broker.calls[-1] == "close"
    records = mc.read_jsonl(real / "live_h2.jsonl")
    assert len(records) == 2
    assert records[-1]["settled"] and records[-1]["balance_mode"] == "REAL"
    assert records[-1]["profit"] == 0.88
    assert (logs / "live_h2.jsonl").read_bytes() == practice_before
    assert marker.read_text() == '{"submission":"unknown"}'
    assert "does not clear" in capsys.readouterr().err


@pytest.mark.parametrize("drift", [False, True])
def test_wrong_account_aborts_without_appending(record_state, broker, monkeypatch, drift):
    _, _, real = record_state
    path = real / "live_h2.jsonl"
    before = path.read_bytes()
    broker.honor_selection = drift
    broker.drift = drift
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    with pytest.raises(RuntimeError, match="expected REAL"):
        settle_missing.main(["--mode", "real"])
    assert path.read_bytes() == before
    assert broker.calls[-1] == "close"
    assert broker.calls.count("history") == (1 if drift else 0)


def test_dry_run_is_offline_and_pending_is_not_success(record_state, broker, monkeypatch):
    _, _, real = record_state
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    (real / "order_submission_pending.json").write_text("incomplete")
    assert settle_missing.main(["--mode", "real", "--dry-run"]) == 1
    assert broker.calls == []


@pytest.mark.parametrize("field,value", [("balance_id", 999), ("balance_id", None),
                                        ("currency", "EUR"), ("currency", None)])
def test_real_record_identity_must_match_before_history(record_state, broker, monkeypatch, field, value):
    _, _, real = record_state
    path = real / "live_h2.jsonl"
    record = mc.read_jsonl(path)[0]
    record[field] = value
    path.write_text(json.dumps(record) + "\n")
    before = path.read_bytes()
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    with pytest.raises(RuntimeError, match="missing or mismatched account identity"):
        settle_missing.main(["--mode", "real"])
    assert "history" not in broker.calls
    assert path.read_bytes() == before
    assert broker.calls[-1] == "close"


def test_real_account_drift_during_history_cannot_append(record_state, broker, monkeypatch):
    _, _, real = record_state
    path = real / "live_h2.jsonl"
    before = path.read_bytes()
    broker.identity_drift = True
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    with pytest.raises(RuntimeError, match="identity or currency changed"):
        settle_missing.main(["--mode", "real"])
    assert path.read_bytes() == before
    assert broker.calls.count("history") == 1


def test_settlement_id_from_another_real_account_does_not_hide_orphan():
    original = {"order_id": 22, "balance_mode": "REAL", "balance_id": 101, "currency": "USD"}
    foreign = {**original, "balance_id": 999, "settled": True}
    assert settle_missing.find_unsettled([original, foreign]) == [original]


def test_backup_preserves_real_evidence_tree_but_not_credentials(record_state, tmp_path):
    state, _, real = record_state
    (real / "order_submission_pending.json").write_text('{"submission":"unknown"}')
    nested = real / "order_records"
    nested.mkdir()
    (nested / "unmatched_history.json").write_text('{"unmatched":[22]}')
    (real / ".env").write_text("private")
    (real / "credentials.json").write_text("private")
    (real / "access-token.json").write_text("private")
    (real / "linked.json").symlink_to(real / "credentials.json")
    destination = tmp_path / "backup"
    result = backup_state.run_backup(state, destination)
    assert result["failed"] == []
    assert "logs/real/order_submission_pending.json" in result["copied"]
    assert (destination / "logs/real/order_records/unmatched_history.json").exists()
    assert (destination / "logs/real/order_submission_pending.json").exists()
    for name in (".env", "credentials.json", "access-token.json", "linked.json"):
        assert not (destination / "logs/real" / name).exists()
    with pytest.raises(ValueError, match="outside"):
        backup_state.run_backup(state, real / "backups")


def test_unreadable_real_tree_does_not_abort_other_backups(record_state, tmp_path, monkeypatch):
    state, _, _ = record_state

    def unreadable(_):
        raise PermissionError("fixture")

    monkeypatch.setattr(backup_state, "real_record_targets", unreadable)
    result = backup_state.run_backup(state, tmp_path / "backup")
    assert "logs/live_h2.jsonl" in result["copied"]
    assert result["failed"] == ["logs/real (PermissionError)"]
