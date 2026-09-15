"""Offline CLI/config boundaries; all paper account, credential and network I/O forbidden."""

from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from atlas.paper.cli import load_config, main
from atlas.paper.journal import PaperJournal


@pytest.fixture(autouse=True)
def local_only(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("CLI fixtures must not use broker I/O or credentials")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("atlas.paper.broker._owned_http", forbidden)
    monkeypatch.setattr("atlas.paper.broker._credentials", forbidden)
    monkeypatch.setattr("atlas.paper.journal._account_lock_root", lambda: tmp_path / "account-locks", raising=False)


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return str(path)


@pytest.fixture
def config(tmp_path):
    return {"schema_version": 1, "mode": "PAPER", "state_dir": str(tmp_path / "paper-state"),
            "credentials_file": str(tmp_path / "not-opened-credentials.json"),
            "account_fingerprint": "a" * 64, "strategy_sha256": "b" * 64,
            "allocated_principal": "100", "risk": {"allowed_strategy_ids": ["test-strategy"]}}


def invoke(capsys, *args):
    code = main(list(args))
    output = capsys.readouterr()
    assert output.err == ""
    return code, json.loads(output.out)


def seed(config_path):
    config = load_config(config_path)
    with PaperJournal(config["state_dir"], config["binding"], create=True, now=1) as journal:
        with journal.transaction("synthetic_cli_fixture_setup", {"test": True}, now=2):
            journal.set("initialized", True)
            journal.set("kill_switch", False)
    return config


def reviewed():
    now = time.time()
    return {"signal_id": "cli-fixture", "strategy_id": "test-strategy", "strategy_sha256": "b" * 64,
            "data_sha256": "c" * 64, "symbol": "SPY", "side": "buy", "quantity": ".5",
            "limit_price": "10", "signal_time": now, "expires_at": now + 120}


def test_config_check_defaults_to_disabled_without_state_or_credential_access(tmp_path, config, capsys):
    path = private_json(tmp_path / "config.json", config)
    code, output = invoke(capsys, "config-check", "--config", path)
    assert code == 0 and output["ok"]
    assert output["mode"] == "PAPER" and output["paper_execution_enabled"] is False
    assert output["credential_contents_read"] is False and output["network_used"] is False
    assert output["broker_readiness_verified"] is False
    assert not Path(config["state_dir"]).exists()
    assert not Path(config["credentials_file"]).exists()
    assert config["credentials_file"] not in json.dumps(output)


def test_old_pilot_key_selection_is_refused_by_new_config_and_preflight(tmp_path, config, capsys):
    old_key = str(Path.home() / ".config/atlas/alpaca-paper/paper-keys.json")
    config["credentials_file"] = old_key
    path = private_json(tmp_path / "config.json", config)
    for args in (("config-check", "--config", path), ("preflight", "--key-file", old_key)):
        code, result = invoke(capsys, *args)
        assert code == 2 and result["error"] == "paper_cli_legacy_pilot_credentials_protected"
        assert old_key not in json.dumps(result)
    assert not Path(config["state_dir"]).exists()


def test_read_only_preflight_needs_no_config_or_journal_and_cannot_enable_writes(tmp_path, capsys, monkeypatch):
    calls = []

    def inspect(api, *, allocated_principal, symbols):
        assert api.provenance == "PAPER_API" and api.execution_enabled is False
        calls.append((allocated_principal, symbols))
        return {"read_errors": [], "account_ready": True, "market_state": "WAIT_MARKET_OPEN",
                "execution_ready": False, "actual_orders_submitted": 0, "account_mutations": 0}

    monkeypatch.setattr("atlas.paper.cli.collect_preflight", inspect)
    code, output = invoke(capsys, "preflight", "--key-file", str(tmp_path / "selected-paper-key.json"),
                          "--symbol", "SPY")
    assert code == 0 and output["ok"]
    assert output["result"]["market_state"] == "WAIT_MARKET_OPEN"
    assert output["result"]["execution_ready"] is False
    assert output["journal_created"] is False
    assert calls == [("100", ("SPY",))]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("principal", ["0", "101", "NaN", "-1"])
def test_preflight_bad_allocation_is_refused_before_credentials(tmp_path, capsys, principal):
    code, result = invoke(capsys, "preflight", "--key-file", str(tmp_path / "missing.json"),
                          "--allocated-principal", principal)
    assert code == 2 and result["ok"] is False
    assert list(tmp_path.iterdir()) == []


def test_preflight_read_failure_is_nonzero_without_journal_creation(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("atlas.paper.cli.collect_preflight", lambda *args, **kwargs:
                        {"read_errors": ["paper_request_rejected"], "account_ready": False})
    code, output = invoke(capsys, "preflight", "--key-file", str(tmp_path / "missing.json"))
    assert code == 3 and output["ok"] is False
    assert output["journal_created"] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", [
    {"mode": "LIVE"}, {"mode": "SIMULATION"}, {"schema_version": True},
    {"schema_version": 2}, {"paper_execution_enabled": "true"},
    {"allocated_principal": "101"}, {"allocated_principal": 100},
    {"allocated_principal": "NaN"}, {"account_fingerprint": "short"},
    {"risk": {"max_order_notional": 10}}, {"risk": {"unknown": "value"}},
    {"risk": {"allowed_strategy_ids": ["bad space"]}}, {"risk": []},
    {"api_key": "private-fixture-secret"}, {"api_secret": "private-fixture-secret"},
    {"origin": "https://attacker.invalid"}, {"live_enabled": True},
])
def test_strict_config_rejects_live_hosts_inline_secrets_and_ambiguous_limits(tmp_path, config, capsys, change):
    path = private_json(tmp_path / "config.json", {**config, **change})
    code, output = invoke(capsys, "config-check", "--config", path)
    assert code == 2 and output["ok"] is False
    assert "private-fixture-secret" not in json.dumps(output)
    assert not Path(config["state_dir"]).exists()


@pytest.mark.parametrize("kind", ["permissions", "symlink", "hardlink", "directory", "oversized", "duplicate", "malformed"])
def test_config_file_must_be_private_unique_regular_bounded_json(tmp_path, config, capsys, kind):
    original = tmp_path / "config.json"
    path = private_json(original, config)
    if kind == "permissions":
        original.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(original)
        path = str(link)
    elif kind == "hardlink":
        os.link(original, tmp_path / "second.json")
    elif kind == "directory":
        path = str(tmp_path)
    elif kind == "oversized":
        original.write_text("x" * 65537)
    elif kind == "duplicate":
        original.write_text('{"mode":"PAPER","mode":"LIVE","private":"never-echo-this"}')
    else:
        original.write_text('{"private":"never-echo-this"')
    code, output = invoke(capsys, "config-check", "--config", path)
    assert code == 2 and not output["ok"]
    assert "never-echo-this" not in json.dumps(output)
    assert not Path(config["state_dir"]).exists()


@pytest.mark.parametrize("bad_path", ["relative-state", "~/state", "atlas-collector-live", "atlas-operator", ".codex", "ATLAS-research-evidence"])
def test_state_paths_cannot_target_relative_or_protected_locations(tmp_path, config, capsys, bad_path):
    config["state_dir"] = bad_path if bad_path in {"relative-state", "~/state"} else str(tmp_path / bad_path)
    path = private_json(tmp_path / "config.json", config)
    code, output = invoke(capsys, "config-check", "--config", path)
    assert code == 2 and not output["ok"]
    assert not (tmp_path / bad_path).exists()


def test_state_cannot_live_in_a_git_checkout_or_contain_config_or_credentials(tmp_path, config, capsys):
    repo = tmp_path / "other-checkout"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: fixture")
    config["state_dir"] = str(repo / "state")
    path = private_json(tmp_path / "config.json", config)
    assert invoke(capsys, "config-check", "--config", path)[0] == 2
    config["state_dir"] = str(tmp_path / "state")
    config["credentials_file"] = str(tmp_path / "state" / "secret.json")
    private_json(Path(path), config)
    assert invoke(capsys, "config-check", "--config", path)[0] == 2


def test_symlinked_parent_cannot_redirect_config_or_state(tmp_path, config, capsys):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(real, target_is_directory=True)
    config["state_dir"] = str(link / "state")
    path = private_json(real / "config.json", config)
    assert invoke(capsys, "config-check", "--config", path)[0] == 2
    assert invoke(capsys, "config-check", "--config", str(link / "config.json"))[0] == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_status_and_halt_are_offline_even_with_execution_enabled_and_missing_keys(tmp_path, config, capsys, enabled):
    config["paper_execution_enabled"] = enabled
    path = private_json(tmp_path / "config.json", config)
    prepared = seed(path)
    code, status = invoke(capsys, "status", "--config", path)
    assert code == 0 and status["result"]["initialized"]
    assert not status["network_command"] and not status["result"]["kill_switch"]
    code, halted = invoke(capsys, "halt", "--config", path)
    assert code == 0 and halted["result"]["kill_switch"]
    assert not halted["network_command"]
    code, status = invoke(capsys, "status", "--config", path)
    assert status["result"]["kill_switch"] and not Path(config["credentials_file"]).exists()
    with PaperJournal(prepared["state_dir"], prepared["binding"]) as journal:
        assert journal.get("kill_switch") is True


@pytest.mark.parametrize("command", ["status", "halt", "reconcile"])
def test_existing_state_commands_cannot_initialize_missing_state(tmp_path, config, capsys, command):
    path = private_json(tmp_path / "config.json", config)
    code, result = invoke(capsys, command, "--config", path)
    assert code == 2 and result["ok"] is False
    assert not Path(config["state_dir"]).exists()


@pytest.mark.parametrize("command", ["submit", "cancel", "replace"])
def test_execution_disabled_blocks_all_mutation_commands_before_network_or_new_state(tmp_path, config, capsys, command):
    path = private_json(tmp_path / "config.json", config)
    args = [command, "--config", path]
    if command in {"submit", "replace"}:
        args += ["--decision", private_json(tmp_path / "decision.json", reviewed())]
    if command in {"cancel", "replace"}:
        args += ["--client-order-id", "owned-fixture-id"]
    code, result = invoke(capsys, *args)
    assert code == 2 and result["error"] == "paper_cli_execution_disabled"
    assert not Path(config["state_dir"]).exists()


def test_decision_file_rejects_inline_authority_and_never_echoes_private_text(tmp_path, config, capsys):
    path = private_json(tmp_path / "config.json", config)
    proposed = {**reviewed(), "api_secret": "private-fixture-decision"}
    decision_path = private_json(tmp_path / "decision.json", proposed)
    code, result = invoke(capsys, "submit", "--config", path, "--decision", decision_path)
    assert code == 2 and result["error"] == "paper_decision_schema_invalid"
    assert "private-fixture-decision" not in json.dumps(result)


def test_configuration_binding_change_cannot_reopen_existing_journal(tmp_path, config, capsys):
    path = private_json(tmp_path / "config.json", config)
    seed(path)
    changed = deepcopy(config)
    changed["risk"]["max_order_notional"] = "9"
    private_json(Path(path), changed)
    code, result = invoke(capsys, "status", "--config", path)
    assert code == 2 and result["error"] in {
        "paper_journal_foreign_binding", "paper_account_registration_binding_or_path_mismatch"}


def test_owner_mismatch_refuses_config_without_touching_state(tmp_path, config, capsys, monkeypatch):
    path = private_json(tmp_path / "config.json", config)
    monkeypatch.setattr("atlas.paper.cli.os.getuid", lambda: -1)
    code, result = invoke(capsys, "config-check", "--config", path)
    assert code == 2 and result["error"] == "paper_cli_private_input_required"
    assert not Path(config["state_dir"]).exists()


def test_initialize_existing_session_is_idempotent_offline_and_preserves_halt(tmp_path, config, capsys):
    path = private_json(tmp_path / "config.json", config)
    seed(path)
    assert invoke(capsys, "halt", "--config", path)[0] == 0
    code, result = invoke(capsys, "initialize", "--config", path)
    assert code == 0 and result["result"]["initialized"]
    assert result["result"]["kill_switch"]


@pytest.mark.parametrize("state", ["rejected", "http_rejected", "unknown", "not_submitted"])
def test_unsuccessful_order_outcome_is_nonzero_even_when_a_wire_request_was_sent(tmp_path, config, capsys, monkeypatch, state):
    path = private_json(tmp_path / "config.json", config)
    decision_path = private_json(tmp_path / "decision.json", reviewed())
    # Fault injection at the CLI result boundary; engine behavior has its own full-flow tests.
    monkeypatch.setattr("atlas.paper.cli._execute", lambda *args, **kwargs:
                        {"new_submission": True, "order": {"state": state}})
    code, result = invoke(capsys, "submit", "--config", path, "--decision", decision_path)
    assert code == 3 and result["ok"] is False


def test_argument_errors_do_not_echo_unrecognized_private_values(capsys):
    code, result = invoke(capsys, "status", "--api-key", "private-argument-value")
    assert code == 2 and result["error"] == "paper_cli_arguments_invalid"
    assert "private-argument-value" not in json.dumps(result)


def test_temporary_demo_proves_only_synthetic_lifecycle_and_removes_state(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("atlas.paper.cli.tempfile.tempdir", str(tmp_path))
    code, result = invoke(capsys, "demo")
    assert code == 0 and result["ok"]
    assert result["provenance"] == "TEST" and result["mode"] == "SIMULATION"
    assert result["synthetic_order_count"] == 1 and result["synthetic_fill_count"] == 2
    assert result["final_order_state"] == "canceled" and result["filled_quantity"] == "0.3"
    assert result["restart_reconciliation_passed"] and result["temporary_state_removed"]
    assert result["actual_orders_submitted"] == 0 and not result["qualification_established"]
    assert not list(tmp_path.glob("atlas-paper-cli-demo-*"))


def test_script_entrypoint_config_check_is_offline_from_another_directory(tmp_path, config):
    path = private_json(tmp_path / "config.json", config)
    script = Path(__file__).resolve().parents[1] / "scripts" / "atlas_paper.py"
    # config-check opens no journal, hence no per-user account-lock directory.
    source = """import runpy, socket, sys
def forbidden(*args, **kwargs): raise AssertionError('network forbidden')
socket.create_connection = forbidden
socket.socket.connect = forbidden
sys.argv = [sys.argv[1], 'config-check', '--config', sys.argv[2]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-c", source, str(script), path], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stderr == ""
    output = json.loads(result.stdout)
    assert output["command"] == "config-check" and output["network_used"] is False
    assert not Path(config["state_dir"]).exists()
