"""Offline input coverage and fold planning; never signals, returns or allocation."""

from bisect import bisect_left
from collections import defaultdict

from atlas.research.campaign_preflight import validate_campaign_plan
from atlas.research.market import MAX_BARS
from atlas.research.multiasset_lab import digest, equity_calendar, research_inputs
from atlas.research.multiasset_validation import schedule_folds
from atlas.research.multiasset_strategies import normalize_spec

DAY = 86400
MAX_SCHEDULE_WORK = 5_000_000


def _grid_count(origin, last, interval, start, end):
    """Count completed native bucket closes in a half-open window arithmetically."""
    first_index = max(1, (start - origin + interval - 1) // interval)
    last_index = min((last - origin) // interval, (end - 1 - origin) // interval)
    return max(0, last_index - first_index + 1)


def _coverage(instrument, start, end, required_days):
    interval = instrument['interval_seconds']
    events = instrument['_events']
    selected = events[bisect_left(events, start):bisect_left(events, end)]
    available = [t for t in selected if instrument['_first_available'][t] < end]
    ingested = [t for t in selected if instrument['_first_ingested'][t] < end]
    publication_dates = {t // DAY for t in instrument['_publication_times'] if start <= t < end}
    if instrument['asset_class'] == 'equity':
        sessions = [s for s in instrument['_sessions']
                    if _grid_count(s['open_time'], s['close_time'], interval, start, end)]
        expected = sum(_grid_count(s['open_time'], s['close_time'], interval, start, end)
                       for s in sessions)
        observed_dates = {instrument['_session_dates'][t] for t in available}
        declared_sessions = len(sessions)
        ceiling = len(publication_dates & instrument['_declared_utc_dates'])
    else:
        expected = _grid_count(0, end - 1, interval, start, end)
        declared_sessions = None
        observed_dates = {t // DAY for t in available}
        ceiling = len(publication_dates)
    return {
        'dataset_id': instrument['dataset_id'], 'symbol': instrument['symbol'],
        'asset_class': instrument['asset_class'], 'interval_seconds': interval,
        'expected_bars_from_declared_calendar': expected,
        'event_window_bars': len(selected), 'available_event_window_bars': len(available),
        'ingested_event_window_bars': len(ingested),
        'availability_basis': 'campaign_publication_clock_only; ingestion_reported_separately',
        'missing_event_buckets': max(0, expected - len(selected)),
        'late_event_buckets': len(selected) - len(available),
        'declared_sessions': declared_sessions, 'observed_session_dates': len(observed_dates),
        'maximum_daily_observations': ceiling, 'required_daily_observations': required_days,
        'daily_observation_ceiling_met': ceiling >= required_days,
        'actual_completed_trades': None,
    }


def plan_campaign(datasets, *, plan, now, assume_archive_close=False):
    """Check explicitly selected inputs using the campaign's scheduling contract.

    Reads price fields only through canonical schema validation. No price-derived
    metrics or strategy signals are computed, and no state or result is written.
    Complete data within a window does not establish warmup, fills or eligibility.
    """
    preflight = validate_campaign_plan(plan)
    if type(assume_archive_close) is not bool:
        raise ValueError('archive close assumption must be a boolean')
    if (not isinstance(datasets, list) or not 1 <= len(datasets) <= 10
            or any(not isinstance(d, dict) or not isinstance(d.get('bars'), list) for d in datasets)
            or sum(len(d['bars']) for d in datasets) > MAX_BARS):
        raise ValueError('planning requires 1..10 datasets and at most 100000 total bars')
    bars, receipts, evidence, _ = research_inputs(
        datasets, now=now, assume_archive_close=assume_archive_close)
    equity_calendar(datasets)  # Same conflict refusal as the execution campaign.
    samples = sorted({b['information_available_time'] for b in bars})
    span = samples[-1] - samples[0] + 1
    minimum = sum(plan[k + '_seconds'] for k in ('train', 'validation', 'test')) + 2 * plan['embargo_seconds']
    potential_windows = max(0, (span - minimum) // plan['test_seconds'] + 1)
    work = len(samples) * potential_windows
    if work > MAX_SCHEDULE_WORK:
        raise ValueError('planning schedule exceeds 5000000 sample-window work limit')
    scheduled = schedule_folds(samples, train_seconds=plan['train_seconds'],
        validation_seconds=plan['validation_seconds'], test_seconds=plan['test_seconds'],
        embargo_seconds=plan['embargo_seconds'], label_horizon_seconds=plan['label_horizon_seconds'])[:plan['max_folds']]

    rows_by_source = defaultdict(list)
    for row in bars:
        rows_by_source[(row['source_id'], row['symbol'])].append(row)
    instruments = []
    for dataset in datasets:
        for symbol in sorted(dataset['symbols']):
            # Dataset membership follows its event keys, not a potentially reused source label.
            keys = {(b['event_time'], b['revision']) for b in dataset['bars'] if b['symbol'] == symbol}
            rows = [b for b in rows_by_source[(dataset['source']['source_id'], symbol)]
                    if (b['event_time'], b['revision']) in keys]
            if not rows:
                continue
            intervals = {b['interval_seconds'] for b in rows}
            if len(intervals) != 1:
                raise ValueError('planning requires one native interval per dataset symbol')
            first_available, first_ingested, session_dates = {}, {}, {}
            for b in rows:
                t = b['event_time']
                first_available[t] = min(first_available.get(t, b['information_available_time']), b['information_available_time'])
                first_ingested[t] = min(first_ingested.get(t, b['ingestion_time']), b['ingestion_time'])
                session_dates[t] = b['session_date']
            events = sorted(first_available)
            instruments.append({
                'dataset_id': dataset['dataset_id'], 'symbol': symbol,
                'asset_class': dataset['asset_class'], 'venue': dataset['venue'],
                'interval_seconds': intervals.pop(), 'volume_unit': dataset['volume_unit'],
                'rows': len(rows), 'unique_events': len(events), 'revisions': len(rows) - len(events),
                'first_event_time': events[0], 'last_event_time': events[-1],
                'first_information_available_time': min(b['information_available_time'] for b in rows),
                'last_information_available_time': max(b['information_available_time'] for b in rows),
                'spread_observations': sum('bid' in b and 'ask' in b for b in rows),
                '_events': events, '_first_available': first_available, '_session_dates': session_dates,
                '_first_ingested': first_ingested,
                '_publication_times': {b['information_available_time'] for b in rows},
                '_sessions': dataset.get('calendar', {}).get('sessions', []),
                '_declared_utc_dates': {s['open_time'] // DAY for s in dataset.get('calendar', {}).get('sessions', [])},
            })
    present = {b['symbol'] for b in bars}
    specs = [normalize_spec(spec) for spec in plan['strategy_specs']]
    missing = sorted({symbol for spec in specs for symbol in spec['symbols']} - present)
    warnings = list(preflight['warnings'])
    if not scheduled:
        warnings.append('no_complete_folds_on_selected_information_availability_clock')
    if missing:
        warnings.append('required_strategy_symbols_missing')
    if any(i['volume_unit'] == 'unknown' for i in instruments):
        warnings.append('unknown_volume_units_do_not_establish_fill_capacity')
    folds = []
    for fold in scheduled:
        splits = {}
        for name, bounds in fold['ranges'].items():
            purge = fold['purged'][name]
            coverage = [_coverage(i, bounds['start'], bounds['end_exclusive'],
                                 preflight['splits'][name]['required_daily_observations']) for i in instruments]
            splits[name] = {
                'available_publication_groups': purge['candidate_count'],
                'purge_kept_publication_groups': purge['kept'],
                'purged_publication_groups': purge['unresolved_at_window_end'],
                'instruments': coverage, 'actual_completed_trades': None,
            }
        folds.append({'fold_id': fold['fold_id'], 'ranges': fold['ranges'],
                      'purged': fold['purged'], 'splits': splits})
    return {
        'schema_version': 1, 'mode': 'planning_only', 'plan_hash': digest(plan),
        'preflight': preflight, 'inputs': receipts, 'evidence_class': evidence,
        'assume_archive_close': assume_archive_close,
        'information_clock': 'assumed_bar_close' if assume_archive_close else 'canonical_information_available_time',
        'unique_publication_groups': len(samples), 'publication_span_seconds': span,
        'schedule_work_upper_bound': work, 'fold_count': len(folds), 'folds': folds,
        'instruments': [{k: v for k, v in i.items() if not k.startswith('_')} for i in instruments],
        'missing_required_symbols': missing, 'warnings': warnings,
        'actual_completed_trades': None, 'qualification_established': False,
        'live_approved': False, 'actual_orders_submitted': 0, 'real_money_allocated': '0',
        'limitations': [
            'Coverage uses native bar closes and the declared calendar; missing buckets are not filled.',
            'Declared calendars, source labels and archive integrity do not prove provider authenticity or historical publication.',
            'Event-window coverage and availability-clock scheduling are distinct; delayed publications may move observations between windows.',
            'Daily counts use publication UTC dates (intersected with declared equity sessions) before strategy warmup, fills and valuation checks; they are ceilings, not actual replay observations.',
            'Availability coverage follows the campaign publication clock only; original ingestion counts are separate and a retrospective close assumption does not prove data was observed then.',
            'A feasible fold is not an independent trade sample, a qualification result or evidence of profitability.',
            'No signals, strategy ranking, returns, replays or one-time forward evaluation were computed.',
            'Already inspected historical data remains retrospective; the close assumption never upgrades its evidence class.',
        ],
    }
