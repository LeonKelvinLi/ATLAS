"""Bounded, standard-library-only data intake primitives.

Importing this module reads no configuration and imports no execution or
strategy implementation. Paths, JSON, calendar rows and credentials fail closed.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")
PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_THRESHOLDS = {
    "min_completed_round_trips": 30, "min_daily_observations": 60,
    "max_drawdown": .10, "min_sharpe": 1.2, "min_profit_factor": 1.25,
    "min_net_return": 0.0,
}
MAX_RETRY_AFTER_SECONDS = 86400


class CollectionInputError(ValueError):
    """A local validation diagnostic, without response or credential text."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes = field(repr=False)
    headers: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class _Credentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)


def timestamp(value, name="timestamp"):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 253402300799:
        raise CollectionInputError(f"{name} must be nonnegative integer UTC seconds")
    return value


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _json(content, *, exact_numbers=False):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CollectionInputError("duplicate_json_key")
            result[key] = value
        return result

    def finite(_):
        raise CollectionInputError("nonfinite_json_constant")

    try:
        return json.loads(content.decode("utf-8"), parse_float=Decimal if exact_numbers else float,
                          parse_constant=finite, object_pairs_hook=unique)
    except (UnicodeError, ValueError, ArithmeticError, RecursionError, AttributeError) as exc:
        if isinstance(exc, CollectionInputError):
            raise
        raise CollectionInputError("invalid_utf8_json") from None


def _external_path(value):
    if not isinstance(value, (str, Path)):
        raise CollectionInputError("explicit_external_path_required")
    original = Path(value).expanduser()
    if not original.is_absolute():
        raise CollectionInputError("external_path_must_be_absolute")
    if any(part.is_symlink() for part in (original, *original.parents)):
        raise CollectionInputError("external_path_symlink_refused")
    resolved = original.resolve()
    if resolved.is_relative_to(PROJECT):
        raise CollectionInputError("state_and_secrets_must_be_outside_repository")
    return resolved


def _read(path, *, max_bytes):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CollectionInputError("input_must_be_regular_file")
            content = stream.read(max_bytes + 1)
    except OSError:
        raise CollectionInputError("input_file_could_not_be_read") from None
    if len(content) > max_bytes:
        raise CollectionInputError("input_exceeds_maximum_size")
    return content


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _credentials(key_file):
    path = _external_path(key_file)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise CollectionInputError("credential_file_owner_mode_or_type_invalid")
            raw = stream.read(65537)
    except OSError:
        raise CollectionInputError("credential_file_unreadable") from None
    if not raw or len(raw) > 65536:
        raise CollectionInputError("credential_file_size_invalid")
    value = _json(raw)
    if type(value) is not dict or set(value) != {"api_key", "api_secret"}:
        raise CollectionInputError("credential_fields_invalid")
    for secret in value.values():
        if (type(secret) is not str or not 1 <= len(secret) <= 1024
                or not secret.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in secret)):
            raise CollectionInputError("credential_fields_invalid")
    return _Credentials(**value)


def parse_utc(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.0{1,9})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise CollectionInputError("timestamp_requires_explicit_timezone_whole_seconds")
    try:
        return timestamp(int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()))
    except (ValueError, OverflowError, OSError):
        raise CollectionInputError("timestamp_invalid") from None


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def candle_url(start, end):
    timestamp(start); timestamp(end)
    if start % 300 or end % 300 or not 0 < end - start <= 86400:
        raise CollectionInputError("public_candle_window_invalid")
    return "https://api.exchange.coinbase.com/products/BTC-USD/candles?" + urlencode({
        "granularity": "300", "start": _utc(start), "end": _utc(end)})


@dataclass(frozen=True)
class CollectionWindow:
    start: int
    end: int
    symbols: tuple[str, ...]
    page_limit: int = 1000
    interval_seconds: int = 300

    def __post_init__(self):
        timestamp(self.start); timestamp(self.end)
        if (self.start % 300 or self.end % 300 or not 0 < self.end - self.start <= 86400
                or self.symbols != ("SPY", "QQQ", "GLD", "USO")
                or type(self.interval_seconds) is not int or self.interval_seconds != 300
                or type(self.page_limit) is not int or not 1 <= self.page_limit <= 1000):
            raise CollectionInputError("collection_window_invalid")


def _calendar_url(request):
    return "https://paper-api.alpaca.markets/v2/calendar?" + urlencode({
        "start": datetime.fromtimestamp(request.start, NY).date().isoformat(),
        "end": datetime.fromtimestamp(request.end - 1, NY).date().isoformat(), "date_type": "TRADING"})


def _bars_url(request, token=None):
    query = {"symbols": ",".join(sorted(request.symbols)), "timeframe": "5Min",
             "start": _utc(request.start), "end": _utc(request.end - 1), "limit": request.page_limit,
             "adjustment": "raw", "feed": "iex", "sort": "asc", "asof": "-", "currency": "USD"}
    if token is not None:
        query["page_token"] = token
    return "https://data.alpaca.markets/v2/stocks/bars?" + urlencode(query)


def alpaca_target(url):
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
        if (type(url) is not str or parsed.scheme != "https" or parsed.fragment or len(url) > 8192
                or any(len(v) != 1 for v in query.values())):
            raise ValueError
        if parsed.netloc == "data.alpaca.markets" and parsed.path == "/v2/stocks/bars":
            required = {"symbols", "timeframe", "start", "end", "limit", "adjustment", "feed", "sort", "asof", "currency"}
            if (not required <= query.keys() or query.keys() - required - {"page_token"}
                    or query["symbols"] != ["GLD,QQQ,SPY,USO"] or query["timeframe"] != ["5Min"]
                    or query["feed"] != ["iex"] or query["adjustment"] != ["raw"]
                    or query["sort"] != ["asc"] or query["asof"] != ["-"] or query["currency"] != ["USD"]):
                raise ValueError
        elif parsed.netloc == "paper-api.alpaca.markets" and parsed.path == "/v2/calendar":
            if set(query) != {"start", "end", "date_type"} or query["date_type"] != ["TRADING"]:
                raise ValueError
        else:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CollectionInputError("collection_read_endpoint_refused") from None
    return parsed.netloc, parsed.path + "?" + parsed.query


def _calendar(raw, request):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 31:
        raise CollectionInputError("historical_calendar_invalid")
    sessions, previous = [], ""
    first = datetime.fromtimestamp(request.start, NY).date().isoformat()
    last = datetime.fromtimestamp(request.end - 1, NY).date().isoformat()
    for row in raw:
        if not isinstance(row, dict) or not {"date", "open", "close"} <= row.keys():
            raise CollectionInputError("historical_calendar_invalid")
        day = row["date"]
        try:
            if (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
                    or not first <= day <= last or day <= previous):
                raise ValueError
            bounds = []
            for key in ("open", "close"):
                value = row[key]
                if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}(?::00)?", value):
                    raise ValueError
                bounds.append(int(datetime.fromisoformat(day + "T" + value).replace(tzinfo=NY).timestamp()))
            opening, closing = bounds
            if row["open"] not in {"09:30", "09:30:00"} or row["close"] not in {"13:00", "13:00:00", "16:00", "16:00:00"}:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise CollectionInputError("historical_calendar_bounds_invalid") from None
        sessions.append({"session_date": day, "open_time": opening, "close_time": closing})
        previous = day
    return {"sessions": sessions}


def _page(raw, page_limit):
    if (not isinstance(raw, dict) or not {"bars", "next_page_token"} <= raw.keys()
            or not isinstance(raw["bars"], dict)):
        raise CollectionInputError("historical_bars_page_invalid")
    token = raw["next_page_token"]
    if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 4096
                              or not token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise CollectionInputError("historical_page_token_invalid")
    if any(not isinstance(rows, list) for rows in raw["bars"].values()):
        raise CollectionInputError("historical_bars_page_invalid")
    count = sum(len(rows) for rows in raw["bars"].values())
    if count > page_limit or (not count and token is not None):
        raise CollectionInputError("historical_page_count_invalid")
    return raw["bars"], token


def retry_after_seconds(value, *, now):
    """Sanitized Retry-After seconds or HTTP-date, capped at one day.

    A missing or malformed value returns None so the caller applies its bounded
    backoff. The response's raw header never becomes an exception or log value.
    """
    if type(value) is not str or not 1 <= len(value) <= 128:
        return None
    text = value.strip()
    try:
        if text.isascii() and text.isdigit():
            delay = int(text)
        else:
            parsed = parsedate_to_datetime(text)
            if parsed.utcoffset() is None:
                return None
            delay = max(0, math.ceil(parsed.timestamp() - now))
        return min(MAX_RETRY_AFTER_SECONDS, delay)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
