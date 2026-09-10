"""One deterministic gateway for ATLAS proposals, execution and reconciliation."""

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid

from atlas.approval import ApprovalError, verify_approval
from atlas.domain import (BrokerError, BrokerRejected, OrderRequest, OrderResult,
                          UnknownSubmission, decimal, financial_context, json_value)
from atlas.evidence import EvidenceStore, external_context
from atlas.ledger import ConflictError, Ledger, digest
from atlas.risk import (RiskPolicy, PortfolioRiskPolicy, account_fingerprint, evaluate_risk,
                        evaluate_portfolio_risk, position_size)
from atlas.strategies import StrategyRegistry


class ServiceError(ValueError):
    pass


class StopRequested(ServiceError):
    pass


class AtlasService:
    def __init__(self, broker, ledger: Ledger, policy: RiskPolicy, *, datasets=None,
                 evidence: EvidenceStore | None = None, registry=None, approval_key=None,
                 kill_switch_path: Path | None = None, clock=time.time,
                 stop_event=None, call_timeout=15.0, shutdown_grace=0.2,
                 portfolio_policy: PortfolioRiskPolicy | None = None, portfolio_context_provider=None):
        scope = "live" if policy.mode.startswith("live-") else policy.mode
        if ledger.scope != scope:
            raise ServiceError("execution journal scope does not match mode")
        self.broker, self.ledger, self.policy = broker, ledger, policy
        if portfolio_policy is not None and not isinstance(portfolio_policy, PortfolioRiskPolicy):
            raise ValueError("portfolio policy requires a trusted deterministic policy")
        if portfolio_context_provider is not None and (portfolio_policy is None or not callable(portfolio_context_provider)):
            raise ValueError("trusted portfolio provider requires an explicit portfolio policy")
        self.portfolio_policy, self.portfolio_context_provider = portfolio_policy, portfolio_context_provider
        self.datasets = deepcopy(datasets or {})
        self.evidence = evidence
        self.registry = registry or StrategyRegistry()
        self._approval_key = approval_key
        self.kill_switch_path = kill_switch_path
        self.clock = clock
        self.stop = stop_event or threading.Event()
        self.call_timeout = float(decimal(call_timeout, positive=True))
        self.shutdown_grace = float(decimal(shutdown_grace, nonnegative=True))
        if not math.isfinite(self.call_timeout) or not 0 < self.call_timeout <= 120:
            raise ValueError("broker call timeout must be finite and at most 120 seconds")
        if not math.isfinite(self.shutdown_grace) or not 0 <= self.shutdown_grace <= 10:
            raise ValueError("shutdown acknowledgement grace must be finite and at most 10 seconds")
        self._signals = {}
        self._current_signals = {}
        self._lock = threading.RLock()
        self._mutations = set()

    def _call(self, fn, *args, mutation=False, **kwargs):
        if self.stop.is_set():
            raise StopRequested("shutdown requested")
        result, done = {}, threading.Event()

        def worker():
            try:
                if self.stop.is_set():
                    raise StopRequested("shutdown before broker call")
                result["started"] = True
                result["value"] = fn(*args, **kwargs)
            except BaseException as exc:
                result["error"] = exc
            finally:
                done.set()

        if mutation:
            self._mutations.add(done)
        threading.Thread(target=worker, daemon=True).start()
        deadline = time.monotonic() + self.call_timeout
        shutdown_deadline = None
        while not done.wait(0.01):
            now = time.monotonic()
            if self.stop.is_set() and shutdown_deadline is None:
                shutdown_deadline = now + (self.shutdown_grace if mutation else 0)
            if now >= deadline or (shutdown_deadline is not None and now >= shutdown_deadline):
                if mutation:
                    raise UnknownSubmission("broker acknowledgement unavailable; call was not cancelled")
                raise BrokerError("broker_read_unavailable")
        if "error" in result:
            if mutation and result.get("started") and isinstance(result["error"], StopRequested):
                raise UnknownSubmission("broker call stopped after it started")
            raise result["error"]
        return result["value"]

    def _assert_no_inflight_mutation(self):
        self._mutations = {done for done in self._mutations if not done.is_set()}
        if self._mutations:
            raise ServiceError("a previous broker mutation is still in flight; wait for reconciliation")

    def _kill_active(self):
        if self.stop.is_set():
            return True
        if self.kill_switch_path is None:
            return False
        try:
            self.kill_switch_path.lstat()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return True

    def _policy_fingerprint(self):
        if self.portfolio_policy is None:
            return self.policy.fingerprint()
        return digest({"order_policy": self.policy.fingerprint(), "portfolio_policy": self.portfolio_policy.fingerprint()})

    @financial_context
    def _portfolio_risk(self, signal, account, quote, quantity, limit_price, fingerprint):
        snapshot = None
        now = int(self.clock())
        if self.portfolio_context_provider is not None:
            try:
                snapshot = self._call(self.portfolio_context_provider, signal=deepcopy(signal), account=account,
                                      quote=quote, quantity=quantity, limit_price=limit_price, now=now)
            except Exception:  # Provider errors cannot create permission or leak provider messages.
                snapshot = None
        matches = False
        try:
            # Bind trusted historical risk to the account/quote refreshed for this exact proposal.
            expected_positions = {p.symbol: (p.quantity, p.market_value) for p in account.positions if p.quantity != 0}
            actual_positions = {p["symbol"]: (decimal(p["quantity"], nonnegative=True), decimal(p["market_value"], nonnegative=True))
                                for p in snapshot["positions"] if decimal(p["quantity"], nonnegative=True) != 0}
            matches = (decimal(snapshot["equity"], positive=True) == account.equity
                       and len(expected_positions) == sum(p.quantity != 0 for p in account.positions)
                       and len(actual_positions) == sum(decimal(p["quantity"], nonnegative=True) != 0 for p in snapshot["positions"])
                       and actual_positions == expected_positions and decimal(snapshot["order_mark_price"], positive=True) == quote.bid)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            pass
        report = evaluate_portfolio_risk(self.portfolio_policy, snapshot, now=int(self.clock()),
                          expected_account_fingerprint=fingerprint, symbol=signal["symbol"], asset_class=signal["asset_class"],
                          strategy_id=signal["strategy_id"], side=signal["direction"], quantity=quantity,
                          limit_price=limit_price, estimated_fees=quantity*quote.fee_per_unit)
        report["checks"].append({"code": "portfolio_account_quote_binding", "passed": matches,
                                 "detail": "portfolio equity, positions and order mark must match refreshed account and quote"})
        report["passed"] = all(c["passed"] for c in report["checks"])
        return report

    def _account(self):
        account = self._call(self.broker.get_account)
        if account.account_id != self.policy.expected_account_id:
            raise ServiceError("expected account identity does not match broker")
        expected_mode = "real" if self.policy.mode.startswith("live-") else "paper"
        if account.mode != expected_mode:
            raise ServiceError("account type does not match execution mode")
        return account

    def account_summary(self):
        with self._lock:
            account = self._account()
            result = {k: v for k, v in json_value(account).items() if k != "account_id"}
            result["account_fingerprint"] = account_fingerprint(self.broker.capabilities.broker, account.account_id)
            result["broker"] = self.broker.capabilities.broker
            return result

    def market_quote(self, symbol, quantity="1", side="buy"):
        return json_value(self._call(self.broker.get_quote, symbol, decimal(quantity, positive=True), side))

    def analyze(self, symbol, research=None):
        self._current_signals = {key: value for key, value in self._current_signals.items() if key[0] != symbol}
        if symbol not in self.datasets:
            return {"symbol": symbol, "available": False, "signals": [],
                    "reason": "no configured verified dataset for this symbol",
                    "model_probability": None, "confidence": None, "trade_submitted": False}
        dataset = self.datasets[symbol]
        if str(dataset.get("role", "")).lower() in {"holdout", "sealed_holdout"} or str(dataset.get("evidence_kind", "")).lower() in {"holdout", "sealed_holdout"}:
            raise ServiceError("protected holdout data cannot generate ordinary research or trading signals")
        research_store = getattr(self, "research_store", None)
        if research_store is not None:
            from atlas.research.store import content_hash
            # This compatibility signal path consumes legacy bar dictionaries,
            # which cannot prove canonical observation disjointness. An active
            # seal therefore blocks it; canonical research uses the lab guard.
            research_store.assert_development_access(content_hash(dataset["bars"]), content_hash({"symbol": symbol}))
        analysis = self.registry.analyze(symbol, dataset["asset_class"], dataset["bars"], now=int(self.clock()))
        for signal in analysis["signals"]:
            signal["data_kind"] = dataset.get("evidence_kind", "unverified")
            signal["data_source"] = dataset.get("source", "unspecified")
            self._signals[signal["signal_id"]] = deepcopy(signal)
            self._current_signals[(symbol, signal["strategy_id"])] = signal["signal_id"]
        return {**analysis, "available": True, "data_kind": dataset.get("evidence_kind", "unverified"),
                "external_context": external_context(research or []), "research_adjustment": None,
                "portfolio_adjustment": None, "trade_submitted": False}

    def signals(self):
        return deepcopy([self._signals[key] for key in self._current_signals.values()])

    def _signal(self, signal_id):
        if signal_id not in self._signals:
            # Restart recovery can recompute evidence from configured data;
            # an immutable proposal alone is not a trusted signal source.
            for symbol in self.datasets:
                self.analyze(symbol)
        if signal_id not in self._signals:
            raise ServiceError("unknown server signal; analyze configured data first")
        signal = deepcopy(self._signals[signal_id])
        self.analyze(signal["symbol"])
        if self._current_signals.get((signal["symbol"], signal["strategy_id"])) != signal_id:
            raise ServiceError("signal was invalidated by new data or strategy configuration")
        enabled = {s["strategy_id"] for s in self.registry.list_strategies() if s["enabled"]}
        if signal["strategy_id"] not in enabled:
            raise ServiceError("strategy is disabled")
        return deepcopy(self._signals[signal_id])

    def _risk(self, signal, quantity, limit_price, *, exclude_proposal_id=None, reference_price=None):
        account = self._account()
        quote = self._call(self.broker.get_quote, signal["symbol"], quantity, signal["direction"])
        opens = self._call(self.broker.get_open_orders)
        account = self._account()
        fingerprint = account_fingerprint(self.broker.capabilities.broker, account.account_id)
        stats = self.ledger.daily_stats(fingerprint, int(self.clock()))
        if exclude_proposal_id is not None:
            # Our committed attempt is included; the budget was defined as
            # attempts before the next order. Competing committed attempts
            # remain counted, including rejected attempts.
            stats = {**stats, "intents_count": max(0, stats["intents_count"] - 1)}
        portfolio = (self._portfolio_risk(signal, account, quote, quantity, limit_price, fingerprint)
                     if self.portfolio_policy is not None else None)
        report = evaluate_risk(self.policy, account, quote, symbol=signal["symbol"],
                               asset_class=signal["asset_class"], side=signal["direction"],
                               quantity=quantity, limit_price=limit_price, strategy=signal,
                               now=int(self.clock()), daily_stats=stats,
                               pending_intents=self.ledger.has_pending(fingerprint, exclude_proposal_id),
                               broker_open_orders=len(opens), kill_switch=self._kill_active(),
                               reference_price=reference_price)
        caps = self.broker.capabilities
        capability_ok = signal["asset_class"] in caps.asset_classes and "limit" in caps.order_types
        report["checks"].append({"code": "broker_capability", "passed": capability_ok,
                                 "detail": "adapter must support this asset class and limit orders"})
        if self.policy.mode.startswith("live-") and signal.get("data_kind") != "verified_live":
            report["checks"].append({"code": "evidence_provenance", "passed": False,
                                     "detail": "synthetic, historical and unverified data cannot authorize live execution"})
        if portfolio is not None:
            report["portfolio_risk"] = portfolio
            report["checks"].extend(portfolio["checks"])
        report["policy_hash"] = self._policy_fingerprint()
        report["passed"] = all(c["passed"] for c in report["checks"])
        return account, quote, report

    def check_trade_risk(self, signal_id, quantity):
        with self._lock:
            signal = self._signal(signal_id)
            qty = decimal(quantity, positive=True)
            quote = self._call(self.broker.get_quote, signal["symbol"], qty, signal["direction"])
            price = quote.ask if signal["direction"] == "buy" else quote.bid
            _, _, risk = self._risk(signal, qty, price)
            return {"signal_id": signal_id, "quantity": str(qty), "limit_price": str(price), "risk": risk,
                    "trade_submitted": False}

    def calculate_position_size(self, symbol):
        with self._lock:
            return position_size(self.policy, self._account(), self._call(self.broker.get_quote, symbol))

    def create_trade_proposal(self, signal_id, quantity, idempotency_key, *, caller="mcp:create_trade_proposal"):
        with self._lock:
            qty = decimal(quantity, positive=True)
            existing = self.ledger.get_proposal_by_idempotency_key(idempotency_key)
            if existing is not None:
                if existing["signal_id"] != signal_id or existing["order"].get("quantity") != str(qty):
                    raise ConflictError("idempotency key cannot change proposal terms")
                return self._proposal_view(existing)
            signal = self._signal(signal_id)
            quote = self._call(self.broker.get_quote, signal["symbol"], qty, signal["direction"])
            price = quote.ask if signal["direction"] == "buy" else quote.bid
            account, quote, risk = self._risk(signal, qty, price)
            now = int(self.clock())
            payload = {
                "id": str(uuid.uuid4()), "kind": "order", "signal_id": signal_id,
                "account_fingerprint": account_fingerprint(self.broker.capabilities.broker, account.account_id),
                "broker": self.broker.capabilities.broker, "mode": self.policy.mode,
                "account_currency": account.currency,
                "strategy_id": signal["strategy_id"], "strategy_version": signal["version"],
                "order": {"symbol": signal["symbol"], "asset_class": signal["asset_class"],
                          "side": signal["direction"], "quantity": str(qty), "order_type": "limit",
                          "limit_price": str(price)},
                "created_at": now, "expires_at": now + self.policy.proposal_ttl_seconds,
                "policy_hash": self._policy_fingerprint(), "quote": json_value(quote),
                "evidence": signal, "risk": risk,
                "status": "risk_rejected" if not risk["passed"] else
                          "awaiting_approval" if self.policy.mode == "live-assisted" else "prepared",
            }
            proposal = self.ledger.save_proposal(payload, idempotency_key)
            self.ledger.record_event("proposal_risk_checked", {"risk": risk}, now=now, caller=caller,
                                     proposal_id=proposal["id"])
            return self._proposal_view(proposal)

    @staticmethod
    def _proposal_view(proposal):
        return {"proposal": proposal, "proposal_hash": digest(proposal), "trade_submitted": False}

    def _verify_proposal(self, proposal, receipt):
        now = int(self.clock())
        if proposal["mode"] != self.policy.mode or proposal["policy_hash"] != self._policy_fingerprint():
            raise ServiceError("proposal no longer matches the configured mode or policy")
        if not proposal["created_at"] <= now < proposal["expires_at"]:
            raise ServiceError("proposal has expired or is not yet valid")
        if proposal.get("kind") == "order" and not proposal["risk"]["passed"]:
            raise ServiceError("proposal was risk-rejected; create a new proposal after resolving the checks")
        if self.policy.mode == "research":
            raise ServiceError("research mode cannot execute")
        if self.policy.mode.startswith("live-"):
            if not self.policy.live_enabled:
                raise ServiceError("live execution is disabled")
            if self.policy.mode == "live-auto" and not self.policy.auto_enabled:
                raise ServiceError("automatic live execution is disabled")
            if self.policy.mode == "live-assisted":
                claims = verify_approval(receipt, proposal, self._approval_key, now=now)
                self.ledger.record_event("approval_verified", {"proposal_hash": claims["proposal_hash"],
                                         "expires_at": claims["expires_at"]}, now=now,
                                         caller="execution-policy", proposal_id=proposal["id"])

    @staticmethod
    def _validated_result(result, intent, order):
        if not isinstance(result, OrderResult) or result.client_order_id != intent["client_order_id"]:
            raise UnknownSubmission("broker response correlation is unverified")
        if result.status not in {"accepted", "partially_filled", "filled", "cancelled", "rejected"}:
            raise UnknownSubmission("broker response state is unverified")
        if result.status != "rejected" and (not isinstance(result.broker_order_id, str) or not result.broker_order_id.strip()):
            raise UnknownSubmission("broker order identity is missing")
        if intent.get("broker_order_id") and result.broker_order_id != intent["broker_order_id"]:
            raise UnknownSubmission("broker order identity changed")
        filled = decimal(result.filled_quantity, nonnegative=True)
        previous_filled = decimal((intent.get("details") or {}).get("filled_quantity", "0"), nonnegative=True)
        if filled < previous_filled:
            raise UnknownSubmission("broker cumulative fill quantity decreased")
        requested = decimal(order["quantity"], positive=True)
        if filled > requested:
            raise UnknownSubmission("broker fill quantity exceeds intent")
        if result.status in {"accepted", "rejected"} and filled != 0:
            raise UnknownSubmission("broker status contradicts its fill quantity")
        if result.status == "partially_filled" and not 0 < filled < requested:
            raise UnknownSubmission("partial fill quantity is inconsistent")
        if result.status == "filled" and filled != decimal(order["quantity"]):
            raise UnknownSubmission("completed broker quantity is inconsistent")
        if filled > 0:
            price = decimal(result.average_fill_price, positive=True)
            limit = decimal(order["limit_price"], positive=True)
            if (order["side"] == "buy" and price > limit) or (order["side"] == "sell" and price < limit):
                raise UnknownSubmission("broker fill violates the approved limit price")
        if result.fees is not None:
            decimal(result.fees, nonnegative=True)
        if result.realized_pnl is not None:
            decimal(result.realized_pnl)
        return result

    def _complete(self, proposal, intent, result):
        result = self._validated_result(result, intent, proposal["order"])
        status = "accepted" if result.status == "partially_filled" else result.status
        return self.ledger.complete_intent(proposal["id"], status, result.broker_order_id,
                                           json_value(result), now=int(self.clock()))

    def execute_trade_proposal(self, proposal_id, receipt=None, *, caller="mcp:execute_trade_proposal"):
        with self._lock:
            existing = self.ledger.get_intent(proposal_id)
            if existing is not None:
                return {"intent": existing, "replayed": True, "new_submission": False}
            self._assert_no_inflight_mutation()
            proposal = self.ledger.get_proposal(proposal_id)
            if proposal.get("kind") != "order":
                raise ServiceError("use the cancellation operation for cancellation proposals")
            self._verify_proposal(proposal, receipt)
            if self._kill_active():
                raise StopRequested("kill switch or shutdown is active")
            signal = self._signal(proposal["signal_id"])
            identity = lambda value: {k: v for k, v in value.items() if k != "computed_at"}
            if identity(signal) != identity(proposal["evidence"]):
                raise ServiceError("proposal evidence no longer matches the server signal")
            terms = proposal["order"]
            intent = self.ledger.claim_intent(proposal_id, int(self.clock()), caller)
            if intent["replayed"]:
                return {"intent": intent, "replayed": True, "new_submission": False}
            # The committed account claim serializes competing processes.
            # Re-read balances, prices, budgets and approval AFTER the claim.
            try:
                account, quote, risk = self._risk(signal, decimal(terms["quantity"]), decimal(terms["limit_price"]),
                                               exclude_proposal_id=proposal_id,
                                               reference_price=proposal["quote"]["ask" if terms["side"] == "buy" else "bid"])
                self._verify_proposal(proposal, receipt)
                if int(self.clock()) // 86400 != intent["created_at"] // 86400:
                    raise ServiceError("submission crossed the UTC daily-risk boundary")
                if account_fingerprint(self.broker.capabilities.broker, account.account_id) != proposal["account_fingerprint"]:
                    raise ServiceError("account identity changed after proposal")
                if account.currency != proposal["account_currency"] or quote.currency != proposal["quote"]["currency"]:
                    raise ServiceError("currency changed after proposal")
                if not risk["passed"]:
                    self.ledger.record_event("execution_risk_rejected", {"risk": risk}, now=int(self.clock()),
                                             caller=caller, proposal_id=proposal_id)
                    raise ServiceError("fresh risk checks rejected the order")
                if self._kill_active():
                    raise StopRequested("shutdown before submission")
            except Exception as exc:
                rejected = self.ledger.complete_intent(proposal_id, "rejected", details={
                    "submitted": False, "reason_code": type(exc).__name__}, now=int(self.clock()))
                return {"intent": rejected, "new_submission": False}
            order = OrderRequest(account.account_id, terms["symbol"], terms["asset_class"], terms["side"],
                                 decimal(terms["quantity"]), decimal(terms["limit_price"]), intent["client_order_id"])
            try:
                result = self._call(self.broker.submit_order, order, mutation=True)
            except (BrokerRejected, StopRequested) as exc:
                # StopRequested only leaves _call when its worker did not start.
                rejected = self.ledger.complete_intent(proposal_id, "rejected", details={
                    "submitted": False, "reason_code": type(exc).__name__}, now=int(self.clock()))
                return {"intent": rejected, "new_submission": False}
            except Exception as exc:
                uncertain = self.ledger.complete_intent(proposal_id, "unknown", details={
                    "reason_code": type(exc).__name__, "requires_reconciliation": True}, now=int(self.clock()))
                return {"intent": uncertain, "new_submission": True}
            try:
                completed = self._complete(proposal, intent, result)
            except (ValueError, BrokerError) as exc:
                completed = self.ledger.complete_intent(proposal_id, "unknown", details={
                    "reason_code": type(exc).__name__, "requires_reconciliation": True}, now=int(self.clock()))
            return {"intent": completed, "new_submission": True}

    def reconcile(self, proposal_id, *, caller="mcp:reconcile_order"):
        with self._lock:
            self._assert_no_inflight_mutation()
            proposal = self.ledger.get_proposal(proposal_id)
            intent = self.ledger.get_intent(proposal_id)
            if intent is None:
                raise ServiceError("proposal has no submitted intent")
            if intent["status"] in {"filled", "rejected", "cancelled"}:
                return {"intent": intent, "replayed": True}
            if proposal.get("kind") == "cancel":
                target = self.ledger.get_proposal(proposal["order"]["target_proposal_id"])
                target_intent = self.ledger.get_intent(target["id"])
                account = self._account()
                if account_fingerprint(self.broker.capabilities.broker, account.account_id) != proposal["account_fingerprint"]:
                    raise ServiceError("reconciliation account mismatch")
                result = self._call(self.broker.get_order_status, broker_order_id=target_intent["broker_order_id"])
                self._account()
                if result is None or result.status in {"accepted", "partially_filled"}:
                    return {"intent": intent, "reconciled": False,
                            "reason": "cancellation is not confirmed terminal; do not retry"}
                self._validated_result(result, target_intent, target["order"])
                if result.status not in {"filled", "cancelled"}:
                    return {"intent": intent, "reconciled": False, "reason": "target terminal outcome is not established"}
                return self.ledger.complete_cancellation(proposal_id, result.status, json_value(result), now=int(self.clock()))
            account = self._account()
            if account_fingerprint(self.broker.capabilities.broker, account.account_id) != proposal["account_fingerprint"]:
                raise ServiceError("reconciliation account mismatch")
            result = self._call(self.broker.get_order_status, client_order_id=intent["client_order_id"])
            self._account()  # Detect account changes during history access.
            if result is None:
                self.ledger.record_event("reconciliation_not_found", {"still_blocked": True},
                                         now=int(self.clock()), caller=caller, proposal_id=proposal_id)
                return {"intent": intent, "reconciled": False, "reason": "not found does not prove non-submission"}
            completed = self._complete(proposal, intent, result)
            return {"intent": completed, "reconciled": completed["status"] in {"filled", "rejected", "cancelled"}}

    def get_trade_proposal(self, proposal_id):
        return self._proposal_view(self.ledger.get_proposal(proposal_id))

    def create_cancellation_proposal(self, target_proposal_id, idempotency_key):
        with self._lock:
            existing = self.ledger.get_proposal_by_idempotency_key(idempotency_key)
            if existing is not None:
                if existing.get("kind") != "cancel" or existing["order"]["target_proposal_id"] != target_proposal_id:
                    raise ConflictError("idempotency key cannot change cancellation target")
                return self._proposal_view(existing)
            target = self.ledger.get_proposal(target_proposal_id)
            intent = self.ledger.get_intent(target_proposal_id)
            if intent is None or intent["status"] != "accepted" or not intent["broker_order_id"]:
                raise ServiceError("cancel requires a known open order; reconcile uncertain submissions first")
            account = self._account()
            fingerprint = account_fingerprint(self.broker.capabilities.broker, account.account_id)
            if target["account_fingerprint"] != fingerprint or not self.broker.capabilities.cancellation:
                raise ServiceError("cancellation account or adapter capability mismatch")
            now = int(self.clock())
            proposal = {"id": str(uuid.uuid4()), "kind": "cancel", "signal_id": f"cancel:{target_proposal_id}",
                        "account_fingerprint": fingerprint, "mode": self.policy.mode,
                        "broker": self.broker.capabilities.broker, "created_at": now,
                        "expires_at": now + self.policy.proposal_ttl_seconds,
                        "policy_hash": self._policy_fingerprint(), "strategy_id": target["strategy_id"],
                        "strategy_version": target["strategy_version"],
                        "order": {"target_proposal_id": target_proposal_id,
                                  "broker_order_id": intent["broker_order_id"], "symbol": target["order"]["symbol"]},
                        "evidence": {"target_proposal_hash": digest(target), "observed_state": intent["status"]},
                        "status": "awaiting_approval" if self.policy.mode == "live-assisted" else "prepared"}
            return self._proposal_view(self.ledger.save_proposal(proposal, idempotency_key))

    def cancel_order(self, proposal_id, receipt=None, *, caller="mcp:cancel_order"):
        with self._lock:
            existing = self.ledger.get_intent(proposal_id)
            if existing is not None:
                return {"intent": existing, "replayed": True, "new_submission": False}
            self._assert_no_inflight_mutation()
            proposal = self.ledger.get_proposal(proposal_id)
            if proposal.get("kind") != "cancel":
                raise ServiceError("cancellation requires its own immutable proposal")
            self._verify_proposal(proposal, receipt)
            account = self._account()
            if account_fingerprint(self.broker.capabilities.broker, account.account_id) != proposal["account_fingerprint"]:
                raise ServiceError("cancellation account mismatch")
            target = self.ledger.get_proposal(proposal["order"]["target_proposal_id"])
            target_intent = self.ledger.get_intent(target["id"])
            child = self.ledger.claim_cancellation(proposal_id, target["id"], int(self.clock()), caller)
            if child["replayed"]:
                return {"intent": child, "replayed": True, "new_submission": False}
            try:
                self._verify_proposal(proposal, receipt)
                self._account()
                result = self._call(self.broker.cancel_order, target_intent["broker_order_id"], mutation=True)
                self._validated_result(result, target_intent, target["order"])
                # An accepted/partially-filled order after requesting cancel
                # is not proof that an asynchronous cancellation has finished.
                outcome = result.status if result.status in {"filled", "cancelled"} else "unknown"
                completed = self.ledger.complete_cancellation(proposal_id, outcome, json_value(result), now=int(self.clock()))
            except Exception as exc:
                completed = self.ledger.complete_cancellation(proposal_id, "unknown", {
                    "reason_code": type(exc).__name__, "requires_reconciliation": True}, now=int(self.clock()))
            return {**completed, "new_submission": True}

    def portfolio_risk(self):
        summary = self.account_summary()
        fingerprint = summary["account_fingerprint"]
        return {"account": summary, "policy": self.policy.public(),
                "pending_reconciliation": self.ledger.has_pending(fingerprint),
                "daily": self.ledger.daily_stats(fingerprint, int(self.clock())),
                "kill_switch_active": self._kill_active(), "trade_submitted": False}

    @financial_context
    def performance(self):
        from atlas.analytics import performance_report

        intents = self.ledger.list_intents(1000)
        # Count known realized settlement P&L once. Open inventory and missing
        # P&L do not become winning trades or zero returns.
        records, by_strategy, execution_fees = [], {}, []
        for intent in intents:
            proposal = self.ledger.get_proposal(intent["proposal_id"])
            if proposal.get("kind") != "cancel":
                try:
                    execution_fees.append(decimal(intent["details"].get("fees"), nonnegative=True))
                except ValueError:
                    execution_fees.append(None)
            if (proposal.get("kind") == "cancel" or intent["status"] not in {"filled", "cancelled"}
                    or proposal["order"].get("side") != "sell"):
                continue
            details = intent["details"]
            if decimal(details.get("filled_quantity", "0"), nonnegative=True) <= 0:
                continue
            record = {"net_pnl": details.get("realized_pnl"), "fees": details.get("fees"),
                      "slippage": None, "strategy": proposal["strategy_id"]}
            records.append(record)
            by_strategy.setdefault(proposal["strategy_id"], []).append(record)
        kind = "live" if self.policy.mode.startswith("live-") else self.policy.mode
        if kind == "research":
            kind = "backtest"
        report = performance_report(records, evidence_kind=kind)
        fee_values = [fee for fee in execution_fees if fee is not None]
        known_fees = str(sum(fee_values, Decimal("0"))) if fee_values else None
        unknown_fees = sum(fee is None for fee in execution_fees)
        total_fees = known_fees if not unknown_fees else None
        proposals = self.ledger.list_proposals(1000)
        closed_sell_fees = report["fees"]
        report.update({"per_strategy": {name: performance_report(rows, evidence_kind=kind)
                                        for name, rows in by_strategy.items()},
                       "intent_count_in_window": len(intents), "window_limit": 1000,
                       "closed_sell_executions": len(records),
                       "dataset_evidence_kinds": sorted({d.get("evidence_kind", "unverified") for d in self.datasets.values()}),
                       "signal_count": len(self._signals),
                       "rejected_attempts_in_window": sum(i["status"] == "rejected" for i in intents),
                       "risk_rejected_proposals_in_window": sum(p.get("status") == "risk_rejected" for p in proposals),
                       "proposal_window_limit": 1000, "proposal_count_in_window": len(proposals),
                       "fees": total_fees, "closed_sell_execution_fees": closed_sell_fees,
                       "execution_fees": {"total": total_fees, "known_subtotal": known_fees,
                                          "unknown_count": unknown_fees, "order_intent_count": len(execution_fees),
                                          "basis": "acknowledged_fees_across_order_intents_excluding_cancellation_intents"},
                       "pnl_basis": "broker_reported_realized_cost_basis_pnl_for_terminal_positive_fill_sell_orders",
                       "per_strategy_basis": "exit_order_strategy_attribution_only; pooled position costs may originate from other strategies",
                       "per_strategy_alpha_established": False,
                       "time_basis": "intent_creation_time_window_not_broker_fill_dates",
                       "dates_are_fill_dates": False,
                       "window_may_be_truncated": len(intents) == 1000,
                       "unrealized_pnl": None, "benchmark_return": None,
                       "live_backtest_divergence": None, "calibration": None,
                       "turnover": None, "per_regime": None, "probability_buckets": None,
                       "market_fill_dates": None, "account_equity_history": None,
                       "metric_availability": {"account_total_return": "unavailable_without_equity_and_cash_flow_history",
                           "turnover": "unavailable_without_complete_fills_and_average_equity",
                           "calibration": "unavailable_without_aligned_probabilities_and_outcomes",
                           "per_regime": "unavailable_without_predefined_timestamped_regime_labels",
                           "live_backtest_divergence": "unavailable_without_matched_strategy_and_execution_windows"},
                       "limitations": ["Operational view covers at most 1000 recent intents.",
                           "Order creation and journal update timestamps are not broker fill dates.",
                           "Realized P&L assumes the adapter includes allocated opening cost and fees; raw fee totals are reported separately, not deducted twice.",
                           "Closed-sell fee subset excludes opening fees; total execution fees stays unknown if any order fee is missing.",
                           "No inferred mark-to-market history, benchmark, regime, calibration or slippage series."]})
        return report

    def readiness(self):
        self.ledger.integrity_check()
        live = self.policy.mode.startswith("live-")
        blockers = []
        if self._kill_active():
            blockers.append("kill_switch_or_shutdown")
        fingerprint = account_fingerprint(self.broker.capabilities.broker, self.policy.expected_account_id)
        if self.ledger.has_pending(fingerprint):
            blockers.append("outstanding_order_requires_reconciliation")
        if self.portfolio_policy is not None and self.portfolio_context_provider is None:
            blockers.append("trusted_portfolio_risk_provider_unavailable")
        if live:
            blockers.append("broker connectivity and complete risk-data preflight have not been performed")
            if not self.policy.live_enabled:
                blockers.append("live_execution_disabled")
            if not self.policy.live_approved_strategies:
                blockers.append("no_live_approved_strategy")
        return {"healthy": True, "execution_ready": not blockers and self.policy.mode == "paper",
                "mode": self.policy.mode, "blockers": blockers,
                "broker": json_value(self.broker.capabilities), "network_probe_performed": False}
