"""External configuration and operator receipts; no service or broker startup."""

import json
import stat

import pytest

from atlas.__main__ import issue_operator_receipt, main
from atlas.approval import verify_approval
from atlas.config import ConfigError, PROJECT, build_service, load_config, read_secret_file
from atlas.domain import UnsupportedCapability
from atlas.ledger import Ledger, digest


def write_json(path, value):
    path.write_text(json.dumps(value))
    return path


def config_file(tmp_path, **changes):
    return write_json(tmp_path / "config.json", {"state_dir": str(tmp_path / "state"), **changes})


def secret_file(tmp_path, name="secret", value=b"a" * 32):
    path = tmp_path / name
    path.write_bytes(value)
    path.chmod(0o600)
    return path


def test_config_defaults_research_without_state_or_synthetic_money(tmp_path):
    config = load_config(config_file(tmp_path), environ={})
    assert config.mode == "research" and config.broker == "unavailable"
    assert config.ledger_path == tmp_path / "state/research/execution.db"
    assert not config.state_dir.exists()
    service = build_service(config)
    try:
        with pytest.raises(UnsupportedCapability):
            service.broker.get_account()
    finally:
        service.ledger.close()


@pytest.mark.parametrize("changes", [
    {"state_dir": "relative"}, {"state_dir": str(PROJECT / "runtime")},
    {"risk": {"expected_account_id": "injected"}}, {"risk": {"live_enabled": True}},
    {"risk": {"max_order_notional": "NaN"}}, {"risk": {"max_daily_trades": True}},
    {"http": {"host": "0.0.0.0"}}, {"http": {"port": True}},
    {"http": {"execution_tools": True}},
    {"api_key": "not accepted"}, {"mode": "live"},
])
def test_invalid_config_refuses_without_creating_state(tmp_path, changes):
    with pytest.raises(ConfigError):
        load_config(config_file(tmp_path, **changes), environ={})
    assert not (tmp_path / "state").exists()


def test_duplicate_json_and_boolean_flag_strings_are_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"mode":"research","mode":"paper"}')
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(path, environ={})
    with pytest.raises(ConfigError, match="exactly true or false"):
        load_config(config_file(tmp_path), environ={"ATLAS_LIVE_TRADING_ENABLED": "1"})


def test_group_writable_policy_file_is_not_server_owned_configuration(tmp_path):
    path = config_file(tmp_path)
    path.chmod(0o664)
    with pytest.raises(ConfigError, match="trusted-owner"):
        load_config(path, environ={})
    assert not (tmp_path / "state").exists()


def test_no_dotenv_autoload_and_config_repr_does_not_show_account(tmp_path):
    (tmp_path / ".env").write_text("ATLAS_LIVE_TRADING_ENABLED=true\n")
    config = load_config(config_file(tmp_path), environ={})
    assert config.policy.live_enabled is False
    assert "expected_account_id" not in repr(config)


@pytest.mark.parametrize("permissions", [0o644, 0o640, 0o400, 0o660])
def test_secret_files_require_exact_owner_only_permissions(tmp_path, permissions):
    path = secret_file(tmp_path)
    path.chmod(permissions)
    with pytest.raises(ConfigError, match="0600"):
        read_secret_file(path)


def test_secret_file_symlink_and_repository_path_refuse(tmp_path):
    path = secret_file(tmp_path)
    link = tmp_path / "secret-link"
    link.symlink_to(path)
    with pytest.raises(ConfigError, match="symlinks"):
        read_secret_file(link)
    with pytest.raises(ConfigError, match="outside"):
        read_secret_file(PROJECT / "secret-that-must-not-be-created")
    assert read_secret_file(path) == b"a" * 32


@pytest.mark.parametrize("environment_name,attribute", [
    ("ATLAS_APPROVAL_KEY_FILE", "approval_key_file"),
    ("ATLAS_HTTP_BEARER_FILE", "http_bearer_file"),
])
def test_config_normalization_cannot_bypass_secret_symlink_rejection(tmp_path, environment_name, attribute):
    secret = secret_file(tmp_path)
    link = tmp_path / "configured-secret-link"
    link.symlink_to(secret)
    with pytest.raises(ConfigError, match="symlinks"):
        config = load_config(config_file(tmp_path), environ={environment_name: str(link)})
        read_secret_file(getattr(config, attribute))


def test_live_modes_share_scope_but_no_real_factory_or_state(tmp_path):
    account = secret_file(tmp_path, "account", b"private-fixture-account")
    env = {"ATLAS_EXPECTED_ACCOUNT_FILE": str(account), "ATLAS_LIVE_TRADING_ENABLED": "true",
           "ATLAS_LIVE_AUTO_ENABLED": "true"}
    assisted = load_config(config_file(tmp_path, mode="live-assisted", broker="robinhood_crypto"), environ=env)
    automatic = load_config(config_file(tmp_path, mode="live-auto", broker="robinhood_crypto"), environ=env)
    assert assisted.ledger_path == automatic.ledger_path == tmp_path / "state/live/execution.db"
    assert "private-fixture-account" not in repr(assisted)
    for config in (assisted, automatic):
        with pytest.raises(UnsupportedCapability, match="integration_unavailable"):
            build_service(config)
    assert not assisted.state_dir.exists()


def test_explicit_paper_seed_and_fixture_required(tmp_path):
    with pytest.raises(ConfigError, match="explicit"):
        load_config(config_file(tmp_path, mode="paper", broker="paper"), environ={})
    quote_path = write_json(tmp_path / "quotes.json", {"DEMO": {
        "symbol": "DEMO", "asset_class": "equity", "bid": "10", "ask": "10.01",
        "timestamp": 100, "market_open": True, "available_quantity": "100", "source": "fixture",
    }})
    config = load_config(config_file(tmp_path, mode="paper", broker="paper",
                        paper={"account_id": "fixture-paper", "cash": "1234", "fee_bps": "5"},
                        quotes_file=str(quote_path)), environ={})
    service = build_service(config, clock=lambda: 100)
    try:
        assert str(service.broker.get_account().cash) == "1234"
        assert service.broker.get_quote("DEMO").timestamp_basis == "synthetic"
        assert config.ledger_path == tmp_path / "state/paper/execution.db"
    finally:
        service.broker.close()
        service.ledger.close()


def approval_proposal(tmp_path):
    path = tmp_path / "live/execution.db"
    proposal = {"id": "proposal-fixture", "mode": "live-assisted", "kind": "order",
                "signal_id": "fixture-signal", "account_fingerprint": "f" * 64, "policy_hash": "p" * 64,
                "evidence": {"kind": "generated-test"},
                "created_at": 100, "expires_at": 200, "risk": {"passed": True},
                "order": {"symbol": "DEMO", "side": "buy", "quantity": "1", "limit_price": "10"}}
    with Ledger(path, "live") as ledger:
        ledger.save_proposal(proposal, "fixture-approval-key")
    return path, proposal


def test_operator_receipt_requires_exact_hash_and_never_submits(tmp_path):
    journal, proposal = approval_proposal(tmp_path)
    key = secret_file(tmp_path, value=b"k" * 32)
    receipt_file = tmp_path / "receipt.json"
    shown = []
    with pytest.raises(ConfigError, match="exact proposal hash"):
        issue_operator_receipt(journal=journal, proposal_id=proposal["id"], key_file=key,
                               output_file=receipt_file, proposal_hash="yes", now=150, output_fn=shown.append)
    assert not receipt_file.exists()
    assert digest(proposal) in shown[0] and "immutable_proposal" in shown[0]
    result = issue_operator_receipt(journal=journal, proposal_id=proposal["id"], key_file=key,
                                   output_file=receipt_file, proposal_hash=digest(proposal), now=150,
                                   output_fn=shown.append)
    receipt = json.loads(receipt_file.read_text())
    verify_approval(receipt, proposal, b"k" * 32, now=151)
    assert stat.S_IMODE(receipt_file.stat().st_mode) == 0o600
    assert result["trade_submitted"] is False
    with Ledger(journal, "live") as ledger:
        assert ledger.list_intents() == []
    assert "kkkk" not in " ".join(shown)


def test_operator_receipt_rejects_expiry_and_existing_output(tmp_path):
    journal, proposal = approval_proposal(tmp_path)
    key = secret_file(tmp_path)
    with pytest.raises(ValueError, match="expired"):
        issue_operator_receipt(journal=journal, proposal_id=proposal["id"], key_file=key,
                               output_file=tmp_path / "expired.json", proposal_hash=digest(proposal),
                               now=201, output_fn=lambda _: None)
    target = tmp_path / "existing.json"
    target.write_text("preserve existing file")
    with pytest.raises(ConfigError, match="written securely"):
        issue_operator_receipt(journal=journal, proposal_id=proposal["id"], key_file=key,
                               output_file=target, proposal_hash=digest(proposal), now=150, output_fn=lambda _: None)
    assert target.read_text() == "preserve existing file"


def test_default_cli_only_prints_help(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    assert "ATLAS offline research" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_demo_cli_is_in_memory_and_explicitly_synthetic(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["demo"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["evidence_kind"] == "synthetic" and output["real_orders"] == 0
    assert output["simulated_execution"]["intent"]["status"] == "filled"
    assert list(tmp_path.iterdir()) == []
