"""Explicit TEST exchange for offline lifecycle and failure demonstrations.

Prices, liquidity, fills and account funding are synthetic. Orders never fill
from a price alone: a test must call ``fill``. This module owns no sockets,
credentials, files, listeners or workers and is not profitability evidence.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid5

from atlas.domain import decimal, financial_context, financial_value
from atlas.paper.broker import Response, _endpoint

_NAMESPACE = UUID("793157b4-0f91-51a0-a4c8-bb3d404cc953")
_CLOSED = frozenset({"filled", "canceled", "expired", "rejected"})
_FAULTS = frozenset({"timeout_before", "timeout_after_accept", "rate_limit", "malformed", "server_error"})


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _response(value, status=200):
    return Response(status, json.dumps(value, sort_keys=True, allow_nan=False).encode())


def _amount(value, *, positive=False):
    if not isinstance(value, str):
        raise ValueError("synthetic_amount_requires_decimal_string")
    return decimal(value, positive=positive, nonnegative=True)


class SyntheticAlpaca:
    provenance = "TEST"

    def __init__(self, now=1789484400.0):
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 < now < 32503680000:
            raise ValueError("synthetic_clock_invalid")
        self.now = float(now)
        self.account_id = str(uuid5(_NAMESPACE, "synthetic-paper-account-only"))
        self.initial_cash = "100"
        self.cash = "100"
        self.is_open = True
        self.orders = {}
        self.holdings = {}
        self.activities = []
        self.quotes = {symbol: {"bid": "9.99", "ask": "10", "bid_lots": 100, "ask_lots": 100}
                       for symbol in ("SPY", "QQQ", "GLD", "USO", "IWM", "TLT")}
        self.faults = []
        self.calls = []
        self._fill_sequence = 0

    def clock(self):
        return self.now

    def advance(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("synthetic_advance_invalid")
        if self.now + seconds >= 32503680000:
            raise ValueError("synthetic_clock_invalid")
        self.now += seconds
        return self.now

    def queue_fault(self, method, path_prefix, kind):
        if method not in {"GET", "POST", "DELETE"} or not isinstance(path_prefix, str) or not path_prefix.startswith("/v2/") or kind not in _FAULTS:
            raise ValueError("synthetic_fault_invalid")
        if kind == "timeout_after_accept" and method not in {"POST", "DELETE"}:
            raise ValueError("synthetic_mutation_fault_required")
        self.faults.append({"method": method, "path_prefix": path_prefix, "kind": kind})

    def request(self, method, url, headers, payload):
        _endpoint(method, url, payload)
        parsed = urlsplit(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        # Authentication headers deliberately never enter fixture state/logs.
        self.calls.append({"method": method, "path": path, "query": parsed.query, "payload": deepcopy(payload)})
        fault = None
        for index, candidate in enumerate(self.faults):
            if candidate["method"] == method and path.startswith(candidate["path_prefix"]):
                fault = self.faults.pop(index)["kind"]
                break
        if fault == "timeout_before":
            raise TimeoutError("synthetic_before_acceptance")
        if fault == "rate_limit":
            return Response(429, headers={"Retry-After": "2"})
        if fault == "malformed":
            return Response(200, b'{"synthetic":"malformed"')
        if fault == "server_error":
            return Response(503)
        result = self._dispatch(method, path, query, payload)
        if fault == "timeout_after_accept":
            if result.status not in {200, 204}:
                raise ValueError("synthetic_after_accept_fault_without_acceptance")
            raise TimeoutError("synthetic_accepted_response_lost")
        return result

    @financial_context
    def _buying_power(self):
        reserved = sum(((Decimal(row["qty"]) - Decimal(row["filled_qty"])) * Decimal(row["limit_price"])
                        for row in self.orders.values() if row["status"] not in _CLOSED and row["side"] == "buy"), Decimal(0))
        return max(Decimal(0), Decimal(self.cash) - reserved)

    @financial_context
    def _positions(self):
        result = []
        for symbol, holding in sorted(self.holdings.items()):
            mark = self.quotes[symbol]["bid"]
            result.append({"symbol": symbol, "asset_class": "us_equity", "side": "long",
                           "qty": holding["qty"], "market_value": str(Decimal(holding["qty"]) * Decimal(mark)),
                           "current_price": mark, "cost_basis": holding["cost_basis"]})
        return result

    @financial_context
    def _account(self):
        long_value = sum((Decimal(row["market_value"]) for row in self._positions()), Decimal(0))
        return {"id": self.account_id, "status": "ACTIVE", "currency": "USD", "cash": self.cash,
                "equity": str(Decimal(self.cash) + long_value), "long_market_value": str(long_value),
                "short_market_value": "0", "non_marginable_buying_power": str(self._buying_power()),
                "buying_power": str(self._buying_power()), "multiplier": "1", "account_blocked": False,
                "trading_blocked": False, "trade_suspended_by_user": False}

    @financial_context
    def _dispatch(self, method, path, query, payload):
        if method == "GET":
            if path == "/v2/account":
                return _response(self._account())
            if path == "/v2/positions":
                return _response(self._positions())
            if path == "/v2/clock":
                return _response({"timestamp": _stamp(self.now), "is_open": self.is_open,
                                  "next_open": _stamp(self.now + 86400), "next_close": _stamp(self.now + 3600)})
            if path.startswith("/v2/assets/"):
                symbol = path.removeprefix("/v2/assets/")
                if symbol not in self.quotes:
                    return Response(404)
                return _response({"symbol": symbol, "class": "us_equity", "status": "active",
                                  "tradable": True, "fractionable": True})
            if path == "/v2/stocks/quotes/latest":
                symbol = query["symbols"][0]
                if symbol not in self.quotes:
                    return Response(404)
                quote = self.quotes[symbol]
                return _response({"quotes": {symbol: {"bp": float(quote["bid"]), "ap": float(quote["ask"]),
                    "bs": quote["bid_lots"], "as": quote["ask_lots"], "t": _stamp(self.now)}}})
            if path == "/v2/orders:by_client_order_id":
                row = self.orders.get(query["client_order_id"][0])
                return Response(404) if row is None else _response(row)
            if path == "/v2/orders":
                rows = list(self.orders.values())
                status = query["status"][0]
                if status != "all":
                    rows = [row for row in rows if (row["status"] in _CLOSED) == (status == "closed")]
                if "after" in query:
                    boundary = datetime.fromisoformat(query["after"][0].replace("Z", "+00:00")).timestamp()
                    rows = [row for row in rows if datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp() > boundary]
                return _response(sorted(rows, key=lambda row: (row["created_at"], row["id"]))[:500])
            if path == "/v2/account/activities":
                rows = self.activities
                if "after" in query:
                    boundary = datetime.fromisoformat(query["after"][0].replace("Z", "+00:00")).timestamp()
                    rows = [row for row in rows if datetime.fromisoformat(row["transaction_time"].replace("Z", "+00:00")).timestamp() > boundary]
                if "page_token" in query:
                    ids = [row["id"] for row in rows]
                    if query["page_token"][0] not in ids:
                        return Response(400)
                    rows = rows[ids.index(query["page_token"][0]) + 1:]
                return _response(rows[:100])
        if method == "POST" and path == "/v2/orders":
            cid = payload["client_order_id"]
            if cid in self.orders:
                return _response({"code": 42210000, "message": "client_order_id must be unique"}, 422)
            symbol, qty = payload["symbol"], Decimal(payload["qty"])
            if symbol not in self.quotes or not self.is_open:
                return Response(403)
            if payload["side"] == "buy":
                if qty * Decimal(payload["limit_price"]) > self._buying_power():
                    return Response(403)
            else:
                available = Decimal(self.holdings.get(symbol, {}).get("qty", "0"))
                reserved = sum((Decimal(row["qty"]) - Decimal(row["filled_qty"]) for row in self.orders.values()
                                if row["symbol"] == symbol and row["side"] == "sell" and row["status"] not in _CLOSED), Decimal(0))
                if qty > available - reserved:
                    return Response(403)
            row = {**deepcopy(payload), "id": str(uuid5(_NAMESPACE, cid)), "asset_class": "us_equity",
                   "status": "new", "filled_qty": "0", "filled_avg_price": None, "notional": None,
                   "created_at": _stamp(self.now), "updated_at": _stamp(self.now), "replaces": None, "replaced_by": None}
            self.orders[cid] = row
            return _response(row)
        if method == "DELETE" and path.startswith("/v2/orders/"):
            broker_id = path.removeprefix("/v2/orders/")
            found = [row for row in self.orders.values() if row["id"] == broker_id]
            if not found:
                return Response(404)
            row = found[0]
            if row["status"] in _CLOSED:
                return Response(422)
            row.update(status="pending_cancel", updated_at=_stamp(self.now))
            return Response(204)
        return Response(404)

    @financial_context
    def fill(self, client_id, qty, price):
        """Apply one explicit synthetic execution, including a cancel/fill race."""
        row = self.orders.get(client_id)
        if row is None or row["status"] in _CLOSED:
            raise ValueError("synthetic_order_not_fillable")
        quantity, execution = _amount(qty, positive=True), _amount(price, positive=True)
        old_filled, target, limit = (Decimal(row[key]) for key in ("filled_qty", "qty", "limit_price"))
        if quantity > target - old_filled or (row["side"] == "buy" and execution > limit) or (row["side"] == "sell" and execution < limit):
            raise ValueError("synthetic_fill_terms_invalid")
        cost = quantity * execution
        symbol, cash = row["symbol"], Decimal(self.cash)
        holding = self.holdings.get(symbol, {"qty": "0", "cost_basis": "0"})
        held, basis = Decimal(holding["qty"]), Decimal(holding["cost_basis"])
        if row["side"] == "buy":
            if cost > cash:
                raise ValueError("synthetic_insufficient_cash")
            cash, held, basis = cash - cost, held + quantity, basis + cost
        else:
            if quantity > held:
                raise ValueError("synthetic_insufficient_inventory")
            basis = financial_value(basis * (held - quantity) / held)
            cash, held = cash + cost, held - quantity
        self.cash = str(cash)
        if held:
            self.holdings[symbol] = {"qty": str(held), "cost_basis": str(basis)}
        else:
            self.holdings.pop(symbol, None)
        filled = old_filled + quantity
        # Preserve actual execution notionals across partials; a rounded prior
        # VWAP is not an adequate ledger for the next cumulative VWAP.
        prior_notional = sum((Decimal(item["qty"]) * Decimal(item["price"]) for item in self.activities
                              if item.get("activity_type") == "FILL" and item.get("order_id") == row["id"]), Decimal(0))
        average = financial_value((prior_notional + cost) / filled)
        row.update(filled_qty=str(filled), filled_avg_price=str(average),
                   status="filled" if filled == target else "partially_filled", updated_at=_stamp(self.now))
        self._fill_sequence += 1
        self.activities.append({"id": f"synthetic-fill-{self._fill_sequence:08d}", "activity_type": "FILL",
            "transaction_time": _stamp(self.now), "order_id": row["id"], "symbol": symbol,
            "side": row["side"], "qty": str(quantity), "price": str(execution), "fee": "0"})
        return deepcopy(row)

    def finish_cancel(self, client_id):
        row = self.orders.get(client_id)
        if row is None or row["status"] not in {"pending_cancel", "partially_filled"}:
            raise ValueError("synthetic_cancellation_not_pending")
        # A partial fill may race an earlier DELETE and change the status. The
        # request receipt must still prove cancellation was requested.
        if not any(call["method"] == "DELETE" and call["path"] == "/v2/orders/" + row["id"] for call in self.calls):
            raise ValueError("synthetic_cancellation_not_requested")
        row.update(status="canceled", updated_at=_stamp(self.now))
        return deepcopy(row)

    def expire(self, client_id):
        row = self.orders.get(client_id)
        if row is None or row["status"] in _CLOSED:
            raise ValueError("synthetic_order_not_open")
        row.update(status="expired", updated_at=_stamp(self.now))
        return deepcopy(row)

    def export_state(self):
        """Return explicit TEST state for a caller-owned crash-test fixture file."""
        return deepcopy({"schema": 1, "provenance": "TEST", "now": self.now, "account_id": self.account_id,
            "initial_cash": self.initial_cash, "cash": self.cash, "is_open": self.is_open, "orders": self.orders,
            "holdings": self.holdings, "activities": self.activities, "quotes": self.quotes,
            "faults": self.faults, "calls": self.calls, "fill_sequence": self._fill_sequence})

    @classmethod
    def from_state(cls, value):
        expected = {"schema", "provenance", "now", "account_id", "initial_cash", "cash", "is_open", "orders",
                    "holdings", "activities", "quotes", "faults", "calls", "fill_sequence"}
        if (not isinstance(value, dict) or set(value) != expected or value["schema"] != 1
                or value["provenance"] != "TEST" or value["initial_cash"] != "100"):
            raise ValueError("synthetic_state_contract_invalid")
        result = cls(value["now"])
        if (value["account_id"] != result.account_id or type(value["is_open"]) is not bool
                or type(value["fill_sequence"]) is not int or value["fill_sequence"] < 0
                or any(type(value[key]) is not dict for key in ("orders", "holdings", "quotes"))
                or any(type(value[key]) is not list for key in ("activities", "faults", "calls"))):
            raise ValueError("synthetic_state_contract_invalid")
        _amount(value["cash"])
        # JSON round-trip both bounds the serialization contract and prevents
        # caller aliases from rewriting reconstructed exchange state.
        saved = json.loads(json.dumps(value, allow_nan=False))
        for name in ("cash", "is_open", "orders", "holdings", "activities", "quotes", "faults", "calls"):
            setattr(result, name, saved[name])
        result._fill_sequence = saved["fill_sequence"]
        return result
