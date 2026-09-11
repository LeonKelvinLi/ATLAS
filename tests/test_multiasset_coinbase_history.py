"""Public history collector tests use synthetic responses and block sockets."""

import json
import socket
from urllib.parse import parse_qs, urlsplit

import pytest

import atlas.research.coinbase_history as history
from atlas.research.market import DatasetError, validate_dataset


START = 20_000 * 86400


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("History tests cannot contact a network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def rows(start, end):
    return [[t, 99.0, 101.0, 100.0, 100.5, 2.25] for t in reversed(range(start, end, 300))]


class Transport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
    def __call__(self, url):
        self.calls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(start, end, *, remove=None):
    payload = rows(start, end)
    if remove is not None:
        del payload[remove]
    return history.PublicResponse(200, json.dumps(payload).encode())


def test_two_sequential_pages_keep_raw_receipts_unknown_units_and_capture_availability(tmp_path):
    stop = START + 2 * 86400
    source = [response(START, START + 86400), response(START + 86400, stop)]
    transport = Transport(source)
    result = history.fetch_history(start=START, end=stop, output_dir=tmp_path / "capture",
                                   transport=transport, clock=lambda: stop + 10)
    assert result["complete"] and result["requests_performed"] == 2
    assert result["bar_count"] == 576 and result["missing_bars"] == 0
    assert result["volume_unit"] == "unknown" and not result["volume_strategies_available"]
    assert result["evidence_class"] == "TEST" and not result["alpha_validation_eligible"]
    data = json.loads((tmp_path / "capture" / "dataset.json").read_text())
    checked = validate_dataset(data, now=stop + 10)
    assert checked["dataset_hash"] == result["dataset_hash"]
    assert len({b["event_time"] for b in data["bars"]}) == 576
    assert all(b["information_available_time"] == stop + 10 == b["ingestion_time"] for b in data["bars"])
    assert (tmp_path / "capture" / "page-01-raw.json").read_bytes() == source[0].body
    receipt = json.loads((tmp_path / "capture" / "page-01-http.json").read_text())
    assert receipt["method"] == "GET" and receipt["http_status"] == 200
    assert "Authorization" not in str(receipt)
    assert (tmp_path / "capture").stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path / "capture").iterdir())


def test_boundary_overlap_is_excluded_and_reported_without_canonical_duplicates(tmp_path):
    stop = START + 86400
    raw = json.dumps(rows(START, stop + 300)).encode()
    transport = Transport([history.PublicResponse(200, raw)])
    result = history.fetch_history(start=START, end=stop, output_dir=tmp_path / "capture",
                                   transport=transport, clock=lambda: stop + 1000)
    assert result["bar_count"] == 288
    receipt = json.loads((tmp_path / "capture" / "page-01-import.json").read_text())
    assert receipt["excluded_rows"][0]["reasons"] == ["closes_after_requested_end"]


def test_missing_bucket_is_not_filled_or_silently_complete_coverage(tmp_path):
    stop = START + 86400
    result = history.fetch_history(start=START, end=stop, output_dir=tmp_path / "capture",
        transport=Transport([response(START, stop, remove=20)]), clock=lambda: stop + 10)
    assert result["bar_count"] == 287 and result["missing_bars"] == 1
    quality = json.loads((tmp_path / "capture" / "quality.json").read_text())
    assert "missing_expected_bars" in quality["blockers"]
    assert "volume_unit_unknown_no_fill_capacity_assumption" in quality["blockers"]


@pytest.mark.parametrize("failure", [history.PublicResponse(429, b'{"message":"slow down"}'),
                                     history.PublicResponse(500, b"failure"), TimeoutError("network interrupted")])
def test_failure_never_retries_and_keeps_failure_receipt(tmp_path, failure):
    stop = START + 2 * 86400
    transport = Transport([failure, response(START, START + 86400)])
    with pytest.raises(Exception):
        history.fetch_history(start=START, end=stop, output_dir=tmp_path / "capture",
                               transport=transport, clock=lambda: stop + 10)
    assert len(transport.calls) == 1
    receipt = json.loads((tmp_path / "capture" / "failure.json").read_text())
    assert not receipt["complete"] and not receipt["automatic_retry"]
    assert not (tmp_path / "capture" / "manifest.json").exists()


def test_existing_capture_cannot_be_overwritten_or_trigger_a_request(tmp_path):
    destination = tmp_path / "capture"
    destination.mkdir()
    transport = Transport([])
    with pytest.raises(DatasetError, match="new external"):
        history.fetch_history(start=START, end=START + 86400, output_dir=destination,
                               transport=transport, clock=lambda: START + 90000)
    assert transport.calls == []


@pytest.mark.parametrize("start,end", [(START + 1, START + 300), (START, START),
                                      (START, START + 36 * 86400), (True, START)])
def test_invalid_or_excess_request_ranges_refuse_before_creating_state(tmp_path, start, end):
    with pytest.raises(ValueError):
        history.fetch_history(start=start, end=end, output_dir=tmp_path / "capture", transport=Transport([]),
                               clock=lambda: START + 40 * 86400)
    assert not (tmp_path / "capture").exists()


@pytest.mark.parametrize("url", [
    "https://api.coinbase.com/products/BTC-USD/candles?granularity=300",
    "https://api.exchange.coinbase.com/accounts",
    "http://api.exchange.coinbase.com/products/BTC-USD/candles",
    "https://api.exchange.coinbase.com/products/ETH-USD/candles?granularity=300",
])
def test_only_exact_public_candle_endpoint_can_reach_http(url):
    with pytest.raises(ValueError):
        history.public_get(url)


def test_http_uses_one_get_no_auth_and_never_follows_redirect(monkeypatch):
    calls = []
    class Response:
        status = 302
        def read1(self, _limit):
            return b""
    class Connection:
        def __init__(self, host, **kwargs):
            calls.append((host, kwargs))
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
        def getresponse(self):
            return Response()
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(history.http.client, "HTTPSConnection", Connection)
    result = history.public_get(history.request_url(START, START + 86400))
    assert result.status == 302
    assert calls[0][0] == "api.exchange.coinbase.com"
    assert calls[1][0][0] == "GET"
    assert calls[1][1]["headers"] == {"Accept": "application/json", "User-Agent": "ATLAS-public-history/1"}
    assert calls[-1] == "closed" and len(calls) == 3
