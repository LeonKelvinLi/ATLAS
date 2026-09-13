from copy import deepcopy
from decimal import Decimal

import pytest

from atlas.research.campaign_preflight import (
    MAX_PLAN_SECONDS, campaign_preflight, validate_campaign_plan,
)
from atlas.research import multiasset_lab as lab


def test_complete_default_contract_matches_existing_receipt_without_mutation():
    plan = lab.default_plan()
    before = deepcopy(plan)
    assert validate_campaign_plan(plan) == campaign_preflight(plan)
    assert plan == before


def test_optional_defaults_and_supported_stricter_thresholds_are_preserved():
    plan = lab.default_plan()
    del plan['research_purpose'], plan['max_replay_evaluations']
    for thresholds in (None, {}, {'min_daily_observations': 90, 'max_drawdown': .05}):
        plan['thresholds'] = thresholds
        before = deepcopy(plan)
        result = validate_campaign_plan(plan)
        assert result['research_purpose'] == 'diagnostic'
        assert result['max_replay_evaluations'] == 5000
        assert result['splits']['test']['required_daily_observations'] == (
            90 if thresholds else 60)
        assert plan == before


@pytest.mark.parametrize('key', sorted(set(lab.default_plan()) - {
    'research_purpose', 'max_replay_evaluations',
}))
def test_every_consumed_contract_key_is_required(key):
    plan = lab.default_plan()
    del plan[key]
    with pytest.raises(ValueError, match='missing fields'):
        validate_campaign_plan(plan)


@pytest.mark.parametrize('key,value', [
    ('version', 'v2'), ('initial_capital_usd', 100), ('initial_capital_usd', '1000'),
    ('objective', 'maximum_test_return'), ('allocation', 'current_test_winner'),
    ('account_scope', 'live'), ('live_approved', True), ('live_approved', 0),
    ('cost_multipliers', ['1']), ('risk_fractions', ['.01']),
    ('seed', True), ('seed', -1), ('seed', 2**64), ('seed', 1.0),
    ('bootstrap_resamples', True), ('bootstrap_resamples', 99),
    ('bootstrap_resamples', 5001), ('bootstrap_resamples', '100'),
    ('embargo_seconds', -1), ('embargo_seconds', True),
    ('label_horizon_seconds', 0), ('label_horizon_seconds', False),
    ('label_horizon_seconds', MAX_PLAN_SECONDS + 1),
    ('latency_stress_seconds', -1), ('latency_stress_seconds', True),
    ('latency_stress_seconds', MAX_PLAN_SECONDS + 1),
    ('latency_stress_seconds', '600'),
    ('max_folds', 0), ('max_replay_evaluations', 1),
    ('thresholds', {'min_sharpe': 1.0}),
    ('thresholds', {'min_daily_observations': 59}),
    ('thresholds', {'max_drawdown': .11}),
    ('thresholds', {'typo': 1}),
    ('research_purpose', 'live'),
    ('research_purpose', []),
    ('strategy_specs', [{'family': []}]),
    ('strategy_specs', [{'family': 'equity_mean_reversion', 'variant': []}]),
])
def test_invalid_execution_settings_fail_before_output_or_data_access(tmp_path, monkeypatch, key, value):
    plan = lab.default_plan()
    plan[key] = value
    output = tmp_path / 'not-created'

    def forbidden(*args, **kwargs):
        pytest.fail('invalid plan reached data preparation or evaluation')

    monkeypatch.setattr(lab, 'research_inputs', forbidden)
    monkeypatch.setattr(lab, 'evaluate', forbidden)
    monkeypatch.setattr(lab, 'source_identity', forbidden)
    with pytest.raises(ValueError):
        lab.run_campaign([{'invalid': 'must not be read'}], output, plan=plan, now=0)
    assert not output.exists()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), Decimal('1'), (1, 2), object()])
def test_nonfinite_and_non_json_nested_values_are_rejected(value):
    plan = lab.default_plan()
    plan['strategy_specs'][0]['extra_metadata'] = {'nested': [value]}
    with pytest.raises(ValueError, match='finite native JSON'):
        validate_campaign_plan(plan)


def test_unknown_keys_non_string_keys_and_cycles_refuse():
    plan = lab.default_plan()
    plan['latency_stress_second'] = 600
    with pytest.raises(ValueError, match='unknown fields: latency_stress_second'):
        validate_campaign_plan(plan)
    del plan['latency_stress_second']
    plan[1] = 'unserializable key contract'
    with pytest.raises(ValueError, match='finite native JSON'):
        validate_campaign_plan(plan)
    del plan[1]
    plan['strategy_specs'].append(plan)
    with pytest.raises(ValueError, match='finite native JSON'):
        validate_campaign_plan(plan)


def test_documented_diagnostic_bounds_are_valid_without_running_replays():
    plan = lab.default_plan()
    plan.update(seed=2**64-1, bootstrap_resamples=5000,
                embargo_seconds=MAX_PLAN_SECONDS,
                label_horizon_seconds=MAX_PLAN_SECONDS,
                latency_stress_seconds=MAX_PLAN_SECONDS)
    assert validate_campaign_plan(plan)['qualification_established'] is False
    plan.update(seed=0, bootstrap_resamples=100, embargo_seconds=0,
                label_horizon_seconds=1, latency_stress_seconds=0)
    assert validate_campaign_plan(plan)['maximum_total_replays'] == 3733


def test_qualification_and_duplicate_checks_are_shared_with_preflight():
    plan = lab.default_plan()
    plan['research_purpose'] = 'qualification'
    with pytest.raises(ValueError, match='train is shorter'):
        validate_campaign_plan(plan)
    plan['research_purpose'] = 'diagnostic'
    plan['strategy_specs'].append(deepcopy(plan['strategy_specs'][0]))
    with pytest.raises(ValueError, match='duplicate hypotheses'):
        validate_campaign_plan(plan)
