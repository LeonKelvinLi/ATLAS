"""Bounded one-shot GET acquisition for ATLAS_DATA_CONTRACT_V1.

This module has no order, account, worker, file-write or retry API. ``start`` and
``end`` bound bar openings in [start,end); only complete five-minute bars whose
closes are <= end survive. Each provider's latest quote is a separate observation
at collection time, never a reconstructed quote for the requested candle window.

Injecting either transport or clock makes EVERY record test data. The built-in
transport owns the receipt clock, rounded upward to whole UTC seconds. Seeing
an archive today does not establish its historical publication or revisions.
Missing bars are not filled, and current source labels do not authenticate data.

Transport protocol: get(url, headers, *, timeout_seconds, max_response_bytes)
returns an object with integer status and exact bytes body (HTTPResponse works).
Alpaca requires an explicit external owner-only key file for real HTTP. An
injected transport can run without credentials. Credentials are never returned.

Primary contracts:
https://docs.alpaca.markets/reference/stockbars
https://docs.alpaca.markets/reference/getcalendar-1
https://docs.alpaca.markets/us/reference/stocklatestquotes-1
https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data
https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles
https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-book
Exchange candle docs leave volume units unspecified. Book sizes are preserved
with unknown units pending a verified unit contract; order counts are not size
multipliers. Auction quotes cannot be represented by V1 and are refused.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import http.client
import re
import ssl
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from atlas.collection.primitives import (
    HTTPResponse, CollectionWindow, NY, _credentials, _bars_url, _calendar,
    _calendar_url, _page, alpaca_target, parse_utc, candle_url,
    _external_path, _json, timestamp, retry_after_seconds,
)
from atlas.research.data_contract_v1 import VERSION, validate_contract, validate_record


MAX_RESPONSE_BYTES = 2_000_000
MAX_PAGES = 4
PAGE_LIMIT = 1000
TIMEOUT_SECONDS = 10
MAX_BATCH_SECONDS = 60
BOOK_URL = "https://api.exchange.coinbase.com/products/BTC-USD/book?level=1"
EQUITIES = ("SPY", "QQQ", "GLD", "USO")
ALPACA_QUOTES_URL = "https://data.alpaca.markets/v2/stocks/quotes/latest?" + urlencode({
    "symbols": ",".join(sorted(EQUITIES)), "feed": "iex", "currency": "USD"})
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})\Z")
_DECIMAL = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z")


class DataCollectorError(ValueError):
    """Fixed diagnostic, plus exact completed captures for caller quarantine.

    ``str(error)`` exposes only the code. Raw responses are separately available
    in ``captures``; they must be stored privately, never logged as diagnostics.
    """

    def __init__(self, code, *, captures=None, status=None, retry_after_seconds=None):
        super().__init__(code)
        self.code = code
        self.captures = [dict(capture) for capture in (captures or [])]
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.retry_after_seconds = (retry_after_seconds if type(retry_after_seconds) is int
                                    and 0 <= retry_after_seconds <= 86400 else None)


def _clock_sample(clock):
    try:
        value = clock()
        if type(value) not in (int, float, Decimal):
            raise ValueError
        value = Decimal(str(value))
        if not value.is_finite() or value < 0:
            raise ValueError
        timestamp(int(value.to_integral_value(rounding=ROUND_CEILING)))
        return value
    except Exception:
        raise DataCollectorError("collector_clock_invalid") from None


def _ceil(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _exchange_target(url):
    """Only one fixed product, five-minute candles and level-one public book."""
    try:
        parsed = urlsplit(url)
        if (type(url) is not str or len(url) > 2048 or parsed.scheme != "https"
                or parsed.netloc != "api.exchange.coinbase.com" or parsed.fragment):
            raise ValueError
        if url != BOOK_URL:
            query = parse_qs(parsed.query, strict_parsing=True)
            if (parsed.path != "/products/BTC-USD/candles"
                    or set(query) != {"start", "end", "granularity"}
                    or any(len(v) != 1 for v in query.values())
                    or query["granularity"] != ["300"]
                    or url != candle_url(parse_utc(query["start"][0]), parse_utc(query["end"][0]))):
                raise ValueError
        return parsed.netloc, parsed.path + "?" + parsed.query
    except Exception:
        raise DataCollectorError("collector_public_endpoint_refused") from None


def _alpaca_target(url):
    if url == ALPACA_QUOTES_URL:
        parsed = urlsplit(url)
        return parsed.netloc, parsed.path + "?" + parsed.query
    host, target = alpaca_target(url)
    if urlsplit(url).path not in {"/v2/calendar", "/v2/stocks/bars"}:
        raise DataCollectorError("collector_alpaca_endpoint_refused")
    return host, target


def alpaca_get(url, headers, *, timeout_seconds, max_response_bytes):
    return _bounded_get(url, headers, target_fn=_alpaca_target,
                        timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes)


def _exchange_get(url, headers, *, timeout_seconds, max_response_bytes):
    return _bounded_get(url, headers, target_fn=_exchange_target,
                        timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes)


def _bounded_get(url, headers, *, target_fn, timeout_seconds, max_response_bytes):
    host, target = target_fn(url)
    connection = http.client.HTTPSConnection(host, timeout=timeout_seconds, context=ssl.create_default_context())
    deadline = time.monotonic() + timeout_seconds
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        if response.status != 200:  # No redirect following, retries or body logging.
            retry = None
            if response.status in (429, 503):
                retry = retry_after_seconds(response.getheader("Retry-After"), now=time.time())
            raise DataCollectorError("collector_http_refused", status=response.status,
                                     retry_after_seconds=retry)
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > max_response_bytes):
            raise DataCollectorError("collector_response_size_invalid")
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DataCollectorError("collector_request_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_response_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_response_bytes:
                raise DataCollectorError("collector_response_size_invalid")
        body = b"".join(chunks)
        if time.monotonic() > deadline or (length is not None and len(body) != int(length)):
            raise DataCollectorError("collector_response_incomplete_or_timeout")
        return HTTPResponse(response.status, body)
    finally:
        connection.close()


def _number(value, *, positive=False, numeric=False):
    if ((numeric and type(value) not in (int, Decimal))
            or (not numeric and type(value) not in (str, int, Decimal))):
        raise DataCollectorError("collector_number_invalid")
    text = str(value)
    if len(text) > 96 or not _DECIMAL.fullmatch(text):
        raise DataCollectorError("collector_number_invalid")
    amount = Decimal(text)
    if (not amount.is_finite() or amount < 0 or (positive and amount <= 0)
            or len(amount.as_tuple().digits) > 60 or abs(amount.adjusted()) > 30):
        raise DataCollectorError("collector_number_invalid")
    return format(amount, "f")


def _prices(values):
    result = {key: _number(value, positive=key != "volume", numeric=True)
              for key, value in values.items()}
    o, h, low, close = (Decimal(result[key]) for key in ("open", "high", "low", "close"))
    if low > min(o, close) or h < max(o, close) or low > h:
        raise DataCollectorError("collector_ohlc_invalid")
    return result


def _record(contract, capture, test, *, kind, symbol, event, source, payload, session=None):
    equity = symbol in EQUITIES
    received = capture["received_at"]
    return {
        "contract_version": VERSION, "record_type": kind, "symbol": symbol,
        "asset_class": "equity" if equity else "crypto",
        "venue": "alpaca-iex" if equity else "coinbase-exchange", "source_id": source,
        "event_time": event, "publication_time": None,
        "available_time": received, "ingestion_time": received,
        "revision": 0, "previous_observation_hash": None,
        "raw_capture_sha256": hashlib.sha256(capture["body"]).hexdigest(),
        "acquisition_kind": "test" if test else (
            "prospective_observation" if event >= contract["created_at"] else "historical_backfill"),
        "revision_basis": "locally_observed_versions", "interval_seconds": 300 if kind == "bar" else 0,
        "session": session, "payload": payload,
    }


def _bar_payload(prices, equity):
    return {**prices, "currency": "USD", "volume_unit": "shares" if equity else "unknown",
            "price_adjustment": "unadjusted",
            "corporate_actions_status": "unknown" if equity else "not_applicable"}


def _quote_time(provider_time, receipt):
    if provider_time is None:
        return receipt["received_at"], {"timestamp_basis": "receipt_only", "provider_timestamp": None}
    try:
        if type(provider_time) is not str or not _ISO.fullmatch(provider_time):
            raise ValueError
        parsed = datetime.fromisoformat(provider_time.replace("Z", "+00:00"))
        event = timestamp(int(parsed.timestamp()))
    except (ValueError, TypeError, OverflowError):
        raise DataCollectorError("collector_quote_timestamp_invalid") from None
    return event, {"timestamp_basis": "provider", "provider_timestamp": provider_time}


def _alpaca_quotes(raw, receipt, contract, test, sessions):
    if type(raw) is not dict or set(raw) != {"quotes"} or type(raw["quotes"]) is not dict:
        raise DataCollectorError("collector_quotes_shape_invalid")
    if set(raw["quotes"]) - set(EQUITIES):
        raise DataCollectorError("collector_unrequested_quote_symbol")
    records = []
    for symbol, row in sorted(raw["quotes"].items()):
        if row is None:  # Missing data remains a missing quote, with its raw response retained.
            continue
        if type(row) is not dict or not {"bp", "ap", "bs", "as"} <= row.keys():
            raise DataCollectorError("collector_quote_shape_invalid")
        payload = {"bid": _number(row["bp"], numeric=True),
                   "ask": _number(row["ap"], numeric=True),
                   "bid_size": _number(row["bs"], numeric=True),
                   "ask_size": _number(row["as"], numeric=True),
                   "currency": "USD", "size_unit": "round_lots"}
        event, timing = _quote_time(row.get("t"), receipt)
        zero_sides = [side for side in ("bid", "ask") if Decimal(payload[side]) == 0]
        if zero_sides:
            # IEX can withdraw a side: zero price/size and a blank exchange.
            # Keep its exact raw receipt, but admit no two-sided V1 quote.
            # Other zero-price shapes remain invalid rather than being hidden.
            if all(Decimal(payload[side + "_size"]) == 0
                   and row.get("bx" if side == "bid" else "ax") in ("", " ")
                   for side in zero_sides):
                continue
            raise DataCollectorError("collector_number_invalid")
        payload.update(timing)
        session = sessions.get(datetime.fromtimestamp(event, NY).date().isoformat())
        records.append(_record(contract, receipt, test, kind="quote", symbol=symbol, event=event,
            source="alpaca-iex-quotes-latest-v2", payload=payload,
            session=None if session is None else {**session, "calendar_id": "US_EQUITY_REGULAR", "calendar_verified": False}))
    return records


def _quote(raw, capture, contract, test):
    if (type(raw) is not dict or not {"bids", "asks", "sequence"} <= raw.keys()
            or raw.keys() - {"bids", "asks", "sequence", "time", "auction_mode", "auction"}
            or type(raw["sequence"]) is not int or not 0 <= raw["sequence"] < 2**64):
        raise DataCollectorError("collector_book_shape_invalid")
    if "auction_mode" in raw and raw["auction_mode"] is not False:
        raise DataCollectorError("collector_auction_quote_unsupported")
    if raw.get("auction") is not None:
        raise DataCollectorError("collector_auction_quote_unsupported")
    payload = {"currency": "USD", "size_unit": "unknown"}
    for side, name in (("bids", "bid"), ("asks", "ask")):
        rows = raw[side]
        if (type(rows) is not list or len(rows) != 1 or type(rows[0]) is not list
                or len(rows[0]) != 3 or type(rows[0][2]) is not int or not 1 <= rows[0][2] < 2**64):
            raise DataCollectorError("collector_book_level_invalid")
        payload[name] = _number(rows[0][0], positive=True)
        payload[name + "_size"] = _number(rows[0][1], positive=True)
    event, timing = _quote_time(raw.get("time"), capture)
    payload.update(timing)
    return _record(contract, capture, test, kind="quote", symbol="BTC-USD", event=event,
                   source="coinbase-exchange-book-v1", payload=payload)


def collect_once(contract, *, provider, start, end, key_file=None, transport=None, clock=None):
    """Return records, exact captures, calendar coverage and provenance string.

    At most one calendar + four Alpaca pages + one latest-quote request, or one
    Coinbase candle page + one book GET. Errors refuse the complete bundle,
    exposing completed captures for the caller's quarantine, with no retry.
    Captures are returned even when their valid response contains zero bars.
    Subsequent invocations return fresh captures of unchanged observations;
    only the append store decides revision identity and actual ingestion time.
    """
    try:
        contract = validate_contract(contract)
        timestamp(start); timestamp(end)
        if (provider not in ("alpaca", "coinbase") or type(provider) is not str
                or start % 300 or end % 300 or not 0 < end - start <= 86400
                or (transport is not None and not callable(transport))
                or (clock is not None and not callable(clock))
                or (provider == "coinbase" and key_file is not None)):
            raise ValueError
    except Exception:
        raise DataCollectorError("collector_request_invalid") from None
    is_test = transport is not None or clock is not None
    clock_fn = time.time if clock is None else clock
    started = previous = _clock_sample(clock_fn)
    if end > int(started.to_integral_value(rounding=ROUND_FLOOR)) or contract["created_at"] > _ceil(started):
        raise DataCollectorError("collector_request_end_or_contract_is_future")
    headers = {"Accept": "application/json", "User-Agent": "ATLAS-contract-v1-data-collector/1",
               "Cache-Control": "no-cache"}
    if provider == "alpaca":
        if key_file is None and transport is None:
            raise DataCollectorError("collector_explicit_key_file_required")
        if key_file is not None:
            try:
                _external_path(key_file)
                credential = _credentials(key_file)
            except Exception:
                raise DataCollectorError("collector_credential_file_invalid") from None
            headers.update({"APCA-API-KEY-ID": credential.api_key, "APCA-API-SECRET-KEY": credential.api_secret})
    getter = transport if transport is not None else (alpaca_get if provider == "alpaca" else _exchange_get)
    captures, records, coverage = [], [], []

    def capture(role, url):
        nonlocal previous
        try:
            (_alpaca_target if provider == "alpaca" else _exchange_target)(url)
            try:
                response = getter(url, dict(headers), timeout_seconds=TIMEOUT_SECONDS, max_response_bytes=MAX_RESPONSE_BYTES)
            except DataCollectorError as exc:
                # Only our built-in transport may export its fixed error codes.
                # Injected exceptions remain untrusted even if they use this class.
                if transport is None:
                    raise
                raise DataCollectorError("collector_request_or_response_invalid",
                    status=exc.status, retry_after_seconds=exc.retry_after_seconds) from None
            except Exception:
                # A test transport may raise our own class with arbitrary text.
                # Only locally produced diagnostics may escape this boundary.
                raise DataCollectorError("collector_request_or_response_invalid") from None
            if type(response.status) is int and 100 <= response.status <= 599 and response.status != 200:
                raw_headers = getattr(response, "headers", {})
                retry = None
                if response.status in (429, 503) and type(raw_headers) is dict:
                    values = [value for key, value in raw_headers.items()
                              if type(key) is str and key.lower() == "retry-after"]
                    if len(values) == 1:
                        retry = retry_after_seconds(values[0], now=float(_clock_sample(clock_fn)))
                raise DataCollectorError("collector_http_response_invalid", status=response.status,
                                         retry_after_seconds=retry)
            if (type(response.status) is not int or response.status != 200
                    or type(response.body) is not bytes or not 0 < len(response.body) <= MAX_RESPONSE_BYTES):
                raise DataCollectorError("collector_http_response_invalid")
            observed = _clock_sample(clock_fn)
            if observed < previous or observed - started > MAX_BATCH_SECONDS:
                raise DataCollectorError("collector_clock_regressed_or_batch_timeout")
            previous = observed
            receipt = {"role": role, "request_url": url, "received_at": _ceil(observed), "body": response.body}
            captures.append(receipt)
            raw = _json(response.body, exact_numbers=True)
            return raw, receipt
        except DataCollectorError:
            raise
        except Exception:
            raise DataCollectorError("collector_request_or_response_invalid") from None

    try:
        if provider == "alpaca":
            request = CollectionWindow(start, end, EQUITIES, page_limit=PAGE_LIMIT)
            raw, receipt = capture("calendar", _calendar_url(request))
            # A weekend/holiday can truthfully return no sessions.
            calendar = _calendar(raw, request) if raw != [] else {"sessions": []}
            if any(datetime.fromtimestamp(row["open_time"], NY).weekday() > 4 for row in calendar["sessions"]):
                raise DataCollectorError("collector_weekend_calendar_session_invalid")
            sessions = {row["session_date"]: row for row in calendar["sessions"]}
            coverage.append({"start_date": datetime.fromtimestamp(start, NY).date().isoformat(),
                "end_date": datetime.fromtimestamp(end - 1, NY).date().isoformat(),
                "sessions": calendar["sessions"], "source_id": "alpaca-paper-calendar-v2",
                "raw_capture_sha256": hashlib.sha256(receipt["body"]).hexdigest(),
                "received_at": receipt["received_at"]})
            token, tokens, last_identity = None, set(), None
            for page in range(MAX_PAGES):
                raw, receipt = capture("bars", _bars_url(request, token))
                mapping, token = _page(raw, PAGE_LIMIT)
                if set(mapping) - set(EQUITIES):
                    raise DataCollectorError("collector_unrequested_symbol")
                for symbol, rows in sorted(mapping.items()):
                    for row in rows:
                        if type(row) is not dict or not {"t", "o", "h", "l", "c", "v"} <= row.keys():
                            raise DataCollectorError("collector_bar_shape_invalid")
                        opening = parse_utc(row["t"])
                        identity = (symbol, opening)
                        if last_identity is not None and identity <= last_identity:
                            raise DataCollectorError("collector_duplicate_or_nonascending_bar")
                        last_identity = identity
                        prices = _prices({key: row[old] for key, old in
                            (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v"))})
                        event = opening + 300
                        session = sessions.get(datetime.fromtimestamp(opening, NY).date().isoformat())
                        if (opening < start or event > end or session is None
                                or not session["open_time"] <= opening < event <= session["close_time"]):
                            continue
                        records.append(_record(contract, receipt, is_test, kind="bar", symbol=symbol,
                            event=event, source="alpaca-iex-bars-v2", payload=_bar_payload(prices, True),
                            session={**session, "calendar_id": "US_EQUITY_REGULAR", "calendar_verified": False}))
                if token is None:
                    break
                if token in tokens:
                    raise DataCollectorError("collector_repeated_page_token")
                tokens.add(token)
            else:
                raise DataCollectorError("collector_pagination_limit_no_partial_bundle")
            raw, receipt = capture("quotes", ALPACA_QUOTES_URL)
            records.extend(_alpaca_quotes(raw, receipt, contract, is_test, sessions))
        else:
            raw, receipt = capture("candles", candle_url(start, end))
            if type(raw) is not list or len(raw) > 300:
                raise DataCollectorError("collector_candle_page_invalid")
            seen = set()
            for row in raw:
                if type(row) is not list or len(row) != 6 or type(row[0]) is not int:
                    raise DataCollectorError("collector_candle_shape_invalid")
                opening = timestamp(row[0])
                if opening % 300 or opening in seen:
                    raise DataCollectorError("collector_candle_duplicate_or_off_grid")
                seen.add(opening)
                prices = _prices(dict(zip(("low", "high", "open", "close", "volume"), row[1:])))
                if not start <= opening < opening + 300 <= end:
                    continue
                records.append(_record(contract, receipt, is_test, kind="bar", symbol="BTC-USD",
                    event=opening + 300, source="coinbase-exchange-candles-v1", payload=_bar_payload(prices, False)))
            raw, receipt = capture("book", BOOK_URL)
            records.append(_quote(raw, receipt, contract, is_test))
        # Validation deliberately retains unknown/stale evidence as blockers.
        records = [validate_record(row, contract, _ceil(previous))["record"] for row in records]
    except DataCollectorError as exc:
        raise DataCollectorError(exc.code, captures=captures, status=exc.status,
                                 retry_after_seconds=exc.retry_after_seconds) from None
    except Exception:
        raise DataCollectorError("collector_payload_contract_invalid", captures=captures) from None
    records.sort(key=lambda row: (row["event_time"], row["symbol"], row["record_type"]))
    return {"records": records, "captures": captures, "calendar_coverage": coverage,
            "provenance": "injected_test_transport_or_clock" if is_test else
                "builtin_pinned_https_get_receipt_clock_provider_authenticity_unverified"}
