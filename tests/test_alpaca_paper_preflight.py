"""Fake HTTP only: paper endpoint containment and conservative snapshot claims."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import socket
import ssl

import pytest

import atlas.alpaca_paper_preflight as preflight
from atlas.alpaca_paper_preflight import HTTPResponse, PreflightError, collect_preflight

KEY = "fixture-paper-key-never-log"
SECRET = "fixture-paper-secret-never-log"
ACCOUNT_ID = "0a388feb-311b-4396-bffc-e994b89f2528"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Alpaca preflight tests cannot contact a network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "owner-paper.json"
    path.write_text(json.dumps({"api_key": KEY, "api_secret": SECRET}))
    path.chmod(0o600)
    return path


def account(**changes):
    return {"id": ACCOUNT_ID, "account_number": "DO-NOT-EXPOSE-ACCOUNT-NUMBER",
            "status": "ACTIVE", "crypto_status": "ACTIVE", "currency": "USD",
            "cash": "100", "equity": "100", "buying_power": "200",
            "non_marginable_buying_power": "100", "account_blocked": False,
            "trading_blocked": False, "trade_suspended_by_user": False,
            "last_equity": "80", **changes}


class FakeTransport:
    def __init__(self, payloads=None):
        self.payloads = [account(), [], []] if payloads is None else payloads
        self.calls = []

    def __call__(self, url, headers, **limits):
        self.calls.append((url, headers, limits))
        item = self.payloads[len(self.calls) - 1]
        return item if isinstance(item, HTTPResponse) else HTTPResponse(200, json.dumps(item).encode())


def test_three_fixed_get_targets_and_exact_100_snapshot_never_enable_execution(key_file):
    transport = FakeTransport()
    result = collect_preflight(key_file, transport=transport, clock=lambda: 100)
    assert [row[0] for row in transport.calls] == [preflight.PAPER_ORIGIN + path for path in preflight.READ_PATHS]
    assert all(row[1]["APCA-API-KEY-ID"] == KEY and row[1]["APCA-API-SECRET-KEY"] == SECRET for row in transport.calls)
    assert all(row[2] == {"timeout_seconds": 10, "max_response_bytes": 1_000_000} for row in transport.calls)
    assert result["current_state_matches_100"] is True
    assert result["current_account_controls_allow_crypto"] is True
    assert result["account"]["buying_power_reported"] == "200"
    assert result["account"]["non_marginable_buying_power"] == "100"
    assert result["execution_enabled"] is False and result["orders_submitted"] == result["account_mutations"] == 0
    assert result["daily_realized_pnl"] is result["estimated_order_fee_usd"] is None
    assert result["account"]["account_accrued_fees"] is None
    assert result["immutable_starting_capital_verified"] is False
    assert result["paper_account_read_verified"] is False
    assert result["evidence_kind"] == "injected_test_transport"
    assert result["open_orders_complete"] is True
    assert "not atomic" in result["freshness_basis"]
    encoded = json.dumps(result)
    for private in (KEY, SECRET, ACCOUNT_ID, "DO-NOT-EXPOSE-ACCOUNT-NUMBER"):
        assert private not in encoded


@pytest.mark.parametrize("origin", [
    "https://api.alpaca.markets", "http://paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets.evil.example", "https://paper-api.alpaca.markets/",
    "https://paper-api.alpaca.markets:443", "https://owner@paper-api.alpaca.markets",
])
def test_origin_rejected_before_key_file_read_or_auth_header_construction(tmp_path, origin):
    transport = FakeTransport()
    with pytest.raises(PreflightError, match="paper_origin_required"):
        collect_preflight(tmp_path / "missing-key-file", origin=origin, transport=transport)
    assert transport.calls == []


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_redirect_never_follows_or_exposes_provider_body(key_file, code):
    transport = FakeTransport([HTTPResponse(code, (SECRET + " https://api.alpaca.markets").encode())])
    with pytest.raises(PreflightError, match="paper_redirect_refused") as error:
        collect_preflight(key_file, transport=transport)
    assert len(transport.calls) == 1
    assert SECRET not in str(error.value)


def test_larger_account_remains_larger_and_is_not_clamped_to_100(key_file):
    data = account(cash="100000", equity="100000", non_marginable_buying_power="100000")
    result = collect_preflight(key_file, transport=FakeTransport([data, [], []]))
    assert result["account"]["cash"] == result["account"]["equity"] == "100000"
    assert result["current_state_matches_100"] is False
    assert "current_state_does_not_match_flat_100_usd" in result["blockers"]


@pytest.mark.parametrize("changes", [
    {"status": "PAPER_ONLY"}, {"crypto_status": None}, {"account_blocked": None},
    {"trading_blocked": True}, {"trade_suspended_by_user": True},
])
def test_unknown_or_blocked_permissions_are_not_assumed_tradable(key_file, changes):
    result = collect_preflight(key_file, transport=FakeTransport([account(**changes), [], []]))
    assert result["current_account_controls_allow_crypto"] is False
    assert "account_crypto_permissions_not_verified_active" in result["blockers"]
    assert result["execution_enabled"] is False


def test_low_cash_only_buying_power_not_hidden_by_margin_power(key_file):
    result = collect_preflight(key_file, transport=FakeTransport([
        account(buying_power="1000", non_marginable_buying_power="10"), [], []]))
    assert "less_than_100_usd_non_marginable_buying_power" in result["blockers"]


@pytest.mark.parametrize("changes", [
    {"cash": "NaN"}, {"equity": "Infinity"}, {"cash": True}, {"cash": 100.0},
    {"accrued_fees": "NaN"}, {"non_marginable_buying_power": None},
    {"status": {}}, {"crypto_status": []}, {"account_blocked": "false"},
])
def test_invalid_or_unknown_required_account_amounts_refuse(key_file, changes):
    with pytest.raises(PreflightError):
        collect_preflight(key_file, transport=FakeTransport([account(**changes), [], []]))


@pytest.mark.parametrize("payloads", [
    [[], [], []], [account(), {}, []], [account(), [], {}],
    [HTTPResponse(200, b'{"id":NaN}')], [HTTPResponse(200, b'{"id":1,"id":2}')],
    [HTTPResponse(200, b"not-json")], [HTTPResponse(200, b"x" * 1_000_001)],
    [HTTPResponse(401, SECRET.encode())],
])
def test_bad_payloads_fail_with_safe_errors(key_file, payloads):
    with pytest.raises(PreflightError) as error:
        collect_preflight(key_file, transport=FakeTransport(payloads))
    assert SECRET not in str(error.value)


def test_positions_and_orders_prevent_clean_account_claim_and_btc_alias_is_explicit(key_file):
    position = {"symbol": "BTCUSD", "asset_class": "crypto", "qty": "0.001", "side": "long",
                "market_value": "100", "cost_basis": "99"}
    order = {"symbol": "BTC/USD", "side": "buy", "status": "new", "qty": "0.0001",
             "notional": None, "filled_qty": "0", "limit_price": "100000",
             "id": "PRIVATE-ORDER-ID", "client_order_id": "PRIVATE-CLIENT-ID"}
    result = collect_preflight(key_file, transport=FakeTransport([account(), [position], [order]]))
    assert result["current_state_matches_100"] is False
    assert result["positions"][0]["canonical_atlas_symbol"] == "BTC-USD"
    assert result["open_orders"][0]["quantity"] == "0.0001"
    assert "PRIVATE-ORDER-ID" not in json.dumps(result)
    assert "PRIVATE-CLIENT-ID" not in json.dumps(result)


def test_full_order_page_is_not_claimed_complete(key_file):
    order = {"symbol": "BTC/USD", "side": "buy", "status": "new", "qty": "0.0001", "filled_qty": "0"}
    result = collect_preflight(key_file, transport=FakeTransport([account(), [], [order] * 500]))
    assert result["open_orders_complete"] is False
    assert "open_order_listing_may_be_truncated" in result["blockers"]


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_owner_only_key_permissions_are_mandatory_before_requests(key_file, mode):
    key_file.chmod(mode)
    transport = FakeTransport()
    with pytest.raises(PreflightError, match="paper_credential_file_invalid"):
        collect_preflight(key_file, transport=transport)
    assert transport.calls == []


def test_key_file_symlink_is_refused_before_requests(key_file, tmp_path):
    link = tmp_path / "key-link"
    link.symlink_to(key_file)
    transport = FakeTransport()
    with pytest.raises(PreflightError, match="paper_credential_file_invalid"):
        collect_preflight(link, transport=transport)
    assert transport.calls == []


@pytest.mark.parametrize("payload", [
    {"api_key": KEY}, {"api_key": KEY, "api_secret": SECRET, "paper": True},
    {"api_key": KEY, "api_secret": "header\r\ninjection"},
])
def test_exact_credential_schema_and_header_injection_refusal(key_file, payload):
    key_file.write_text(json.dumps(payload))
    transport = FakeTransport()
    with pytest.raises(PreflightError, match="paper_credential_fields_invalid"):
        collect_preflight(key_file, transport=transport)
    assert transport.calls == []


@pytest.mark.parametrize("clock_values", [[100, 131], [100, 99]])
def test_snapshot_receipt_clock_is_bounded_and_cannot_rewind(key_file, clock_values):
    values = iter(clock_values)
    with pytest.raises(PreflightError, match="paper_snapshot_window_invalid"):
        collect_preflight(key_file, transport=FakeTransport(), clock=lambda: next(values))


def test_transport_exception_text_is_never_exposed(key_file):
    def failed(*args, **kwargs):
        raise PreflightError(SECRET)
    with pytest.raises(PreflightError, match="^paper_transport_failed$"):
        collect_preflight(key_file, transport=failed)


class FakeResponse:
    def __init__(self, body=b"{}", status=200, length=None):
        self.body, self.status = body, status
        self.length = str(len(body)) if length is None else length

    def getheader(self, name):
        return self.length if name == "Content-Length" else None

    def read1(self, count):
        chunk, self.body = self.body[:count], self.body[count:]
        return chunk


def owned_http(monkeypatch, responses):
    calls, connections = [], []
    class Connection:
        sock = None
        def __init__(self, host, *, timeout, context):
            assert host == preflight.PAPER_HOST and timeout == 10
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            self.closed = False
            connections.append(self)
        def request(self, method, path, *, headers):
            calls.append((method, path, headers))
        def getresponse(self):
            return responses[len(calls) - 1]
        def close(self):
            self.closed = True
    monkeypatch.setattr(preflight.http.client, "HTTPSConnection", Connection)
    return calls, connections


def test_owned_transport_uses_verified_tls_only_three_gets_and_closes_connections(key_file, monkeypatch):
    calls, connections = owned_http(monkeypatch, [FakeResponse(json.dumps(row).encode()) for row in [account(), [], []]])
    result = collect_preflight(key_file, clock=lambda: 100)
    assert [(row[0], row[1]) for row in calls] == [("GET", path) for path in preflight.READ_PATHS]
    assert all(item.closed for item in connections)
    assert result["paper_account_read_verified"] is True
    assert result["execution_enabled"] is False


@pytest.mark.parametrize("response,code", [
    (FakeResponse(SECRET.encode(), status=302), "paper_redirect_refused"),
    (FakeResponse(SECRET.encode(), status=500), "paper_http_request_failed"),
    (FakeResponse(b"{}", length="1000001"), "paper_response_size_invalid"),
    (FakeResponse(b"{}", length="3"), "paper_response_length_mismatch"),
])
def test_owned_transport_refuses_redirect_http_error_and_size_failure(key_file, monkeypatch, response, code):
    calls, connections = owned_http(monkeypatch, [deepcopy(response)])
    with pytest.raises(PreflightError, match=code) as error:
        collect_preflight(key_file)
    assert len(calls) == 1 and connections[0].closed
    assert SECRET not in str(error.value)


def test_owned_transport_deadline_bounds_body_reads(key_file, monkeypatch):
    calls, connections = owned_http(monkeypatch, [FakeResponse(b"{}")])
    times = iter([0, 11])
    monkeypatch.setattr(preflight.time, "monotonic", lambda: next(times))
    with pytest.raises(PreflightError, match="paper_request_timeout"):
        collect_preflight(key_file, clock=lambda: 100)
    assert len(calls) == 1 and connections[0].closed


def load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts/alpaca_paper_preflight.py"
    spec = importlib.util.spec_from_file_location("alpaca_preflight_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_needs_no_credentials_or_network(capsys):
    with pytest.raises(SystemExit) as result:
        load_cli().main(["--help"])
    assert result.value.code == 0
    assert "api_key and api_secret" in " ".join(capsys.readouterr().out.split())


def test_cli_unexpected_exception_redacts_credentials(key_file, monkeypatch, capsys):
    cli = load_cli()
    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(cli, "collect_preflight", fail)
    assert cli.main(["--key-file", str(key_file)]) == 2
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.err)["execution_enabled"] is False


def test_cli_does_not_overwrite_existing_report_or_contact_broker(key_file, tmp_path, monkeypatch, capsys):
    cli = load_cli()
    destination = tmp_path / "existing.json"
    destination.write_text("preserve")
    def fail(*args, **kwargs):
        pytest.fail("existing report must refuse before account contact")
    monkeypatch.setattr(cli, "collect_preflight", fail)
    assert cli.main(["--key-file", str(key_file), "--output", str(destination)]) == 2
    assert destination.read_text() == "preserve"
    assert "paper_report_destination_must_be_new" in capsys.readouterr().err
