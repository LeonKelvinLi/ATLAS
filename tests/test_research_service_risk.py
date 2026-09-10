"""Portfolio vetoes use trusted fake snapshots and are repeated before submission."""

from dataclasses import replace

import pytest

from atlas.risk import PortfolioRiskPolicy, account_fingerprint
from atlas.service import ServiceError
from tests.test_platform_execution import D, SYMBOL, STRATEGY, deny_network, execute, make_service, prepare


def attach(h, changes=None):
    state = dict(changes or {})
    h.service.portfolio_policy = PortfolioRiskPolicy(max_gross_leverage="0.5", max_drawdown="0.2", max_positions=2)

    def provider(*, account, quote, **kwargs):
        return {"account_fingerprint": account_fingerprint(h.broker.capabilities.broker, account.account_id),
                "observed_at": int(h.clock()), "equity": str(account.equity), "order_mark_price": str(quote.bid),
                "positions": [{"symbol": p.symbol, "asset_class": "equity", "quantity": str(p.quantity),
                               "market_value": str(p.market_value)} for p in account.positions],
                "drawdown": "0", "global_enabled": True, "account_enabled": True, "broker_enabled": True,
                "data_quality_passed": True, "strategy_health": {STRATEGY: "HEALTHY"},
                "symbol_enabled": {SYMBOL: True}, **state}

    h.service.portfolio_context_provider = provider
    return state


def test_healthy_trusted_portfolio_allows_fake_order(make_service):
    h = make_service(); attach(h)
    proposal = prepare(h)
    assert proposal["risk"]["portfolio_risk"]["passed"]
    execute(h, proposal)
    assert len(h.broker.submit_calls) == 1


@pytest.mark.parametrize("change", [
    {"strategy_health": {STRATEGY: "DISABLED"}}, {"global_enabled": False},
    {"data_quality_passed": False}, {"drawdown": "0.25"}, {"observed_at": 1},
    {"equity": "1000000"}, {"order_mark_price": "1000"},
    {"account_fingerprint": "wrong-account"}, {"positions": None},
])
def test_fresh_health_or_portfolio_change_vetoes_prepared_order(make_service, change):
    h = make_service(); state = attach(h)
    proposal = prepare(h)
    assert proposal["risk"]["passed"]
    state.update(change)
    result = execute(h, proposal)
    assert not h.broker.submit_calls
    assert result["intent"]["status"] == "rejected"


def test_missing_trusted_provider_never_falls_back_to_zero_exposure(make_service):
    h = make_service(); attach(h)
    h.service.portfolio_context_provider = None
    proposal = prepare(h)
    assert proposal["status"] == "risk_rejected"
    assert "trusted_portfolio_risk_provider_unavailable" in h.service.readiness()["blockers"]
    assert not h.broker.submit_calls


def test_portfolio_policy_change_invalidates_exact_proposal(make_service):
    h = make_service(); attach(h)
    proposal = prepare(h)
    h.service.portfolio_policy = replace(h.service.portfolio_policy, max_positions=1)
    with pytest.raises(ServiceError, match="policy"):
        execute(h, proposal)
    assert not h.broker.submit_calls


def test_provider_latency_rechecks_account_and_quote_freshness(make_service):
    h = make_service(); attach(h)
    original = h.service.portfolio_context_provider

    def slow_provider(**kwargs):
        h.clock.now += 31
        return original(**kwargs)

    h.service.portfolio_context_provider = slow_provider
    proposal = prepare(h)
    assert not proposal["risk"]["passed"]
    assert any(row["code"] == "quote_freshness" and not row["passed"] for row in proposal["risk"]["checks"])
    assert not h.broker.submit_calls
