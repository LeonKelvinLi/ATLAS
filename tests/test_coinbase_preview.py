"""Cost, precision, cap and CLI checks using synthetic Coinbase snapshots."""

from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, localcontext
import importlib.util
import json
from pathlib import Path
import socket

import pytest

from atlas.brokers.coinbase_public import CoinbasePublicError, FLAGS
from atlas.coinbase_preview import build_preview, validate_config

NOW = 1789078453


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Coinbase preview tests cannot contact a network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def config(**changes):
    return {"schema": 1, "product_id": "BTC-USD", "allocation_usd": "100", "order_budget_usd": "100",
            "maker_fee_bps": "60", "taker_fee_bps": "120", **changes}


def snapshot():
    return {
        "product": {"product_id": "BTC-USD", "product_type": "SPOT", "base_currency_id": "BTC", "quote_currency_id": "USD",
                    "status": "online", "base_increment": "0.00000001", "quote_increment": "0.01", "price_increment": "0.01",
                    "base_min_size": "0.00000001", "base_max_size": "3400", "quote_min_size": "1", "quote_max_size": "150000000",
                    **{name: False for name in FLAGS}},
        "pricebook": {"product_id": "BTC-USD", "time": "2026-09-10T22:14:12.794990Z",
                      "bids": [{"price": "77075.04", "size": "0.01797682"}],
                      "asks": [{"price": "77075.07", "size": "0.10379491"}]},
        "observed_at": str(NOW), "product_observed_at": str(NOW),
        "transport_provenance": "injected_test_transport", "public_only": True,
        "account_permissions_verified": False, "source": "https://api.coinbase.com",
    }


def preview(data=None, settings=None):
    return build_preview(data or snapshot(), settings or config(), now=NOW)


def test_fee_inclusive_cap_exact_grids_and_no_execution_claims():
    result = preview()
    assert not result["execution_enabled"] and not result["account_connected"]
    assert result["orders_submitted"] == result["account_mutations"] == 0
    assert result["actual_account_fee_tier"] is None and result["comparison"]["maker_fill_probability_unknown"]
    assert result["comparison"]["candidates_are_alternatives_not_combined_allocations"]
    for key in ("maker_candidate", "taker_candidate"):
        candidate = result[key]
        qty = Decimal(candidate["quantity_btc"])
        fee = Decimal(candidate["entry_fee_reserve_usd"])
        limit = Decimal(candidate.get("limit_price_usd", candidate.get("marketable_limit_price_usd")))
        all_in = Decimal(candidate["entry_all_in_usd"])
        reserved = Decimal(candidate["limit_worst_case_all_in_usd"])
        assert qty % Decimal("0.00000001") == 0 and limit % Decimal("0.01") == 0
        assert fee % Decimal("0.01") == 0
        assert 0 < all_in <= reserved <= 100
        assert qty * limit + Decimal(candidate["limit_worst_case_fee_reserve_usd"]) == reserved
        assert reserved + Decimal(candidate["unused_simulated_allocation_usd"]) == 100
        assert not candidate["fill_verified"] and not candidate["order_submitted"]
        exit_price = Decimal(candidate["conservative_exit_break_even_price_usd"])
        exit_notional = qty * exit_price
        # A fee rounded upward by less than one quote increment cannot defeat
        # the explicitly conservative break-even threshold.
        assert exit_notional * Decimal("0.988") - Decimal("0.01") >= reserved
    assert Decimal(result["comparison"]["maker_cost_advantage_bps"]) > 0


@pytest.mark.parametrize("changes", [
    {"allocation_usd": "100.01"}, {"allocation_usd": "99"}, {"order_budget_usd": "100.000000001"},
    {"order_budget_usd": "0"}, {"order_budget_usd": 2}, {"order_budget_usd": "NaN"},
    {"maker_fee_bps": None}, {"taker_fee_bps": None}, {"maker_fee_bps": 60}, {"taker_fee_bps": "-1"},
    {"taker_fee_bps": "1000.01"}, {"maker_fee_bps": "Infinity"}, {"schema": True},
    {"product_id": "ETH-USD"}, {"key_file": "/private/key"}, {"execution_enabled": True},
])
def test_config_requires_explicit_fee_scenarios_and_fixed_100_allocation(changes):
    with pytest.raises(CoinbasePublicError):
        validate_config(config(**changes))


@pytest.mark.parametrize("budget", ["0.01", "0.99"])
def test_below_minimum_budget_is_unavailable_not_silently_raised(budget):
    result = preview(settings=config(order_budget_usd=budget))
    assert not result["maker_candidate"]["available"] and not result["taker_candidate"]["available"]
    assert result["comparison"] is None


@pytest.mark.parametrize("budget", ["1.02", "2", "10", "100"])
@pytest.mark.parametrize("fees", [("0", "0"), ("60", "120"), ("0.123", "1.234"), ("1000", "1000")])
def test_budget_is_respected_across_fee_tiers_and_small_orders(budget, fees):
    result = preview(settings=config(order_budget_usd=budget, maker_fee_bps=fees[0], taker_fee_bps=fees[1]))
    for key in ("maker_candidate", "taker_candidate"):
        candidate = result[key]
        if candidate["available"]:
            assert Decimal(candidate["limit_worst_case_all_in_usd"]) <= Decimal(budget)
            assert Decimal(candidate["entry_notional_usd"]) >= 1


def test_depth_cost_and_marketable_limit_worst_case_are_separate_and_bounded():
    data = snapshot()
    data["product"].update(base_increment="0.01", base_min_size="0.01", base_max_size="10")
    data["pricebook"].update(bids=[{"price": "99.99", "size": "1"}],
                             asks=[{"price": "100", "size": "0.2"}, {"price": "200", "size": "1"}])
    result = preview(data, config(maker_fee_bps="0", taker_fee_bps="0"))
    taker = result["taker_candidate"]
    assert taker["quantity_btc"] == "0.50"
    assert Decimal(taker["entry_notional_usd"]) == 80
    assert Decimal(taker["entry_all_in_usd"]) == 80
    assert Decimal(taker["marketable_limit_price_usd"]) == 200
    assert Decimal(taker["limit_worst_case_all_in_usd"]) == 100
    assert Decimal(taker["conservative_exit_break_even_price_usd"]) == 200
    assert len(taker["depth_levels_used"]) == 2


def test_taker_uses_only_available_depth_and_does_not_invent_liquidity():
    data = snapshot()
    data["pricebook"]["asks"][0]["size"] = "0.00002"
    result = preview(data)
    assert Decimal(result["taker_candidate"]["quantity_btc"]) == Decimal("0.00002")
    assert Decimal(result["taker_candidate"]["unused_order_budget_usd"]) > 98
    assert result["taker_candidate"]["fill_probability"] is None


def test_post_only_blocks_taker_and_limit_only_preserves_limit_scenarios():
    data = snapshot()
    data["product"]["post_only"] = True
    result = preview(data)
    assert result["maker_candidate"]["available"]
    assert result["taker_candidate"] == {"available": False, "reason": "product_post_only"}
    data["product"]["post_only"] = False
    data["product"]["limit_only"] = True
    assert preview(data)["taker_candidate"]["available"]


def test_stale_product_refuses_even_with_fresh_book():
    data = snapshot()
    data["product_observed_at"] = str(NOW - 11)
    with pytest.raises(CoinbasePublicError, match="snapshot_stale"):
        preview(data)


@pytest.mark.parametrize("change", [
    {"public_only": False}, {"observed_at": str(NOW + 1)}, {"product_observed_at": str(NOW + 1)},
    {"observed_at": None}, {"transport_provenance": "unverified"},
])
def test_unknown_or_future_snapshots_refuse(change):
    data = snapshot()
    data.update(change)
    with pytest.raises(CoinbasePublicError):
        preview(data)


@pytest.mark.parametrize("side,field,value", [("bids", "price", "77075.041"), ("asks", "size", "0.103794911")])
def test_book_off_current_product_grid_refuses(side, field, value):
    data = snapshot()
    data["pricebook"][side][0][field] = value
    with pytest.raises(CoinbasePublicError, match="increment_invalid"):
        preview(data)


def test_spread_filter_refuses_expensive_market():
    data = snapshot()
    data["pricebook"]["bids"][0]["price"] = "76000"
    with pytest.raises(CoinbasePublicError, match="spread_exceeds"):
        preview(data)


def test_product_maximum_order_size_limits_candidate():
    data = snapshot()
    data["product"].update(base_max_size="0.00003", quote_max_size="2")
    result = preview(data)
    for key in ("maker_candidate", "taker_candidate"):
        candidate = result[key]
        assert Decimal(candidate["quantity_btc"]) <= Decimal("0.00003")
        assert Decimal(candidate["limit_worst_case_notional_usd"]) <= 2


def test_ambient_decimal_context_cannot_change_output():
    expected = preview()
    with localcontext() as context:
        context.prec = 4
        context.rounding = ROUND_DOWN
        actual = preview()
    assert actual == expected


def test_snapshot_and_config_inputs_are_not_mutated():
    data, settings = snapshot(), config()
    before = deepcopy((data, settings))
    preview(data, settings)
    assert (data, settings) == before


def cli_module():
    source = Path(__file__).resolve().parents[1] / "scripts" / "coinbase_preview.py"
    spec = importlib.util.spec_from_file_location("coinbase_preview_cli_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_requires_external_explicit_config_and_writes_once_owner_only(tmp_path, monkeypatch, capsys):
    cli = cli_module()
    cfg, output = tmp_path / "config.json", tmp_path / "report.json"
    cfg.write_text(json.dumps(config()))
    calls = []
    class Client:
        def snapshot(self):
            calls.append("public-read")
            return snapshot()
    monkeypatch.setattr(cli, "CoinbasePublicClient", Client)
    monkeypatch.setattr(cli, "build_preview", lambda data, settings: build_preview(data, settings, now=NOW))
    assert cli.main(["--config", str(cfg), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["orders_submitted"] == 0 and report["account_connected"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    assert calls == ["public-read"]
    original = output.read_bytes()
    assert cli.main(["--config", str(cfg), "--output", str(output)]) == 2
    assert output.read_bytes() == original and calls == ["public-read"]
    assert "destination_must_be_new" in capsys.readouterr().err


@pytest.mark.parametrize("invalid", [None, {"schema": 1}, config(maker_fee_bps=None), config(api_key="PRIVATE")])
def test_cli_bad_config_has_no_network_or_report(tmp_path, monkeypatch, invalid, capsys):
    cli = cli_module()
    cfg, output = tmp_path / "config.json", tmp_path / "report.json"
    cfg.write_text(json.dumps(invalid))
    monkeypatch.setattr(cli, "CoinbasePublicClient", lambda: pytest.fail("invalid config cannot access network"))
    assert cli.main(["--config", str(cfg), "--output", str(output)]) == 2
    assert not output.exists() and "PRIVATE" not in capsys.readouterr().err


def test_cli_refuses_symlink_output_before_network(tmp_path, monkeypatch):
    cli = cli_module()
    cfg, output = tmp_path / "config.json", tmp_path / "report.json"
    cfg.write_text(json.dumps(config()))
    output.symlink_to(tmp_path / "new-target.json")
    monkeypatch.setattr(cli, "CoinbasePublicClient", lambda: pytest.fail("symlink destination cannot access network"))
    assert cli.main(["--config", str(cfg), "--output", str(output)]) == 2
    assert not output.exists()
