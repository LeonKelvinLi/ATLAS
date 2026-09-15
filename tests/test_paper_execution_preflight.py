"""Offline equity preflight checks; broker traffic and state writes are forbidden."""
import builtins
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from atlas.paper import broker
from atlas.paper.broker import AlpacaPaperAPI, Response
from atlas.paper.models import PaperError
from atlas.paper.preflight import collect_preflight
from atlas.paper.simulation import SyntheticAlpaca
from atlas.risk import account_fingerprint


@pytest.fixture(autouse=True)
def prohibit_network_credentials_and_writes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight tests prohibit network, credentials, mutations and state writes")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(broker, "_credentials", forbidden)
    monkeypatch.setattr(AlpacaPaperAPI, "submit_order", forbidden)
    monkeypatch.setattr(AlpacaPaperAPI, "cancel_order", forbidden)
    monkeypatch.setattr(AlpacaPaperAPI, "bind_gate", forbidden)
    for name in ("mkdir", "touch", "write_text", "write_bytes", "unlink", "rename", "replace"):
        monkeypatch.setattr(Path, name, forbidden)
    for module in (builtins, io):
        original = module.open
        def guarded(file, mode="r", *args, _original=original, **kwargs):
            if any(flag in mode for flag in "wax+"):
                forbidden()
            return _original(file, mode, *args, **kwargs)
        monkeypatch.setattr(module, "open", guarded)
    original_os_open = os.open
    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            forbidden()
        return original_os_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded_os_open)


def fixture(transform=None):
    sim = SyntheticAlpaca()
    counts = {}
    def request(method, url, headers, payload):
        assert method == "GET" and payload is None
        assert not {"APCA-API-KEY-ID", "APCA-API-SECRET-KEY"}.intersection(headers)
        path = urlsplit(url).path
        counts[path] = counts.get(path, 0) + 1
        response = sim.request(method, url, headers, payload)
        if transform:
            row = json.loads(response.body)
            replacement = transform(path, row, counts[path], sim)
            if isinstance(replacement, Response):
                return replacement
            return Response(200, json.dumps(replacement).encode())
        return response
    return sim, AlpacaPaperAPI(transport=request, clock=sim.clock)


def run(api, sim, **kwargs):
    return collect_preflight(api, symbols=("SPY",), clock=sim.clock, **kwargs)


def test_complete_read_only_test_report_uses_new_namespace_and_has_no_order_authority():
    sim, api = fixture()
    result = collect_preflight(api, clock=sim.clock)
    assert result["account_ready"] and result["market_data_ready"]
    assert result["provenance"] == "TEST" and not result["account_read_verified"]
    assert result["account_fingerprint"] == account_fingerprint("alpaca-paper", sim.account_id)
    assert result["account_fingerprint"] != account_fingerprint("alpaca_paper", sim.account_id)
    assert result["account_fingerprint_namespace"] == "alpaca-paper"
    assert result["execution_ready"] is result["order_permission_verified"] is False
    assert result["actual_orders_submitted"] == result["account_mutations"] == 0
    assert result["journal_created"] is False
    assert result["history_counts"] == {"positions": 0, "orders": 0, "activities": 0}
    assert len(sim.calls) == 14
    assert [call["path"] for call in sim.calls[:5]] == ["/v2/account", "/v2/positions", "/v2/orders", "/v2/account/activities", "/v2/account"]
    assert "status=all" in sim.calls[2]["query"]
    encoded = json.dumps(result)
    assert sim.account_id not in encoded and "API-SECRET" not in encoded
    assert not sim.orders and not sim.activities


@pytest.mark.parametrize(("changes", "blocker"), [
    ({"cash": "101", "equity": "101"}, "dedicated_cash_matches_principal"),
    ({"equity": "99"}, "dedicated_cash_matches_principal"),
    ({"non_marginable_buying_power": "99"}, "non_marginable_buying_power"),
    ({"non_marginable_buying_power": "200"}, "non_marginable_buying_power"),
    ({"multiplier": "2"}, "multiplier_one"),
    ({"trading_blocked": True}, "account_active"),
    ({"status": "ACCOUNT_UPDATED"}, "account_active"),
    ({"short_market_value": "-1"}, "no_market_exposure"),
    ({"long_market_value": "1"}, "no_market_exposure"),
])
def test_current_account_requirements_are_exact(changes, blocker):
    sim, api = fixture(lambda path, row, n, sim: {**row, **changes} if path == "/v2/account" else row)
    result = run(api, sim)
    assert not result["account_ready"] and blocker in result["account_blockers"]


def test_account_changes_between_reads_are_not_atomic_readiness():
    def change(path, row, count, sim):
        return {**row, "cash": "99"} if path == "/v2/account" and count == 2 else row
    sim, api = fixture(change)
    result = run(api, sim)
    assert "account_stable" in result["account_blockers"]


def test_changed_account_identity_is_consistent_in_report_and_fails_readiness():
    def change(path, row, count, sim):
        return {**row, "id": "00000000-0000-4000-8000-000000000002"} if path == "/v2/account" and count == 2 else row
    sim, api = fixture(change)
    result = run(api, sim)
    assert result["account_fingerprint"] == result["account"]["fingerprint"]
    assert result["account_fingerprint"] != account_fingerprint("alpaca-paper", sim.account_id)
    assert "account_stable" in result["account_blockers"] and not result["account_ready"]


@pytest.mark.parametrize("history", ["positions", "orders", "activities"])
def test_nonempty_account_history_blocks_initialization(history):
    def change(path, row, count, sim):
        if history == "positions" and path == "/v2/positions":
            return [{"symbol": "SPY", "asset_class": "us_equity", "side": "long", "qty": "1", "market_value": "10", "current_price": "10", "cost_basis": "10"}]
        if history == "activities" and path == "/v2/account/activities":
            return [{"id": "synthetic-transfer", "activity_type": "TRANS", "date": "2026-09-15"}]
        if history == "orders" and path == "/v2/orders":
            stamp = datetime.fromtimestamp(sim.now, timezone.utc).isoformat()
            return [{"id": "00000000-0000-4000-8000-000000000001", "client_order_id": "historical-test",
                "symbol": "SPY", "asset_class": "us_equity", "side": "buy", "qty": "1", "limit_price": "10",
                "filled_qty": "0", "filled_avg_price": None, "type": "limit", "time_in_force": "day",
                "extended_hours": False, "status": "canceled", "created_at": stamp, "updated_at": stamp,
                "replaces": None, "replaced_by": None}]
        return row
    sim, api = fixture(change)
    result = run(api, sim)
    assert not result["account_ready"]
    assert result["history_counts"][history] == 1


def test_market_closed_is_waiting_with_good_account_and_no_quote_get():
    sim, api = fixture()
    sim.is_open = False
    result = run(api, sim)
    assert result["account_ready"] and result["market_state"] == "WAIT_MARKET_OPEN"
    assert not result["market_data_ready"] and not result["read_errors"]
    assert result["assets"]["SPY"]["blockers"] == ["wait_market_open"]
    assert not any("quotes" in call["path"] for call in sim.calls)


@pytest.mark.parametrize("changes", [{"tradable": False}, {"status": "inactive"}])
def test_ineligible_asset_does_not_query_quote(changes):
    sim, api = fixture(lambda path, row, n, sim: {**row, **changes} if path.startswith("/v2/assets/") else row)
    result = run(api, sim)
    assert result["account_ready"] and not result["assets"]["SPY"]["equity_eligible"]
    assert not result["market_data_ready"]
    assert not any("quotes" in call["path"] for call in sim.calls)


def test_fractionable_false_is_not_whole_share_or_account_permission_failure():
    sim, api = fixture(lambda path, row, n, sim: {**row, "fractionable": False} if path.startswith("/v2/assets/") else row)
    result = run(api, sim)
    asset = result["assets"]["SPY"]
    assert asset["equity_eligible"] and asset["data_ready"]
    assert not asset["fractional_asset_eligible"] and not asset["account_fractional_permission_verified"]


@pytest.mark.parametrize("age", [11, 31])
def test_stale_quote_never_passes_data_readiness(age):
    def change(path, row, count, sim):
        if "quotes" in path:
            row["quotes"]["SPY"]["t"] = datetime.fromtimestamp(sim.now - age, timezone.utc).isoformat()
        return row
    sim, api = fixture(change)
    result = run(api, sim)
    assert result["account_ready"] and not result["market_data_ready"]
    if age == 11:
        assert "quote_fresh" in result["assets"]["SPY"]["blockers"]
    else:
        assert result["read_errors"] == [{"step": "quote_SPY", "code": "paper_quote_crossed_stale_or_future"}]


def test_wide_spread_fails_default_policy_threshold():
    sim, api = fixture()
    sim.quotes["SPY"]["bid"] = "9"
    result = run(api, sim)
    assert not result["market_data_ready"] and "spread" in result["assets"]["SPY"]["blockers"]


def test_slow_reads_expire_the_report_instead_of_refreshing_receipt_time():
    def change(path, row, count, sim):
        if path.startswith("/v2/assets/"):
            sim.advance(11)
        return row
    sim, api = fixture(change)
    result = run(api, sim)
    assert "snapshot_fresh" in result["account_blockers"]
    assert not result["market_clock_fresh"] and not result["market_data_ready"]


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_read_failure_stops_without_retries_or_provider_body_leak(status):
    secret = "never-print-provider-body-or-secret"
    sim, api = fixture(lambda path, row, n, sim: Response(status, json.dumps({"message": secret}).encode(), {"Retry-After": "900"}))
    result = run(api, sim)
    assert len(sim.calls) == 1 and not result["account_ready"]
    assert len(result["read_errors"]) == 1 and secret not in json.dumps(result)
    assert result["account_read_verified"] is False


def test_incomplete_saturated_order_history_fails_closed():
    sim, api = fixture(lambda path, row, n, sim: [{}] * 500 if path == "/v2/orders" else row)
    result = run(api, sim)
    assert len(sim.calls) == 3 and not result["account_ready"]
    assert result["read_errors"] == [{"step": "all_orders", "code": "paper_order_history_incomplete"}]


def test_all_account_activity_pages_are_read_and_not_erased():
    sim, api = fixture()
    sim.activities = [{"id": f"transfer-{i}", "activity_type": "TRANS", "date": "2026-09-15"}
                      for i in range(100)]
    result = run(api, sim)
    assert not result["account_ready"] and result["history_counts"]["activities"] == 100
    assert sum(call["path"] == "/v2/account/activities" for call in sim.calls) == 2


def test_lower_principal_requires_the_actual_matching_cash_not_a_virtual_slice():
    sim, api = fixture()
    assert not run(api, sim, allocated_principal="50")["account_ready"]
    sim, api = fixture()
    sim.cash = "50"
    assert run(api, sim, allocated_principal="50")["account_ready"]


def test_provider_clock_eleven_seconds_old_is_not_execution_fresh():
    def change(path, row, count, sim):
        if path == "/v2/clock":
            row["timestamp"] = datetime.fromtimestamp(sim.now - 11, timezone.utc).isoformat()
        return row
    sim, api = fixture(change)
    result = run(api, sim)
    assert result["account_ready"] and not result["market_clock_fresh"]
    assert not result["market_data_ready"]


def test_account_currency_contract_error_is_reported_without_subsequent_reads():
    sim, api = fixture(lambda path, row, n, sim: {**row, "currency": "EUR"} if path == "/v2/account" else row)
    result = run(api, sim)
    assert not result["account_ready"] and len(sim.calls) == 1
    assert result["read_errors"] == [{"step": "account_initial", "code": "paper_account_contract_invalid"}]


def test_owned_api_identity_uses_only_mock_credentials_and_pinned_mock_http(monkeypatch):
    sim = SyntheticAlpaca()
    monkeypatch.setattr(broker, "_credentials", lambda path: SimpleNamespace(api_key="SYNTHETIC-KEY", api_secret="SYNTHETIC-SECRET"))
    def mocked_owned_http(method, url, headers, payload, *, paper_execution_enabled):
        assert method == "GET" and not paper_execution_enabled
        assert urlsplit(url).netloc in {"paper-api.alpaca.markets", "data.alpaca.markets"}
        return sim.request(method, url, {}, payload)
    monkeypatch.setattr(broker, "_owned_http", mocked_owned_http)
    api = AlpacaPaperAPI(credentials_path="synthetic-not-a-file", clock=sim.clock)
    result = run(api, sim)
    assert result["provenance"] == "PAPER_API" and result["account_read_verified"]
    assert "SYNTHETIC-SECRET" not in json.dumps(result)
    assert not result["execution_ready"]


@pytest.mark.parametrize("symbols", [(), "SPY", ("SPY", "SPY"), ("BTC/USD",), ("../SPY",), ("SPY",) * 11])
def test_invalid_symbols_refused_before_any_read(symbols):
    sim, api = fixture()
    with pytest.raises(PaperError, match="paper_preflight_symbols_invalid"):
        collect_preflight(api, symbols=symbols, clock=sim.clock)
    assert not sim.calls


@pytest.mark.parametrize("principal", ["101", "0", "-1", "NaN", 100])
def test_invalid_allocation_refused_before_any_read(principal):
    sim, api = fixture()
    with pytest.raises(PaperError):
        run(api, sim, allocated_principal=principal)
    assert not sim.calls


def test_strict_component_identity_and_clock_validation_before_network():
    class DerivedAPI(AlpacaPaperAPI):
        pass
    with pytest.raises(PaperError, match="paper_preflight_configuration_invalid"):
        collect_preflight(DerivedAPI())
    sim, api = fixture()
    with pytest.raises(PaperError, match="paper_timestamp_invalid"):
        collect_preflight(api, clock=lambda: float("nan"))
    assert not sim.calls


def test_clock_rewind_is_a_fixed_error():
    sim, api = fixture()
    values = iter((sim.now, sim.now - 1))
    with pytest.raises(PaperError, match="paper_preflight_clock_rewind"):
        collect_preflight(api, clock=lambda: next(values))
    assert not sim.calls
