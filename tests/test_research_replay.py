"""Causal replay/accounting scenarios; synthetic observations and no network."""

from dataclasses import replace
from decimal import Decimal as D
import sqlite3

import pytest

from atlas.research.replay import ExecutionModel, ReplayEngine, run_replay


def bar(t, close="100", volume="100", **changes):
    return {"symbol": "XYZ", "asset_class": "equity", "venue": "TEST", "currency": "USD", "event_time": t,
            "interval_seconds": 60, "information_available_time": t, "ingestion_time": t,
            "timezone": "UTC", "session": "synthetic", "session_date": "2020-01-01",
            "open": close, "high": close, "low": close, "close": close, "volume": volume, "volume_unit": "shares",
            "revision": 0, **changes}


def decision(t=60, key="buy", **changes):
    return {"signal_id": key, "symbol": "XYZ", "decision_time": t, "expires_at": 1000,
            "strategy_id": "fixture", "strategy_version": "1", "side": "buy", "quantity": "10",
            "order_type": "market", **changes}


def model(**changes):
    return ExecutionModel(fee_bps="0", spread_bps="0", slippage_bps="0", latency_seconds=0,
                          participation_rate="0.5", quantity_increment="1", minimum_quantity="1", **changes)


def run(bars=None, decisions=None, **kwargs):
    return run_replay(bars or [bar(60), bar(120), bar(180)], decisions or [decision()],
                      model=kwargs.pop("model", model()), **kwargs)


def test_decision_event_never_fills_and_sample_end_does_not_liquidate():
    result = run()
    assert result["fills"][0]["fill_time"] == 120
    assert result["fills"][0]["decision_time"] == 60
    assert result["portfolio"]["positions"][0]["quantity"] == "10"
    assert result["portfolio"]["cash"] == "9000"
    assert result["trades"] == []
    assert result["evidence_class"] == "SYNTHETIC"


def test_target_weight_zero_liquidates_exact_inventory_with_positive_spread():
    enter = decision(target_weight="0.5")
    exit_order = decision(120, "exit", target_weight="0")
    for row in (enter, exit_order):
        del row["quantity"], row["side"]
    result = run(decisions=[enter, exit_order], model=replace(model(), spread_bps=D("10"), fee_bps=D("1")))
    assert [order["status"] for order in result["orders"]] == ["filled", "filled"]
    assert result["portfolio"]["positions"] == []
    assert D(result["portfolio"]["cash"]) < 10000


def test_cash_baseline_has_no_orders_or_inferred_return():
    result = run_replay([bar(60), bar(120, close="200")], [], model=model())
    assert result["orders"] == [] and result["fills"] == []
    assert result["metrics"]["net_return"] == "0"


@pytest.mark.parametrize("unit", [None, "unknown", "quote_currency"])
def test_unknown_or_wrong_volume_units_cannot_fund_fills(unit):
    with pytest.raises(ValueError, match="volume units"):
        run([bar(60, volume_unit=unit)])


@pytest.mark.parametrize("currency", [None, "EUR", "usd"])
def test_missing_or_foreign_currency_is_not_assumed_usd(currency):
    with pytest.raises(ValueError, match="USD"):
        run([bar(60, currency=currency)])


def test_full_volume_is_used_only_when_published_and_partial_fills_share_capacity():
    bars = [bar(60), bar(120, volume="10"), bar(180, volume="10")]
    decisions = [decision(key="a", quantity="4"), decision(key="b", quantity="4", strategy_id="other")]
    result = run(bars, decisions)
    assert [f["quantity"] for f in result["fills"]] == ["4", "1", "3"]
    assert sum(D(f["quantity"]) for f in result["fills"] if f["fill_time"] == 120) == 5
    assert len(result["portfolio"]["positions"]) == 2


def test_latency_and_delayed_publication_change_fill_time():
    result = run([bar(60), bar(120, information_available_time=125), bar(180)],
                 model=replace(model(), latency_seconds=100))
    assert result["fills"][0]["fill_time"] == 180
    assert result["fills"][0]["submission_time"] == 160


def test_missing_liquidity_keeps_order_unfilled():
    result = run([bar(60), bar(120, volume="0")])
    assert result["fills"] == [] and result["orders"][0]["status"] == "accepted"
    assert result["portfolio"]["cash"] == "10000"


def test_limit_waits_for_subsequent_marketable_price_and_gap_respects_limit():
    result = run([bar(60), bar(120, close="110"), bar(180, close="80")],
                 [decision(order_type="limit", limit_price="90")])
    assert result["fills"][0]["fill_time"] == 180 and result["fills"][0]["fill_price"] == "80"


def test_partial_cancel_preserves_executed_inventory_and_releases_cash():
    result = run([bar(60), bar(120, volume="10"), bar(180)],
                 [decision(), {"signal_id": "cancel", "action": "cancel", "target_signal_id": "buy", "decision_time": 150}])
    assert result["orders"][0]["status"] == "cancelled"
    assert result["orders"][0]["filled_quantity"] == "5"
    assert result["portfolio"]["reserved_cash"] == "0"
    assert result["portfolio"]["positions"][0]["quantity"] == "5"


def test_expiry_before_event_and_impossible_lot_reject_without_fill():
    result = run(decisions=[decision(expires_at=120), decision(key="badlot", quantity="0.5", strategy_id="other")])
    assert result["fills"] == []
    assert {o["reason"] for o in result["orders"]} == {"signal_expired", "quantity_increment"}


def test_cash_constraints_apply_at_submission_and_after_adverse_gap():
    rejected = run(decisions=[decision(quantity="200")])
    assert rejected["orders"][0]["reason"] == "insufficient_cash"
    result = run([bar(60), bar(120, close="200")], [decision(quantity="100")])
    assert D(result["portfolio"]["cash"]) >= 0
    assert result["orders"][0]["filled_quantity"] == "50"


def test_average_cost_includes_opening_fees_and_sleeves_cannot_take_each_others_pnl():
    result = run([bar(60), bar(120), bar(180, close="110"), bar(240, close="110")],
        [decision(quantity="2"), decision(180, "sell", side="sell", quantity="1"),
         decision(180, "wrong-sleeve", side="sell", quantity="1", strategy_id="other")],
        model=replace(model(), fee_bps=D("100")))
    assert result["trades"][0]["net_pnl"] == "7.9"
    assert result["trades"][0]["fees"] == "2.1"
    assert result["portfolio"]["positions"][0]["cost"] == "101"
    assert next(o for o in result["orders"] if o["signal_id"] == "wrong-sleeve")["reason"] == "insufficient_sleeve_inventory"
    metrics = result["metrics"]
    assert metrics["fees"] == metrics["fees_paid"] == "3.1"
    assert metrics["closed_sell_allocated_fees"] == "2.1"
    assert metrics["slippage"] == result["execution_quality"]["slippage_cost"] == "0"
    assert "all_simulated_execution_fills" in metrics["fees_basis"]
    assert "event_observation_mean_equity" in metrics["turnover_basis"]
    assert metrics["holding_period_distribution"] is None and metrics["holding_period_reason"]
    assert metrics["closed_sell_allocated_slippage"] is None
    assert not any("aggregate costs are unavailable" in warning for warning in metrics["warnings"])


def test_open_inventory_reports_all_paid_fees_and_slippage_without_inventing_closed_trades():
    result = run(model=replace(model(), fee_bps=D("100"), slippage_bps=D("100")))
    metrics = result["metrics"]
    assert result["trades"] == [] and metrics["closed_sell_allocated_fees"] == "0"
    assert metrics["fees"] == "10.1" and metrics["slippage"] == "10"
    assert metrics["net_pnl"] is None and D(metrics["net_return"]) < 0


def test_revisions_do_not_mint_liquidity_or_retroactively_change_fills():
    result = run([bar(60), bar(120, volume="10"),
                  bar(120, close="1", volume="999999", revision=1, information_available_time=130), bar(180, volume="0")])
    assert len(result["fills"]) == 1 and result["fills"][0]["quantity"] == "5"
    assert result["fills"][0]["fill_price"] == "100"
    assert any(e["kind"] == "revision_not_executable" for e in result["events"])


def test_duplicate_decisions_replay_but_changed_signal_rejects():
    result = run(decisions=[decision(), decision()])
    assert len(result["orders"]) == 1 and len(result["fills"]) == 1
    with pytest.raises(ValueError, match="signal id"):
        run(decisions=[decision(), decision(quantity="11")])


def test_stale_data_blocks_fills_and_new_orders():
    result = run([bar(60), bar(120, information_available_time=600)],
                 [decision(), decision(610, "late", strategy_id="other")])
    assert result["fills"] == []
    assert result["orders"][-1]["reason"] == "missing_or_stale_market_data"


@pytest.mark.parametrize("changes,reason", [
    ({"trading_enabled": False}, "trading_enabled"),
    ({"broker_enabled": False}, "broker_enabled"),
    ({"account_enabled": False}, "account_enabled"),
    ({"data_quality_passed": False}, "data_quality_passed"),
    ({"disabled_strategies": ("fixture",)}, "strategy_disabled"),
    ({"disabled_symbols": ("XYZ",)}, "symbol_disabled"),
])
def test_permission_and_quality_vetoes_block_new_risk(changes, reason):
    result = run(model=replace(model(), **changes))
    assert result["fills"] == [] and result["orders"][0]["reason"] == reason


def test_drawdown_blocks_new_risk_but_allows_inventory_reduction():
    result = run([bar(60), bar(120), bar(180, close="50"), bar(240, close="50")],
        [decision(), decision(180, "new-risk", quantity="1", strategy_id="other"),
         decision(180, "reduce", side="sell", quantity="5")], model=replace(model(), max_drawdown=D("0.04")))
    assert next(o for o in result["orders"] if o["signal_id"] == "new-risk")["reason"] == "maximum_drawdown"
    assert next(o for o in result["orders"] if o["signal_id"] == "reduce")["status"] == "filled"
    assert result["portfolio"]["positions"][0]["quantity"] == "5"


def test_daily_loss_uses_simulated_fill_time_and_blocks_new_buy():
    result = run([bar(60), bar(120), bar(180, close="50"), bar(240, close="50")],
        [decision(), decision(120, "sell", side="sell", quantity="10"),
         decision(180, "new-risk", quantity="1")], model=replace(model(), max_daily_loss=D("50")))
    assert result["portfolio"]["daily_realized_pnl"] == {"0": "-500"}
    assert next(o for o in result["orders"] if o["signal_id"] == "new-risk")["reason"] == "maximum_daily_loss"


def test_turnover_budget_caps_partial_buys_and_exposes_portfolio_breakdown():
    result = run(model=replace(model(), max_turnover=D("0.05")))
    assert result["orders"][0]["filled_quantity"] == "5"
    assert result["orders"][0]["reason"] == "maximum_turnover"
    assert result["portfolio"]["exposure_by_asset_class"] == {"equity": "500"}
    assert result["portfolio"]["exposure_by_symbol"] == {"XYZ": "500"}


def test_partial_restart_and_idempotent_complete_replay_are_identical(tmp_path):
    bars, decisions = [bar(60), bar(120, volume="10"), bar(180)], [decision()]
    path = tmp_path / "research.db"
    engine = ReplayEngine(bars, decisions, model=model(), state_path=path)
    partial = engine.advance(max_events=2)
    assert partial["orders"][0]["status"] == "partially_filled"
    engine.close()
    expected = run(bars, decisions)
    resumed = run(bars, decisions, state_path=path)
    assert resumed == expected
    assert run(bars, decisions, state_path=path) == resumed


@pytest.mark.parametrize("changed", ["fees", "cash", "data"])
def test_restart_refuses_changed_execution_or_data_assumptions(tmp_path, changed):
    path = tmp_path / "research.db"
    run(state_path=path)
    args = {"state_path": path}
    if changed == "fees":
        args["model"] = replace(model(), fee_bps=D("2"))
    elif changed == "cash":
        args["initial_cash"] = "9999"
    else:
        args["bars"] = [bar(60), bar(120, close="101"), bar(180)]
    with pytest.raises(ValueError, match="changed"):
        run(**args)


@pytest.mark.parametrize("after_commit", [False, True])
def test_crash_before_or_after_commit_resumes_without_duplicated_fill(tmp_path, monkeypatch, after_commit):
    path = tmp_path / "research.db"
    bars, decisions = [bar(60), bar(120), bar(180)], [decision()]
    engine = ReplayEngine(bars, decisions, model=model(), state_path=path)
    engine.advance(max_events=1)
    if after_commit:
        def unavailable():
            raise OSError("synthetic directory sync outage")
        monkeypatch.setattr(engine, "_sync", unavailable)
    else:
        original = engine._event
        def fail_after_fill(kind, *args, **kwargs):
            original(kind, *args, **kwargs)
            if kind == "fill":
                raise OSError("synthetic crash before checkpoint commit")
        monkeypatch.setattr(engine, "_event", fail_after_fill)
    with pytest.raises(OSError):
        engine.step()
    with pytest.raises(ValueError, match="requires recovery"):
        engine.result()
    engine.close()
    assert run(bars, decisions, state_path=path) == run(bars, decisions)


def test_closed_persistent_engine_cannot_silently_continue_without_durability(tmp_path):
    path = tmp_path / "research.db"
    engine = ReplayEngine([bar(60), bar(120), bar(180)], [decision()], model=model(), state_path=path)
    engine.advance(max_events=1)
    checkpoint = engine.result()
    engine.close()
    with pytest.raises(ValueError, match="closed replay"):
        engine.step()
    assert engine.result() == checkpoint
    assert run(state_path=path) == run()


def test_concurrent_stale_worker_cannot_overwrite_committed_progress(tmp_path):
    path = tmp_path / "research.db"
    args = ([bar(60), bar(120)], [decision()])
    first = ReplayEngine(*args, model=model(), state_path=path)
    second = ReplayEngine(*args, model=model(), state_path=path)
    try:
        first.step()
        with pytest.raises(ValueError, match="another replay worker"):
            second.step()
    finally:
        first.close()
        second.close()
    assert run(*args, state_path=path) == run(*args)


def test_empty_corrupt_or_tampered_state_is_not_reinitialized(tmp_path):
    path = tmp_path / "empty.db"
    path.touch()
    with pytest.raises(ValueError, match="empty"):
        run(state_path=path)
    path = tmp_path / "valid.db"
    run(state_path=path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE checkpoint SET payload='{}'")
    with pytest.raises(ValueError, match="corruption"):
        run(state_path=path)
