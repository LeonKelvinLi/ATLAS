"""Offline Alpaca equity-paper contract fixtures; no network or credentials.

The sole transport is an in-memory response fixture. The caller's attempt map
models a durable intent journal but is not one. Production activation requires
a separately implemented journal/transport and resolution of broker readiness.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
import time
from urllib.parse import quote
from uuid import UUID

from atlas.domain import decimal, financial_context


PAPER_ORIGIN = "https://paper-api.alpaca.markets"
DATA_ORIGIN = "https://data.alpaca.markets"
ORDER_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel",
                  "filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "suspended"}
TERMINAL = {"filled", "canceled", "expired", "rejected"}
INTENT_KEYS = {"schema", "client_order_id", "account_fingerprint", "symbol", "side", "quantity",
               "limit_price", "time_in_force", "extended_hours", "reservation_usd", "fee_buffer_usd",
               "risk_reservation_id", "reviewed_at", "expires_at", "intent_sha256"}


class EquityPaperError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise EquityPaperError("equity_paper_uuid_invalid")
    return value


def _number(value, *, positive=False):
    if not isinstance(value, str):
        raise EquityPaperError("equity_paper_amount_requires_decimal_string")
    try:
        result = decimal(value, positive=positive, nonnegative=True)
        if result > Decimal("1e12") or (result and result < Decimal("1e-12")):
            raise ValueError
        return result
    except (ValueError, ArithmeticError):
        raise EquityPaperError("equity_paper_amount_invalid") from None


def _utc(value):
    if not isinstance(value, str) or not (value.endswith("Z") or value.endswith("+00:00")):
        raise EquityPaperError("equity_paper_timestamp_must_be_utc")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset().total_seconds() != 0:
        raise EquityPaperError("equity_paper_timestamp_must_be_utc")
    return parsed.timestamp()


def account_fingerprint(account_id):
    return hashlib.sha256(_uuid(account_id).encode()).hexdigest()


def intent_hash(intent):
    return hashlib.sha256(_json({k: v for k, v in intent.items() if k != "intent_sha256"}).encode()).hexdigest()


@financial_context
def normalize_intent(intent):
    """Validate reviewed terms without inferring quantity, side or capital."""
    if not isinstance(intent, dict) or set(intent) != INTENT_KEYS or type(intent["schema"]) is not int or intent["schema"] != 1:
        raise EquityPaperError("equity_paper_intent_schema_invalid")
    if (intent["intent_sha256"] != intent_hash(intent)
            or not isinstance(intent["account_fingerprint"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", intent["account_fingerprint"])):
        raise EquityPaperError("equity_paper_review_hash_invalid")
    _uuid(intent["client_order_id"])
    _uuid(intent["risk_reservation_id"])
    if not isinstance(intent["symbol"], str) or not re.fullmatch(r"[A-Z]{1,8}(?:\.[A-Z])?", intent["symbol"]):
        raise EquityPaperError("equity_paper_symbol_invalid")
    if intent["side"] not in {"buy", "sell"} or intent["time_in_force"] != "day" or intent["extended_hours"] is not False:
        raise EquityPaperError("equity_paper_only_regular_day_limit_orders")
    qty, price = _number(intent["quantity"], positive=True), _number(intent["limit_price"], positive=True)
    reservation, fee = _number(intent["reservation_usd"]), _number(intent["fee_buffer_usd"])
    if (qty % Decimal("0.000000001") or price % (Decimal(".01") if price >= 1 else Decimal(".0001"))
            or qty * price > 100 or reservation > 100 or fee > 100
            or (intent["side"] == "buy" and (qty * price < 1 or qty * price + fee > reservation))
            or (intent["side"] == "sell" and reservation != 0)):
        raise EquityPaperError("equity_paper_precision_or_reservation_invalid")
    if (any(type(intent[k]) is not int or intent[k] < 0 for k in ("reviewed_at", "expires_at"))
            or not 0 < intent["expires_at"] - intent["reviewed_at"] <= 300):
        raise EquityPaperError("equity_paper_review_window_invalid")
    return {"client_order_id": intent["client_order_id"], "symbol": intent["symbol"], "side": intent["side"],
            "qty": format(qty, "f"), "limit_price": format(price, "f"),
            "type": "limit", "time_in_force": "day", "extended_hours": False}


@dataclass(frozen=True)
class FixtureResponse:
    status: int
    body: object = None


class FixtureTransport:
    """Predetermined responses only; cannot accept a network callback."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        if any(type(r) is not FixtureResponse and not isinstance(r, Exception) for r in self.responses):
            raise EquityPaperError("equity_paper_fixture_responses_required")

    def request(self, method, url, payload=None):
        self.calls.append({"method": method, "url": url, "payload": deepcopy(payload)})
        if not self.responses:
            raise EquityPaperError("equity_paper_fixture_exhausted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


class EquityPaperAdapter:
    """Fake-only preflight/submission/reconciliation mapping, activation blocked."""

    def __init__(self, transport, *, expected_account_fingerprint, attempts, clock=time.time):
        if type(transport) is not FixtureTransport or type(attempts) is not dict:
            raise EquityPaperError("equity_paper_offline_fixtures_only")
        if not isinstance(expected_account_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_account_fingerprint):
            raise EquityPaperError("equity_paper_account_binding_required")
        self.transport, self.attempts, self.clock = transport, attempts, clock
        self.expected = expected_account_fingerprint

    def _request(self, method, path, payload=None, *, data=False):
        # All calls originate from fixed methods below; no URL supplied by a caller.
        origin = DATA_ORIGIN if data else PAPER_ORIGIN
        return self.transport.request(method, origin + path, payload)

    def _get(self, path, *, data=False):
        response = self._request("GET", path, data=data)
        if type(response.status) is not int or response.status != 200:
            raise EquityPaperError("equity_paper_fixture_read_rejected")
        return response.body

    def _intent(self, intent):
        payload = normalize_intent(intent)
        now = float(decimal(self.clock(), nonnegative=True))
        if intent["account_fingerprint"] != self.expected:
            raise EquityPaperError("equity_paper_account_mismatch")
        if not intent["reviewed_at"] <= now < intent["expires_at"]:
            raise EquityPaperError("equity_paper_review_expired_or_future")
        return payload, now

    @financial_context
    def preflight(self, intent):
        payload, started = self._intent(intent)
        account = self._get("/v2/account")
        if (account_fingerprint(account["id"]) != self.expected or account.get("status") != "ACTIVE"
                or any(account.get(k) is not False for k in ("account_blocked", "trading_blocked", "trade_suspended_by_user"))
                or str(account.get("multiplier")) != "1" or _number(account["short_market_value"]) != 0):
            raise EquityPaperError("equity_paper_account_restricted")
        cash, equity, buying_power = (_number(account[k]) for k in ("cash", "equity", "non_marginable_buying_power"))
        if cash > 100 or equity > 100 or buying_power > cash or _number(intent["reservation_usd"]) > cash:
            raise EquityPaperError("equity_paper_dedicated_100_cash_account_required")
        positions = self._get("/v2/positions")
        owned = Decimal(0)
        if not isinstance(positions, list) or len(positions) > 1:
            raise EquityPaperError("equity_paper_positions_invalid")
        for row in positions:
            if row.get("asset_class") != "us_equity" or row.get("side") != "long" or row.get("symbol") != intent["symbol"]:
                raise EquityPaperError("equity_paper_unmanaged_position")
            owned += _number(row["qty"], positive=True)
        if intent["side"] == "sell" and _number(intent["quantity"]) > owned:
            raise EquityPaperError("equity_paper_short_sale_forbidden")
        orders = self._get("/v2/orders?status=open&limit=500&direction=asc&nested=false")
        if orders != []:
            raise EquityPaperError("equity_paper_existing_orders_unresolved")
        clock = self._get("/v2/clock")
        if clock.get("is_open") is not True or not 0 <= started - _utc(clock["timestamp"]) <= 10 or _utc(clock["next_close"]) <= started:
            raise EquityPaperError("equity_paper_regular_session_closed_or_stale")
        encoded = quote(intent["symbol"], safe="")
        asset = self._get("/v2/assets/" + encoded)
        if (asset.get("symbol") != intent["symbol"] or asset.get("class") != "us_equity"
                or asset.get("status") != "active" or asset.get("tradable") is not True
                or type(asset.get("fractionable")) is not bool
                or (_number(intent["quantity"]) % 1 and not asset["fractionable"])):
            raise EquityPaperError("equity_paper_asset_or_fractional_permission_invalid")
        quote_body = self._get("/v2/stocks/" + encoded + "/quotes/latest?feed=iex", data=True)
        if quote_body.get("symbol") != intent["symbol"]:
            raise EquityPaperError("equity_paper_quote_symbol_mismatch")
        q = quote_body["quote"]
        # Quote prices from this API are JSON numbers; converting through str
        # permits finite Decimal validation without binary-float sizing.
        if any(isinstance(q.get(k), bool) for k in ("bp", "ap", "bs", "as")):
            raise EquityPaperError("equity_paper_quote_invalid")
        bid, ask = _number(str(q["bp"]), positive=True), _number(str(q["ap"]), positive=True)
        if bid >= ask or _number(str(q["bs"]), positive=True) <= 0 or _number(str(q["as"]), positive=True) <= 0:
            raise EquityPaperError("equity_paper_quote_invalid")
        now = float(decimal(self.clock(), nonnegative=True))
        if not started <= now <= started + 10 or not 0 <= now - _utc(q["t"]) <= 10 or now >= intent["expires_at"]:
            raise EquityPaperError("equity_paper_quote_or_review_stale")
        return {"payload": payload, "cash_usd": str(cash), "equity_usd": str(equity),
                "owned_quantity": str(owned), "asset_fractionable": asset["fractionable"],
                "bid": str(bid), "ask": str(ask), "observed_at": now,
                "account_fingerprint": self.expected, **self.capability()}

    @staticmethod
    def capability():
        return {"transport_provenance": "injected_test_fixture", "broker_activation_blocked": True,
                "actual_orders_submitted": 0, "live_approved": False,
                "limitations": ["Offline response fixtures only; no authenticated account or broker acceptance.",
                                "Caller attempt map models persistence but is not a production durable journal.",
                                "Existing Alpaca crypto pilot remains halted; this module cannot reset or operate it."]}

    @financial_context
    def _order(self, body, intent, previous):
        payload = normalize_intent(intent)
        if not isinstance(body, dict) or any(body.get(k) != payload[k] for k in ("client_order_id", "symbol", "side", "type", "time_in_force")):
            raise EquityPaperError("equity_paper_order_correlation_failed")
        broker_id = _uuid(body["id"])
        if (_number(body["qty"]) != _number(payload["qty"]) or _number(body["limit_price"]) != _number(payload["limit_price"])
                or body.get("status") not in ORDER_STATUSES or body.get("extended_hours") is not False
                or (previous.get("broker_order_id") is not None and previous["broker_order_id"] != broker_id)):
            raise EquityPaperError("equity_paper_order_correlation_failed")
        filled = _number(body["filled_qty"])
        if not _number(previous.get("filled_qty", "0")) <= filled <= _number(payload["qty"]):
            raise EquityPaperError("equity_paper_fill_quantity_invalid")
        average = None if body.get("filled_avg_price") is None else _number(body["filled_avg_price"], positive=True)
        if (filled > 0) != (average is not None):
            raise EquityPaperError("equity_paper_fill_price_invalid")
        if body["status"] == "filled" and filled != _number(payload["qty"]):
            raise EquityPaperError("equity_paper_filled_status_quantity_invalid")
        if average is not None and ((intent["side"] == "buy" and average > _number(payload["limit_price"]))
                                    or (intent["side"] == "sell" and average < _number(payload["limit_price"]))):
            raise EquityPaperError("equity_paper_fill_violates_limit")
        if previous.get("terminal") and (previous.get("status") != body["status"] or _number(previous["filled_qty"]) != filled
                                          or previous.get("filled_avg_price") != (str(average) if average is not None else None)):
            raise EquityPaperError("equity_paper_terminal_order_changed")
        return {"broker_order_id": broker_id, "status": body["status"], "filled_qty": str(filled),
                "filled_avg_price": str(average) if average is not None else None,
                "terminal": body["status"] in TERMINAL, "state": "terminal" if body["status"] in TERMINAL else "acknowledged"}

    def submit(self, intent, *, reservation):
        """Consume the fixture attempt before one fake POST; never auto-retry."""
        payload, _ = self._intent(intent)
        client_id = intent["client_order_id"]
        if client_id in self.attempts:
            if self.attempts[client_id]["intent_sha256"] != intent["intent_sha256"]:
                raise EquityPaperError("equity_paper_client_id_terms_changed")
            return {**deepcopy(self.attempts[client_id]), "resubmitted": False, **self.capability()}
        expected_reservation = {"reservation_id": intent["risk_reservation_id"], "client_order_id": client_id,
                                "intent_sha256": intent["intent_sha256"], "reserved_cash_usd": intent["reservation_usd"],
                                "state": "committed"}
        if reservation != expected_reservation:
            raise EquityPaperError("equity_paper_committed_reservation_required")
        self.preflight(intent)
        attempt = {"intent_sha256": intent["intent_sha256"], "client_order_id": client_id,
                   "state": "unknown", "broker_order_id": None, "filled_qty": "0", "terminal": False,
                   "post_attempts": 1}
        self.attempts[client_id] = attempt
        try:
            response = self._request("POST", "/v2/orders", payload)
            if response.status in {400, 401, 403, 422, 429}:
                attempt.update(state="http_rejected", http_status=response.status)
            elif response.status == 200:
                attempt.update(self._order(response.body, intent, attempt))
            else:
                attempt["diagnostic"] = "acceptance_unknown"
        except Exception:
            attempt["diagnostic"] = "acceptance_unknown"
        return {**deepcopy(attempt), **self.capability()}

    def reconcile(self, intent):
        normalize_intent(intent)  # Expired review may still reconcile its existing exact order.
        client_id = intent["client_order_id"]
        attempt = self.attempts.get(client_id)
        if attempt is None or attempt["intent_sha256"] != intent["intent_sha256"] or intent["account_fingerprint"] != self.expected:
            raise EquityPaperError("equity_paper_existing_exact_intent_required")
        try:
            response = self._request("GET", "/v2/orders:by_client_order_id?client_order_id=" + client_id)
            if response.status == 200:
                attempt.update(self._order(response.body, intent, attempt))
            elif response.status == 404:
                attempt["lookup_missing_is_not_proof_of_no_submission"] = True
            else:
                attempt["diagnostic"] = "reconciliation_unavailable"
        except Exception:
            attempt["diagnostic"] = "reconciliation_unavailable"
        return {**deepcopy(attempt), **self.capability()}
