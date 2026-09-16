"""Explicit synthetic intake/real gateway integration; no broker or key access."""

from copy import deepcopy
import math
import shutil
import socket
from types import SimpleNamespace

import pytest

from atlas.paper.broker import AlpacaPaperAPI
from atlas.paper.engine import PaperEngine
from atlas.paper.intake import PaperIntake
from atlas.paper.intake_cycle import run_cycle
from atlas.paper.journal import PaperJournal
from atlas.paper.models import PaperError
from atlas.paper.risk import PaperRiskPolicy
from atlas.paper.simulation import SyntheticAlpaca
from test_paper_intake import Clock, Transport, OPEN, SPEC, POLICY, populate


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("cycle fixtures cannot contact networks or credentials")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr("atlas.paper.broker._credentials", refuse)
    monkeypatch.setattr("atlas.paper.broker._owned_http", refuse)
    monkeypatch.setattr("atlas.research.data_collector._credentials", refuse)
    monkeypatch.setattr("atlas.research.data_collector.alpaca_get", refuse)
    monkeypatch.setattr("atlas.paper.journal._account_lock_root", lambda: tmp_path / "account-locks")


@pytest.fixture
def session(tmp_path):
    clock = Clock()
    sim = SyntheticAlpaca(now=OPEN)
    sim.quotes["SPY"].update(bid="97.99", ask="98")
    api = AlpacaPaperAPI(transport=sim.request, clock=clock)
    fingerprint = api.account()["fingerprint"]
    transport = Transport(clock)
    intake = PaperIntake.testing(tmp_path / "intake", clock=clock, transport=transport,
        generation_id="cycle-test", spec=SPEC, account_fingerprint=fingerprint, risk_policy=POLICY)
    populate((intake, clock, transport))
    clock.at = math.ceil(clock())
    sim.now = clock()
    reg = intake.registration()
    binding = {"mode": "SIMULATION", "account_fingerprint": fingerprint,
               "strategy_sha256": reg["implementation"]["strategy_sha256"],
               "policy_sha256": POLICY.fingerprint(), "allocated_principal": "100"}
    path = tmp_path / "engine"
    journal = PaperJournal(path, binding, create=True, now=clock())
    engine = PaperEngine(api, journal, POLICY, clock=clock)
    engine.initialize()
    s = SimpleNamespace(clock=clock, sim=sim, api=api, intake=intake, transport=transport,
                        reg=reg, binding=binding, journal=journal, engine=engine, path=path)
    yield s
    s.journal.close()


def cycle(s, **changes):
    args = {"symbol": "SPY", "quantity": ".09", "limit_price": "98",
            "expected_registration_sha256": s.reg["registration_sha256"]}
    return run_cycle(s.intake, s.engine, **(args | changes))


def posts(s):
    return [row for row in s.sim.calls if row["method"] == "POST"]


def advance(s, seconds):
    s.clock.at += seconds
    s.sim.now = s.clock()


def test_default_prepares_persisted_candidate_and_pin_without_orders(session):
    s = session
    before = len(s.sim.calls)
    result = cycle(s)
    assert result["status"] == "CANDIDATE_ONLY"
    assert result["execute_requested"] is False and result["actual_orders_submitted"] == 0
    assert result["intake"]["paper_candidate_eligible"] is False
    assert result["intake"]["evidence_class"] == "TEST"
    assert (s.intake._root / "receipts" / result["intake"]["receipt_name"]).is_file()
    assert s.journal.get("intake_binding") == {"registration_sha256": s.reg["registration_sha256"],
        "intake_directory": str(s.intake._root), "mode": "SIMULATION"}
    assert len(s.sim.calls) > before and posts(s) == [] and s.journal.orders() == []


def test_explicit_simulation_uses_existing_engine_and_remains_test(session):
    result = cycle(session, execute=True)
    assert result["status"] == "SUBMISSION_RESULT"
    assert "order" in result["engine"], result["engine"]
    assert result["engine"]["order"]["state"] == "new"
    assert result["engine"]["order"]["observation"]["filled_qty"] == "0"
    assert result["engine"]["post_authorizations"] == 1
    assert result["actual_orders_submitted"] == 0
    assert result["provenance"] == "TEST" and result["mode"] == "SIMULATION"
    assert result["intake"]["paper_candidate_eligible"] is False
    assert result["live_enabled"] is False and result["qualification_established"] is False
    assert len(posts(session)) == 1


def test_repeat_and_restart_reuse_same_receipt_pin_and_order(session):
    s = session
    first = cycle(s, execute=True)
    original_pin = s.journal.get("intake_binding")
    s.journal.close()
    s.journal = PaperJournal(s.path, s.binding, now=s.clock())
    s.api = AlpacaPaperAPI(transport=s.sim.request, clock=s.clock)
    s.engine = PaperEngine(s.api, s.journal, POLICY, clock=s.clock)
    s.intake = PaperIntake.testing(s.intake._root, clock=s.clock, transport=s.transport)
    repeated = cycle(s, execute=True)
    assert repeated["engine"]["replayed"] is True
    assert repeated["engine"]["order"]["client_order_id"] == first["engine"]["order"]["client_order_id"]
    assert repeated["intake"]["receipt_sha256"] == first["intake"]["receipt_sha256"]
    assert s.journal.get("intake_binding") == original_pin and len(posts(s)) == 1


@pytest.mark.parametrize("change", ["generation", "path"])
def test_existing_pin_refuses_another_generation_or_path_before_reads(session, tmp_path, change):
    s = session
    cycle(s)
    original_pin = s.journal.get("intake_binding")
    if change == "generation":
        other = PaperIntake.testing(tmp_path / "other", clock=s.clock, transport=s.transport,
            generation_id="other-generation", spec=SPEC, account_fingerprint=s.binding["account_fingerprint"],
            risk_policy=POLICY)
    else:
        shutil.copytree(s.intake._root, tmp_path / "copy")
        other = PaperIntake.testing(tmp_path / "copy", clock=s.clock, transport=s.transport)
    before = len(s.sim.calls)
    with pytest.raises(PaperError, match="intake_already_bound"):
        run_cycle(other, s.engine, symbol="SPY", quantity=".09", limit_price="98",
                  expected_registration_sha256=other.registration()["registration_sha256"])
    assert len(s.sim.calls) == before and s.journal.get("intake_binding") == original_pin


@pytest.mark.parametrize("change", ["account", "policy", "strategy"])
def test_actual_registration_mismatch_precedes_broker_reads(session, tmp_path, change):
    s = session
    spec = {k: v for k, v in SPEC.items() if k != "strategy_id"} | {"z_threshold": "1.5"} if change == "strategy" else SPEC
    if change == "strategy":
        from atlas.research.multiasset_strategies import normalize_spec
        spec = normalize_spec(spec)
    policy = PaperRiskPolicy(allowed_strategy_ids=POLICY.allowed_strategy_ids,
                             max_order_notional="5") if change == "policy" else POLICY
    if change == "strategy":
        policy = PaperRiskPolicy(allowed_strategy_ids=(spec["strategy_id"],))
    other = PaperIntake.testing(tmp_path / "foreign", clock=s.clock, transport=s.transport,
        generation_id="foreign", spec=spec,
        account_fingerprint="b" * 64 if change == "account" else s.binding["account_fingerprint"],
        risk_policy=policy)
    before = len(s.sim.calls)
    with pytest.raises(PaperError, match="binding_mismatch"):
        run_cycle(other, s.engine, symbol="SPY", quantity=".09", limit_price="98",
                  expected_registration_sha256=other.registration()["registration_sha256"])
    assert len(s.sim.calls) == before and s.journal.get("intake_binding") is None


@pytest.mark.parametrize("change", [{"expected_registration_sha256": "b" * 64}, {"symbol": "GLD"}])
def test_request_binding_mismatch_precedes_reads(session, change):
    before = len(session.sim.calls)
    with pytest.raises(PaperError, match="binding_mismatch"):
        cycle(session, **change)
    assert len(session.sim.calls) == before


def test_paper_intake_cannot_join_simulation_engine(session, tmp_path):
    s = session
    intake = PaperIntake.register(tmp_path / "owned", generation_id="owned", spec=SPEC,
        account_fingerprint=s.binding["account_fingerprint"], risk_policy=POLICY)
    before = len(s.sim.calls)
    with pytest.raises(PaperError, match="binding_mismatch"):
        run_cycle(intake, s.engine, symbol="SPY", quantity=".09", limit_price="98",
                  expected_registration_sha256=intake.registration()["registration_sha256"])
    assert len(s.sim.calls) == before


def test_test_intake_cannot_join_paper_engine_before_auth(session, tmp_path):
    s = session
    s.journal.close()
    binding = {**s.binding, "mode": "PAPER"}
    with PaperJournal(tmp_path / "paper-state", binding, create=True, now=s.clock()) as journal:
        engine = PaperEngine(AlpacaPaperAPI(), journal, POLICY)
        with pytest.raises(PaperError, match="binding_mismatch"):
            run_cycle(s.intake, engine, symbol="SPY", quantity=".09", limit_price="98",
                      expected_registration_sha256=s.reg["registration_sha256"], execute=True)


def test_disabled_paper_write_flag_refuses_without_auth(tmp_path):
    # Targeted local mode-gate fixture only: no account was initialized by a broker.
    intake = PaperIntake.register(tmp_path / "owned", generation_id="owned", spec=SPEC,
        account_fingerprint="a" * 64, risk_policy=POLICY)
    reg = intake.registration()
    binding = {"mode": "PAPER", "account_fingerprint": "a" * 64,
               "strategy_sha256": reg["implementation"]["strategy_sha256"],
               "policy_sha256": POLICY.fingerprint(), "allocated_principal": "100"}
    with PaperJournal(tmp_path / "paper-state", binding, create=True, now=reg["created_at"]) as journal:
        with journal.transaction("synthetic_mode_gate_fixture", {}, now=reg["created_at"]):
            journal.set("initialized", True)
        engine = PaperEngine(AlpacaPaperAPI(), journal, POLICY)
        with pytest.raises(PaperError, match="execution_disabled"):
            run_cycle(intake, engine, symbol="SPY", quantity=".09", limit_price="98",
                      expected_registration_sha256=reg["registration_sha256"], execute=True)
        assert journal.get("intake_binding") is None


@pytest.mark.parametrize("fault", ["unknown", "pending_cancel", "rejected", "unmanaged_position"])
def test_unreconciled_account_never_projects_or_submits(session, monkeypatch, fault):
    s = session
    if fault == "unknown":
        s.sim.queue_fault("POST", "/v2/orders", "timeout_after_accept")
        first = cycle(s, execute=True)
        assert first["engine"]["order"]["state"] == "unknown"
    elif fault in {"pending_cancel", "rejected"}:
        first = cycle(s, execute=True)
        cid = first["engine"]["order"]["client_order_id"]
        if fault == "pending_cancel":
            s.engine.cancel(cid)
        else:
            s.sim.orders[cid]["status"] = "rejected"
    else:
        s.sim.holdings["SPY"] = {"qty": ".09", "cost_basis": "8.82"}
    before = len(posts(s))
    monkeypatch.setattr(s.intake, "project", lambda **k: pytest.fail("unreconciled holdings projected"))
    result = cycle(s, execute=True)
    assert result["status"] == "RECONCILIATION_BLOCKED"
    assert result["intake"] is None and len(posts(s)) == before


def test_filled_authoritative_holdings_produce_no_action(session):
    s = session
    first = cycle(s, execute=True)
    s.sim.fill(first["engine"]["order"]["client_order_id"], ".09", "98")
    result = cycle(s, execute=True)
    assert result["status"] == "NO_ACTION"
    assert result["intake"]["reason"] == "target_already_satisfied"
    assert result["engine"]["positions"][0]["qty"] == "0.09" and len(posts(s)) == 1


def test_stale_inputs_no_submission(session):
    advance(session, 301)
    result = cycle(session, execute=True)
    assert result["status"] == "NO_ACTION" and posts(session) == []
    assert result["intake"]["reason"] == "original_signal_stale"


def test_risk_cap_is_enforced_by_real_engine(session):
    result = cycle(session, quantity=".2", execute=True)
    assert result["status"] == "SUBMISSION_RESULT"
    assert result["engine"]["risk"]["passed"] is False
    assert result["engine"]["new_submission"] is False and posts(session) == []


def test_kill_remains_engine_authority(session):
    session.engine.halt()
    with pytest.raises(PaperError, match="kill_switch_active"):
        cycle(session, execute=True)
    assert posts(session) == []


def test_forged_projection_flags_cannot_cross_join(session, monkeypatch):
    original = cycle(session)["intake"]
    forged = deepcopy(original)
    forged["paper_candidate_eligible"] = True
    monkeypatch.setattr(session.intake, "project", lambda **k: forged)
    with pytest.raises(PaperError):
        cycle(session, execute=True)
    assert posts(session) == []


@pytest.mark.parametrize("options", [{"execute": 1}, {"execute": "true"}, {"ttl_seconds": True}, {"ttl_seconds": 301}])
def test_options_strict_before_reads(session, options):
    before = len(session.sim.calls)
    with pytest.raises(PaperError, match="options_invalid"):
        cycle(session, **options)
    assert len(session.sim.calls) == before
