"""Synthetic HTTP captures only. No provider, secret store, orders or services."""

from dataclasses import replace
import copy
import json
import os
import socket
from urllib.parse import parse_qs, urlsplit

import pytest

from atlas.alpaca_paper_preflight import HTTPResponse
from atlas.research.data_adapters import (
    AlpacaHistoricalAdapter, DataAdapterError, DatabentoHistoricalAdapter,
    HistoricalBarsRequest, MassiveHistoricalAdapter, RawCapture,
    _bars_url, _calendar_url, _safe_get, _target,
    archive_historical_bundle, normalize_alpaca_history, parse_utc,
    provider_capabilities, verify_historical_archive,
)
from atlas.research.market import load_dataset
from scripts.stock_data_preflight import main


START = parse_utc("2026-09-08T13:30:00Z")
END = START + 900
NOW = parse_utc("2026-09-10T22:00:00Z")
CALENDAR = [{"date": "2026-09-08", "open": "09:30", "close": "16:00"}]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("test must not access network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def request(**changes):
    return HistoricalBarsRequest(**({"provider": "alpaca", "asset_class": "equity", "symbols": ("SPY",),
                                     "start": START, "end": END, "interval_seconds": 300} | changes))


def bar(offset=0, **changes):
    from datetime import datetime, timezone
    return {"t": datetime.fromtimestamp(START + offset, timezone.utc).isoformat().replace("+00:00", "Z"),
            "o": 100, "h": 103, "l": 99, "c": 101, "v": 123, "n": 2, "vw": 100.123, **changes}


def raw(value):
    return json.dumps(value, separators=(",", ":")).encode()


def captures(req=None, *, pages=None, calendar=None):
    req = request() if req is None else req
    result = [RawCapture("calendar", _calendar_url(req), NOW,
                         raw(CALENDAR if calendar is None else calendar))]
    for symbol in sorted(req.symbols):
        result.append(RawCapture("asset:" + symbol, "https://paper-api.alpaca.markets/v2/assets/" + symbol, NOW,
                                 raw({"symbol": symbol, "class": "us_equity", "status": "active",
                                      "tradable": True, "fractionable": True})))
    pages = [{"bars": {s: [bar(0), bar(300), bar(600)] for s in sorted(req.symbols)},
              "next_page_token": None}] if pages is None else pages
    token = None
    for index, page in enumerate(pages):
        result.append(RawCapture("bars", _bars_url(req, token), NOW + index, raw(page)))
        token = page.get("next_page_token")
    return tuple(result)


def normalize(req=None, **kwargs):
    req = request() if req is None else req
    return normalize_alpaca_history(req, captures(req, **kwargs), now=NOW + 100, source_kind="test")


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "fake-paper-key.json"
    path.write_text(json.dumps({"api_key": "synthetic-key", "api_secret": "synthetic-secret"}))
    path.chmod(0o600)
    return path


def test_normalization_preserves_archive_availability_and_exact_raw_lineage():
    bundle = normalize()
    assert bundle.dataset["availability_basis"] == "unknown"
    assert bundle.dataset["revision_policy"] == "latest_only"
    assert bundle.dataset["price_adjustment"] == "unadjusted"
    assert bundle.dataset["volume_unit"] == "shares"
    assert bundle.quality["evidence_class"] == "TEST"
    assert bundle.quality["alpha_validation_eligible"] is False
    assert {b["information_available_time"] for b in bundle.dataset["bars"]} == {NOW}
    assert {b["event_time"] for b in bundle.dataset["bars"]} == {START + 300, START + 600, END}
    assert bundle.dataset["bars"][0]["volume"] == "123"  # no quote round-lot conversion
    assert "historical_availability_not_established" in bundle.quality["blockers"]
    assert bundle.receipt["actual_execution_costs"] is None
    assert bundle.receipt["orders_submitted"] == 0
    assert bundle.receipt["raw_row_count"] == bundle.receipt["retained_row_count"] == 3
    assert bundle.receipt["captures"] == [c.descriptor() for c in bundle.captures]


def test_real_archival_source_stays_historical_unverified():
    result = normalize_alpaca_history(request(), captures(), now=NOW)
    assert result.quality["evidence_class"] == "HISTORICAL_UNVERIFIED"
    assert result.quality["provider_authenticity_verified"] is False


@pytest.mark.parametrize("changes", [
    {"provider": "unlisted"}, {"asset_class": "future"}, {"asset_class": "crypto"},
    {"symbols": ("SPY", "SPY")}, {"symbols": ("../SPY",)}, {"symbols": ("spy",)},
    {"symbols": ("CL=F",)}, {"symbols": []}, {"symbols": ()}, {"feed": "sip"},
    {"adjustment": "split"}, {"interval_seconds": 86400}, {"interval_seconds": True},
    {"start": True}, {"end": START}, {"end": START + 32 * 86400}, {"start": START + 1},
    {"max_pages": 0}, {"max_pages": 21}, {"max_pages": True}, {"page_limit": 0},
    {"max_pages": 20, "page_limit": 10000},
])
def test_invalid_request_refuses_before_provider_access(changes):
    with pytest.raises(DataAdapterError):
        request(**changes)


@pytest.mark.parametrize("value", ["2026-09-08", "2026-09-08T09:30:00", "2026-09-08T09:30:00.001Z", None,
                                  "2026-13-08T09:30:00Z", "2026-09-08T09:30:00.000000001Z"])
def test_no_implicit_timezone_or_subsecond_rounding(value):
    with pytest.raises(DataAdapterError):
        parse_utc(value)


def test_offset_timestamp_equivalent_and_zero_nanoseconds_allowed():
    assert parse_utc("2026-09-08T09:30:00-04:00") == START
    assert parse_utc("2026-09-08T13:30:00.000000000Z") == START


def test_pagination_symbol_major_input_becomes_explicit_event_major_canonical():
    req = request(symbols=("SPY", "QQQ"), page_limit=3, max_pages=2)
    pages = [{"bars": {"QQQ": [bar(0), bar(300), bar(600)]}, "next_page_token": "next/one+="},
             {"bars": {"SPY": [bar(0), bar(300), bar(600)]}, "next_page_token": None}]
    bundle = normalize(req, pages=pages)
    assert [(r["event_time"], r["symbol"]) for r in bundle.dataset["bars"]] == [
        (START + 300, "QQQ"), (START + 300, "SPY"),
        (START + 600, "QQQ"), (START + 600, "SPY"), (END, "QQQ"), (END, "SPY")]
    assert {b["information_available_time"] for b in bundle.dataset["bars"]} == {NOW + 1}
    assert parse_qs(urlsplit(bundle.captures[-1].request_url).query)["page_token"] == ["next/one+="]


def test_premarket_and_partial_window_exclusions_are_recorded_not_repaired():
    pages = [{"bars": {"SPY": [bar(-300), bar(0), bar(300), bar(600), bar(900)]}, "next_page_token": None}]
    bundle = normalize(pages=pages)
    assert bundle.receipt["raw_row_count"] == 5
    assert bundle.receipt["retained_row_count"] == 3
    assert len(bundle.receipt["excluded_rows"]) == 2
    assert "outside_explicit_regular_session" in bundle.receipt["excluded_rows"][0]["reasons"]


def test_missing_intraday_bar_is_reported_not_forward_filled():
    bundle = normalize(pages=[{"bars": {"SPY": [bar(0), bar(600)]}, "next_page_token": None}])
    assert len(bundle.dataset["bars"]) == 2
    assert bundle.quality["gaps"][0]["missing_bars"] == 1


def test_requested_window_missing_edges_are_visible_even_without_internal_gap():
    bundle = normalize(pages=[{"bars": {"SPY": [bar(300)]}, "next_page_token": None}])
    assert bundle.quality["gaps"] == []  # canonical quality checks only the observed span
    coverage = bundle.receipt["requested_window_coverage"]["SPY"]
    assert coverage["expected_complete_regular_buckets"] == 3
    assert coverage["missing_buckets"] == 2
    assert coverage["all_expected_buckets_present"] is False


@pytest.mark.parametrize("pages,match", [
    ([{"bars": {"SPY": [bar(0), bar(0)]}, "next_page_token": None}], "duplicate"),
    ([{"bars": {"SPY": [bar(300), bar(0)]}, "next_page_token": None}], "nonascending"),
    ([{"bars": {"SPY": [bar(60)]}, "next_page_token": None}], "off_session_grid"),
    ([{"bars": {"OTHER": [bar()]}, "next_page_token": None}], "unrequested"),
    ([{"bars": {}, "next_page_token": None}], "no_complete_regular_bars"),
    ([{"bars": {"SPY": [bar(h=98)]}, "next_page_token": None}], "ohlc"),
    ([{"bars": {"SPY": [bar(v=-1)]}, "next_page_token": None}], "value_invalid"),
    ([{"bars": {"SPY": [bar(c=True)]}, "next_page_token": None}], "json_number"),
    ([{"bars": {"SPY": [bar(c="101")]}, "next_page_token": None}], "json_number"),
    ([{"bars": {"SPY": [bar()]}, "next_page_token": "still_more"}], "incomplete"),
    ([{"bars": {}, "next_page_token": "still_more"}], "page_count"),
    ([{"bars": {"SPY": [bar()]}}], "page_invalid"),
    ([{"bars": {"SPY": [bar()]}, "next_page_token": 123}], "token_invalid"),
])
def test_bad_data_refuses_without_partial_dataset(pages, match):
    with pytest.raises(DataAdapterError, match=match):
        normalize(pages=pages)


def test_duplicate_across_pages_refuses_even_identical_prices():
    pages = [{"bars": {"SPY": [bar()]}, "next_page_token": "next"},
             {"bars": {"SPY": [bar()]}, "next_page_token": None}]
    with pytest.raises(DataAdapterError, match="duplicate"):
        normalize(pages=pages)


def test_missing_requested_symbol_is_not_silently_dropped():
    with pytest.raises(DataAdapterError, match="requested_symbol"):
        normalize(request(symbols=("SPY", "QQQ")),
                  pages=[{"bars": {"SPY": [bar()]}, "next_page_token": None}])


@pytest.mark.parametrize("calendar", [[], [{"date": "2026-09-08", "open": "08:00", "close": "16:00"}],
                                     CALENDAR * 2, [{"date": "2026-09-07", "open": "09:30", "close": "16:00"}]])
def test_bad_calendar_refuses(calendar):
    with pytest.raises(DataAdapterError, match="calendar"):
        normalize(calendar=calendar)


def test_early_close_bounds_filter_afternoon_and_do_not_invent_sessions():
    req = request(end=START + 7 * 3600)
    pages = [{"bars": {"SPY": [bar(0), bar(3 * 3600), bar(4 * 3600)]}, "next_page_token": None}]
    bundle = normalize(req, calendar=[{"date": "2026-09-08", "open": "09:30", "close": "13:00"}], pages=pages)
    assert len(bundle.dataset["bars"]) == 2
    assert bundle.receipt["excluded_rows"][0]["reasons"] == ["outside_explicit_regular_session"]


def test_decimal_precision_does_not_round_trip_through_float():
    caps = list(captures())
    content = caps[-1].body.replace(b'"c":101', b'"c":101.000000000000000000000000000000000001')
    caps[-1] = replace(caps[-1], body=content)
    bundle = normalize_alpaca_history(request(), tuple(caps), now=NOW, source_kind="test")
    assert bundle.dataset["bars"][0]["close"] == "101.000000000000000000000000000000000001"


@pytest.mark.parametrize("content", [b'{"bars":{},"bars":{},"next_page_token":null}', b'{"bars":NaN}', b'not json'])
def test_invalid_or_duplicate_json_is_rejected(content):
    caps = (*captures()[:-1], replace(captures()[-1], body=content))
    with pytest.raises(DataAdapterError, match="json_invalid"):
        normalize_alpaca_history(request(), caps, now=NOW)


def test_fetch_makes_only_bounded_pinned_reads_with_explicit_feed_and_adjustment(key_file):
    req = request()
    replies = list(captures(req))
    calls = []

    def fake_get(url, headers, **kwargs):
        expected = replies[len(calls)]
        assert url == expected.request_url
        assert headers["APCA-API-KEY-ID"] == "synthetic-key"
        assert headers["APCA-API-SECRET-KEY"] == "synthetic-secret"
        calls.append(url)
        return HTTPResponse(200, expected.body)

    bundle = AlpacaHistoricalAdapter(key_file, get=fake_get, clock=lambda: NOW, source_kind="test").fetch_bars(req)
    assert len(calls) == 3
    assert [urlsplit(c).hostname for c in calls] == ["paper-api.alpaca.markets", "paper-api.alpaca.markets", "data.alpaca.markets"]
    query = parse_qs(urlsplit(calls[-1]).query)
    assert query["feed"] == ["iex"] and query["adjustment"] == ["raw"] and query["asof"] == ["-"]
    assert query["end"] == ["2026-09-08T13:44:59Z"]
    assert all("orders" not in c and "account" not in c for c in calls)
    assert "synthetic-secret" not in json.dumps(bundle.receipt)


def test_pagination_exhaustion_does_not_return_truncated_data(key_file):
    req = request(max_pages=1)
    replies = list(captures(req, pages=[{"bars": {"SPY": [bar()]}, "next_page_token": "more"}]))
    calls = []

    def fake(url, headers, **kwargs):
        calls.append(url)
        return HTTPResponse(200, replies[len(calls) - 1].body)

    with pytest.raises(DataAdapterError, match="pagination_limit_reached"):
        AlpacaHistoricalAdapter(key_file, get=fake, clock=lambda: NOW).fetch_bars(req)
    assert len(calls) == 3


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_http_refusal_is_not_retried_or_printed_with_provider_body(key_file, status):
    calls = []

    def refuse(url, headers, **kwargs):
        calls.append(url)
        return HTTPResponse(status, b'"synthetic-secret should not escape"')

    with pytest.raises(DataAdapterError) as caught:
        AlpacaHistoricalAdapter(key_file, get=refuse, clock=lambda: NOW).fetch_bars(request())
    assert str(caught.value) == "historical_http_" + str(status)
    assert len(calls) == 1


@pytest.mark.parametrize("url", [
    "https://api.alpaca.markets/v2/orders", "https://paper-api.alpaca.markets/v2/orders",
    "https://paper-api.alpaca.markets/v2/account", "https://data.alpaca.markets.evil/v2/stocks/bars",
    "https://user@paper-api.alpaca.markets/v2/assets/SPY", "http://paper-api.alpaca.markets/v2/assets/SPY",
    "https://paper-api.alpaca.markets:443/v2/assets/SPY", "https://paper-api.alpaca.markets/v2/assets/SPY#fragment",
    "https://paper-api.alpaca.markets/v2/assets/SPY?api_key=secret",
    "https://paper-api.alpaca.markets/v2/assets/../orders",
])
def test_transport_rejects_unapproved_endpoint_before_connection(url):
    with pytest.raises(DataAdapterError, match="endpoint_refused"):
        _safe_get(url, {})


def test_transport_refuses_feed_changes_and_duplicate_query_keys():
    url = _bars_url(request())
    for bad in (url.replace("feed=iex", "feed=sip"), url + "&feed=iex", url + "&api_key=anything"):
        with pytest.raises(DataAdapterError, match="endpoint_refused"):
            _target(bad)


def test_key_permissions_and_symlink_refuse_before_network(key_file, tmp_path):
    key_file.chmod(0o644)
    with pytest.raises(DataAdapterError, match="credential"):
        AlpacaHistoricalAdapter(key_file, clock=lambda: NOW).fetch_bars(request())
    key_file.chmod(0o600)
    alias = tmp_path / "key-link"
    alias.symlink_to(key_file)
    with pytest.raises(DataAdapterError, match="credential"):
        AlpacaHistoricalAdapter(alias, clock=lambda: NOW).fetch_bars(request())


def test_future_window_refuses_before_reading_missing_key(tmp_path):
    with pytest.raises(DataAdapterError, match="end_is_future"):
        AlpacaHistoricalAdapter(tmp_path / "missing", clock=lambda: START).fetch_bars(request())


def test_current_metadata_does_not_certify_historical_membership_or_map_futures():
    bundle = normalize(request(symbols=("GLD", "USO")))
    assets = bundle.receipt["assets_current_only"]
    assert [a["symbol"] for a in assets] == ["GLD", "USO"]
    assert all(a["historical_universe_membership_verified"] is False for a in assets)
    assert all(a["instrument_role"].endswith("not_futures") for a in assets)
    assert bundle.dataset["asset_class"] == "equity"


def test_archive_replays_digests_and_canonical_dataset_privately(tmp_path):
    bundle = normalize()
    destination = tmp_path / "archive"
    report = archive_historical_bundle(bundle, destination)
    assert report["integrity_verified"] is True
    assert report["alpha_validation_eligible"] is False
    assert os.stat(destination).st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in destination.iterdir())
    dataset = load_dataset(destination / "dataset.json", now=NOW)
    assert dataset["dataset_hash"] == bundle.quality["dataset_hash"]
    assert verify_historical_archive(destination, now=NOW)["retained_row_count"] == 3
    with pytest.raises(DataAdapterError, match="new_external_destination"):
        archive_historical_bundle(bundle, destination)


def test_archive_tamper_is_detected_even_when_file_manifest_is_rehashed(tmp_path):
    destination = tmp_path / "archive"
    archive_historical_bundle(normalize(), destination)
    data = json.loads((destination / "dataset.json").read_text())
    data["bars"][0]["close"] = "102"
    (destination / "dataset.json").write_text(json.dumps(data))
    from atlas.research.imports import _sha
    receipt = json.loads((destination / "receipt.json").read_text())
    raw_data = (destination / "dataset.json").read_bytes()
    receipt["files"]["dataset.json"] = {"sha256": _sha(raw_data), "bytes": len(raw_data)}
    (destination / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(DataAdapterError, match="integrity_mismatch"):
        verify_historical_archive(destination, now=NOW)


def test_mutated_bundle_refuses_before_archive_creation(tmp_path):
    bundle = normalize()
    bundle.dataset["bars"][0]["close"] = "102"
    destination = tmp_path / "archive"
    with pytest.raises(DataAdapterError, match="replay_mismatch"):
        archive_historical_bundle(bundle, destination)
    assert not destination.exists()


@pytest.mark.parametrize("factory,provider", [(DatabentoHistoricalAdapter, "databento"), (MassiveHistoricalAdapter, "massive")])
def test_unconfigured_providers_fail_honestly_without_network(factory, provider):
    adapter = factory()
    assert adapter.capabilities.built_in_transport is False
    assert adapter.capabilities.execution_supported is False
    with pytest.raises(DataAdapterError, match="provider_unavailable"):
        adapter.fetch_bars(request(provider=provider))


def test_injected_normalizer_cannot_backdate_availability_or_claim_observed_history():
    payload = copy.deepcopy(normalize().dataset)
    payload["availability_basis"] = "observed"
    payload["revision_policy"] = "as_observed"
    adapter = MassiveHistoricalAdapter(fetcher=lambda req: captures(), normalizer=lambda req, caps: payload,
                                       clock=lambda: NOW)
    with pytest.raises(DataAdapterError, match="normalizer_contract_mismatch"):
        adapter.fetch_bars(request(provider="massive"))


def test_injected_provider_cannot_relabel_another_providers_dataset():
    payload = copy.deepcopy(normalize().dataset)
    adapter = MassiveHistoricalAdapter(fetcher=lambda req: captures(), normalizer=lambda req, caps: payload,
                                       clock=lambda: NOW)
    with pytest.raises(DataAdapterError, match="normalizer_contract_mismatch"):
        adapter.fetch_bars(request(provider="massive"))


def test_injected_capture_iterator_is_bounded():
    seen = []

    def fetcher(req):
        for i in range(100):
            seen.append(i)
            yield captures()[0]

    adapter = DatabentoHistoricalAdapter(fetcher=fetcher, normalizer=lambda *args: pytest.fail("must bound first"),
                                         clock=lambda: NOW)
    with pytest.raises(DataAdapterError, match="capture_count"):
        adapter.fetch_bars(request(provider="databento"))
    assert len(seen) == 32


def test_cli_capabilities_has_no_key_or_network_requirement(capsys):
    assert main(["--provider", "databento", "--capabilities"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["built_in_transport"] is False
    assert report["execution_supported"] is False


def test_cli_checks_output_before_provider_call(tmp_path, capsys, monkeypatch):
    import scripts.stock_data_preflight as cli
    monkeypatch.setattr(cli, "AlpacaHistoricalAdapter", lambda *a, **kw: pytest.fail("no provider call permitted"))
    assert main(["--key-file", "/missing", "--symbols", "SPY", "--start", "2026-09-08T13:30:00Z",
                 "--end", "2026-09-08T13:45:00Z", "--output-dir", str(tmp_path)]) == 2
    report = json.loads(capsys.readouterr().err)
    assert report["orders_submitted"] == 0
    assert report["error"] == "historical_archive_requires_new_external_destination"


def test_cli_complete_fake_fetch_writes_archive(tmp_path, capsys, monkeypatch):
    import scripts.stock_data_preflight as cli

    class Fake:
        def __init__(self, key):
            assert key == "/explicit-fake-key"

        def fetch_bars(self, req):
            return normalize(req)

    monkeypatch.setattr(cli, "AlpacaHistoricalAdapter", Fake)
    target = tmp_path / "new-archive"
    assert main(["--key-file", "/explicit-fake-key", "--symbols", "SPY", "--start", "2026-09-08T13:30:00Z",
                 "--end", "2026-09-08T13:45:00Z", "--output-dir", str(target)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["integrity_verified"] is True
    assert report["orders_submitted"] == 0
    assert report["execution_enabled"] is False
