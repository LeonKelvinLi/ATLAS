"""Offline provider-capture tests. No HTTP or account sessions are opened."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from atlas.alpaca_paper_preflight import HTTPResponse
from atlas.research import data_collector as module
from atlas.research.data_collector import DataCollectorError, collect_once
from atlas.research.data_contract_v1 import default_contract, validate_record


START = int(datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc).timestamp())
END = START + 600
NOW = END + 10
CALENDAR = [{"date": "2026-09-08", "open": "09:30", "close": "16:00"}]


def contract(created=START):
    return default_contract("collector-test", created - 180 * 86400, created)


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def bar(opening=START, **values):
    return {"t": datetime.fromtimestamp(opening, timezone.utc).isoformat(),
            "o": 100, "h": 102, "l": 99, "c": 101, "v": 12, **values}


def candle(opening=START):
    return [opening, 99, 102, 100, 101, 12]


def book(**values):
    return {"sequence": 15, "bids": [["100.01", "2.5", 7]],
            "asks": [["100.02", "3.125", 2]],
            "time": datetime.fromtimestamp(NOW - 1, timezone.utc).isoformat(), **values}


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, **options):
        self.calls.append((url, deepcopy(headers), options))
        assert options == {"timeout_seconds": 10, "max_response_bytes": 2_000_000}
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if hasattr(value, "status"):
            return value
        return HTTPResponse(200, value if type(value) is bytes else encoded(value))


def collect(responses, *, provider="coinbase", now=NOW, settings=None, **options):
    transport = Transport([*responses, *([{"quotes": {}}] if provider == "alpaca" else [])])
    result = collect_once(contract() if settings is None else settings, provider=provider,
                          start=START, end=END, transport=transport, clock=lambda: now, **options)
    return result, transport


def test_coinbase_keeps_exact_receipts_decimal_precision_and_uncertainty():
    raw = ('[[' + str(START) + ',99,102,100,100.12345678901234567890123456789,12]]').encode()
    result, transport = collect([raw, book()])
    bars, quotes = result["records"][:1], result["records"][1:]
    row = bars[0]
    assert row["payload"]["close"] == "100.12345678901234567890123456789"
    assert row["event_time"] == START + 300
    assert row["raw_capture_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["captures"][0]["body"] is raw
    assert row["payload"]["volume_unit"] == "unknown"
    assert row["payload"]["corporate_actions_status"] == "not_applicable"
    assert row["publication_time"] is None
    assert row["available_time"] == row["ingestion_time"] == NOW
    assert row["revision"] == 0 and row["previous_observation_hash"] is None
    quote = quotes[0]
    assert quote["event_time"] == NOW - 1 and quote["interval_seconds"] == 0
    assert quote["payload"]["bid_size"] == "2.5"  # Never multiplied by seven orders.
    assert quote["payload"]["size_unit"] == "unknown"
    assert result["calendar_coverage"] == []
    assert all(row["acquisition_kind"] == "test" for row in result["records"])
    quality = validate_record(quote, contract(), NOW)["quality"]
    assert {"quote_size_unit_unknown", "publication_time_unknown", "not_prospective_observation"} <= set(quality["blockers"])
    assert len(transport.calls) == 2
    assert transport.calls[1][0] == module.BOOK_URL
    assert not any(key.startswith("APCA") for _, headers, _ in transport.calls for key in headers)


def test_candle_boundaries_duplicates_and_missing_buckets_are_not_imputed():
    result, _ = collect([[candle(END), candle(START), candle(START - 300)], book()])
    assert [r["event_time"] for r in result["records"] if r["record_type"] == "bar"] == [START + 300]
    assert len(json.loads(result["captures"][0]["body"])) == 3
    empty, _ = collect([[], book()])
    assert [r["record_type"] for r in empty["records"]] == ["quote"]
    assert empty["captures"][0]["body"] == b"[]"


def test_receipt_ceil_and_unknown_quote_time_are_explicit():
    result, _ = collect([[candle()], book(time=None)], now=Decimal(str(NOW)) + Decimal("0.000000001"))
    quote = result["records"][-1]
    assert quote["event_time"] == quote["available_time"] == NOW + 1
    assert quote["payload"]["timestamp_basis"] == "receipt_only"
    assert quote["payload"]["provider_timestamp"] is None
    assert "quote_provider_timestamp_unknown" in validate_record(quote, contract(), NOW + 1)["quality"]["blockers"]


def test_provider_fraction_and_timezone_preserved_without_invented_publication():
    provider_time = datetime.fromtimestamp(NOW - 1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".987654321Z"
    result, _ = collect([[], book(time=provider_time)])
    assert result["records"][0]["payload"]["provider_timestamp"] == provider_time
    assert result["records"][0]["event_time"] == NOW - 1
    assert result["records"][0]["publication_time"] is None


def test_stale_quote_is_retained_as_blocked_evidence():
    result, _ = collect([[], book(time=datetime.fromtimestamp(START - 1000, timezone.utc).isoformat())])
    row = result["records"][0]
    assert "stale_at_receipt" in validate_record(row, contract(), NOW)["quality"]["blockers"]


def test_alpaca_symbol_major_pagination_calendar_and_raw_page_lineage():
    first = {"bars": {"GLD": [bar()], "QQQ": [bar()]}, "next_page_token": "cursor&secret=not-a-key"}
    second = {"bars": {"SPY": [bar(START + 300)], "USO": [bar()]}, "next_page_token": None}
    result, transport = collect([CALENDAR, first, second], provider="alpaca")
    assert {r["symbol"] for r in result["records"]} == set(module.EQUITIES)
    assert len(result["captures"]) == 4
    coverage = result["calendar_coverage"][0]
    assert coverage["source_id"] == "alpaca-paper-calendar-v2"
    assert coverage["start_date"] == coverage["end_date"] == "2026-09-08"
    assert coverage["sessions"][0]["open_time"] == START
    assert coverage["raw_capture_sha256"] == hashlib.sha256(encoded(CALENDAR)).hexdigest()
    assert parse_qs(urlsplit(transport.calls[2][0]).query)["page_token"] == ["cursor&secret=not-a-key"]
    for row in result["records"]:
        assert row["session"]["calendar_verified"] is False
        assert row["payload"]["volume_unit"] == "shares"
        assert row["payload"]["corporate_actions_status"] == "unknown"
        expected = first if row["symbol"] in {"GLD", "QQQ"} else second
        assert row["raw_capture_sha256"] == hashlib.sha256(encoded(expected)).hexdigest()
    for url, _, _ in transport.calls:
        assert urlsplit(url).path in {"/v2/calendar", "/v2/stocks/bars", "/v2/stocks/quotes/latest"}


def test_empty_equity_calendar_and_missing_symbols_remain_empty_not_invented():
    result, _ = collect([[], {"bars": {}, "next_page_token": None}], provider="alpaca")
    assert result["records"] == []
    assert result["calendar_coverage"][0]["sessions"] == []
    partial, _ = collect([CALENDAR, {"bars": {"SPY": [bar()]}, "next_page_token": None}], provider="alpaca")
    assert [r["symbol"] for r in partial["records"]] == ["SPY"]


def test_alpaca_preopen_and_incomplete_rows_are_excluded_with_raw_preserved():
    page = {"bars": {"SPY": [bar(START - 300), bar(), bar(END)]}, "next_page_token": None}
    result, _ = collect([CALENDAR, page], provider="alpaca")
    assert len(result["records"]) == 1
    assert result["captures"][1]["body"] == encoded(page)


@pytest.mark.parametrize("date,opening,closing", [
    ("2026-11-27", "14:30", "18:00"),  # Friday after Thanksgiving, standard time, early close.
    ("2026-07-02", "13:30", "17:00"),  # Daylight time, declared early close.
])
def test_explicit_calendar_early_close_and_dst(date, opening, closing):
    start = int(datetime.fromisoformat(date + "T" + opening + ":00+00:00").timestamp())
    end = int(datetime.fromisoformat(date + "T" + closing + ":00+00:00").timestamp())
    transport = Transport([[{"date": date, "open": "09:30", "close": "13:00"}],
        {"bars": {"SPY": [bar(start), bar(end - 300), bar(end)]}, "next_page_token": None}, {"quotes": {}}])
    settings = contract(created=start)
    result = collect_once(settings, provider="alpaca", start=start, end=end + 300,
                          transport=transport, clock=lambda: end + 1000)
    assert [r["event_time"] for r in result["records"]] == [start + 300, end]
    assert all(r["session"]["close_time"] == end for r in result["records"])


def test_each_response_has_its_own_monotonic_ceil_receipt_not_final_batch_time():
    clocks = iter([NOW + .1, NOW + .2, NOW + 1.2, NOW + 2.2, NOW + 3.2])
    transport = Transport([CALENDAR, {"bars": {"SPY": [bar()]}, "next_page_token": "next"},
                            {"bars": {"USO": [bar()]}, "next_page_token": None}, {"quotes": {}}])
    result = collect_once(contract(), provider="alpaca", start=START, end=END,
                          transport=transport, clock=lambda: next(clocks))
    assert [c["received_at"] for c in result["captures"]] == [NOW + 1, NOW + 2, NOW + 3, NOW + 4]
    assert {r["symbol"]: r["available_time"] for r in result["records"]} == {"SPY": NOW + 2, "USO": NOW + 3}


@pytest.mark.parametrize("inject_transport,inject_clock", [(True, False), (False, True), (True, True)])
def test_either_injection_forces_test_provenance(monkeypatch, inject_transport, inject_clock):
    fake = Transport([[candle()], book()])
    monkeypatch.setattr(module, "_exchange_get", fake)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    result = collect_once(contract(), provider="coinbase", start=START, end=END,
                          transport=fake if inject_transport else None,
                          clock=(lambda: NOW) if inject_clock else None)
    assert result["provenance"] == "injected_test_transport_or_clock"
    assert all(r["acquisition_kind"] == "test" for r in result["records"])


def test_builtin_path_separates_historical_and_prospective_by_creation(monkeypatch):
    # Patch internals to inspect policy without network; this does not authenticate fixtures.
    fake = Transport([[candle(START), candle(START + 300)], book()])
    monkeypatch.setattr(module, "_exchange_get", fake)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    result = collect_once(contract(created=START + 600), provider="coinbase", start=START, end=END)
    assert [r["acquisition_kind"] for r in result["records"]] == [
        "historical_backfill", "prospective_observation", "prospective_observation"]
    assert all(not validate_record(r, contract(created=START + 600), NOW)["quality"]["qualification_eligible"]
               for r in result["records"])


def test_identical_snapshots_are_captured_again_for_store_owned_deduplication():
    one, _ = collect([[candle()], book()])
    two, _ = collect([[candle()], book()], now=NOW + 1)
    assert [c["body"] for c in one["captures"]] == [c["body"] for c in two["captures"]]
    assert two["records"][0]["available_time"] == one["records"][0]["available_time"] + 1
    assert two["records"][0]["revision"] == 0


@pytest.mark.parametrize("change", [
    {"provider": "other"}, {"provider": []}, {"start": True}, {"end": END + .0},
    {"start": START + 1}, {"end": END + 1}, {"end": START},
    {"start": END - 86700}, {"end": END + 300}, {"clock": 42}, {"transport": 42},
    {"key_file": "/must-not-read"},
])
def test_invalid_requests_refuse_before_transport(change):
    fake = Transport([])
    options = dict(provider="coinbase", start=START, end=END, transport=fake, clock=lambda: NOW)
    options.update(change)
    with pytest.raises(DataCollectorError):
        collect_once(contract(), **options)
    assert fake.calls == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "100"])
def test_invalid_clock_refuses_before_transport(value):
    with pytest.raises(DataCollectorError, match="clock_invalid"):
        collect([[], book()], now=value)


@pytest.mark.parametrize("ticks", [[NOW + .9, NOW + .1], [NOW, NOW + 61]])
def test_subsecond_clock_regression_and_batch_timeout_refuse(ticks):
    clock = iter(ticks)
    fake = Transport([[]])
    with pytest.raises(DataCollectorError, match="clock_regressed_or_batch_timeout"):
        collect_once(contract(), provider="coinbase", start=START, end=END,
                     transport=fake, clock=lambda: next(clock))
    assert len(fake.calls) == 1


@pytest.mark.parametrize("response", [
    HTTPResponse(302, b"secret"), HTTPResponse(401, b"secret"), HTTPResponse(429, b"secret"),
    HTTPResponse(True, b"[]"), HTTPResponse(200, "[]"), HTTPResponse(200, b""),
    HTTPResponse(200, b" " * (module.MAX_RESPONSE_BYTES + 1)),
    RuntimeError("private secret response"), SimpleNamespace(status=200),
    DataCollectorError("private secret transport diagnostic"),
    b"[[NaN]]", b'{"bars":{},"bars":{}}', b"not json", b"[" * 2000,
])
def test_response_failures_are_bounded_sanitized_and_not_retried(response):
    fake = Transport([response])
    with pytest.raises(DataCollectorError) as caught:
        collect_once(contract(), provider="coinbase", start=START, end=END,
                     transport=fake, clock=lambda: NOW)
    assert len(fake.calls) == 1
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("candles", [
    [candle(), candle()], [candle(START + 1)], [[True, 99, 102, 100, 101, 10]],
    [[START, 99, 102, 100, 101]], [[START, 110, 102, 100, 101, 12]],
    [[START, 99, 102, 0, 101, 12]], [[START, 99, 102, 100, 101, -1]],
    [[START, 99, 102, "100", 101, 12]], [candle()] * 301,
])
def test_malformed_candles_refuse_before_book_request(candles):
    fake = Transport([candles])
    with pytest.raises(DataCollectorError):
        collect_once(contract(), provider="coinbase", start=START, end=END,
                     transport=fake, clock=lambda: NOW)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("changes", [
    {"bids": []}, {"asks": [["99", "1", 1]]}, {"bids": [["100", "2", True]]},
    {"bids": [["100", "2", 0]]}, {"asks": [["101", "0", 1]]},
    {"sequence": True}, {"sequence": -1}, {"auction_mode": True}, {"auction_mode": 0},
    {"auction": {}}, {"unknown_flag": True}, {"time": "2026-09-08T13:30:00"},
    {"time": "invalid"}, {"time": "2026-09-09T00:00:00Z"},
    {"bids": [["1e100000", "2", 1]]}, {"asks": [["NaN", "2", 1]]},
])
def test_bad_book_cannot_become_ordinary_quote(changes):
    with pytest.raises(DataCollectorError):
        collect([[], book(**changes)])


@pytest.mark.parametrize("pages", [
    [{"bars": {"AAPL": [bar()]}, "next_page_token": None}],
    [{"bars": {"SPY": [bar(), bar()]}, "next_page_token": None}],
    [{"bars": {"SPY": [bar(START + 1)]}, "next_page_token": None}],
    [{"bars": {}, "next_page_token": "next"}],
    [{"bars": {"SPY": [bar()]}, "next_page_token": "x"},
     {"bars": {"SPY": [bar(START + 300)]}, "next_page_token": "x"}],
    [{"bars": {"SPY": [bar(START + 300)]}, "next_page_token": "x"},
     {"bars": {"SPY": [bar()]}, "next_page_token": None}],
])
def test_alpaca_bad_symbols_grid_duplicates_and_tokens_refuse(pages):
    with pytest.raises(DataCollectorError):
        collect([CALENDAR, *pages], provider="alpaca")


def test_page_allowance_refuses_partial_bundle():
    pages = [{"bars": {"SPY": [bar(START + i * 300)]}, "next_page_token": "p" + str(i)}
             for i in range(module.MAX_PAGES)]
    fake = Transport([CALENDAR, *pages])
    with pytest.raises(DataCollectorError, match="pagination_limit_no_partial_bundle"):
        collect_once(contract(), provider="alpaca", start=START, end=END,
                     transport=fake, clock=lambda: NOW)
    assert len(fake.calls) == module.MAX_PAGES + 1


def test_real_alpaca_requires_explicit_external_private_key_before_get(monkeypatch):
    fake = Transport([])
    monkeypatch.setattr(module, "alpaca_get", fake)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    with pytest.raises(DataCollectorError, match="explicit_key_file_required"):
        collect_once(contract(), provider="alpaca", start=START, end=END)
    assert fake.calls == []


@pytest.mark.parametrize("url", [
    "http://api.exchange.coinbase.com/products/BTC-USD/book?level=1",
    "https://evil.example/products/BTC-USD/book?level=1",
    "https://api.exchange.coinbase.com@evil.example/products/BTC-USD/book?level=1",
    "https://api.exchange.coinbase.com:443/products/BTC-USD/book?level=1",
    "https://api.exchange.coinbase.com/products/BTC-USD/book?level=2",
    "https://api.exchange.coinbase.com/products/ETH-USD/book?level=1",
    "https://api.exchange.coinbase.com/orders",
    "https://api.exchange.coinbase.com/products/BTC-USD/book?level=1#fragment",
    "https://api.exchange.coinbase.com/products/BTC-USD/book?level=1&level=1",
])
def test_public_target_pins_origin_product_and_get_path(url):
    with pytest.raises(DataCollectorError, match="endpoint_refused"):
        module._exchange_target(url)


def test_exchange_transport_uses_get_tls_and_closes_without_redirect(monkeypatch):
    calls = []
    class Connection:
        sock = None
        def __init__(self, host, **kwargs):
            calls.append((host, kwargs))
        def request(self, method, target, **kwargs):
            calls.append((method, target, kwargs))
        def getresponse(self):
            return SimpleNamespace(status=302)
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    with pytest.raises(DataCollectorError, match="http_refused"):
        module._exchange_get(module.BOOK_URL, {"Accept": "application/json"},
                             timeout_seconds=10, max_response_bytes=100)
    assert calls[0][0] == "api.exchange.coinbase.com"
    assert calls[0][1]["context"].check_hostname is True
    assert calls[1][0] == "GET" and calls[1][1] == "/products/BTC-USD/book?level=1"
    assert calls[-1] == "closed"


def test_alpaca_quotes_are_separate_current_records_with_round_lot_uncertainty():
    when = datetime.fromtimestamp(NOW - 1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"
    quotes = {"quotes": {"SPY": {"t": when, "bp": 100, "ap": 101, "bs": 2, "as": 3,
                                 "bx": "V", "ax": "V", "c": ["R"], "z": "A"}, "QQQ": None}}
    fake = Transport([CALENDAR, {"bars": {"SPY": [bar()]}, "next_page_token": None}, quotes])
    result = collect_once(contract(), provider="alpaca", start=START, end=END, transport=fake, clock=lambda: NOW)
    assert len(result["records"]) == 2
    candle_record, quote = result["records"]
    assert candle_record["record_type"] == "bar"
    assert quote["record_type"] == "quote" and quote["event_time"] == NOW - 1 > END
    assert quote["payload"]["provider_timestamp"] == when
    assert quote["payload"]["size_unit"] == "round_lots"
    assert quote["payload"]["bid_size"] == "2" and quote["payload"]["ask_size"] == "3"
    assert quote["raw_capture_sha256"] == hashlib.sha256(encoded(quotes)).hexdigest()
    assert "round_lot_conversion_not_verified" in validate_record(quote, contract(), NOW)["quality"]["blockers"]
    assert fake.calls[-1][0] == module.ALPACA_QUOTES_URL
    query = parse_qs(urlsplit(fake.calls[-1][0]).query)
    assert query == {"symbols": ["GLD,QQQ,SPY,USO"], "feed": ["iex"], "currency": ["USD"]}


def test_alpaca_current_quote_does_not_borrow_historical_calendar_session():
    tomorrow = NOW + 86400
    quotes = {"quotes": {"SPY": {"bp": 100, "ap": 101, "bs": 2, "as": 3,
        "t": datetime.fromtimestamp(tomorrow - 1, timezone.utc).isoformat()}}}
    fake = Transport([CALENDAR, {"bars": {}, "next_page_token": None}, quotes])
    result = collect_once(contract(), provider="alpaca", start=START, end=END,
                          transport=fake, clock=lambda: tomorrow)
    quote = result["records"][0]
    assert quote["session"] is None
    assert "session_metadata_unknown" in validate_record(quote, contract(), tomorrow)["quality"]["blockers"]


def test_alpaca_close_withdrawn_asks_keep_final_bars_and_exact_quote_receipt():
    closing = int(datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc).timestamp())
    quotes = {"quotes": {
        "QQQ": {"ap": 0, "as": 0, "ax": " ", "bp": 709.36, "bs": 280,
                "bx": "V", "t": "2026-09-14T20:00:59.916362115Z"},
        "USO": {"ap": 0, "as": 0, "ax": " ", "bp": 151.32, "bs": 100,
                "bx": "V", "t": "2026-09-14T20:00:05.538115756Z"},
        "SPY": {"ap": 781.87, "as": 40, "ax": "V", "bp": 736.25, "bs": 40,
                "bx": "V", "t": "2026-09-14T20:00:00.005351580Z"},
    }}
    raw = encoded(quotes)
    bars = {symbol: [bar(closing - 300)] for symbol in module.EQUITIES}
    fake = Transport([[{"date": "2026-09-14", "open": "09:30", "close": "16:00"}],
                      {"bars": bars, "next_page_token": None}, raw])
    result = collect_once(contract(), provider="alpaca", start=closing - 300,
                          end=closing, transport=fake, clock=lambda: closing + 75)
    admitted_bars = [row for row in result["records"] if row["record_type"] == "bar"]
    admitted_quotes = [row for row in result["records"] if row["record_type"] == "quote"]
    assert {row["symbol"] for row in admitted_bars} == set(module.EQUITIES)
    assert all(row["event_time"] == closing for row in admitted_bars)
    assert [row["symbol"] for row in admitted_quotes] == ["SPY"]
    assert result["captures"][-1]["body"] == raw
    assert all(row["acquisition_kind"] == "test" for row in result["records"])
    assert all(Decimal(row["payload"][side]) > 0 for row in admitted_quotes for side in ("bid", "ask"))


@pytest.mark.parametrize("absent_sides", [("bid",), ("ask",), ("bid", "ask")])
def test_alpaca_unavailable_quote_sides_are_missing_even_during_open_session(absent_sides):
    quote = {"bp": 100, "ap": 101, "bs": 2, "as": 3, "bx": "V", "ax": "V"}
    for side in absent_sides:
        prefix = "b" if side == "bid" else "a"
        quote.update({prefix + "p": 0, prefix + "s": 0, prefix + "x": " "})
    raw = encoded({"quotes": {"SPY": quote}})
    fake = Transport([CALENDAR, {"bars": {"SPY": [bar()]}, "next_page_token": None}, raw])
    result = collect_once(contract(), provider="alpaca", start=START, end=END,
                          transport=fake, clock=lambda: NOW)
    assert [row["record_type"] for row in result["records"]] == ["bar"]
    assert result["captures"][-1]["body"] == raw


@pytest.mark.parametrize("changes", [
    {"as": 1}, {"ax": "V"}, {"ax": None}, {"ax": "\t"}, {"ap": False},
    {"as": False}, {"bp": -1}, {"bs": -1}, {"ap": "0"}, {"t": "invalid"},
])
def test_withdrawn_side_shape_does_not_hide_malformed_quotes(changes):
    quote = {"bp": 100, "ap": 0, "bs": 2, "as": 0, "bx": "V", "ax": " "}
    quote.update(changes)
    raw = encoded({"quotes": {"SPY": quote}})
    fake = Transport([CALENDAR, {"bars": {"SPY": [bar()]}, "next_page_token": None}, raw])
    with pytest.raises(DataCollectorError) as caught:
        collect_once(contract(), provider="alpaca", start=START, end=END,
                     transport=fake, clock=lambda: NOW)
    assert caught.value.captures[-1]["body"] == raw


@pytest.mark.parametrize("quotes", [
    {"quotes": {"AAPL": {}}}, {"quotes": []}, {"quotes": {"SPY": {}}},
    {"quotes": {"SPY": {"bp": 101, "ap": 100, "bs": 1, "as": 2}}},
    {"quotes": {"SPY": {"bp": 0, "ap": 100, "bs": 1, "as": 2}}},
    {"quotes": {"SPY": {"bp": 100, "ap": 101, "bs": True, "as": 2}}},
])
def test_invalid_alpaca_quotes_refuse_and_preserve_completed_captures(quotes):
    fake = Transport([CALENDAR, {"bars": {}, "next_page_token": None}, quotes])
    with pytest.raises(DataCollectorError) as caught:
        collect_once(contract(), provider="alpaca", start=START, end=END,
                     transport=fake, clock=lambda: NOW)
    assert len(caught.value.captures) == 3
    assert caught.value.captures[-1]["body"] == encoded(quotes)


def test_second_get_failure_preserves_successful_candles_for_quarantine_only():
    raw = encoded([candle()])
    fake = Transport([raw, RuntimeError("secret")])
    with pytest.raises(DataCollectorError) as caught:
        collect_once(contract(), provider="coinbase", start=START, end=END,
                     transport=fake, clock=lambda: NOW)
    error = caught.value
    assert str(error) == error.code == "collector_request_or_response_invalid"
    assert error.captures == [{"role": "candles", "request_url": fake.calls[0][0], "received_at": NOW, "body": raw}]
    assert not hasattr(error, "records")  # Failed parsing is never a partial validated bundle.


def test_json_invalid_raw_response_is_retained_in_fixed_error_capture():
    raw = b'{"quotes":{},"quotes":{}}'
    fake = Transport([raw])
    with pytest.raises(DataCollectorError) as caught:
        collect_once(contract(), provider="coinbase", start=START, end=END,
                     transport=fake, clock=lambda: NOW)
    assert caught.value.captures[0]["body"] is raw
    assert "quotes" not in str(caught.value)


def test_error_capture_metadata_is_copied():
    original = [{"role": "candles", "request_url": "https://example.test", "received_at": NOW, "body": b"[]"}]
    error = DataCollectorError("fixture", captures=original)
    original[0]["role"] = "changed"
    original.clear()
    assert error.captures[0]["role"] == "candles"


@pytest.mark.parametrize("url", [
    "https://paper-api.alpaca.markets/v2/account",
    "https://paper-api.alpaca.markets/v2/assets/SPY",
    "https://api.alpaca.markets/v2/calendar?start=2026-09-08&end=2026-09-08&date_type=TRADING",
    module.ALPACA_QUOTES_URL.replace("feed=iex", "feed=sip"),
    module.ALPACA_QUOTES_URL.replace("SPY", "AAPL"),
    module.ALPACA_QUOTES_URL + "&page_token=x",
    module.ALPACA_QUOTES_URL.replace("https://", "http://"),
])
def test_alpaca_target_refuses_accounts_other_feeds_and_unrequested_symbols(url):
    with pytest.raises(ValueError):
        module._alpaca_target(url)


@pytest.mark.parametrize("body,length,limit", [(b"abcdef", None, 5), (b"abc", "4", 5),
                                                (b"abc", "2", 5), (b"abc", "6", 5),
                                                (b"abc", "invalid", 5)])
def test_builtin_get_bounds_response_and_content_length(monkeypatch, body, length, limit):
    calls = []
    class Response:
        status = 200
        remaining = body
        def getheader(self, name):
            assert name == "Content-Length"
            return length
        def read1(self, maximum):
            part, self.remaining = self.remaining[:maximum], self.remaining[maximum:]
            return part
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs):
            calls.append("open")
        def request(self, method, target, **kwargs):
            assert method == "GET"
        def getresponse(self):
            return Response()
        def close(self):
            calls.append("close")
    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    with pytest.raises(DataCollectorError):
        module._exchange_get(module.BOOK_URL, {}, timeout_seconds=10, max_response_bytes=limit)
    assert calls == ["open", "close"]


@pytest.mark.parametrize("error,code", [
    (TimeoutError("private transport detail"), "collector_request_timeout"),
    (module.socket.gaierror("private transport detail"), "collector_dns_failed"),
    (module.ssl.SSLCertVerificationError("private transport detail"), "collector_tls_certificate_failed"),
    (module.ssl.SSLError("private transport detail"), "collector_tls_failed"),
    (module.http.client.RemoteDisconnected("private transport detail"), "collector_remote_disconnected"),
    (ConnectionResetError("private transport detail"), "collector_connection_failed"),
    (ConnectionRefusedError("private transport detail"), "collector_connection_failed"),
    (BrokenPipeError("private transport detail"), "collector_connection_failed"),
    (OSError("private transport detail"), "collector_network_io_failed"),
    (module.http.client.IncompleteRead(b"private transport detail"), "collector_response_incomplete"),
    (module.http.client.BadStatusLine("private transport detail"), "collector_http_protocol_invalid"),
    (RuntimeError("private transport detail"), "collector_request_or_response_invalid"),
])
@pytest.mark.parametrize("stage", ["request", "headers", "body"])
def test_builtin_network_failure_keeps_fixed_category_role_and_single_attempt(monkeypatch, error, code, stage):
    calls = []

    def fail(where):
        calls.append(where)
        if where == stage:
            raise error

    class Response:
        status = 200
        def getheader(self, name):
            return None
        def read1(self, limit):
            fail("body")
            return b""

    class Connection:
        sock = None
        def __init__(self, host, **kwargs):
            calls.append("open")
        def request(self, method, target, **kwargs):
            assert method == "GET"
            fail("request")
        def getresponse(self):
            fail("headers")
            return Response()
        def close(self):
            calls.append("close")

    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    with pytest.raises(DataCollectorError) as caught:
        collect_once(contract(), provider="coinbase", start=START, end=END)
    assert caught.value.code == code
    assert caught.value.role == "candles"
    assert caught.value.captures == []
    assert caught.value.status is None and caught.value.retry_after_seconds is None
    assert "private transport detail" not in str(caught.value)
    assert calls.count("open") == calls.count("request") == calls.count("close") == 1


@pytest.mark.parametrize("request_elapsed,headers_allowed", [(8, True), (10, False), (11, False)])
def test_header_wait_receives_only_remaining_request_budget(monkeypatch, request_elapsed, headers_allowed):
    elapsed = [0]
    calls, timeouts = [], []

    class Connection:
        sock = SimpleNamespace(settimeout=lambda value: timeouts.append(value))
        def __init__(self, *args, **kwargs):
            assert kwargs["timeout"] == 10
        def request(self, *args, **kwargs):
            elapsed[0] = request_elapsed
        def getresponse(self):
            calls.append("headers")
            assert timeouts[-1] == 10 - request_elapsed
            raise TimeoutError("private header detail")
        def close(self):
            calls.append("close")

    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    with pytest.raises(DataCollectorError, match="^collector_request_timeout$"):
        module._exchange_get(module.BOOK_URL, {}, timeout_seconds=10, max_response_bytes=100)
    assert calls == (["headers", "close"] if headers_allowed else ["close"])
    assert timeouts == ([2] if headers_allowed else [])


def test_injected_error_subclass_cannot_export_properties_or_class_name():
    class PrivateSecretError(DataCollectorError):
        def __init__(self):
            ValueError.__init__(self, "private diagnostic detail")
        @property
        def status(self):
            pytest.fail("injected error accessor must not be called")
        @property
        def retry_after_seconds(self):
            pytest.fail("injected error accessor must not be called")

    with pytest.raises(DataCollectorError) as caught:
        collect([PrivateSecretError()])
    assert caught.value.code == "collector_request_or_response_invalid"
    assert caught.value.role == "candles"
    assert caught.value.status is None and caught.value.retry_after_seconds is None
    assert "PrivateSecretError" not in repr(caught.value)


def test_injected_error_cannot_select_role_or_export_owned_network_category():
    error = DataCollectorError("collector_dns_failed", status=429, retry_after_seconds=7, role="quotes")
    with pytest.raises(DataCollectorError) as caught:
        collect([[], error])
    assert caught.value.code == "collector_request_or_response_invalid"
    assert caught.value.role == "book"
    assert caught.value.status == 429 and caught.value.retry_after_seconds == 7
    assert len(caught.value.captures) == 1


@pytest.mark.parametrize("responses,role,captures", [
    ([b"not JSON"], "candles", 1),
    ([[], {"bids": []}], "book", 2),
])
def test_invalid_payload_role_retains_exact_completed_capture_quarantine(responses, role, captures):
    with pytest.raises(DataCollectorError) as caught:
        collect(responses)
    assert caught.value.role == role
    assert len(caught.value.captures) == captures
    assert not hasattr(caught.value, "records")


def test_common_contract_failure_does_not_blame_last_fetched_endpoint(monkeypatch):
    def reject_bar(record, *args):
        assert record["record_type"] == "bar"
        raise ValueError("private contract detail")
    monkeypatch.setattr(module, "validate_record", reject_bar)
    with pytest.raises(DataCollectorError) as caught:
        collect([[candle()], book()])
    assert caught.value.code == "collector_payload_contract_invalid"
    assert caught.value.role is None
    assert [c["role"] for c in caught.value.captures] == ["candles", "book"]
    assert "private contract detail" not in str(caught.value)


def test_diagnostic_boundary_accepts_only_owned_labels_and_exact_primitives():
    class Hostile:
        def __str__(self):
            pytest.fail("untrusted values must not be converted")
        def __hash__(self):
            pytest.fail("untrusted values must not be hashed")
        def __eq__(self, other):
            pytest.fail("untrusted values must not be compared")

    hostile = Hostile()
    assert module.safe_failure_diagnostic(hostile, status=hostile,
        retry_after_seconds=hostile, role=hostile) == {
            "error": "collector_request_or_response_invalid", "http_status": None,
            "retry_after_seconds": None, "request_role": None}
    assert module.safe_failure_diagnostic("collector_http_refused", status=503,
        retry_after_seconds=86400, role="book") == {
            "error": "collector_http_refused", "http_status": 503,
            "retry_after_seconds": 86400, "request_role": "book"}
    diagnostic = module.safe_failure_diagnostic("private detail", status=True,
        retry_after_seconds=86401, role="private detail")
    assert diagnostic == {"error": "collector_request_or_response_invalid", "http_status": None,
                          "retry_after_seconds": None, "request_role": None}
