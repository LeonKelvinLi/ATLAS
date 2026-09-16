"""Explicit intake-to-engine cycle; default records evidence without orders.

The caller supplies owned components, reviewed size/price and an exact intake
registration. Holdings come only from successful engine reconciliation. No
credential loading, acquisition, initialization, scheduler or live route is added.
"""

from pathlib import Path
import re

from atlas.domain import financial_context
from atlas.ledger import digest
from atlas.paper.engine import PaperEngine
from atlas.paper.intake import PaperIntake, _read_json
from atlas.paper.models import PaperError, amount, decision, sha256


def _projection(intake, report, registration, binding, *, symbol, quantity, price):
    """Accept only this invocation's source-bound, durably persisted result."""
    test = registration["role"] == "TEST"
    fixed = {"registration_sha256": registration["registration_sha256"],
             "account_fingerprint": binding["account_fingerprint"],
             "policy_sha256": binding["policy_sha256"], "strategy_sha256": binding["strategy_sha256"],
             "symbol": symbol, "source_path": "injected_test" if test else "owned_alpaca_https_get",
             "archive_integrity_verified": True, "provider_authenticity_verified": False,
             "prospective_qualified": False, "execution_authorized": False, "live_enabled": False}
    if (type(report) is not dict or any(report.get(k) != v or type(report.get(k)) is not type(v)
                                      for k, v in fixed.items())
            or type(report.get("receipt_name")) is not str
            or not re.fullmatch(r"(?:signal|observation)-[a-f0-9]{64}\.json", report["receipt_name"])):
        raise PaperError("paper_cycle_projection_binding_invalid")
    saved = _read_json(intake._root / "receipts" / report["receipt_name"])
    if (saved != {k: v for k, v in report.items() if k not in {"receipt_name", "receipt_sha256", "replayed"}}
            or report.get("receipt_sha256") != digest(saved)):
        raise PaperError("paper_cycle_projection_not_persisted")
    if report.get("status") == "NO_ACTION":
        if report.get("candidate") is not None or report.get("paper_candidate_eligible") is not False:
            raise PaperError("paper_cycle_projection_binding_invalid")
        return None
    if (report.get("status") != "CANDIDATE" or report.get("paper_candidate_eligible") is not (not test)
            or report.get("evidence_class") != ("TEST" if test else "PROSPECTIVE_OBSERVATION_UNVERIFIED")):
        raise PaperError("paper_cycle_projection_binding_invalid")
    reviewed = decision(report["candidate"])
    if (reviewed["strategy_sha256"] != binding["strategy_sha256"]
            or reviewed["strategy_id"] != registration["implementation"]["strategy"]["strategy_id"]
            or reviewed["symbol"] != symbol or amount(reviewed["quantity"]) != quantity
            or amount(reviewed["limit_price"]) != price
            or reviewed["data_sha256"] != digest(report["evidence"])):
        raise PaperError("paper_cycle_candidate_binding_invalid")
    return reviewed


@financial_context
def run_cycle(intake, engine, *, symbol, quantity, limit_price,
              expected_registration_sha256, execute=False, ttl_seconds=300):
    """Reconcile, project and optionally submit one candidate through the engine.

    Default ``execute=False`` still performs broker reads and persists the intake
    pin, reconciliation and projection. It never calls submit. ``execute=True``
    additionally requires the engine's existing write flag; TEST intake can only
    exercise a SIMULATION engine and remains ineligible for PAPER. The selected
    journal must already be initialized. Reopening the same pin is idempotent;
    selecting another generation/path is refused, including after restart.
    """
    if type(intake) is not PaperIntake or type(engine) is not PaperEngine:
        raise PaperError("paper_cycle_owned_components_required")
    if type(execute) is not bool or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
        raise PaperError("paper_cycle_options_invalid")
    quantity_value, price_value = amount(quantity, positive=True), amount(limit_price, positive=True)
    expected = sha256(expected_registration_sha256)
    # Serialize with other engine operations while holdings are being projected.
    # These private handles are confined to this owned join, never strategy code.
    with engine._lock:
        reg = intake.registration()
        binding = engine._journal.binding
        test = reg["role"] == "TEST"
        if (reg["registration_sha256"] != expected
                or binding["mode"] != ("SIMULATION" if test else "PAPER")
                or engine._api.provenance != ("TEST" if test else "PAPER_API")
                or reg["account_fingerprint"] != binding["account_fingerprint"]
                or reg["policy_sha256"] != binding["policy_sha256"]
                or reg["policy_sha256"] != engine._policy.fingerprint()
                or reg["implementation"]["strategy_sha256"] != binding["strategy_sha256"]
                or symbol not in reg["implementation"]["strategy"]["spec"]["symbols"]):
            raise PaperError("paper_cycle_intake_engine_binding_mismatch")
        if engine.status()["initialized"] is not True:
            raise PaperError("paper_cycle_initialized_session_required")
        if execute and not engine._api.execution_enabled:
            raise PaperError("paper_cycle_execution_disabled")
        pin = {"registration_sha256": expected, "intake_directory": str(Path(intake._root)),
               "mode": binding["mode"]}
        prior = engine._journal.get("intake_binding")
        if prior is not None and prior != pin:
            raise PaperError("paper_cycle_intake_already_bound")
        if prior is None:
            with engine._journal.transaction("intake_bound", pin, now=engine._now()):
                engine._journal.set("intake_binding", pin)
        status = engine.reconcile()
        result = {"mode": binding["mode"], "provenance": engine._api.provenance,
                  "execute_requested": execute, "intake": None, "engine": status,
                  "actual_orders_submitted": status["actual_orders_submitted"],
                  "live_enabled": False, "qualification_established": False}
        if status.get("reconciled") is not True:
            return {**result, "status": "RECONCILIATION_BLOCKED"}
        held = next((row["qty"] for row in status["positions"] if row["symbol"] == symbol), "0")
        projected = intake.project(symbol=symbol, quantity=quantity, limit_price=limit_price,
            held_quantity=held, expected_registration_sha256=expected, ttl_seconds=ttl_seconds)
        reviewed = _projection(intake, projected, reg, binding, symbol=symbol,
                               quantity=quantity_value, price=price_value)
        result["intake"] = projected
        if reviewed is None:
            return {**result, "status": "NO_ACTION"}
        if not execute:
            return {**result, "status": "CANDIDATE_ONLY"}
        submitted = engine.submit(reviewed)
        return {**result, "status": "SUBMISSION_RESULT", "engine": submitted,
                "actual_orders_submitted": submitted["actual_orders_submitted"]}
