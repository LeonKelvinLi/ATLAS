"""Official Crypto REST V2 contract, with no built-in networking or credentials.

The caller supplies a transport and an Ed25519 signer. The transport must send
the exact body string and path received, preserve decimal response values, and
raise TransportFailure for known HTTP failures. No request is retried here.
Public contract reviewed 2026-09-09; see docs/robinhood-research.md.
"""

import base64
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import re
import threading
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from atlas.domain import (
    AccountSnapshot, BrokerCapabilities, BrokerError, BrokerRejected,
    OrderRequest, OrderResult, Position, Quote, UnknownSubmission, decimal, financial_context, financial_value,
)

API_ORIGIN = "https://trading.robinhood.com"
TRADING_PATH = "/api/v2/crypto/trading/"
_SYMBOL = re.compile(r"[A-Z0-9]+-USD\Z")
_ASSET = re.compile(r"[A-Z0-9]+\Z")
_STATES = {
    "open": "accepted", "pending": "accepted", "partially_filled": "partially_filled",
    "filled": "filled", "canceled": "cancelled", "failed": "rejected",
}
_TERMINAL = frozenset({"filled", "cancelled", "rejected"})


class TransportFailure(Exception):
    """HTTP evidence without response bodies, URLs, headers, or credentials."""
    def __init__(self, status_code: int):
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise ValueError("invalid HTTP status")
        self.status_code = status_code
        super().__init__("broker_http_failure")


def _uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value
    except (TypeError, ValueError, AttributeError):
        raise BrokerError("invalid_order_identifier") from None


def _body_json(value) -> str:
    """Emit Decimal values as JSON numbers, without rounding through float."""
    if isinstance(value, Decimal):
        return format(decimal(value), "f")
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key) + ":" + _body_json(item)
            for key, item in sorted(value.items())
        ) + "}"
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _timestamp(value) -> int:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("missing timestamp timezone")
    return int(parsed.timestamp())


class RobinhoodCrypto:
    capabilities = BrokerCapabilities(
        broker="robinhood_crypto", asset_classes=("crypto",), account_modes=("real",),
        official=True, network_enabled=False, server_idempotency=False,
        limitations=(
            "Injected transport only; live connectivity is unverified.",
            "Limit orders only; no documented paper endpoint.",
            "Cash, total equity, daily realized P&L, and quote liquidity are unavailable.",
            "Order lookup is bounded paginated history; UUID retention guarantees are unverified.",
        ),
    )

    def __init__(self, *, transport: Callable, signer: Callable[[bytes], bytes],
                 api_key: str, expected_account_id: str, clock: Callable[[], float],
                 max_pages: int = 20):
        if not callable(transport) or not callable(signer) or not callable(clock):
            raise ValueError("transport, signer, and clock are required")
        if not isinstance(api_key, str) or not api_key or any(c.isspace() for c in api_key):
            raise ValueError("invalid API key configuration")
        if not isinstance(expected_account_id, str) or not expected_account_id:
            raise ValueError("expected account is required")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")
        self._transport, self._signer, self._clock = transport, signer, clock
        self._api_key, self._account_id = api_key, expected_account_id
        self._max_pages = max_pages
        self._attempts: dict[str, str] = {}
        self._mutation_lock = threading.RLock()

    def __repr__(self):
        return "RobinhoodCrypto(transport=injected, credentials=redacted)"

    def _request(self, method: str, path: str, payload=None, *, mutation=False) -> dict:
        body = "" if payload is None else _body_json(payload)
        try:
            timestamp = str(int(self._clock()))
            message = f"{self._api_key}{timestamp}{path}{method}{body}".encode("utf-8")
            signature = self._signer(message)
            if not isinstance(signature, bytes) or len(signature) != 64:
                raise ValueError
            headers = {
                "x-api-key": self._api_key, "x-timestamp": timestamp,
                "x-signature": base64.b64encode(signature).decode("ascii"),
                "Content-Type": "application/json; charset=utf-8",
            }
        except Exception:
            raise BrokerError("request_signing_failed") from None
        try:
            response = self._transport(method, path, body, headers)
        except TransportFailure as exc:
            code = {
                400: "broker_validation_error", 401: "broker_unauthorized",
                403: "broker_forbidden", 404: "broker_not_found", 429: "broker_rate_limited",
            }.get(exc.status_code, "broker_http_error")
            if mutation and exc.status_code not in {400, 401, 403, 404, 429}:
                raise UnknownSubmission("broker_submission_unknown") from None
            raise (BrokerRejected(code) if mutation else BrokerError(code)) from None
        except Exception:
            raise (UnknownSubmission("broker_submission_unknown") if mutation
                   else BrokerError("broker_transport_failed")) from None
        if not isinstance(response, dict) or "errors" in response or "error" in response:
            # Without an HTTP status, an error-shaped body cannot prove rejection.
            raise (UnknownSubmission("broker_response_unknown") if mutation
                   else BrokerError("broker_response_invalid")) from None
        return response

    def _pages(self, path: str) -> list[dict]:
        origin_path = urlsplit(path).path
        required = [(k, v) for k, v in parse_qsl(urlsplit(path).query) if k == "account_number"]
        visited, rows = set(), []
        for _ in range(self._max_pages):
            if path in visited:
                raise BrokerError("pagination_cycle")
            visited.add(path)
            page = self._request("GET", path)
            batch = page.get("results")
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise BrokerError("broker_collection_invalid")
            rows.extend(batch)
            if "next" not in page:
                raise BrokerError("pagination_metadata_missing")
            next_url = page["next"]
            if next_url is None:
                return rows
            if not isinstance(next_url, str) or not next_url:
                raise BrokerError("pagination_invalid")
            parsed = urlsplit(next_url)
            if (parsed.scheme and parsed.scheme != "https") or (
                parsed.netloc and parsed.netloc != "trading.robinhood.com"
            ) or parsed.fragment or parsed.path != origin_path:
                raise BrokerError("pagination_origin_mismatch")
            query = parse_qsl(parsed.query, keep_blank_values=True)
            if required and [(k, v) for k, v in query if k == "account_number"] != required:
                raise BrokerError("pagination_account_mismatch")
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
        raise BrokerError("pagination_limit_reached")

    def _account_record(self, *, mutation=False) -> dict:
        rows = self._pages(TRADING_PATH + "accounts/")
        matches = [row for row in rows if row.get("account_number") == self._account_id]
        if len(matches) != 1:
            raise BrokerError("expected_account_unavailable")
        account = matches[0]
        if account.get("buying_power_currency") != "USD":
            raise BrokerError("account_currency_unsupported")
        if mutation and (account.get("status") != "active" or account.get("is_api_tradable") is not True):
            raise BrokerRejected("account_not_tradable")
        return account

    def _pair(self, symbol: str) -> dict:
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise BrokerError("unsupported_crypto_symbol")
        pairs = self._pages(TRADING_PATH + "trading_pairs/?" + urlencode({"symbol": symbol}))
        matches = [pair for pair in pairs if pair.get("symbol") == symbol]
        if len(matches) != 1:
            raise BrokerError("trading_pair_unavailable")
        pair = matches[0]
        if pair.get("is_api_tradable") is not True or pair.get("quote_code") != "USD":
            raise BrokerError("trading_pair_not_tradable")
        try:
            for field in ("asset_increment", "quote_increment", "max_order_size", "min_order_amount"):
                decimal(pair[field], positive=True)
        except (KeyError, ValueError):
            raise BrokerError("trading_pair_invalid") from None
        return pair

    @financial_context
    def get_quote(self, symbol: str, quantity: Decimal = Decimal("1"), side: str = "buy") -> Quote:
        pair = self._pair(symbol)
        try:
            quantity = decimal(quantity, positive=True)
            if side not in {"buy", "sell"} or quantity > decimal(pair["max_order_size"]):
                raise ValueError
        except ValueError:
            raise BrokerError("invalid_quote_request") from None
        book_side = "ask" if side == "buy" else "bid"
        query = urlencode({"symbol": symbol, "side": book_side, "quantity": format(quantity, "f")})
        response = self._request("GET", TRADING_PATH + "estimated_price/?" + query)
        try:
            rows = response["results"]
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError
            row = rows[0]
            if row["symbol"] != symbol or row["side"] != book_side or decimal(row["quantity"]) != quantity:
                raise ValueError
            bid, ask = decimal(row["bid"], positive=True), decimal(row["ask"], positive=True)
            if ask < bid:
                raise ValueError
            fee = decimal(row["est_fee"], nonnegative=True)
            timestamp = _timestamp(row["timestamp"])
            increment = decimal(pair["asset_increment"], positive=True)
            price = ask if side == "buy" else bid
            minimum = ((decimal(pair["min_order_amount"]) / price / increment)
                       .to_integral_value(rounding=ROUND_CEILING) * increment)
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
            raise BrokerError("broker_quote_invalid") from None
        return Quote(
            symbol=symbol, asset_class="crypto", bid=bid, ask=ask, timestamp=timestamp,
            market_open=None, available_quantity=None, source="robinhood_crypto_v2_estimated_price",
            fee_per_unit=financial_value(fee / quantity, rounding=ROUND_CEILING),
            quantity_increment=increment, minimum_quantity=minimum,
        )

    @financial_context
    def get_account(self) -> AccountSnapshot:
        account = self._account_record()
        try:
            buying_power = decimal(account["buying_power"], nonnegative=True)
        except (KeyError, ValueError):
            raise BrokerError("account_buying_power_invalid") from None
        positions, limitations = [], [
            "cash_unavailable", "equity_unavailable", "daily_realized_pnl_unavailable",
            "account_components_are_separate_requests",
            "position_quantity_includes_unavailable_inventory",
        ]
        valuation_times = [int(self._clock())]
        query = urlencode({"account_number": self._account_id})
        for row in self._pages(TRADING_PATH + "holdings/?" + query):
            if row.get("account_number") != self._account_id:
                raise BrokerError("holding_account_mismatch")
            try:
                asset = row["asset_code"]
                if not isinstance(asset, str) or not _ASSET.fullmatch(asset):
                    raise ValueError
                quantity = decimal(row["total_quantity"], nonnegative=True)
                available = decimal(row["quantity_available_for_trading"], nonnegative=True)
                if available > quantity:
                    raise ValueError
            except (KeyError, ValueError):
                raise BrokerError("holding_invalid") from None
            market_value = Decimal("0") if quantity == 0 else None
            if quantity:
                try:
                    quote = self.get_quote(asset + "-USD", quantity, "sell")
                    if quote.timestamp > int(self._clock()):
                        raise BrokerError("position_quote_from_future")
                    market_value = quote.bid * quantity
                    valuation_times.append(quote.timestamp)
                except BrokerError:
                    limitations.append("position_valuation_unavailable")
            positions.append(Position(asset + "-USD", quantity, market_value))
        return AccountSnapshot(
            account_id=self._account_id, mode="real", currency="USD", equity=None,
            cash=None, buying_power=buying_power, positions=tuple(positions),
            observed_at=min(valuation_times), daily_realized_pnl=None,
            tradable=account.get("status") == "active" and account.get("is_api_tradable") is True,
            limitations=tuple(dict.fromkeys(limitations)),
        )

    def _orders(self) -> list[dict]:
        rows = self._pages(TRADING_PATH + "orders/?" + urlencode({"account_number": self._account_id}))
        if any(row.get("account_number") != self._account_id for row in rows):
            raise BrokerError("order_account_mismatch")
        return rows

    def _result(self, row: dict) -> OrderResult:
        try:
            if row["account_number"] != self._account_id:
                raise ValueError
            order_id, client_id = _uuid(row["id"]), _uuid(row["client_order_id"])
            status = _STATES[row["state"]]
            filled = decimal(row["filled_asset_quantity"], nonnegative=True)
            average = None if row.get("average_price") is None else decimal(row["average_price"], positive=True)
            if filled > 0 and average is None:
                raise ValueError
            if status == "filled" and filled <= 0:
                raise ValueError
            fee = None if row.get("fee_charged") is None else decimal(row["fee_charged"], nonnegative=True)
        except (KeyError, ValueError, TypeError, BrokerError):
            raise BrokerError("broker_order_invalid") from None
        return OrderResult(order_id, client_id, status, filled, average, fee)

    def _find(self, *, broker_order_id=None, client_order_id=None) -> dict | None:
        if (broker_order_id is None) == (client_order_id is None):
            raise BrokerError("one_order_identifier_required")
        field, value = ("id", _uuid(broker_order_id)) if broker_order_id is not None else (
            "client_order_id", _uuid(client_order_id))
        matches = [row for row in self._orders() if row.get(field) == value]
        if len(matches) > 1:
            raise BrokerError("order_lookup_ambiguous")
        return matches[0] if matches else None

    def get_order_status(self, broker_order_id=None, client_order_id=None) -> OrderResult | None:
        row = self._find(broker_order_id=broker_order_id, client_order_id=client_order_id)
        return None if row is None else self._result(row)

    def get_open_orders(self) -> tuple[OrderResult, ...]:
        results = [self._result(row) for row in self._orders()]
        return tuple(result for result in results if result.status not in _TERMINAL)

    @financial_context
    def _validate_order(self, order: OrderRequest) -> dict:
        if order.account_id != self._account_id:
            raise BrokerRejected("order_account_mismatch")
        if order.asset_class != "crypto" or order.order_type != "limit" or order.side not in {"buy", "sell"}:
            raise BrokerRejected("order_type_unsupported")
        _uuid(order.client_order_id)
        pair = self._pair(order.symbol)
        try:
            quantity, price = decimal(order.quantity, positive=True), decimal(order.limit_price, positive=True)
            if (quantity % decimal(pair["asset_increment"]) or price % decimal(pair["quote_increment"])
                    or quantity > decimal(pair["max_order_size"])
                    or quantity * price < decimal(pair["min_order_amount"])):
                raise ValueError
        except (ValueError, ArithmeticError):
            raise BrokerRejected("order_size_or_price_invalid") from None
        return {
            "symbol": order.symbol, "client_order_id": order.client_order_id,
            "side": order.side, "type": "limit",
            "limit_order_config": {"asset_quantity": quantity, "limit_price": price, "time_in_force": "gtc"},
        }

    @staticmethod
    def _matches_request(row: dict, payload: dict) -> bool:
        try:
            config = row["limit_order_config"]
            return (all(row[key] == payload[key] for key in ("symbol", "client_order_id", "side", "type"))
                    and decimal(config["asset_quantity"]) == payload["limit_order_config"]["asset_quantity"]
                    and decimal(config["limit_price"]) == payload["limit_order_config"]["limit_price"])
        except (KeyError, TypeError, ValueError):
            return False

    def submit_order(self, order: OrderRequest) -> OrderResult:
        with self._mutation_lock:
            payload = self._validate_order(order)
            self._account_record(mutation=True)
            fingerprint = hashlib.sha256(_body_json(payload).encode()).hexdigest()
            previous = self._attempts.get(order.client_order_id)
            if previous is not None and previous != fingerprint:
                raise BrokerRejected("client_order_id_conflict")
            existing = self._find(client_order_id=order.client_order_id)
            if existing is not None:
                if not self._matches_request(existing, payload):
                    raise BrokerRejected("client_order_id_conflict")
                return self._result(existing)
            if previous is not None:
                raise UnknownSubmission("order_reconciliation_required")
            self._attempts[order.client_order_id] = fingerprint
            path = TRADING_PATH + "orders/?" + urlencode({"account_number": self._account_id})
            response = self._request("POST", path, payload, mutation=True)
            try:
                if not self._matches_request(response, payload):
                    raise ValueError
                result = self._result(response)
                if result.filled_quantity > payload["limit_order_config"]["asset_quantity"]:
                    raise ValueError
                return result
            except (ValueError, BrokerError):
                raise UnknownSubmission("broker_submission_response_mismatch") from None

    def cancel_order(self, broker_order_id: str) -> OrderResult:
        with self._mutation_lock:
            _uuid(broker_order_id)
            self._account_record(mutation=True)
            existing = self._find(broker_order_id=broker_order_id)
            if existing is None:
                raise BrokerRejected("order_not_found")
            current = self._result(existing)
            if current.status in _TERMINAL:
                return current
            response = self._request("POST", TRADING_PATH + f"orders/{broker_order_id}/cancel/", mutation=True)
            try:
                result = self._result(response)
                if result.broker_order_id != broker_order_id or result.client_order_id != current.client_order_id:
                    raise ValueError
                return result
            except (ValueError, BrokerError):
                raise UnknownSubmission("broker_cancel_response_unknown") from None
