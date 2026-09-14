"""Coverage must preserve data clocks and never expose held-out observations."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from atlas.research import data_coverage as coverage
from atlas.research.data_contract_v1 import default_contract


DAY = 86400
START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
HOLDOUT = START + 180 * DAY + 600
CREATED = HOLDOUT - DAY
NOW = HOLDOUT + DAY
NY = ZoneInfo('America/New_York')


def contract():
    return default_contract('coverage-fixture-v1', START, CREATED)


def bar(event=None, *, symbol='BTC-USD', ingestion=None, kind='historical_backfill', revision=0):
    event = START + 300 if event is None else event
    asset = 'crypto' if symbol == 'BTC-USD' else 'equity'
    session = None
    if asset == 'equity':
        day = datetime.fromtimestamp(event, NY).date()
        session = dict(calendar_id='Alpaca-calendar', session_date=day.isoformat(),
                       open_time=int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY).timestamp()),
                       close_time=int(datetime(day.year, day.month, day.day, 16, tzinfo=NY).timestamp()),
                       calendar_verified=False)
    receipt = event if ingestion is None else ingestion
    return {
        'contract_version': 'ATLAS_DATA_CONTRACT_V1', 'record_type': 'bar',
        'symbol': symbol, 'asset_class': asset,
        'venue': 'coinbase-exchange' if asset == 'crypto' else 'alpaca-iex',
        'source_id': 'fixture-provider', 'event_time': event, 'publication_time': None,
        'available_time': receipt, 'ingestion_time': receipt, 'revision': revision,
        'previous_observation_hash': None if revision == 0 else 'a' * 64,
        'raw_capture_sha256': 'b' * 64, 'acquisition_kind': kind,
        'revision_basis': 'locally_observed_versions', 'interval_seconds': 300,
        'session': session,
        'payload': {'open': '17001', 'high': '17009', 'low': '16999', 'close': '17007',
                    'volume': '9997', 'volume_unit': 'base_asset' if asset == 'crypto' else 'shares',
                    'currency': 'USD', 'price_adjustment': 'unadjusted',
                    'corporate_actions_status': 'not_applicable' if asset == 'crypto' else 'unknown'},
    }


def quote(event, *, ingestion=None, revision=0):
    result = bar(event, ingestion=ingestion, kind='prospective_observation' if event >= CREATED else 'historical_backfill',
                 revision=revision)
    result['record_type'] = 'quote'
    result['interval_seconds'] = 0
    result['payload'] = {
        'bid': '17005', 'ask': '17008', 'bid_size': '11', 'ask_size': '13',
        'size_unit': 'base_asset', 'currency': 'USD', 'timestamp_basis': 'provider',
        'provider_timestamp': datetime.fromtimestamp(event, timezone.utc).isoformat(),
    }
    return result


def calendar(day='2026-01-02', *, opening_hour=9, closing_hour=16, received=START, end_date=None):
    parsed = datetime.fromisoformat(day)
    session = {'session_date': day,
               'open_time': int(parsed.replace(hour=opening_hour, minute=30, tzinfo=NY).timestamp()),
               'close_time': int(parsed.replace(hour=closing_hour, tzinfo=NY).timestamp())}
    return {'start_date': day, 'end_date': end_date or day, 'sessions': [session],
            'source_id': 'fixture-calendar', 'raw_capture_sha256': 'c' * 64, 'received_at': received}


def item(report, name='research', symbol='BTC-USD'):
    return next(row for row in report['windows'][name]['instruments'] if row['symbol'] == symbol)


def run(rows=(), *, now=NOW, calendars=(), freeze=None):
    return coverage.coverage_report(contract(), rows, now=now, calendar_coverage=calendars, strategy_freeze=freeze)


def test_empty_report_is_exact_about_future_unknown_and_missing_assets():
    result = run()
    btc = item(result)
    assert btc['expected_event_buckets'] == 90 * 288 - 1
    assert btc['missing_due_event_buckets'] == btc['expected_event_buckets']
    assert item(result, symbol='SPY')['expected_event_buckets'] is None
    assert item(result, symbol='SPY')['calendar_uncovered_date_count'] > 0
    assert result['windows']['prospective_holdout']['elapsed'] is False
    assert 'window_not_elapsed' in item(result, 'prospective_holdout')['blockers']
    assert result['qualification_established'] is False
    assert result['can_support_90_90_90'] is False
    assert result['actual_orders_submitted'] == 0


def test_event_bars_are_distinct_from_late_backfill_and_original_ingestion_is_preserved():
    row = bar(ingestion=NOW)
    before = deepcopy(row)
    result = run([row])
    btc = item(result)
    assert btc['observed_event_buckets'] == 1
    assert btc['as_of_window_end_event_buckets'] == 0
    assert btc['late_event_buckets'] == 1
    assert btc['event_daily_observations'] == 1
    assert btc['daily_observations_as_of_window_end'] == 0
    assert 'stale_at_receipt' in btc['blockers']
    assert row == before


def test_as_of_window_end_keeps_original_visible_revision_even_after_later_update():
    original = bar()
    revision = bar(ingestion=NOW, revision=1)
    revision['payload']['close'] = '17008'
    result = run([revision, original])  # No input-order assumption.
    btc = item(result)
    assert btc['observed_event_buckets'] == 1
    assert btc['as_of_window_end_event_buckets'] == 1
    assert btc['late_event_buckets'] == 0


def test_future_receipts_are_structurally_validated_then_excluded_from_as_of_view():
    row = bar(ingestion=NOW + 1)
    result = run([row])
    assert result['future_receipts_excluded'] == 1
    assert item(result)['observed_event_buckets'] == 0
    row['payload']['high'] = '1'
    with pytest.raises(coverage.DataCoverageError, match='OHLC'):
        run([row])


def test_availability_does_not_override_later_ingestion_clock():
    row = bar(ingestion=NOW)
    row['available_time'] = row['event_time']
    assert item(run([row]))['as_of_window_end_event_buckets'] == 0


def test_complete_bar_boundaries_and_embargo_are_not_rounded_into_windows():
    c = contract()
    end = c['windows']['research']['end_exclusive']
    vstart = c['windows']['validation']['start']
    rows = [bar(START), bar(START + 300), bar(end), bar(vstart), bar(vstart + 300)]
    result = run(rows)
    assert item(result)['observed_event_buckets'] == 1
    assert item(result, 'validation')['observed_event_buckets'] == 1
    assert result['visible_records_outside_complete_window_bars_or_quotes'] == 3


def test_holdout_requires_actual_prospective_kind_and_pre_start_freeze():
    event = HOLDOUT + 300
    rows = [bar(event, kind='historical_backfill'), bar(event + 300, kind='test'),
            bar(event + 600, kind='prospective_observation')]
    result = run(rows, freeze={'strategy_sha256': 'd' * 64, 'frozen_at': CREATED})
    btc = item(result, 'prospective_holdout')
    assert btc['observed_event_buckets'] == 3
    assert btc['prospective_as_of_window_end_event_buckets'] == 1
    assert btc['daily_observations_as_of_window_end'] == 1
    assert 'holdout_contains_nonprospective_or_late_observations' in btc['blockers']
    assert 'strategy_not_frozen' not in btc['blockers']


@pytest.mark.parametrize('freeze,code', [
    (None, 'strategy_not_frozen'),
    ({'strategy_sha256': 'd' * 64, 'frozen_at': HOLDOUT}, 'strategy_not_frozen_before_holdout'),
    ({'strategy_sha256': 'd' * 64, 'frozen_at': NOW + 1}, 'strategy_freeze_not_visible_as_of_now'),
])
def test_freeze_does_not_get_invented_or_backdated(freeze, code):
    assert code in item(run(freeze=freeze), 'prospective_holdout')['blockers']


@pytest.mark.parametrize('freeze', [
    {}, {'strategy_sha256': 'invalid', 'frozen_at': CREATED},
    {'strategy_sha256': 'd' * 64, 'frozen_at': CREATED - 1},
    {'strategy_sha256': 'd' * 64, 'frozen_at': CREATED, 'extra': True},
])
def test_invalid_freeze_refuses(freeze):
    with pytest.raises(coverage.DataCoverageError):
        run(freeze=freeze)


def test_full_session_gaps_distinguished_from_explicit_closed_dates():
    cal = calendar(end_date='2026-01-04')
    opening = cal['sessions'][0]['open_time']
    result = run([bar(opening + 300, symbol='SPY')], calendars=[cal])
    spy = item(result, symbol='SPY')
    assert spy['expected_buckets_from_known_calendar'] == 78
    assert spy['missing_due_event_buckets'] == 77
    assert spy['declared_sessions'] == 1
    assert spy['incomplete_session_count'] == 1
    assert spy['missing_due_session_count'] == 0
    qqq = item(result, symbol='QQQ')
    assert qqq['missing_due_session_count'] == 1
    assert qqq['missing_session_dates_sample'] == ['2026-01-02']
    assert 'missing_bucket_cause_unknown_no_imputation' in qqq['blockers']


def test_early_close_calendar_counts_actual_42_bars():
    result = run(calendars=[calendar(closing_hour=13)])
    assert item(result, symbol='SPY')['expected_buckets_from_known_calendar'] == 42


def test_calendar_late_receipt_is_not_contemporaneous_calendar_knowledge():
    result = run(calendars=[calendar(received=NOW)])
    assert item(result, symbol='SPY')['calendar_dates_received_after_window'] == 1
    assert 'calendar_not_available_by_window_end' in item(result, symbol='SPY')['blockers']


def test_future_calendar_receipt_does_not_fill_as_of_coverage():
    result = run(calendars=[calendar(received=NOW + 1)])
    assert result['calendar']['future_descriptors_excluded'] == 1
    assert item(result, symbol='SPY')['expected_buckets_from_known_calendar'] == 0


def test_identical_overlapping_calendar_ranges_merge_and_conflicts_refuse():
    cal = calendar()
    assert run(calendars=[cal, deepcopy(cal)])['calendar']['covered_dates_as_of_now'] == 1
    changed = deepcopy(cal)
    changed['sessions'] = []
    with pytest.raises(coverage.DataCoverageError, match='contradictory'):
        run(calendars=[cal, changed])


@pytest.mark.parametrize('mutate', [
    lambda c: c.update(end_date='2025-12-01'),
    lambda c: c.update(raw_capture_sha256='bad'),
    lambda c: c.update(source_id=''),
    lambda c: c['sessions'].append(deepcopy(c['sessions'][0])),
    lambda c: c['sessions'][0].update(open_time=c['sessions'][0]['open_time'] + 3600),
    lambda c: c['sessions'][0].update(session_date='2026-01-03'),
    lambda c: c.update(unknown='no'),
])
def test_bad_calendar_inputs_fail_closed(mutate):
    cal = calendar()
    mutate(cal)
    with pytest.raises(coverage.DataCoverageError):
        run(calendars=[cal])


def test_point_in_time_quotes_require_both_visibility_clocks_and_event_freshness():
    close = HOLDOUT + 300
    row = bar(close, kind='prospective_observation')
    assert item(run([row, quote(close - 10)]), 'prospective_holdout')['bars_with_quote_available_by_close'] == 1
    assert item(run([row, quote(close - 10, ingestion=close + 1)]), 'prospective_holdout')['bars_with_quote_available_by_close'] == 0
    assert item(run([row, quote(close - 61)]), 'prospective_holdout')['bars_with_quote_available_by_close'] == 0
    assert item(run([bar(), quote(close)]))['bars_with_quote_available_by_close'] == 0


def test_quote_update_after_close_does_not_erase_visible_original_quote():
    close = HOLDOUT + 300
    rows = [bar(close, kind='prospective_observation'), quote(close - 10),
            quote(close - 10, ingestion=close + 1, revision=1)]
    assert item(run(rows), 'prospective_holdout')['bars_with_quote_available_by_close'] == 1


def test_duplicate_observation_identity_rejected_instead_of_inflating_coverage():
    row = bar()
    with pytest.raises(coverage.DataCoverageError, match='duplicate observation'):
        run([row, deepcopy(row)])


def test_multiple_sources_cannot_inflate_counts_or_silently_certify_events():
    original, other = bar(), bar()
    other['source_id'] = 'second-source'
    result = run([original, other])
    assert item(result)['observed_event_buckets'] == 1
    assert 'multiple_sources_for_same_event' in item(result)['blockers']
    assert 'multiple_sources_for_same_event' not in item(result, 'validation')['blockers']


def test_crypto_current_freshness_is_separate_from_archived_window_gaps():
    fresh = bar((NOW // 300) * 300, kind='prospective_observation')
    result = run([fresh])
    diagnostic = next(i for i in result['current_freshness'] if i['symbol'] == 'BTC-USD')
    assert 'current_bar_stale' not in diagnostic['blockers']
    assert diagnostic['affects_archived_window_coverage'] is False
    old = bar((NOW // 300) * 300 - 1200, kind='prospective_observation')
    diagnostic = next(i for i in run([old])['current_freshness'] if i['symbol'] == 'BTC-USD')
    assert 'current_bar_stale' in diagnostic['blockers']


def test_equity_weekend_freshness_uses_last_declared_session_not_wall_clock_age():
    cal = calendar('2026-07-10', received=CREATED, end_date='2026-07-12')
    close = cal['sessions'][0]['close_time']
    sunday = int(datetime(2026, 7, 12, 12, tzinfo=timezone.utc).timestamp())
    result = run([bar(close, symbol='SPY', kind='prospective_observation')], now=sunday, calendars=[cal])
    spy = next(i for i in result['current_freshness'] if i['symbol'] == 'SPY')
    assert spy['expected_latest_bar_close'] == close
    assert 'current_bar_stale' not in spy['blockers']
    assert 'current_calendar_coverage_unknown' not in spy['blockers']


def test_calendar_gap_prevents_guessing_latest_equity_session():
    cal = calendar('2026-07-10', received=CREATED)
    sunday = int(datetime(2026, 7, 12, 12, tzinfo=timezone.utc).timestamp())
    result = run(now=sunday, calendars=[cal])
    spy = next(i for i in result['current_freshness'] if i['symbol'] == 'SPY')
    assert 'current_calendar_coverage_unknown' in spy['blockers']


def test_bounded_streams_refuse_before_silently_truncating(monkeypatch):
    monkeypatch.setattr(coverage, 'MAX_RECORDS', 2)
    with pytest.raises(coverage.DataCoverageError, match='record limit'):
        run((bar(START + 300 * (i + 1)) for i in range(3)))
    monkeypatch.setattr(coverage, 'MAX_CALENDAR_DESCRIPTORS', 1)
    with pytest.raises(coverage.DataCoverageError, match='calendar descriptor limit'):
        run(calendars=[calendar(), calendar()])


def test_report_is_json_safe_no_prices_no_mutations_no_strategy_threshold_changes():
    c, rows, cal = contract(), [bar(HOLDOUT + 300, kind='prospective_observation')], [calendar()]
    before = deepcopy((c, rows, cal))
    result = coverage.coverage_report(c, rows, now=NOW, calendar_coverage=cal)
    text = json.dumps(result, allow_nan=False)
    for secret in ('17001', '17009', '16999', '17007', '9997', 'payload', 'net_return', 'sharpe', 'signal'):
        assert secret not in text
    assert (c, rows, cal) == before
    assert result['strategy_thresholds_changed'] is False
    assert result['holdout_prices_exposed'] is False


def test_record_session_mismatch_is_reported_even_when_bar_time_fits_both_sessions():
    cal = calendar(closing_hour=13)
    close = cal['sessions'][0]['open_time'] + 300
    # Fixture bar declares a 16:00 close, conflicting with the separate calendar.
    result = run([bar(close, symbol='SPY')], calendars=[cal])
    spy = item(result, symbol='SPY')
    assert spy['record_calendar_conflict_count'] == 1
    assert 'record_session_conflicts_with_calendar' in spy['blockers']


def test_complete_numerical_history_does_not_become_original_as_of_or_qualified_data():
    end = contract()['windows']['research']['end_exclusive']
    rows = (bar(event, ingestion=NOW) for event in range(START + 300, end, 300))
    result = run(rows)
    btc = item(result)
    assert btc['observed_event_buckets'] == btc['expected_event_buckets'] == 25919
    assert btc['numerical_coverage_complete'] is True
    assert btc['event_daily_observations'] == 90
    assert btc['missing_event_buckets'] == 0
    assert btc['clock_coverage_complete'] is False
    assert btc['as_of_window_end_event_buckets'] == 0
    assert result['can_support_90_90_90'] is False
    assert result['qualification_established'] is False


def test_calendar_import_uses_ingestion_visibility_instead_of_archived_capture_receipt():
    cal = calendar(received=START)
    cal['ingestion_time'] = NOW
    result = run(calendars=[cal])
    spy = item(result, symbol='SPY')
    assert spy['expected_buckets_from_known_calendar'] == 78
    assert spy['calendar_dates_received_after_window'] == 0
    assert spy['calendar_dates_not_visible_by_window_end'] == 1
    assert 'calendar_not_available_by_window_end' in spy['blockers']
    assert result['calendar']['descriptors_without_ingestion_time'] == 0


def test_calendar_future_import_is_not_yet_visible_despite_old_capture_receipt():
    cal = calendar(received=START)
    cal['ingestion_time'] = NOW + 1
    result = run(calendars=[cal])
    assert result['calendar']['future_descriptors_excluded'] == 1
    assert item(result, symbol='SPY')['expected_buckets_from_known_calendar'] == 0


def test_calendar_merge_prefers_earliest_effective_visibility_not_earliest_capture():
    late_import = calendar(received=START)
    late_import['ingestion_time'] = NOW
    timely = calendar(received=START + DAY)
    timely['ingestion_time'] = START + DAY
    result = run(calendars=[late_import, timely])
    assert item(result, symbol='SPY')['calendar_dates_received_after_window'] == 0


def test_calendar_ingestion_must_follow_its_capture_receipt():
    cal = calendar(received=START)
    cal['ingestion_time'] = START - 1
    with pytest.raises(coverage.DataCoverageError, match='ingestion cannot precede'):
        run(calendars=[cal])


def test_later_revision_cannot_erase_as_of_window_end_unknown_volume_units():
    original = bar()
    original['payload']['volume_unit'] = 'unknown'
    correction = bar(ingestion=NOW, revision=1)
    btc = item(run([original, correction]))
    assert 'volume_unit_unknown' not in btc['quality_blocker_counts']
    assert btc['as_of_quality_blocker_counts']['volume_unit_unknown'] == 1
    assert 'volume_unit_unknown' in btc['blockers']


def test_quote_unknown_size_at_bar_close_cannot_be_repaired_by_a_later_revision():
    close = HOLDOUT + 300
    original = quote(close - 10)
    original['payload']['size_unit'] = 'unknown'
    correction = quote(close - 10, ingestion=close + 1, revision=1)
    btc = item(run([bar(close, kind='prospective_observation'), original, correction]), 'prospective_holdout')
    assert btc['bars_with_quote_available_by_close'] == 1
    assert 'quote_size_unit_unknown' not in btc['quote_quality_blocker_counts']
    assert btc['quote_at_close_quality_blocker_counts']['quote_size_unit_unknown'] == 1
    assert 'quote_size_unit_unknown' in btc['blockers']


def test_quote_revision_selected_at_close_can_differ_from_both_first_and_current_version():
    close = HOLDOUT + 300
    first = quote(close - 30)
    middle = quote(close - 30, ingestion=close - 10, revision=1)
    middle['payload']['size_unit'] = 'unknown'
    current = quote(close - 30, ingestion=close + 1, revision=2)
    rows = [current, bar(close, kind='prospective_observation'), first, middle]
    btc = item(run(rows), 'prospective_holdout')
    assert btc['quote_at_close_quality_blocker_counts']['quote_size_unit_unknown'] == 1
    assert 'quote_size_unit_unknown' not in btc['quote_quality_blocker_counts']


def test_quote_revision_corrected_before_close_replaces_unknown_original_for_that_decision():
    close = HOLDOUT + 300
    first = quote(close - 30)
    first['payload']['size_unit'] = 'unknown'
    corrected = quote(close - 30, ingestion=close - 10, revision=1)
    rows = [first, corrected, bar(close, kind='prospective_observation')]
    btc = item(run(rows), 'prospective_holdout')
    assert btc['bars_with_quote_available_by_close'] == 1
    assert 'quote_size_unit_unknown' not in btc['quote_at_close_quality_blocker_counts']


def test_newer_quote_event_wins_over_later_revision_of_older_quote_event():
    close = HOLDOUT + 300
    old = quote(close - 50)
    old_correction = quote(close - 50, ingestion=close - 1, revision=1)
    old_correction['payload']['size_unit'] = 'unknown'
    recent = quote(close - 20)
    rows = [old, recent, old_correction, bar(close, kind='prospective_observation')]
    btc = item(run(rows), 'prospective_holdout')
    assert btc['bars_with_quote_available_by_close'] == 1
    assert 'quote_size_unit_unknown' not in btc['quote_at_close_quality_blocker_counts']


def test_before_first_bar_calendar_gap_prevents_guessing_previous_session_close():
    friday = calendar('2026-07-10', received=CREATED)
    tuesday = calendar('2026-07-14', received=CREATED)
    now = int(datetime(2026, 7, 14, 9, 31, tzinfo=NY).timestamp())
    close = friday['sessions'][0]['close_time']
    rows = [bar(close, symbol='SPY', kind='prospective_observation')]
    result = run(rows, now=now, calendars=[friday, tuesday])
    spy = next(i for i in result['current_freshness'] if i['symbol'] == 'SPY')
    assert spy['expected_latest_bar_close'] is None
    assert 'current_calendar_coverage_unknown' in spy['blockers']


def test_before_first_bar_explicit_closed_dates_allow_previous_session_reference():
    friday = calendar('2026-07-10', received=CREATED, end_date='2026-07-13')
    tuesday = calendar('2026-07-14', received=CREATED)
    now = int(datetime(2026, 7, 14, 9, 31, tzinfo=NY).timestamp())
    close = friday['sessions'][0]['close_time']
    rows = [bar(close, symbol='SPY', kind='prospective_observation')]
    result = run(rows, now=now, calendars=[friday, tuesday])
    spy = next(i for i in result['current_freshness'] if i['symbol'] == 'SPY')
    assert spy['expected_latest_bar_close'] == close
    assert 'current_calendar_coverage_unknown' not in spy['blockers']
