"""Synthetic keys and injected HTTP only; no Coinbase account or network access."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import socket
import ssl
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
import jwt
import pytest

import atlas.coinbase_preflight as module
from atlas.coinbase_preflight import HTTPResponse, PreflightError, collect_preflight

PORTFOLIO = "0a388feb-311b-4396-bffc-e994b89f2528"
OTHER_PORTFOLIO = "f1d71f59-47f1-4e5d-adb0-65f5b6d30d72"
ACCOUNT = "46d22878-e806-4126-ac7a-cbff4b422653"
OTHER_ACCOUNT = "835d8d64-771b-4230-a4ca-d1685235760f"
KEY_NAME = "organizations/fixture-org/apiKeys/fixture-key"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Coinbase preflight tests must not contact a network")
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)


def pem_for(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "synthetic-key.json"
    path.write_text(json.dumps({"name": KEY_NAME, "privateKey": pem_for(ec.generate_private_key(ec.SECP256R1()))}))
    path.chmod(0o600)
    return path


def permissions(**changes):
    return {"can_view": True, "can_trade": False, "can_transfer": False,
            "portfolio_uuid": PORTFOLIO, "portfolio_type": "DEFAULT", **changes}


def account(**changes):
    return {"uuid": ACCOUNT, "name": "PRIVATE ACCOUNT NAME", "retail_portfolio_id": PORTFOLIO,
            "currency": "USD", "available_balance": {"value": "987.65", "currency": "USD"},
            "hold": {"value": "12.35", "currency": "USD"}, "active": True, "ready": True, **changes}


def page(accounts=None, **changes):
    accounts = [account()] if accounts is None else accounts
    return {"accounts": accounts, "size": len(accounts), "has_next": False, "cursor": "", **changes}


def fees(**changes):
    return {"fee_tier": {"maker_fee_rate": "0.006", "taker_fee_rate": "0.012", "pricing_tier": "Intro1"}, **changes}


def product(**changes):
    return {"product_id": "BTC-USD", "product_type": "SPOT", "base_currency_id": "BTC",
            "quote_currency_id": "USD", "status": "online", "base_increment": "0.00000001",
            "quote_increment": "0.01", "base_min_size": "0.00000001", "base_max_size": "1000",
            "price_increment": "0.01",
            "quote_min_size": "1", "quote_max_size": "10000000", "view_only": False,
            "is_disabled": False, "trading_disabled": False, "cancel_only": False,
            "limit_only": False, "post_only": False, "auction_mode": False, **changes}


class FakeTransport:
    def __init__(self, payloads=None):
        self.payloads = payloads if payloads is not None else [permissions(), page(), fees(), product()]
        self.calls = []

    def __call__(self, url, headers, **limits):
        self.calls.append((url, headers, limits))
        value = self.payloads[len(self.calls) - 1]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, HTTPResponse) else HTTPResponse(200, json.dumps(value).encode())


def collect(key_file, transport=None, **kwargs):
    return collect_preflight(key_file, expected_portfolio=PORTFOLIO,
                             transport=transport or FakeTransport(), clock=lambda: 100, **kwargs)


def test_genuine_balances_fees_and_four_read_routes_never_enable_execution(key_file):
    transport = FakeTransport()
    report = collect(key_file, transport)
    assert [c[0] for c in transport.calls] == [module.ORIGIN + p for p in (
        module.PERMISSIONS_PATH, module.ACCOUNTS_PATH + "?limit=250", module.FEES_PATH, module.PRODUCT_PATH)]
    assert all(c[2] == {"timeout_seconds": 10, "max_response_bytes": 1_000_000} for c in transport.calls)
    assert report["accounts"][0]["available_balance"] == "987.65"
    assert report["accounts"][0]["hold"] == "12.35"
    assert report["account_count"] == 1 and report["accounts_complete"] is True
    assert report["fees"]["maker_fee_rate"] == "0.006" and report["fees"]["taker_fee_rate"] == "0.012"
    assert report["equity_usd"] is None and report["allocation_enforced"] is False
    assert report["account_read_verified"] is False and report["evidence_kind"] == "injected_test_transport"
    assert report["account_environment"] == "live" and report["execution_enabled"] is False
    assert report["orders_submitted"] == report["account_mutations"] == 0
    assert report["product"]["ordinary_limit_buy_product_flags_allow"] is True
    assert report["product"]["account_trade_permission_verified"] is False
    encoded = json.dumps(report)
    for private in (KEY_NAME, "PRIVATE ACCOUNT NAME", PORTFOLIO, ACCOUNT, "BEGIN PRIVATE KEY"):
        assert private not in encoded


def test_fresh_jwts_verify_signature_and_bind_path_without_query(key_file):
    transport = FakeTransport()
    collect(key_file, transport)
    private_key = serialization.load_pem_private_key(json.loads(key_file.read_text())["privateKey"].encode(), None)
    nonces = set()
    for url, headers, _ in transport.calls:
        assert headers["Authorization"].startswith("Bearer ")
        token = headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, private_key.public_key(), algorithms=["ES256"],
                            options={"verify_exp": False, "verify_nbf": False})
        header = jwt.get_unverified_header(token)
        assert claims == {"sub": KEY_NAME, "iss": "cdp", "nbf": 100, "exp": 220,
                          "uri": "GET api.coinbase.com" + urlsplit(url).path}
        assert header["kid"] == KEY_NAME and header["alg"] == "ES256"
        assert header["nonce"] not in nonces
        nonces.add(header["nonce"])
    assert len(nonces) == 4


@pytest.mark.parametrize("changes", [
    {"can_view": False}, {"can_trade": True}, {"can_transfer": True}, {"can_view": "true"},
    {"can_trade": 0}, {"can_transfer": None}, {"portfolio_uuid": OTHER_PORTFOLIO}, {"portfolio_uuid": "invalid"},
])
def test_permission_or_portfolio_refusal_before_any_account_read(key_file, changes):
    transport = FakeTransport([permissions(**changes)])
    with pytest.raises(PreflightError):
        collect(key_file, transport)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("origin", ["https://api.coinbase.com/", "http://api.coinbase.com", "https://api.coinbase.com:443",
                                    "https://api.coinbase.com.evil.test", "https://user@api.coinbase.com"])
def test_origin_refused_before_reading_secret(tmp_path, origin):
    with pytest.raises(PreflightError, match="coinbase_origin_required"):
        collect_preflight(tmp_path / "nonexistent", expected_portfolio=PORTFOLIO, origin=origin)


@pytest.mark.parametrize("portfolio", [None, 1, "", "BAD-ID", PORTFOLIO.upper()])
def test_expected_portfolio_required_before_secret_read(tmp_path, portfolio):
    with pytest.raises(PreflightError, match="coinbase_expected_portfolio_invalid"):
        collect_preflight(tmp_path / "nonexistent", expected_portfolio=portfolio)


def test_key_file_must_be_owner_only_and_not_symlink(key_file, tmp_path):
    key_file.chmod(0o644)
    with pytest.raises(PreflightError, match="coinbase_credential_file_invalid"):
        collect(key_file)
    key_file.chmod(0o600)
    linked = tmp_path / "linked.json"
    linked.symlink_to(key_file)
    with pytest.raises(PreflightError, match="coinbase_credential_file_invalid"):
        collect(linked)


@pytest.mark.parametrize("change", [
    {"name": "bad-key-id"}, {"extra": "no-unknown-export-fields"}, {"privateKey": "not a PEM"},
    {"name": None}, {"privateKey": 123},
])
def test_invalid_credential_format_cannot_reach_transport(key_file, change):
    record = json.loads(key_file.read_text())
    record.update(change)
    key_file.write_text(json.dumps(record))
    transport = FakeTransport()
    with pytest.raises(PreflightError) as error:
        collect(key_file, transport)
    assert "PRIVATE KEY" not in str(error.value) and transport.calls == []


@pytest.mark.parametrize("curve", [ec.SECP384R1(), ec.SECP256K1(), None])
def test_non_p256_credentials_refused(key_file, curve):
    key = ed25519.Ed25519PrivateKey.generate() if curve is None else ec.generate_private_key(curve)
    key_file.write_text(json.dumps({"name": KEY_NAME, "privateKey": pem_for(key)}))
    with pytest.raises(PreflightError, match="coinbase_es256_private_key_required"):
        collect(key_file)


def test_credentials_and_http_response_repr_hide_values(key_file):
    credentials = module._credentials(key_file)
    assert repr(credentials) == "_Credentials()"
    assert "SECRET" not in repr(HTTPResponse(200, b"SECRET"))


def test_permission_discovery_is_exactly_one_read_and_does_not_claim_binding(key_file):
    transport = FakeTransport([permissions()])
    report = module.collect_permissions(key_file, transport=transport, clock=lambda: 100)
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == module.ORIGIN + module.PERMISSIONS_PATH
    assert report["portfolio_uuid"] == PORTFOLIO
    assert report["permissions"]["can_trade"] is False
    assert report["expected_portfolio_binding_verified"] is False
    assert report["account_balances_read"] is False and report["execution_enabled"] is False
    assert report["key_permission_read_verified"] is False
    assert KEY_NAME not in json.dumps(report)


@pytest.mark.parametrize("changes", [{"can_trade": True}, {"can_transfer": True}, {"can_view": False}])
def test_discovery_also_refuses_broad_keys(key_file, changes):
    transport = FakeTransport([permissions(**changes)])
    with pytest.raises(PreflightError, match="coinbase_view_only_key_required"):
        module.collect_permissions(key_file, transport=transport)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("bad_url", [
    module.ORIGIN + module.PREFIX + "/orders", module.ORIGIN + "/api/v3/brokerage/accounts?limit=251",
    module.ORIGIN + module.ACCOUNTS_PATH + "?limit=250&cursor=x&cursor=y",
    module.ORIGIN + module.ACCOUNTS_PATH + "?cursor=x&limit=250", module.ORIGIN + module.ACCOUNTS_PATH,
    module.ORIGIN + module.ACCOUNTS_PATH + "?limit=250&cursor=",
    module.ORIGIN + module.PERMISSIONS_PATH + "#fragment", "https://evil.test" + module.PERMISSIONS_PATH,
])
def test_owned_transport_allowlist_rejects_before_network(bad_url):
    with pytest.raises(PreflightError, match="coinbase_read_endpoint_required"):
        module._safe_get(bad_url, {}, timeout_seconds=10, max_response_bytes=1000)


def test_complete_pagination_preserves_different_currency_balances_and_hides_cursor(key_file):
    cursor = "PRIVATE/CURSOR+DATA=="
    btc = account(uuid=OTHER_ACCOUNT, currency="BTC", available_balance={"value": "0.2", "currency": "BTC"},
                  hold={"value": "0", "currency": "BTC"})
    transport = FakeTransport([permissions(), page(has_next=True, cursor=cursor), page([btc]), fees(), product()])
    report = collect(key_file, transport)
    assert report["account_count"] == 2
    assert report["accounts"][1]["available_balance"] == "0.2"
    assert "cursor=PRIVATE%2FCURSOR%2BDATA%3D%3D" in transport.calls[2][0]
    assert cursor not in json.dumps(report)


def test_duplicate_accounts_refuse_partial_success(key_file):
    transport = FakeTransport([permissions(), page(has_next=True, cursor="next"), page()])
    with pytest.raises(PreflightError, match="coinbase_duplicate_account"):
        collect(key_file, transport)
    assert len(transport.calls) == 3


def test_pagination_cycle_refused(key_file):
    transport = FakeTransport([permissions(), page(has_next=True, cursor="next"),
                               page([account(uuid=OTHER_ACCOUNT)], has_next=True, cursor="next")])
    with pytest.raises(PreflightError, match="coinbase_pagination_cycle"):
        collect(key_file, transport)


def test_max_page_limit_refuses_partial_collection(key_file, monkeypatch):
    monkeypatch.setattr(module, "MAX_PAGES", 1)
    with pytest.raises(PreflightError, match="coinbase_account_listing_incomplete"):
        collect(key_file, FakeTransport([permissions(), page(has_next=True, cursor="next")]))


@pytest.mark.parametrize("bad_page", [
    page(size=5), page(has_next="false"), page(size=True), page(accounts=[], has_next=True, cursor="next"),
    page(has_next=True, cursor=None), page(has_next=True, cursor="x\n"), {"accounts": []},
])
def test_missing_or_invalid_page_metadata_fails_closed(key_file, bad_page):
    with pytest.raises(PreflightError):
        collect(key_file, FakeTransport([permissions(), bad_page]))


def test_optional_page_size_can_be_absent_when_completion_fields_are_valid(key_file):
    first = page()
    del first["size"]
    assert collect(key_file, FakeTransport([permissions(), first, fees(), product()]))["accounts_complete"] is True


@pytest.mark.parametrize("changes", [
    {"retail_portfolio_id": OTHER_PORTFOLIO}, {"retail_portfolio_id": None}, {"uuid": "bad"},
    {"currency": "usd"}, {"available_balance": {"value": "1", "currency": "BTC"}},
    {"hold": None}, {"active": "true"}, {"ready": None},
])
def test_account_contract_refuses_unknown_or_mismatched_data(key_file, changes):
    with pytest.raises(PreflightError):
        collect(key_file, FakeTransport([permissions(), page([account(**changes)])]))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1e5", " 1", "1\n", "01", "1" * 31, 1, True, None])
def test_numeric_fields_not_coerced_or_truncated(key_file, value):
    row = account(hold={"value": value, "currency": "USD"})
    with pytest.raises(PreflightError, match="coinbase_financial_field_invalid"):
        collect(key_file, FakeTransport([permissions(), page([row])]))


def test_empty_account_list_is_observed_empty_without_zero_equity_claim(key_file):
    report = collect(key_file, FakeTransport([permissions(), page([]), fees(), product()]))
    assert report["account_count"] == 0 and report["accounts"] == [] and report["equity_usd"] is None


@pytest.mark.parametrize("tier", [None, {}, {"maker_fee_rate": "1", "taker_fee_rate": "0.01"},
                                    {"maker_fee_rate": None, "taker_fee_rate": "0.01"}])
def test_fee_tier_missing_or_invalid_never_becomes_zero(key_file, tier):
    with pytest.raises(PreflightError):
        collect(key_file, FakeTransport([permissions(), page(), fees(fee_tier=tier)]))


def test_fee_surcharges_missing_are_unknown_and_gst_not_applied_as_guessed_units(key_file):
    report = collect(key_file)
    assert report["fees"]["has_cost_plus_commission"] is None
    assert report["fees"]["goods_and_services_tax"] is None
    assert report["fees"]["all_in_fee_rate_verified"] is False
    payload = fees(has_cost_plus_commission=True, goods_and_services_tax={"rate": "18", "type": "INCLUSIVE"})
    report = collect(key_file, FakeTransport([permissions(), page(), payload, product()]))
    assert report["fees"]["goods_and_services_tax"] == {"rate_reported": "18", "type": "INCLUSIVE", "rate_units_verified": False}
    assert report["fees"]["maker_fee_rate"] == "0.006"
    assert report["fees"]["all_in_fee_rate_verified"] is False


@pytest.mark.parametrize("changes", [
    {"has_cost_plus_commission": "false"}, {"goods_and_services_tax": "unknown"},
    {"goods_and_services_tax": {"rate": "NaN", "type": "INCLUSIVE"}},
])
def test_malformed_surcharge_fields_refused(key_file, changes):
    with pytest.raises(PreflightError):
        collect(key_file, FakeTransport([permissions(), page(), fees(**changes)]))


def test_price_increment_distinct_from_quote_increment(key_file):
    report = collect(key_file, FakeTransport([permissions(), page(), fees(), product(price_increment="0.001")]))
    assert report["product"]["price_increment"] == "0.001"
    assert report["product"]["quote_increment"] == "0.01"


@pytest.mark.parametrize("flag", ["view_only", "is_disabled", "trading_disabled", "cancel_only", "auction_mode"])
def test_product_restrictions_remain_visible_and_block_product_flag_claim(key_file, flag):
    report = collect(key_file, FakeTransport([permissions(), page(), fees(), product(**{flag: True})]))
    assert report["product"][flag] is True
    assert report["product"]["ordinary_limit_buy_product_flags_allow"] is False


@pytest.mark.parametrize("change", [
    {"view_only": None}, {"view_only": "false"}, {"view_only": 0}, {"product_id": "ETH-USD"},
    {"product_type": "FUTURE"}, {"base_currency_id": "XBT"}, {"quote_currency_id": "USDC"},
    {"base_increment": "0"}, {"quote_max_size": "0.5"}, {"base_min_size": "1001"},
])
def test_unknown_or_inconsistent_product_contract_never_assumed_tradable(key_file, change):
    with pytest.raises(PreflightError):
        collect(key_file, FakeTransport([permissions(), page(), fees(), product(**change)]))


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 429, 500])
def test_bad_http_status_stops_without_retry_or_body_leak(key_file, status):
    transport = FakeTransport([HTTPResponse(status, b"PRIVATE PROVIDER BODY")])
    with pytest.raises(PreflightError) as error:
        collect(key_file, transport)
    assert len(transport.calls) == 1 and "PRIVATE" not in str(error.value)
    assert str(error.value) == ("coinbase_redirect_refused" if 300 <= status < 400 else f"coinbase_http_{status}")


@pytest.mark.parametrize("raw", [b"", b"not json", b'{"can_view":true,"can_view":false}', b'{"x":NaN}', b"\xff"])
def test_invalid_json_refuses_without_payload_leak(key_file, raw):
    with pytest.raises(PreflightError):
        collect(key_file, FakeTransport([HTTPResponse(200, raw)]))


def test_transport_exception_is_sanitized(key_file):
    with pytest.raises(PreflightError, match="^coinbase_transport_failed$"):
        collect(key_file, FakeTransport([ValueError("PRIVATE KEY PROVIDER MESSAGE")]))


@pytest.mark.parametrize("times", [[100, 99], [100, 100, 99], [100, 100, 221], [100, float("nan")]])
def test_regressing_nonfinite_or_stale_observations_refused(key_file, times):
    times = iter(times)
    with pytest.raises(PreflightError):
        collect_preflight(key_file, expected_portfolio=PORTFOLIO, transport=FakeTransport(), clock=lambda: next(times))


def script_module():
    path = Path(__file__).resolve().parents[1] / "scripts/coinbase_preflight.py"
    spec = importlib.util.spec_from_file_location("coinbase_preflight_cli_test", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_cli_report_new_external_private_and_does_not_overwrite(key_file, tmp_path, monkeypatch, capsys):
    cli = script_module()
    calls = []
    def fake(key_file, *, expected_portfolio):
        calls.append(expected_portfolio)
        return {"execution_enabled": False, "accounts": []}
    monkeypatch.setattr(cli, "collect_preflight", fake)
    output = tmp_path / "report.json"
    argv = ["--key-file", str(key_file), "--expected-portfolio", PORTFOLIO, "--output", str(output)]
    assert cli.main(argv) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    assert cli.main(argv) == 2
    assert output.read_bytes() == original and calls == [PORTFOLIO]
    assert "coinbase_report_destination_must_be_new" in capsys.readouterr().err


def test_cli_sanitizes_unknown_errors_and_writes_no_report(key_file, tmp_path, monkeypatch, capsys):
    cli = script_module()
    def fail(*args, **kwargs):
        raise ValueError("SECRET SHOULD NOT BE PRINTED")
    monkeypatch.setattr(cli, "collect_preflight", fail)
    output = tmp_path / "report.json"
    assert cli.main(["--key-file", str(key_file), "--expected-portfolio", PORTFOLIO, "--output", str(output)]) == 2
    assert not output.exists()
    assert "SECRET" not in capsys.readouterr().err


def test_cli_permission_discovery_never_dispatches_account_reads(key_file, tmp_path, monkeypatch):
    cli = script_module()
    def forbidden(*args, **kwargs):
        pytest.fail("discovery may not read balances")
    monkeypatch.setattr(cli, "collect_preflight", forbidden)
    monkeypatch.setattr(cli, "collect_permissions", lambda key: {"portfolio_uuid": PORTFOLIO})
    output = tmp_path / "permissions.json"
    assert cli.main(["--key-file", str(key_file), "--permissions-only", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["portfolio_uuid"] == PORTFOLIO
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("selection", [[], ["--permissions-only", "--expected-portfolio", PORTFOLIO]])
def test_cli_requires_exactly_one_discovery_or_bound_portfolio_selection(key_file, tmp_path, selection):
    cli = script_module()
    with pytest.raises(SystemExit) as error:
        cli.main(["--key-file", str(key_file), "--output", str(tmp_path / "report.json"), *selection])
    assert error.value.code == 2


def test_tls_transport_fixed_get_validates_certificate_and_closes(monkeypatch):
    calls = []
    class Response:
        status = 200
        sent = False
        def getheader(self, name):
            return "2"
        def read1(self, size):
            if self.sent:
                return b""
            self.sent = True
            return b"{}"
    class Connection:
        sock = None
        def __init__(self, host, *, timeout, context):
            assert host == module.HOST and timeout == 10
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
        def request(self, method, path, *, headers):
            calls.append((method, path))
        def getresponse(self):
            return Response()
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    response = module._safe_get(module.ORIGIN + module.PERMISSIONS_PATH, {},
                                timeout_seconds=10, max_response_bytes=100)
    assert response == HTTPResponse(200, b"{}")
    assert calls == [("GET", module.PERMISSIONS_PATH), "closed"]
