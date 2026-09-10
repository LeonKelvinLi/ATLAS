"""IQ Option MCP server.

Wraps the community-maintained (unofficial, reverse-engineered) iqoptionapi
websocket library and exposes account, market-data and trading tools over MCP.

Safety model:
  - Connects to the PRACTICE balance by default (IQ_DEFAULT_BALANCE).
  - Any order-placing/closing tool refuses to act on the REAL balance unless
    IQ_ALLOW_REAL=1 is set in the environment / .env file.

Credentials come from ATLAS_ENV_FILE, or .env in ATLAS_STATE_DIR when unset
(see .env.example). The state directory defaults to this script's directory.
"""

import difflib
import os
import threading
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from runtime_paths import ENV_FILE, load_env_file

CALL_TIMEOUT = 30  # seconds; the underlying library busy-waits forever on lost replies


def _load_env() -> None:
    load_env_file(ENV_FILE)


_load_env()

mcp = FastMCP("iqoption")

_client: Any = None


class IQError(Exception):
    pass


def _call(fn, *args, timeout: float = CALL_TIMEOUT, **kwargs):
    # The library's blocking calls can busy-wait forever if a reply is lost;
    # each call runs on a daemon thread with a hard timeout so neither the
    # request nor process shutdown can hang on a stuck call.
    box: dict = {}
    done = threading.Event()

    def runner():
        try:
            box["value"] = fn(*args, **kwargs)
        except Exception as exc:
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=runner, daemon=True).start()
    if not done.wait(timeout):
        raise IQError(
            f"IQ Option did not answer within {timeout}s. The connection may be "
            "stale - run iq_connect to reconnect and try again."
        )
    if "error" in box:
        raise box["error"]
    return box["value"]


def _get_client():
    global _client
    if _client is None:
        email = os.environ.get("IQ_EMAIL", "")
        password = os.environ.get("IQ_PASSWORD", "")
        if not email or not password:
            raise IQError(
                f"Missing credentials. Put IQ_EMAIL and IQ_PASSWORD in {ENV_FILE} "
                "(copy .env.example) and try again."
            )
        from iqoptionapi.stable_api import IQ_Option

        _client = IQ_Option(email, password)
    return _client


def _connect(sms_code: str | None = None) -> dict:
    client = _get_client()
    check, reason = _call(client.connect, sms_code or None, timeout=90)
    if not check:
        if reason == "2FA":
            raise IQError(
                "Two-factor auth: IQ Option just sent an SMS code to the account "
                "owner. Call iq_connect again with sms_code set to that code."
            )
        raise IQError(f"Login failed: {reason}")

    default_mode = os.environ.get("IQ_DEFAULT_BALANCE", "PRACTICE").upper()
    if default_mode == "REAL" and os.environ.get("IQ_ALLOW_REAL") != "1":
        default_mode = "PRACTICE"
    _call(client.change_balance, default_mode)
    return _status_dict(client)


def _ensure_connected():
    client = _get_client()
    if not client.check_connect():
        _connect()
    return client


def _status_dict(client) -> dict:
    return {
        "connected": client.check_connect(),
        "balance_mode": _call(client.get_balance_mode),
        "balance": _call(client.get_balance),
        "currency": _call(client.get_currency),
        "server_time": _call(client.get_server_timestamp),
    }


def _require_trading_allowed(client) -> None:
    mode = _call(client.get_balance_mode)
    if mode != "PRACTICE" and os.environ.get("IQ_ALLOW_REAL") != "1":
        raise IQError(
            f"Refusing to trade: the active balance is {mode} and real-money "
            "trading is disabled. Set IQ_ALLOW_REAL=1 in .env to enable it, or "
            "switch to PRACTICE with iq_switch_balance."
        )


def _resolve_asset(asset: str) -> str:
    from iqoptionapi.constants import ACTIVES

    if asset in ACTIVES:
        return asset
    upper = asset.upper()
    if upper in ACTIVES:
        return upper
    close = difflib.get_close_matches(upper, ACTIVES.keys(), n=8, cutoff=0.5)
    raise IQError(
        f"Unknown asset '{asset}'. Close matches: {close or 'none'}. "
        "Use iq_find_asset to search the full list."
    )


@mcp.tool()
def iq_connect(sms_code: str = "") -> dict:
    """Connect (or reconnect) to IQ Option. If the account has SMS two-factor
    auth, the first call triggers the SMS; call again with sms_code to finish.
    Returns account status on success."""
    return _connect(sms_code or None)


@mcp.tool()
def iq_status() -> dict:
    """Current connection status, active balance mode (PRACTICE/REAL), balance
    amount, currency and server time. Connects first if needed."""
    return _status_dict(_ensure_connected())


@mcp.tool()
def iq_switch_balance(mode: str) -> dict:
    """Switch the active balance between PRACTICE and REAL. Switching to REAL is
    allowed for viewing, but trading tools stay blocked unless IQ_ALLOW_REAL=1."""
    mode = mode.upper()
    if mode not in ("PRACTICE", "REAL"):
        raise IQError("mode must be PRACTICE or REAL")
    client = _ensure_connected()
    _call(client.change_balance, mode)
    return _status_dict(client)


@mcp.tool()
def iq_reset_practice_balance() -> dict:
    """Reset the PRACTICE (demo) balance back to its default amount."""
    client = _ensure_connected()
    result = _call(client.reset_practice_balance)
    return {"result": result.get("msg", result), "balance": _call(client.get_balance)}


@mcp.tool()
def iq_find_asset(query: str) -> list[str]:
    """Search the known asset/active names (e.g. EURUSD, EURUSD-OTC, APPLE,
    AMAZON). Case-insensitive substring match; no network call."""
    from iqoptionapi.constants import ACTIVES

    q = query.upper()
    return sorted(name for name in ACTIVES if q in name.upper())[:50]


@mcp.tool()
def iq_get_candles(asset: str, interval_seconds: int = 60, count: int = 100) -> list[dict]:
    """Historical OHLC candles for an asset, newest last. interval_seconds is
    the candle size (60 = 1m, 300 = 5m, 3600 = 1h); count is capped at 1000."""
    client = _ensure_connected()
    name = _resolve_asset(asset)
    count = max(1, min(count, 1000))
    candles = _call(client.get_candles, name, interval_seconds, count, time.time())
    return [
        {
            "from": c.get("from"),
            "to": c.get("to"),
            "open": c.get("open"),
            "high": c.get("max"),
            "low": c.get("min"),
            "close": c.get("close"),
            "volume": c.get("volume"),
        }
        for c in candles
    ]


@mcp.tool()
def iq_open_assets(market_type: str = "") -> dict:
    """Which assets are currently open for trading, grouped by market type
    (turbo, binary, digital, cfd, forex, crypto). Optionally filter to one
    market_type. Slow call (~10-30s)."""
    client = _ensure_connected()
    open_time = _call(client.get_all_open_time, timeout=90)
    result: dict[str, list[str]] = {}
    for mtype, assets in open_time.items():
        if market_type and mtype != market_type:
            continue
        result[mtype] = sorted(a for a, v in assets.items() if v.get("open"))
    return result


@mcp.tool()
def iq_payouts() -> dict:
    """Payout ratio per asset for binary/turbo options (e.g. 0.85 = 85% profit
    on a winning trade). Slow call."""
    client = _ensure_connected()
    profit = _call(client.get_all_profit, timeout=90)
    return {asset: dict(kinds) for asset, kinds in profit.items()}


@mcp.tool()
def iq_instruments(instrument_type: str) -> list[dict]:
    """List tradable instruments for margin markets: instrument_type is one of
    'cfd', 'forex', 'crypto'. Returns instrument ids to use with iq_place_order."""
    if instrument_type not in ("cfd", "forex", "crypto"):
        raise IQError("instrument_type must be cfd, forex or crypto")
    client = _ensure_connected()
    data = _call(client.get_instruments, instrument_type, timeout=60)
    instruments = data.get("instruments", []) if isinstance(data, dict) else []
    return [
        {
            "id": i.get("id"),
            "name": i.get("name"),
            "active_id": i.get("active_id"),
            "is_suspended": i.get("is_suspended"),
        }
        for i in instruments
    ]


@mcp.tool()
def iq_positions(instrument_type: str = "cfd") -> list[dict]:
    """Open positions for an instrument type ('cfd', 'forex', 'crypto',
    'digital-option', 'turbo-option', 'binary-option')."""
    client = _ensure_connected()
    check, data = _call(client.get_positions, instrument_type)
    if not check:
        raise IQError(f"Could not fetch positions for {instrument_type}")
    positions = data.get("positions", []) if isinstance(data, dict) else data
    trimmed = []
    for p in positions or []:
        trimmed.append(
            {
                "id": p.get("id"),
                "instrument_id": p.get("instrument_id"),
                "instrument_type": p.get("instrument_type"),
                "type": p.get("type"),
                "buy_amount": p.get("buy_amount"),
                "invest": p.get("invest"),
                "leverage": p.get("leverage"),
                "open_price": p.get("open_price"),
                "current_price": p.get("current_price"),
                "pnl": p.get("pnl"),
                "pnl_net": p.get("pnl_net"),
                "swap": p.get("swap"),
                "status": p.get("status"),
                "open_time": p.get("open_time"),
            }
        )
    return trimmed


@mcp.tool()
def iq_place_binary(asset: str, direction: str, amount: float, duration_minutes: int = 1) -> dict:
    """Place a binary option trade. direction is 'call' (price up) or 'put'
    (price down); amount is the stake in account currency; duration_minutes is
    the expiry (1, 5, 15...). Blocked on the REAL balance unless IQ_ALLOW_REAL=1.
    Returns the order id - pass it to iq_binary_result to learn the outcome."""
    direction = direction.lower()
    if direction not in ("call", "put"):
        raise IQError("direction must be 'call' or 'put'")
    if amount <= 0:
        raise IQError("amount must be positive")
    client = _ensure_connected()
    _require_trading_allowed(client)
    name = _resolve_asset(asset)
    check, order_id = _call(client.buy, amount, name, direction, duration_minutes, timeout=60)
    if not check:
        raise IQError(f"Order rejected: {order_id}")
    return {"order_id": order_id, "asset": name, "direction": direction,
            "amount": amount, "duration_minutes": duration_minutes,
            "balance_mode": _call(client.get_balance_mode)}


@mcp.tool()
def iq_binary_result(order_id: int, wait_seconds: int = 75) -> dict:
    """Wait for a binary option to expire and report the outcome. Blocks until
    the option closes or wait_seconds passes (set it a bit above the option's
    remaining duration). result is 'win', 'loose' or 'equal'."""
    client = _ensure_connected()
    result, profit = _call(client.check_win_v4, order_id, timeout=wait_seconds)
    return {"order_id": order_id, "result": result, "profit": profit}


@mcp.tool()
def iq_place_order(
    instrument_type: str,
    instrument_id: str,
    side: str,
    amount: float,
    leverage: int = 1,
    order_type: str = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
    stop_loss_kind: str | None = None,
    stop_loss_value: float | None = None,
    take_profit_kind: str | None = None,
    take_profit_value: float | None = None,
) -> dict:
    """Place a margin order (CFD / forex / crypto). side is 'buy' or 'sell';
    order_type is 'market', 'limit' (needs limit_price) or 'stop' (needs
    stop_price). stop_loss/take_profit kinds are 'percent', 'diff' or 'price'.
    Use iq_instruments to find instrument_id values. Blocked on the REAL
    balance unless IQ_ALLOW_REAL=1. Returns the order id."""
    if instrument_type not in ("cfd", "forex", "crypto"):
        raise IQError("instrument_type must be cfd, forex or crypto")
    side = side.lower()
    if side not in ("buy", "sell"):
        raise IQError("side must be 'buy' or 'sell'")
    if amount <= 0:
        raise IQError("amount must be positive")
    client = _ensure_connected()
    _require_trading_allowed(client)
    check, result = _call(
        client.buy_order,
        instrument_type=instrument_type,
        instrument_id=instrument_id,
        side=side,
        amount=amount,
        leverage=leverage,
        type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        stop_lose_kind=stop_loss_kind,
        stop_lose_value=stop_loss_value,
        take_profit_kind=take_profit_kind,
        take_profit_value=take_profit_value,
        timeout=60,
    )
    if not check:
        raise IQError(f"Order rejected: {result}")
    return {"order_id": result, "instrument_id": instrument_id, "side": side,
            "amount": amount, "leverage": leverage,
            "balance_mode": _call(client.get_balance_mode)}


@mcp.tool()
def iq_close_position(order_id: int) -> dict:
    """Close an open margin position by the order id returned from
    iq_place_order (for positions listed by iq_positions, use their 'id').
    Blocked on the REAL balance unless IQ_ALLOW_REAL=1."""
    client = _ensure_connected()
    _require_trading_allowed(client)
    ok = _call(client.close_position, order_id, timeout=60)
    if not ok:
        raise IQError(f"Could not close position for order {order_id}")
    return {"closed": True, "order_id": order_id, "balance": _call(client.get_balance)}


@mcp.tool()
def iq_cancel_order(order_id: int) -> dict:
    """Cancel a pending (not yet filled) margin order."""
    client = _ensure_connected()
    _require_trading_allowed(client)
    _call(client.cancel_order, order_id, timeout=60)
    return {"cancelled": True, "order_id": order_id}


if __name__ == "__main__":
    mcp.run()
