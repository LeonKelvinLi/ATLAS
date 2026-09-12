import copy

import pytest

from atlas.research.campaign_preflight import DAY, campaign_preflight
from atlas.research.multiasset_lab import default_plan, run_campaign


def test_default_receipt_counts_every_replay_and_flags_short_windows():
    report = campaign_preflight(default_plan())
    assert report['hypothesis_risk_variants'] == 177
    assert report['maximum_candidate_windows'] == 531
    assert report['maximum_strategy_replays'] == 3717
    assert report['maximum_total_replays'] == 3733
    assert report['splits']['validation']['maximum_utc_dates'] == 6
    assert len(report['warnings']) == 3
    assert report['outside_campaign_attempt_history'] == 'unknown'
    assert report['qualification_established'] is False


def test_budget_includes_benchmarks_and_cash_before_files_or_data(tmp_path):
    plan = default_plan()
    plan['max_replay_evaluations'] = 3717
    output = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError, match='3733 exceeds budget'):
        run_campaign([{'invalid': 'data never evaluated'}], output, plan=plan)
    assert not output.exists()
    plan['max_replay_evaluations'] = 3733
    assert campaign_preflight(plan)['maximum_total_replays'] == 3733
    plan['max_replay_evaluations'] = 3732
    with pytest.raises(ValueError, match='exceeds budget'):
        campaign_preflight(plan)


def test_qualification_requires_sufficient_elapsed_time_but_cannot_grant_evidence():
    plan = default_plan()
    plan['research_purpose'] = 'qualification'
    with pytest.raises(ValueError, match='train is shorter'):
        campaign_preflight(plan)
    plan.update(train_seconds=60*DAY, validation_seconds=60*DAY, test_seconds=60*DAY)
    report = campaign_preflight(plan)
    assert not report['warnings']
    assert report['splits']['test']['actual_session_count'] is None
    assert report['splits']['test']['actual_completed_trades'] is None
    assert report['qualification_established'] is False
    assert report['live_approved'] is False
    plan['thresholds']['min_daily_observations'] = 90
    with pytest.raises(ValueError, match='90 elapsed days'):
        campaign_preflight(plan)


@pytest.mark.parametrize('key,value', [
    ('max_folds', True), ('max_folds', 11), ('max_replay_evaluations', True),
    ('max_replay_evaluations', 21001), ('max_replay_evaluations', 0),
    ('research_purpose', 'winning'), ('train_seconds', True),
    ('test_seconds', float('nan')), ('validation_seconds', 0),
    ('risk_fractions', ['0.01']),
])
def test_malformed_workload_is_rejected(key, value):
    plan = default_plan()
    plan[key] = value
    with pytest.raises(ValueError):
        campaign_preflight(plan)


def test_duplicate_hypotheses_rejected_and_input_unchanged():
    plan = default_plan()
    original = copy.deepcopy(plan)
    campaign_preflight(plan)
    assert plan == original
    plan['strategy_specs'].append(plan['strategy_specs'][0])
    with pytest.raises(ValueError, match='duplicate'):
        campaign_preflight(plan)


def test_existing_plan_uses_explicit_receipt_defaults():
    plan = default_plan()
    del plan['max_replay_evaluations'], plan['research_purpose']
    result = campaign_preflight(plan)
    assert result['research_purpose'] == 'diagnostic'
    assert result['max_replay_evaluations'] == 5000
