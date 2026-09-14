"""Offline evidence for the data-only process graph and bounded HTTP failures."""

from datetime import datetime, timezone
from decimal import Decimal
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from atlas.collection import primitives
from atlas.collection.primitives import CollectionInputError, HTTPResponse
from atlas.research import data_collector as collector
from atlas.research.data_contract_v1 import default_contract


ROOT = Path(__file__).resolve().parents[1]
NOW = 1789480210
END = NOW // 300 * 300
START = END - 600


def test_collector_and_archive_load_without_site_packages_or_execution_modules():
    # -I ignores environment and -S removes site-packages: this succeeds on the
    # same standard-library-only interpreter used by the deployment package.
    script = '''
import importlib.abc, json, sys
sys.path.insert(0, sys.argv[1])
allowed = {"atlas", "atlas.collection", "atlas.collection.primitives", "atlas.research",
           "atlas.research.data_contract_v1", "atlas.research.data_collector", "atlas.research.data_archive"}
class RejectOtherAtlas(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("atlas.") and fullname not in allowed:
            raise AssertionError("collection imported forbidden module: " + fullname)
sys.meta_path.insert(0, RejectOtherAtlas())
import atlas.research.data_collector, atlas.research.data_archive
foreign = sorted({name.split(".")[0] for name in sys.modules}
                 - set(sys.stdlib_module_names) - {"atlas", "__main__"})
foreign = [name for name in foreign if not name.startswith("_sysconfigdata_")]
assert not foreign, foreign
print(json.dumps(sorted(name for name in sys.modules if name.startswith("atlas"))))
'''
    completed = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(ROOT)],
                               capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert "atlas.research.data_collector" in loaded
    assert "atlas.research.data_archive" in loaded
    assert not any(name.startswith(("atlas.risk", "atlas.config", "atlas.brokers", "atlas.ledger")) for name in loaded)


def test_qualification_constants_remain_single_shared_unchanged_mapping():
    # Verify the research module binds this exact shared mapping without loading
    # its scientific dependencies into the minimal collection test environment.
    tree = ast.parse((ROOT / "atlas/research/multiasset_validation.py").read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and node.module == "atlas.collection.primitives"]
    assert any(alias.name == "DEFAULT_THRESHOLDS" and alias.asname is None
               for node in imports for alias in node.names)
    assert not any(isinstance(node, ast.Name) and node.id == "DEFAULT_THRESHOLDS"
                   and isinstance(node.ctx, ast.Store) for node in ast.walk(tree))
    assert primitives.DEFAULT_THRESHOLDS == {
        "min_completed_round_trips": 30, "min_daily_observations": 60,
        "max_drawdown": .10, "min_sharpe": 1.2, "min_profit_factor": 1.25,
        "min_net_return": 0.0,
    }


@pytest.mark.parametrize("value,expected", [
    ("0", 0), ("30", 30), ("86401", 86400), ("99999999999999999", 86400),
    ("Tue, 15 Sep 2026 13:50:40 GMT", 1230),
    ("Tue, 15 Sep 2026 13:30:00 GMT", 0),
    ("Tue, 15 Sep 2026 13:50:40", None),
    ("", None), (None, None), (12, None), ("secret token", None),
    ("1.5", None), ("-1", None), ("１２", None), ("x" * 129, None),
])
def test_retry_after_delta_or_http_date_is_bounded_and_never_raw(value, expected):
    now = int(datetime(2026, 9, 15, 13, 30, 10, tzinfo=timezone.utc).timestamp())
    assert primitives.retry_after_seconds(value, now=now) == expected


@pytest.mark.parametrize("status,header,expected", [(429, "17", 17), (503, "900000", 86400),
                                                    (401, "secret", None), (302, None, None)])
def test_real_transport_errors_preserve_safe_status_without_body_or_credentials(monkeypatch, status, header, expected):
    calls = []
    class Response:
        def __init__(self):
            self.status = status
        def getheader(self, name):
            assert name == "Retry-After"
            return header
        def read1(self, count):
            pytest.fail("error body must not be read or logged")
    class Connection:
        sock = None
        def __init__(self, host, **kwargs):
            assert host == "api.exchange.coinbase.com"
            assert kwargs["context"].check_hostname
        def request(self, method, target, headers):
            calls.append((method, target))
        def getresponse(self):
            return Response()
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(collector.http.client, "HTTPSConnection", Connection)
    with pytest.raises(collector.DataCollectorError) as caught:
        collector._exchange_get(collector.BOOK_URL, {}, timeout_seconds=10, max_response_bytes=10)
    error = caught.value
    assert error.status == status and error.retry_after_seconds == expected
    assert str(error) == "collector_http_refused" and "secret" not in repr(error)
    assert calls == [("GET", "/products/BTC-USD/book?level=1"), "closed"]


def test_rate_limit_metadata_survives_collect_once_and_retains_prior_capture():
    contract = default_contract("retry-test", NOW - 180 * 86400, NOW)
    responses = iter([HTTPResponse(200, b"[]"), HTTPResponse(429, b"secret", {"Retry-After": "31"})])
    with pytest.raises(collector.DataCollectorError) as caught:
        collector.collect_once(contract, provider="coinbase", start=START, end=END,
            transport=lambda *args, **kwargs: next(responses), clock=lambda: NOW)
    error = caught.value
    assert error.status == 429 and error.retry_after_seconds == 31
    assert str(error) == "collector_http_response_invalid"
    assert len(error.captures) == 1 and error.captures[0]["body"] == b"[]"


def test_injected_error_code_cannot_leak_but_valid_rate_limit_metadata_survives():
    contract = default_contract("retry-test", NOW - 180 * 86400, NOW)
    def transport(*args, **kwargs):
        raise collector.DataCollectorError("secret credential", status=429, retry_after_seconds=8)
    with pytest.raises(collector.DataCollectorError) as caught:
        collector.collect_once(contract, provider="coinbase", start=START, end=END,
                               transport=transport, clock=lambda: NOW)
    assert str(caught.value) == "collector_request_or_response_invalid"
    assert caught.value.status == 429 and caught.value.retry_after_seconds == 8


@pytest.mark.parametrize("url", [
    "https://api.alpaca.markets/v2/orders",
    "https://paper-api.alpaca.markets/v2/orders",
    "https://paper-api.alpaca.markets/v2/account",
    "https://paper-api.alpaca.markets/v2/positions",
    "https://paper-api.alpaca.markets/v2/assets/SPY",
    "https://data.alpaca.markets/v2/stocks/bars?symbols=AAPL",
    "https://api.exchange.coinbase.com/orders",
])
def test_alpaca_transport_refuses_every_account_or_order_path_before_connect(monkeypatch, url):
    def refuse(*args, **kwargs):
        pytest.fail("non-data endpoint must be refused before network")
    monkeypatch.setattr(collector.http.client, "HTTPSConnection", refuse)
    with pytest.raises(ValueError, match="endpoint_refused"):
        collector.alpaca_get(url, {}, timeout_seconds=10, max_response_bytes=10)


def test_secure_credentials_need_explicit_owner_only_external_file(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text('{"api_key":"example_key","api_secret":"example_secret"}')
    path.chmod(0o600)
    value = primitives._credentials(path)
    assert value.api_key == "example_key"
    assert "example" not in repr(value)
    path.chmod(0o640)
    with pytest.raises(CollectionInputError):
        primitives._credentials(path)
    path.chmod(0o600)
    linked = tmp_path / "symlink.json"
    linked.symlink_to(path)
    with pytest.raises(CollectionInputError, match="symlink"):
        primitives._credentials(linked)
    hardlink = tmp_path / "hardlink.json"
    os.link(path, hardlink)
    with pytest.raises(CollectionInputError, match="owner_mode_or_type"):
        primitives._credentials(path)


def test_strict_decimal_json_and_external_path_reject_unsafe_inputs():
    assert primitives._json(b'{"price":1.12345678901234567890}', exact_numbers=True)["price"] == Decimal("1.12345678901234567890")
    for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b"\xff"):
        with pytest.raises(CollectionInputError):
            primitives._json(raw)
    for path in ("relative/path", ROOT / "credentials.json"):
        with pytest.raises(CollectionInputError):
            primitives._external_path(path)
