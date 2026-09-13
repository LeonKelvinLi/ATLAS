"""Planning coverage is calendar/provenance evidence, never trading evidence."""

from copy import deepcopy
from datetime import datetime, timezone
import builtins
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import urllib.request

import pytest

from atlas.research import campaign_planning as planning
from atlas.research import multiasset_lab as lab
from atlas.research import multiasset_validation as validation
from atlas.research import replay
from atlas.research.campaign_preflight import validate_campaign_plan


DAY = 86400
NOW = 1789158600  # 2026-09-11 UTC, fixed independently of the test machine.
START = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())


def campaign_plan(**changes):
    result = lab.default_plan(train_days=1, validation_days=1, test_days=1,
                              embargo_seconds=0, max_folds=1)
    result['strategy_specs'] = result['strategy_specs'][:1]
    result.update(changes)
    return result


def crypto(*, days=4, interval=1800, start=START, symbol='BTC-USD'):
    bars = []
    for event in range(start + interval, start + days * DAY + 1, interval):
        bars.append({
            'symbol': symbol, 'asset_class': 'crypto', 'venue': 'SYNTHETIC',
            'event_time': event, 'interval_seconds': interval,
            'information_available_time': event, 'ingestion_time': event,
            'timezone': 'UTC', 'session': '24/7',
            'session_date': datetime.fromtimestamp(event - 1, timezone.utc).date().isoformat(),
            'open': '100', 'high': '101', 'low': '99', 'close': '100',
            'volume': '10', 'revision': 0, 'source_id': 'planning-fixture',
        })
    return {
        'schema_version': '1.0', 'dataset_id': 'planning-' + symbol,
        'source': {'source_id': 'planning-fixture', 'kind': 'synthetic',
                   'description': 'Deterministic planning fixture, not market evidence.'},
        'asset_class': 'crypto', 'symbols': [symbol], 'venue': 'SYNTHETIC',
        'price_adjustment': 'unadjusted',
        'universe': {'selection_method': 'fixed_fixture', 'survivorship_bias': 'unknown'},
        'availability_basis': 'synthetic', 'revision_policy': 'synthetic',
        'currency': 'USD', 'volume_unit': 'base_asset', 'bars': bars,
        'calendar': {'calendar_id': 'UTC_24_7', 'timezone': 'UTC', 'version': '1'},
    }


def equity(*, early_close=False):
    result = lab.synthetic_datasets(days=4, start='2026-09-01')[0]
    result['symbols'] = ['SPY']
    result['bars'] = [r for r in result['bars'] if r['symbol'] == 'SPY']
    if early_close:
        session = result['calendar']['sessions'][1]
        session['close_time'] -= 3 * 3600
        result['bars'] = [r for r in result['bars']
                          if r['session_date'] != session['session_date']
                          or r['event_time'] <= session['close_time']]
    return result


def archived(data):
    result = deepcopy(data)
    result['source']['kind'] = 'real'
    result['availability_basis'] = 'unknown'
    result['revision_policy'] = 'latest_only'
    for row in result['bars']:
        row['information_available_time'] = row['ingestion_time'] = NOW - 1
    return result


def instrument(split, symbol):
    return next(item for item in split['instruments'] if item['symbol'] == symbol)


def expected_closes(dataset, symbol, start, end):
    interval = next(r['interval_seconds'] for r in dataset['bars'] if r['symbol'] == symbol)
    if dataset['asset_class'] == 'crypto':
        first = ((start + interval - 1) // interval) * interval
        return set(range(first, end, interval))
    return {event for session in dataset['calendar']['sessions']
            for event in range(session['open_time'] + interval, session['close_time'] + 1, interval)
            if start <= event < end}


def assert_no_qualification(result):
    assert result['mode'] == 'planning_only'
    assert result['qualification_established'] is False
    assert result['live_approved'] is False
    assert result['actual_orders_submitted'] == 0
    assert result['preflight']['qualification_established'] is False
    for fold in result['folds']:
        for split in fold['splits'].values():
            assert split['actual_completed_trades'] is None
            assert not {'sharpe', 'cagr', 'net_return', 'profit_factor', 'fills'} & split.keys()


def test_publication_folds_and_receipts_reuse_existing_contract_without_mutation():
    datasets = [equity(), crypto()]
    plan = campaign_plan(embargo_seconds=300)
    original = deepcopy((datasets, plan))
    bars, receipts, evidence, _ = lab.research_inputs(datasets, now=NOW)
    samples = sorted({r['information_available_time'] for r in bars})
    expected = validation.schedule_folds(
        samples, train_seconds=plan['train_seconds'], validation_seconds=plan['validation_seconds'],
        test_seconds=plan['test_seconds'], embargo_seconds=plan['embargo_seconds'],
        label_horizon_seconds=plan['label_horizon_seconds'])[:plan['max_folds']]
    result = planning.plan_campaign(datasets, plan=plan, now=NOW)
    assert (datasets, plan) == original
    assert result['preflight'] == validate_campaign_plan(plan)
    assert result['plan_hash'] == lab.digest(plan)
    assert result['inputs'] == receipts and result['evidence_class'] == evidence == 'SYNTHETIC'
    assert result['fold_count'] == len(result['folds']) == len(expected) == 1
    for actual, reference in zip(result['folds'], expected):
        for field in ('fold_id', 'ranges', 'purged'):
            assert actual[field] == reference[field]
        for name, split in actual['splits'].items():
            purge = reference['purged'][name]
            assert split['available_publication_groups'] == purge['candidate_count']
            assert split['purge_kept_publication_groups'] == purge['kept']
            assert split['purged_publication_groups'] == purge['unresolved_at_window_end']
    assert_no_qualification(result)


def test_native_coverage_uses_half_open_calendar_closes_not_elapsed_hour_counts():
    datasets = [equity(), crypto()]
    result = planning.plan_campaign(datasets, plan=campaign_plan(), now=NOW)
    fold = result['folds'][0]
    for name, split in fold['splits'].items():
        start, end = (fold['ranges'][name][key] for key in ('start', 'end_exclusive'))
        for dataset in datasets:
            symbol = dataset['symbols'][0]
            actual = instrument(split, symbol)
            events = expected_closes(dataset, symbol, start, end)
            assert actual['dataset_id'] == dataset['dataset_id']
            assert actual['asset_class'] == dataset['asset_class']
            assert actual['expected_bars_from_declared_calendar'] == len(events)
            assert actual['event_window_bars'] == actual['available_event_window_bars'] == len(events)
            assert actual['ingested_event_window_bars'] == len(events)
            assert actual['availability_basis'] == 'campaign_publication_clock_only; ingestion_reported_separately'
            assert actual['missing_event_buckets'] == actual['late_event_buckets'] == 0
            assert actual['required_daily_observations'] == 60
            assert actual['daily_observation_ceiling_met'] is False
            assert actual['maximum_daily_observations'] <= 2
            if dataset['asset_class'] == 'crypto':
                assert actual['declared_sessions'] is None
            else:
                assert actual['declared_sessions'] == 1
                assert actual['observed_session_dates'] == 1
    assert_no_qualification(result)


def test_missing_event_and_late_publication_are_distinct_no_interpolation():
    datasets = [equity(), crypto()]
    baseline = planning.plan_campaign(datasets, plan=campaign_plan(), now=NOW)
    bounds = baseline['folds'][0]['ranges']['train']
    rows = datasets[0]['bars']
    missing = rows.pop(4)
    late = rows[8]
    late['information_available_time'] = late['ingestion_time'] = bounds['end_exclusive']
    rows.sort(key=lambda r: (r['information_available_time'], r['symbol']))
    result = planning.plan_campaign(datasets, plan=campaign_plan(), now=NOW)
    actual = instrument(result['folds'][0]['splits']['train'], 'SPY')
    previous = instrument(baseline['folds'][0]['splits']['train'], 'SPY')
    assert bounds['start'] <= missing['event_time'] < bounds['end_exclusive']
    assert actual['expected_bars_from_declared_calendar'] == previous['expected_bars_from_declared_calendar']
    assert actual['event_window_bars'] == previous['event_window_bars'] - 1
    assert actual['available_event_window_bars'] == previous['available_event_window_bars'] - 2
    assert actual['missing_event_buckets'] == 1 and actual['late_event_buckets'] == 1


def test_late_ingestion_is_reported_separately_from_campaign_publication_clock():
    datasets = [equity(), crypto()]
    plan = campaign_plan()
    baseline = planning.plan_campaign(datasets, plan=plan, now=NOW)
    end = baseline['folds'][0]['ranges']['train']['end_exclusive']
    datasets[0]['bars'][5]['ingestion_time'] = end
    result = planning.plan_campaign(datasets, plan=plan, now=NOW)
    actual = instrument(result['folds'][0]['splits']['train'], 'SPY')
    previous = instrument(baseline['folds'][0]['splits']['train'], 'SPY')
    assert actual['event_window_bars'] == previous['event_window_bars']
    assert actual['available_event_window_bars'] == previous['available_event_window_bars']
    assert actual['ingested_event_window_bars'] == previous['ingested_event_window_bars'] - 1
    assert actual['late_event_buckets'] == 0
    assert actual['availability_basis'] == 'campaign_publication_clock_only; ingestion_reported_separately'


def test_early_close_reduces_expected_buckets_without_counting_missing_afternoon():
    dataset = equity(early_close=True)
    result = planning.plan_campaign([dataset, crypto()], plan=campaign_plan(), now=NOW)
    second = instrument(result['folds'][0]['splits']['validation'], 'SPY')
    first = instrument(result['folds'][0]['splits']['train'], 'SPY')
    assert first['expected_bars_from_declared_calendar'] == 78
    assert second['expected_bars_from_declared_calendar'] == 42
    assert second['event_window_bars'] == second['available_event_window_bars'] == 42
    assert second['missing_event_buckets'] == second['late_event_buckets'] == 0
    assert second['declared_sessions'] == second['maximum_daily_observations'] == 1


def test_entire_declared_session_missing_stays_visible_in_calendar_coverage():
    data = equity()
    absent_date = data['calendar']['sessions'][1]['session_date']
    data['bars'] = [row for row in data['bars'] if row['session_date'] != absent_date]
    result = planning.plan_campaign([data, crypto()], plan=campaign_plan(), now=NOW)
    coverage = instrument(result['folds'][0]['splits']['validation'], 'SPY')
    assert coverage['declared_sessions'] == 1
    assert coverage['expected_bars_from_declared_calendar'] == coverage['missing_event_buckets'] == 78
    assert coverage['event_window_bars'] == coverage['available_event_window_bars'] == 0
    assert coverage['observed_session_dates'] == coverage['maximum_daily_observations'] == 0
    assert coverage['daily_observation_ceiling_met'] is False


def test_delayed_prior_session_publication_counts_its_publication_date_not_its_event_date():
    data = equity()
    first, second = data['calendar']['sessions'][:2]
    data['bars'] = [row for row in data['bars'] if row['session_date'] != second['session_date']]
    late = next(row for row in data['bars'] if row['event_time'] == first['close_time'])
    late['information_available_time'] = late['ingestion_time'] = second['open_time'] + 300
    data['bars'].sort(key=lambda row: (row['information_available_time'], row['symbol']))
    result = planning.plan_campaign([data, crypto()], plan=campaign_plan(), now=NOW)
    coverage = instrument(result['folds'][0]['splits']['validation'], 'SPY')
    assert coverage['event_window_bars'] == coverage['observed_session_dates'] == 0
    assert coverage['maximum_daily_observations'] == 1
    assert coverage['missing_event_buckets'] == 78
    assert_no_qualification(result)


def test_revision_adds_publication_event_but_not_market_bucket_or_session():
    datasets = [equity(), crypto()]
    baseline = planning.plan_campaign(datasets, plan=campaign_plan(), now=NOW)
    revision = deepcopy(datasets[0]['bars'][5])
    revision.update(revision=1, close='100', open='100', high='101', low='99', volume='999999',
                    information_available_time=revision['event_time'] + 1,
                    ingestion_time=revision['event_time'] + 1)
    datasets[0]['bars'].append(revision)
    datasets[0]['bars'].sort(key=lambda r: (r['information_available_time'], r['symbol']))
    result = planning.plan_campaign(datasets, plan=campaign_plan(), now=NOW)
    prior = baseline['folds'][0]['splits']['train']
    after = result['folds'][0]['splits']['train']
    assert after['available_publication_groups'] == prior['available_publication_groups'] + 1
    for field in ('event_window_bars', 'available_event_window_bars', 'declared_sessions',
                  'observed_session_dates', 'maximum_daily_observations', 'missing_event_buckets'):
        assert instrument(after, 'SPY')[field] == instrument(prior, 'SPY')[field]


def test_capture_only_archive_has_no_historical_folds_and_assumption_never_upgrades_evidence():
    datasets = [archived(crypto())]
    before = deepcopy(datasets)
    native = planning.plan_campaign(datasets, plan=campaign_plan(), now=NOW)
    assumed = planning.plan_campaign(datasets, plan=campaign_plan(), now=NOW, assume_archive_close=True)
    assert native['fold_count'] == 0 and native['folds'] == []
    assert assumed['fold_count'] == 1
    assert native['evidence_class'] == assumed['evidence_class'] == 'HISTORICAL_UNVERIFIED'
    assert not native['inputs'][0]['archive_close_assumption']
    assert assumed['inputs'][0]['archive_close_assumption'] is True
    for split in assumed['folds'][0]['splits'].values():
        coverage = instrument(split, 'BTC-USD')
        assert coverage['available_event_window_bars'] > 0
        assert coverage['ingested_event_window_bars'] == 0
    assert datasets == before
    assert_no_qualification(native)
    assert_no_qualification(assumed)


def test_missing_required_symbols_are_explicit_without_rejecting_usable_dataset():
    plan = campaign_plan()
    required = {symbol for spec in plan['strategy_specs'] for symbol in spec['symbols']}
    result = planning.plan_campaign([crypto()], plan=plan, now=NOW)
    assert set(result['missing_required_symbols']) == required - {'BTC-USD'}
    assert 'SPY' in result['missing_required_symbols']
    assert result['fold_count'] == 1
    assert {item['symbol'] for item in result['instruments']} == {'BTC-USD'}


def test_strategy_id_shorthand_preserves_coverage_and_missing_symbols_but_hashes_raw_plan():
    full = campaign_plan()
    shorthand = deepcopy(full)
    shorthand['strategy_specs'] = [{'strategy_id': spec['strategy_id']} for spec in full['strategy_specs']]
    original = deepcopy(shorthand)
    expanded = planning.plan_campaign([crypto()], plan=full, now=NOW)
    compact = planning.plan_campaign([crypto()], plan=shorthand, now=NOW)
    assert shorthand == original
    assert compact['folds'] == expanded['folds']
    assert compact['preflight'] == expanded['preflight']
    assert compact['missing_required_symbols'] == expanded['missing_required_symbols']
    assert compact['plan_hash'] == lab.digest(shorthand)
    assert compact['plan_hash'] != expanded['plan_hash']


def test_unknown_volume_remains_unknown_and_does_not_prevent_calendar_planning():
    data = crypto()
    data['volume_unit'] = 'unknown'
    result = planning.plan_campaign([data], plan=campaign_plan(), now=NOW)
    assert result['fold_count'] == 1
    assert 'volume_unit_unknown_no_fill_capacity_assumption' in result['inputs'][0]['quality']['blockers']
    assert data['volume_unit'] == 'unknown'
    assert result['instruments'][0]['volume_unit'] == 'unknown'
    assert_no_qualification(result)


def test_future_prices_change_receipts_but_not_schedule_or_coverage():
    datasets = [crypto()]
    plan = campaign_plan()
    baseline = planning.plan_campaign(datasets, plan=plan, now=NOW)
    cutoff = baseline['folds'][0]['ranges']['test']['start']
    for row in datasets[0]['bars']:
        if row['event_time'] >= cutoff:
            row.update(open='1000', high='1001', low='999', close='1000', volume='999999')
    changed = planning.plan_campaign(datasets, plan=plan, now=NOW)
    assert changed['inputs'][0]['dataset_hash'] != baseline['inputs'][0]['dataset_hash']
    assert changed['folds'] == baseline['folds']
    assert changed['plan_hash'] == baseline['plan_hash']
    assert changed['missing_required_symbols'] == baseline['missing_required_symbols']


def test_sixty_observed_dates_are_only_a_ceiling_never_completed_trades_or_qualification():
    end = NOW // DAY * DAY
    data = crypto(days=200, interval=DAY, start=end - 200 * DAY)
    plan = campaign_plan(research_purpose='qualification', train_seconds=60 * DAY,
                         validation_seconds=60 * DAY, test_seconds=60 * DAY)
    result = planning.plan_campaign([data], plan=plan, now=NOW)
    assert result['fold_count'] == 1
    for split in result['folds'][0]['splits'].values():
        item = instrument(split, 'BTC-USD')
        assert item['maximum_daily_observations'] == 60
        assert item['daily_observation_ceiling_met'] is True
        assert split['actual_completed_trades'] is None
    assert_no_qualification(result)


def test_planning_invokes_no_replay_signals_files_process_database_or_network(monkeypatch):
    datasets = [crypto()]
    plan = campaign_plan()

    def forbidden(*args, **kwargs):
        pytest.fail('planning invoked an execution, I/O, or signal path')

    with monkeypatch.context() as guard:
        for module, names in ((lab, ('run_campaign', 'evaluate', 'signals_for', 'benchmark',
                                    'metrics_for', 'write_json', 'source_identity')),
                              (validation, ('metrics_for', 'stress_diagnostics', 'rank_candidates', 'gate_result')),
                              (replay, ('run_replay',)),
                              (planning, ('evaluate', 'signals_for', 'metrics_for', 'write_json', 'source_identity'))):
            for name in names:
                if hasattr(module, name):
                    guard.setattr(module, name, forbidden)
        guard.setattr(replay.ReplayEngine, '__init__', forbidden)
        for module, name in ((builtins, 'open'), (io, 'open'), (os, 'open'),
                             (Path, 'mkdir'), (Path, 'write_text'), (Path, 'write_bytes'),
                             (socket, 'socket'), (socket, 'create_connection'),
                             (sqlite3, 'connect'), (subprocess, 'Popen'), (urllib.request, 'urlopen')):
            guard.setattr(module, name, forbidden)
        result = planning.plan_campaign(datasets, plan=plan, now=NOW)
    assert result['fold_count'] == 1
    assert_no_qualification(result)


def test_more_than_ten_datasets_rejected_before_canonical_work(monkeypatch):
    def canonical_work(*args, **kwargs):
        pytest.fail('dataset count should fail before canonical parsing')

    monkeypatch.setattr(lab, 'research_inputs', canonical_work)
    if hasattr(planning, 'research_inputs'):
        monkeypatch.setattr(planning, 'research_inputs', canonical_work)
    with pytest.raises(ValueError, match='dataset|10|bound|limit'):
        planning.plan_campaign([{}] * 11, plan=campaign_plan(), now=NOW)


@pytest.mark.parametrize('partition', [1, 2])
def test_more_than_one_hundred_thousand_total_rows_rejected_before_canonical_work(monkeypatch, partition):
    def canonical_work(*args, **kwargs):
        pytest.fail('row count should fail before canonical parsing')

    monkeypatch.setattr(lab, 'research_inputs', canonical_work)
    if hasattr(planning, 'research_inputs'):
        monkeypatch.setattr(planning, 'research_inputs', canonical_work)
    oversized = crypto(days=2)
    oversized['bars'] = [oversized['bars'][0]] * (100_001 if partition == 1 else 50_001)
    with pytest.raises(ValueError, match='rows|bars|100000|100,000|bound|limit'):
        planning.plan_campaign([oversized] * partition, plan=campaign_plan(), now=NOW)


def test_scheduler_work_is_bounded_before_enumerating_excessive_windows(monkeypatch):
    # 5,760 minute publications times thousands of possible short windows exceed
    # the five-million work ceiling even though the requested result has one fold.
    data = crypto(days=4, interval=60)
    plan = campaign_plan(train_seconds=120, validation_seconds=120, test_seconds=120,
                         label_horizon_seconds=1)

    def unbounded_schedule(*args, **kwargs):
        pytest.fail('scheduler must not run after its work ceiling is exceeded')

    monkeypatch.setattr(validation, 'schedule_folds', unbounded_schedule)
    if hasattr(planning, 'schedule_folds'):
        monkeypatch.setattr(planning, 'schedule_folds', unbounded_schedule)
    with pytest.raises(ValueError, match='work|bound|limit'):
        planning.plan_campaign([data], plan=plan, now=NOW)
