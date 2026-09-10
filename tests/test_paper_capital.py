"""Bounded synthetic pilot funding, configuration and restart contracts."""

from decimal import Decimal as D
import json
import socket
import sqlite3

import pytest

from atlas.brokers.paper import PaperBroker
from atlas.config import ConfigError, PROJECT, build_service, load_config
from atlas.domain import BrokerError, BrokerRejected, OrderRequest, Quote


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("paper capital tests cannot contact a network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def paper(path, *, cash="100", initial_capital_limit="100", account_id="pilot"):
    quote = Quote("TEST", "equity", D("9.99"), D("10"), 100, True,
                  D("1000"), "synthetic-capital-test", timestamp_basis="synthetic")
    return PaperBroker({"TEST": quote}, cash=cash, initial_capital_limit=initial_capital_limit,
                       account_id=account_id, path=path, fee_bps="100", clock=lambda: 100)


def buy(client="buy", quantity="1"):
    return OrderRequest("pilot", "TEST", "equity", "buy", D(quantity), D("10"), client)


def inspect(path):
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
        return {"metadata": dict(db.execute("SELECT key,value FROM metadata")),
                "account": db.execute("SELECT * FROM account").fetchall(),
                "orders": db.execute("SELECT * FROM orders").fetchall(),
                "version": db.execute("PRAGMA user_version").fetchone()[0]}


def test_bound_starting_capital_and_fill_survive_restart_without_reseeding(tmp_path):
    path = tmp_path / "pilot.db"
    broker = paper(path)
    fill = broker.submit_order(buy())
    account = broker.get_account()
    assert account.cash == D("89.9")
    assert account.positions[0].cost_basis == D("10.1")
    broker.close()

    state = inspect(path)
    assert state["version"] == 3
    assert state["metadata"]["initial_cash"] == "100"
    assert state["metadata"]["initial_capital_limit"] == "100"
    restored = paper(path, cash="100.00", initial_capital_limit="100.0")
    try:
        assert restored.get_account() == account
        assert restored.submit_order(buy()) == fill
        assert restored.get_account() == account
        with pytest.raises(BrokerRejected, match="insufficient_buying_power"):
            restored.submit_order(buy("beyond-remaining-funds", "9"))
        assert restored.get_account() == account
        assert restored.get_order_status(client_order_id="beyond-remaining-funds") is None
    finally:
        restored.close()


@pytest.mark.parametrize("changes", [
    {"cash": "99"}, {"initial_capital_limit": "200"},
    {"initial_capital_limit": None}, {"cash": "1000", "initial_capital_limit": "1000"},
])
def test_reopening_cannot_change_or_remove_capital_commitment(tmp_path, changes):
    path = tmp_path / "pilot.db"
    paper(path).close()
    before = inspect(path)
    with pytest.raises(BrokerError, match="capital_binding"):
        paper(path, **changes)
    assert inspect(path) == before
    paper(path).close()


@pytest.mark.parametrize("original_cash", ["100", "10000"])
def test_existing_uncapped_account_cannot_be_adopted_as_bounded_pilot(tmp_path, original_cash):
    path = tmp_path / "old.db"
    paper(path, cash=original_cash, initial_capital_limit=None).close()
    before = inspect(path)
    with pytest.raises(BrokerError, match="capital_history_unverified"):
        paper(path)
    assert inspect(path) == before
    restored = paper(path, cash="1", initial_capital_limit=None)
    try:
        assert restored.get_account().cash == D(original_cash)
    finally:
        restored.close()


def test_seed_above_limit_refuses_before_creating_any_state(tmp_path):
    path = tmp_path / "missing-directory" / "pilot.db"
    with pytest.raises(BrokerError, match="initial_capital_limit_exceeded"):
        paper(path, cash="100.01")
    assert not path.parent.exists()


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "-1", "0"])
def test_invalid_capital_limit_refuses_before_creating_state(tmp_path, value):
    path = tmp_path / "pilot.db"
    with pytest.raises(ValueError):
        paper(path, initial_capital_limit=value)
    assert not path.exists()


def test_initial_funding_bound_does_not_discard_profit_or_reseed_losses(tmp_path):
    path = tmp_path / "pilot.db"
    broker = paper(path)
    broker.submit_order(buy())
    broker.quotes["TEST"] = Quote("TEST", "equity", D("20"), D("20.01"),
                                   100, True, D("100"), "synthetic-capital-test")
    broker.submit_order(OrderRequest("pilot", "TEST", "equity", "sell", D("1"), D("20"), "exit"))
    assert broker.get_account().cash == D("109.7")
    broker.close()
    restored = paper(path)
    try:
        assert restored.get_account().cash == D("109.7")
    finally:
        restored.close()


def test_capital_metadata_damage_blocks_active_instance_before_any_order(tmp_path):
    path = tmp_path / "pilot.db"
    broker = paper(path)
    try:
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM metadata WHERE key='initial_cash'")
        with pytest.raises(BrokerError, match="capital_history_unverified"):
            broker.submit_order(buy())
        assert inspect(path)["orders"] == []
    finally:
        broker.close()


def config(tmp_path, **paper_changes):
    data = json.loads((PROJECT / "deploy/paper-100-config.example.json").read_text())
    data["state_dir"] = str(tmp_path / "state")
    data["datasets_file"] = str(tmp_path / "datasets.json")
    data["quotes_file"] = str(tmp_path / "quotes.json")
    data["paper"].update(paper_changes)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("changes", [
    {"cash": "100.01"}, {"initial_capital_limit": None},
    {"initial_capital_limit": True}, {"initial_capital_limit": "0"},
])
def test_config_refuses_invalid_funding_without_state(tmp_path, changes):
    with pytest.raises(ConfigError, match="financial configuration"):
        load_config(config(tmp_path, **changes), environ={})
    assert not (tmp_path / "state").exists()


def test_pilot_template_factory_binds_funding_and_risk_with_execution_tools_disabled(tmp_path):
    selected = load_config(config(tmp_path), environ={})
    assert selected.mode == "paper" and selected.broker == "paper"
    assert selected.execution_tools is False
    assert selected.policy.max_order_notional == 10
    assert selected.policy.max_position_notional == 25
    assert selected.policy.max_daily_loss == 5
    assert selected.policy.max_daily_trades == 4
    assert not selected.state_dir.exists()
    (tmp_path / "datasets.json").write_text("{}")
    (tmp_path / "quotes.json").write_text(json.dumps({"BTC-USD": {
        "symbol": "BTC-USD", "asset_class": "crypto", "bid": "99.99", "ask": "100",
        "timestamp": 100, "market_open": True, "available_quantity": "100", "source": "test fixture",
    }}))
    service = build_service(selected, clock=lambda: 100)
    try:
        assert service.broker.initial_capital_limit == 100
        assert service.broker.get_account().cash == 100
        assert service.broker.get_account().account_id == selected.policy.expected_account_id
        assert service.broker.get_quote("BTC-USD").timestamp_basis == "synthetic"
        assert service.broker.capabilities.network_enabled is False
    finally:
        service.broker.close()
        service.ledger.close()
