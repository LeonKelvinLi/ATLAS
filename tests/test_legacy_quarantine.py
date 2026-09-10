"""Legacy broker mutation helpers cannot bypass the public proposal interface."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

import server
import live_h2_runner
import supervisor
from execution_mode import reject_legacy_real_mode


@pytest.mark.parametrize("mode", ["paper", "practice"])
def test_legacy_research_and_practice_selection_remains_available(mode):
    reject_legacy_real_mode(mode)


@pytest.mark.parametrize("allow_real", ["0", "1"])
@pytest.mark.parametrize("module", [live_h2_runner, supervisor])
def test_legacy_real_cli_refuses_before_configuration_or_runtime(module, allow_real, monkeypatch, capsys):
    monkeypatch.setenv("IQ_ALLOW_REAL", allow_real)
    monkeypatch.setattr(module, "_load_env" if module is live_h2_runner else "load_env_file",
                        lambda *args: pytest.fail("quarantine must precede configuration and all runtime work"))
    argv = ["--mode", "real", "--amount", "1"]
    monkeypatch.setattr(sys, "argv", [module.__name__, *argv])
    with pytest.raises(SystemExit) as error:
        module.main() if module is live_h2_runner else module.main(argv)
    assert error.value.code == 2
    assert "quarantined" in capsys.readouterr().err


MUTATIONS = {"iq_place_binary", "iq_place_order", "iq_close_position", "iq_cancel_order"}


def test_legacy_mutation_helpers_are_absent_from_advertised_mcp_tools(monkeypatch):
    monkeypatch.setattr(server, "_get_client", lambda: pytest.fail("listing tools must not connect"))
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert not names & MUTATIONS
    assert {"iq_status", "iq_positions", "iq_get_candles", "iq_binary_result"} <= names
    assert all(callable(getattr(server, name)) for name in MUTATIONS)


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_legacy_mcp_cannot_invoke_hidden_mutation_by_name(name, monkeypatch):
    monkeypatch.setattr(server, "_get_client", lambda: pytest.fail("hidden tool must not connect"))
    with pytest.raises(Exception, match="Unknown tool|unknown tool|not found"):
        asyncio.run(server.mcp.call_tool(name, {"order_id": 1}))


@pytest.mark.parametrize("acknowledgement", [None, True, False, {"success": True}])
def test_legacy_python_cancellation_does_not_fabricate_completion(monkeypatch, acknowledgement):
    calls = []
    client = SimpleNamespace(
        get_balance_mode=lambda: "PRACTICE",
        cancel_order=lambda order_id: calls.append(order_id) or acknowledgement,
    )
    monkeypatch.setattr(server, "_selected_balance", "PRACTICE")
    monkeypatch.setattr(server, "_timed_out_calls", set())
    monkeypatch.setattr(server, "_ensure_connected", lambda: client)
    monkeypatch.setattr(server, "_call", lambda fn, *args, timeout=30, **kwargs: fn(*args, **kwargs))
    result = server.iq_cancel_order(42)
    assert calls == [42]
    assert "cancelled" not in result
    assert result["cancellation_status"] == "unknown"
    assert result["request_acknowledged"] is (acknowledgement if isinstance(acknowledgement, bool) else None)
