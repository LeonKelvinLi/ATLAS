"""Bounded metadata-only coverage of one immutable 90/90/90 data generation.

This module does no I/O, pricing, strategy evaluation or holdout release. Counts
refer to completed, fully contained native bars; no-trade intervals are never
silently filled. Provider labels and local hashes cannot certify authenticity.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import heapq
import re
from zoneinfo import ZoneInfo

from atlas.research.data_contract_v1 import validate_contract, validate_record, _session


MAX_RECORDS = 1_000_000
MAX_CALENDAR_DESCRIPTORS = 10_000
MAX_CALENDAR_DATE_ASSERTIONS = 1_000_000
MAX_CALENDAR_SESSION_ASSERTIONS = 200_000
MAX_DETAIL_ITEMS = 20
DAY = 86400
NY = ZoneInfo('America/New_York')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_SOURCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')


class DataCoverageError(ValueError):
    """Coverage inputs are malformed, contradictory or exceed bounded work."""


def _integer(value, name):
    if type(value) is not int or not 0 <= value <= 253402214400:
        raise DataCoverageError(name + ' must be bounded nonnegative UTC seconds')
    return value


def _date(value):
    if type(value) is not str:
        raise DataCoverageError('calendar dates must use YYYY-MM-DD')
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise DataCoverageError('calendar dates must use YYYY-MM-DD') from None
    if result.isoformat() != value:
        raise DataCoverageError('calendar dates must use YYYY-MM-DD')
    return result


def _dates(start, end):
    """Local dates touched by a half-open UTC interval."""
    first = datetime.fromtimestamp(start, NY).date()
    last = datetime.fromtimestamp(end - 1, NY).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _calendar(descriptors, now):
    """Merge explicit complete response ranges, including declared closed days."""
    merged, descriptor_count, assertions, session_count, future = {}, 0, 0, 0, 0
    missing_ingestion = 0
    for item in descriptors:
        descriptor_count += 1
        if descriptor_count > MAX_CALENDAR_DESCRIPTORS:
            raise DataCoverageError('calendar descriptor limit exceeded')
        required = {'start_date', 'end_date', 'sessions', 'source_id', 'raw_capture_sha256', 'received_at'}
        if (type(item) is not dict or not required <= set(item)
                or set(item) - required - {'ingestion_time'}):
            raise DataCoverageError('calendar descriptor has missing or unknown fields')
        first, last = _date(item['start_date']), _date(item['end_date'])
        days = (last - first).days + 1
        if not 1 <= days <= 366:
            raise DataCoverageError('calendar coverage range must contain 1 through 366 dates')
        assertions += days
        if assertions > MAX_CALENDAR_DATE_ASSERTIONS:
            raise DataCoverageError('calendar date assertion limit exceeded')
        received = _integer(item['received_at'], 'calendar received_at')
        ingested = _integer(item.get('ingestion_time', received), 'calendar ingestion_time')
        if ingested < received:
            raise DataCoverageError('calendar ingestion cannot precede its capture receipt')
        if 'ingestion_time' not in item:
            missing_ingestion += 1
        visible = max(received, ingested)
        if (type(item['source_id']) is not str or not _SOURCE.fullmatch(item['source_id'])
                or type(item['raw_capture_sha256']) is not str
                or not _HASH.fullmatch(item['raw_capture_sha256'])):
            raise DataCoverageError('calendar source identity and raw capture hash required')
        if type(item['sessions']) is not list or len(item['sessions']) > 366:
            raise DataCoverageError('calendar sessions must be a bounded list')
        sessions = {}
        for session in item['sessions']:
            session_count += 1
            if session_count > MAX_CALENDAR_SESSION_ASSERTIONS:
                raise DataCoverageError('calendar session assertion limit exceeded')
            if type(session) is not dict or set(session) != {'session_date', 'open_time', 'close_time'}:
                raise DataCoverageError('calendar session has missing or unknown fields')
            day = _date(session['session_date'])
            if not first <= day <= last or day in sessions:
                raise DataCoverageError('duplicate or out-of-range calendar session')
            try:
                _session(dict(session, calendar_id='declared-calendar', calendar_verified=False))
            except ValueError as exc:
                raise DataCoverageError(str(exc)) from exc
            sessions[day] = (session['open_time'], session['close_time'])
        if visible > now:
            future += 1
            continue
        for offset in range(days):
            day = first + timedelta(days=offset)
            assertion = sessions.get(day)
            previous = merged.get(day)
            if previous is not None and previous['session'] != assertion:
                raise DataCoverageError('contradictory calendar assertions for ' + day.isoformat())
            if previous is None or visible < previous['visible_at']:
                merged[day] = {'session': assertion, 'received_at': received,
                               'ingestion_time': ingested, 'visible_at': visible}
    return merged, {'descriptors': descriptor_count, 'future_descriptors_excluded': future,
                    'covered_dates_as_of_now': len(merged), 'authenticity_verified': False,
                    'descriptors_without_ingestion_time': missing_ingestion,
                    'visibility_basis': 'max(capture_receipt, archive_ingestion); omitted ingestion uses receipt only'}


def _window_name(event, interval, windows):
    for name, bounds in windows.items():
        if bounds['start'] <= event - interval and event < bounds['end_exclusive']:
            return name
    return None


def _expected(asset_class, bounds, calendar, interval):
    start, end = bounds['start'], bounds['end_exclusive']
    if asset_class == 'crypto':
        first = ((start + interval + interval - 1) // interval) * interval
        return set(range(first, end, interval)), {}, [], 0
    days = _dates(start, end)
    missing = [day.isoformat() for day in days if day not in calendar]
    late_dates = sum(1 for day in days if day in calendar
                     and calendar[day]['visible_at'] >= end)
    by_session = {}
    for day in days:
        assertion = calendar.get(day)
        if assertion is None or assertion['session'] is None:
            continue
        opening, closing = assertion['session']
        events = {event for event in range(opening + interval, closing + 1, interval)
                  if start <= event - interval and event < end}
        if events:
            by_session[day.isoformat()] = events
    return set().union(*by_session.values()) if by_session else set(), by_session, missing, late_dates


def _rank(row):
    return row['revision'], row['ingestion_time'], row['available_time']


def _put(index, row, quality):
    key = (row['symbol'], row['record_type'], row['event_time'], row['source_id'])
    previous = index.get(key)
    if previous is None or _rank(row) > _rank(previous['record']):
        # Retain only metadata needed by coverage. Prices never enter the index.
        keep = {name: row[name] for name in (
            'symbol', 'record_type', 'event_time', 'source_id', 'revision', 'available_time',
            'ingestion_time', 'acquisition_kind', 'session', 'asset_class')}
        index[key] = {'record': keep, 'blockers': quality['blockers']}


def _group(index):
    result = defaultdict(dict)
    collisions = defaultdict(set)
    for item in index.values():
        row = item['record']
        key, event = (row['symbol'], row['record_type']), row['event_time']
        if event in result[key]:
            collisions[key].add(event)
            if _rank(row) <= _rank(result[key][event]['record']):
                continue
        result[key][event] = item
    return result, collisions


def _quote_coverage(bars, quotes, max_age, *, prospective_only=False):
    """Count and assess the newest quote revision actually visible at each close.

    The heap selects the latest quote event while the active map invalidates
    superseded revisions. A correction after a decision cannot repair the
    quality of the observation used at that earlier decision. Work is bounded
    by O((quotes + bars) log(quotes)); no per-bar scan of quote history.
    """
    ordered = sorted(quotes, key=lambda item: (
        item['record']['ingestion_time'], item['record']['event_time'],
        item['record']['revision'], item['record']['source_id']))
    active, heap, blockers = {}, [], Counter()
    pointer, count = 0, 0
    for close in sorted(bars):
        while pointer < len(ordered) and ordered[pointer]['record']['ingestion_time'] <= close:
            item = ordered[pointer]
            row = item['record']
            key = (row['event_time'], row['source_id'])
            previous = active.get(key)
            if previous is None or _rank(row) > _rank(ordered[previous]['record']):
                active[key] = pointer
                heapq.heappush(heap, (-row['event_time'], -row['revision'],
                                      -row['ingestion_time'], row['source_id'], pointer))
            pointer += 1
        while heap:
            _, _, _, _, index = heap[0]
            row = ordered[index]['record']
            if active[(row['event_time'], row['source_id'])] == index:
                break
            heapq.heappop(heap)
        if not heap:
            continue
        selected = ordered[heap[0][-1]]
        if 0 <= close - selected['record']['event_time'] <= max_age:
            count += 1
            blockers.update(code for code in selected['blockers']
                            if code != 'not_prospective_observation' or prospective_only)
    return count, blockers


def _freshness(symbol, asset_class, groups, calendar, now, policies):
    bars, quotes = groups.get((symbol, 'bar'), {}), groups.get((symbol, 'quote'), {})
    latest_bar, latest_quote = max(bars, default=None), max(quotes, default=None)
    blockers = []
    if asset_class == 'crypto':
        expected_bar, quote_reference = now // 300 * 300, now
    else:
        today = datetime.fromtimestamp(now, NY).date()
        expected_bar = quote_reference = None
        if today in calendar:
            sessions = sorted((day, item['session']) for day, item in calendar.items()
                              if item['session'] is not None and item['session'][0] <= now)
            if sessions:
                day, (opening, closing) = sessions[-1]
                # A gap may conceal an entire session. Do not claim Friday is
                # the latest session when Monday's calendar is absent.
                if all(day + timedelta(days=i) in calendar for i in range((today - day).days + 1)):
                    quote_reference = min(now, closing)
                    latest = min(now, closing)
                    expected_bar = opening + ((latest - opening) // 300) * 300
                    if expected_bar == opening:
                        previous = sessions[:-1]
                        expected_bar = None
                        if previous:
                            prior_day, prior_session = previous[-1]
                            if all(prior_day + timedelta(days=i) in calendar
                                   for i in range((today - prior_day).days + 1)):
                                expected_bar = prior_session[1]
        if expected_bar is None:
            blockers.append('current_calendar_coverage_unknown')
    if latest_bar is None:
        blockers.append('current_bar_unavailable')
    elif expected_bar is not None and expected_bar - latest_bar > policies['max_bar_lag_seconds']:
        blockers.append('current_bar_stale')
    if latest_quote is None:
        blockers.append('current_quote_unavailable')
    elif quote_reference is not None and quote_reference - latest_quote > policies['max_quote_age_seconds']:
        blockers.append('current_quote_stale')
    return {'symbol': symbol, 'latest_bar_event_time': latest_bar,
            'latest_quote_event_time': latest_quote, 'expected_latest_bar_close': expected_bar,
            'quote_freshness_reference_time': quote_reference, 'blockers': sorted(blockers),
            'calendar_basis': 'UTC_24_7' if asset_class == 'crypto' else 'explicit_declared_sessions',
            'affects_archived_window_coverage': False}


def coverage_report(contract, records, *, now, calendar_coverage=(), strategy_freeze=None):
    """Report fixed-window data sufficiency without exposing observations.

    ``records`` and ``calendar_coverage`` may be bounded iterables. Future receipts
    are structurally validated, then excluded from every as-of count. Window-end
    counts select the latest revision visible strictly before that exclusive end,
    independently of the revision visible now. The store verifies hash chains;
    this pure report additionally refuses duplicate observation identities.
    """
    contract = validate_contract(contract)
    now = _integer(now, 'now')
    if now < contract['created_at']:
        raise DataCoverageError('coverage now precedes contract creation')
    calendar, calendar_summary = _calendar(calendar_coverage, now)
    windows, policies = contract['windows'], contract['policies']
    freeze_blockers = []
    if strategy_freeze is None:
        freeze_blockers.append('strategy_not_frozen')
    else:
        if (type(strategy_freeze) is not dict or set(strategy_freeze) != {'strategy_sha256', 'frozen_at'}
                or type(strategy_freeze['strategy_sha256']) is not str
                or not _HASH.fullmatch(strategy_freeze['strategy_sha256'])):
            raise DataCoverageError('strategy freeze requires exact hash and frozen_at fields')
        frozen = _integer(strategy_freeze['frozen_at'], 'frozen_at')
        if frozen < contract['created_at']:
            raise DataCoverageError('strategy freeze precedes contract creation')
        if frozen > now:
            freeze_blockers.append('strategy_freeze_not_visible_as_of_now')
        if frozen >= windows['prospective_holdout']['start']:
            freeze_blockers.append('strategy_not_frozen_before_holdout')
    latest, end_visible, seen = {}, {name: {} for name in windows}, set()
    quote_versions = defaultdict(list)
    record_count = future_count = outside_count = 0
    for raw in records:
        record_count += 1
        if record_count > MAX_RECORDS:
            raise DataCoverageError('record limit exceeded')
        if type(raw) is not dict:
            raise DataCoverageError('record must be a mapping')
        ingestion = _integer(raw.get('ingestion_time'), 'ingestion_time')
        try:
            checked = validate_record(raw, contract, now=max(now, ingestion))
        except ValueError as exc:
            raise DataCoverageError('record ' + str(record_count) + ': ' + str(exc)) from exc
        row = checked['record']
        if max(row['available_time'], row['ingestion_time']) > now:
            future_count += 1
            continue
        identity = (row['symbol'], row['record_type'], row['event_time'], row['source_id'], row['revision'])
        if identity in seen:
            raise DataCoverageError('duplicate observation identity in coverage input')
        seen.add(identity)
        _put(latest, row, checked['quality'])
        if row['record_type'] == 'quote':
            quote_versions[row['symbol']].append({
                'record': {field: row[field] for field in
                           ('event_time', 'available_time', 'ingestion_time', 'revision', 'source_id')},
                'blockers': checked['quality']['blockers'],
            })
        name = _window_name(row['event_time'], row['interval_seconds'], windows)
        if name is None:
            outside_count += 1
        elif row['ingestion_time'] < windows[name]['end_exclusive']:
            _put(end_visible[name], row, checked['quality'])
    groups, collisions = _group(latest)
    window_reports, overall_blockers = {}, []
    for name, bounds in windows.items():
        start, end = bounds['start'], bounds['end_exclusive']
        window_asof, asof_collisions = _group(end_visible[name])
        elapsed = now >= end
        instruments = []
        for instrument in contract['universe']:
            symbol, asset_class = instrument['symbol'], instrument['asset_class']
            expected, sessions, uncovered, calendar_late = _expected(asset_class, bounds, calendar, 300)
            due = {event for event in expected if event <= now}
            bars = {event: item for event, item in groups.get((symbol, 'bar'), {}).items()
                    if start <= event - 300 and event < end}
            quotes = {event: item for event, item in groups.get((symbol, 'quote'), {}).items()
                      if start <= event < end}
            asof = window_asof.get((symbol, 'bar'), {})
            prospective = {event: item for event, item in bars.items()
                           if item['record']['acquisition_kind'] == 'prospective_observation'}
            prospective_asof = {event: item for event, item in asof.items()
                                if item['record']['acquisition_kind'] == 'prospective_observation'}
            eligible = prospective_asof if name == 'prospective_holdout' else asof
            missing = expected - set(bars)
            missing_due = due - set(bars)
            out_of_calendar = set(bars) - expected if not uncovered else set()
            record_calendar_conflicts = 0
            if asset_class == 'equity':
                for observation in bars.values():
                    declared = observation['record']['session']
                    assertion = calendar.get(_date(declared['session_date']))
                    if (assertion is not None and assertion['session'] !=
                            (declared['open_time'], declared['close_time'])):
                        record_calendar_conflicts += 1
            missing_sessions = [day for day, events in sessions.items() if not events.intersection(bars)]
            missing_due_sessions = [day for day, events in sessions.items()
                                    if events.intersection(due) and not events.intersection(bars)]
            incomplete_sessions = [day for day, events in sessions.items() if events - set(bars)]
            daily_observations = {datetime.fromtimestamp(event - 1, timezone.utc if asset_class == 'crypto' else NY).date()
                                  for event in eligible if event in expected}
            event_daily_observations = {datetime.fromtimestamp(event - 1, timezone.utc if asset_class == 'crypto' else NY).date()
                                        for event in bars if event in expected}
            min_days = contract['qualification_thresholds']['min_daily_observations']
            quality_counts = Counter(blocker for item in bars.values() for blocker in item['blockers']
                                     if blocker != 'not_prospective_observation' or name == 'prospective_holdout')
            asof_quality_counts = Counter(blocker for item in asof.values() for blocker in item['blockers']
                                          if blocker != 'not_prospective_observation' or name == 'prospective_holdout')
            quote_quality_counts = Counter(blocker for item in quotes.values() for blocker in item['blockers']
                                           if blocker != 'not_prospective_observation' or name == 'prospective_holdout')
            quote_history = [item for item in quote_versions[symbol]
                             if start <= item['record']['event_time'] < end]
            quoted, quote_at_close_quality = _quote_coverage(
                bars, quote_history, policies['max_quote_age_seconds'],
                prospective_only=name == 'prospective_holdout')
            blockers = (set(quality_counts) | set(quote_quality_counts)
                        | set(asof_quality_counts) | set(quote_at_close_quality))
            if not elapsed:
                blockers.add('window_not_elapsed')
            if uncovered:
                blockers.add('calendar_coverage_unknown')
            if calendar_late:
                blockers.add('calendar_not_available_by_window_end')
            if not bars:
                blockers.add('instrument_bars_missing')
            if missing_due:
                blockers.add('missing_due_event_buckets')
                blockers.add('missing_bucket_cause_unknown_no_imputation')
            if missing_due_sessions:
                blockers.add('missing_declared_sessions')
            if out_of_calendar:
                blockers.add('bars_outside_declared_calendar')
            if record_calendar_conflicts:
                blockers.add('record_session_conflicts_with_calendar')
            if len(set(expected) - set(eligible)):
                blockers.add('insufficient_as_of_event_coverage')
            if len(daily_observations) < min_days:
                blockers.add('insufficient_daily_observations')
            if not quotes:
                blockers.add('quotes_unavailable')
            if quoted < len(bars):
                blockers.add('event_time_quote_coverage_incomplete')
            if (collisions[(symbol, 'bar')].intersection(bars)
                    or collisions[(symbol, 'quote')].intersection(quotes)
                    or asof_collisions[(symbol, 'bar')].intersection(asof)):
                blockers.add('multiple_sources_for_same_event')
            if name == 'prospective_holdout':
                blockers.update(freeze_blockers)
                if len(prospective_asof) < len(bars):
                    blockers.add('holdout_contains_nonprospective_or_late_observations')
            # Known local schema fields explicitly cannot certify these external
            # evidence gates, including when a window is empty.
            blockers.update({'provider_authenticity_not_verified', 'cost_source_not_verified'})
            if asset_class == 'equity':
                blockers.add('calendar_authenticity_not_verified')
            numerical = (elapsed and not uncovered and not missing and not out_of_calendar and not record_calendar_conflicts
                         and len(event_daily_observations) >= min_days and bool(expected))
            clock_coverage = (not uncovered and not (expected - set(eligible)) and not calendar_late
                              and len(daily_observations) >= min_days)
            result = {
                'symbol': symbol, 'asset_class': asset_class, 'venue': instrument['venue'],
                'expected_event_buckets': None if uncovered else len(expected),
                'expected_buckets_from_known_calendar': len(expected), 'expected_event_buckets_due': len(due),
                'remaining_future_event_buckets': len(expected - due),
                'observed_event_buckets': len(bars), 'as_of_window_end_event_buckets': len(asof),
                'prospective_event_buckets': len(prospective),
                'prospective_as_of_window_end_event_buckets': len(prospective_asof),
                'late_event_buckets': len(set(bars) - set(asof)),
                'missing_event_buckets': None if uncovered else len(missing),
                'missing_due_event_buckets': len(missing_due),
                'missing_due_event_times_sample': sorted(missing_due)[:MAX_DETAIL_ITEMS],
                'missing_bucket_cause': 'unknown_no_trade_vs_provider_gap; no imputation',
                'declared_sessions': len(sessions) if asset_class == 'equity' else None,
                'missing_session_count': len(missing_sessions),
                'missing_due_session_count': len(missing_due_sessions),
                'missing_session_dates_sample': missing_sessions[:MAX_DETAIL_ITEMS],
                'incomplete_session_count': len(incomplete_sessions),
                'calendar_uncovered_date_count': len(uncovered),
                'calendar_uncovered_dates_sample': uncovered[:MAX_DETAIL_ITEMS],
                'calendar_dates_received_after_window': (sum(
                    1 for day in _dates(start, end) if day in calendar and calendar[day]['received_at'] >= end)
                    if asset_class == 'equity' else 0),
                'calendar_dates_not_visible_by_window_end': calendar_late,
                'out_of_calendar_event_count': len(out_of_calendar),
                'record_calendar_conflict_count': record_calendar_conflicts,
                'event_daily_observations': len(event_daily_observations),
                'daily_observations_as_of_window_end': len(daily_observations),
                'required_daily_observations': min_days,
                'quote_observations': len(quotes), 'bars_with_quote_available_by_close': quoted,
                'quality_blocker_counts': dict(sorted(quality_counts.items())),
                'as_of_quality_blocker_counts': dict(sorted(asof_quality_counts.items())),
                'quote_at_close_quality_blocker_counts': dict(sorted(quote_at_close_quality.items())),
                'quote_quality_blocker_counts': dict(sorted(quote_quality_counts.items())),
                'numerical_coverage_complete': numerical, 'clock_coverage_complete': clock_coverage,
                'blockers': sorted(blockers),
            }
            instruments.append(result)
            overall_blockers.extend(name + ':' + symbol + ':' + code for code in sorted(blockers))
        window_reports[name] = {
            **bounds, 'duration_days': (end - start) // DAY, 'elapsed': elapsed,
            'remaining_seconds': max(0, end - now),
            'prospective_only_required': name == 'prospective_holdout',
            'numerical_coverage_complete': all(item['numerical_coverage_complete'] for item in instruments),
            'clock_coverage_complete': all(item['clock_coverage_complete'] for item in instruments),
            'instruments': instruments,
        }
    return {
        'schema_version': 'ATLAS_DATA_COVERAGE_V1', 'contract_version': contract['version'],
        'generation_id': contract['generation_id'], 'as_of': now, 'mode': 'data_quality_only',
        'input_records': record_count, 'future_receipts_excluded': future_count,
        'visible_records_outside_complete_window_bars_or_quotes': outside_count,
        'calendar': calendar_summary, 'windows': window_reports,
        'numerical_coverage_complete': all(w['numerical_coverage_complete'] for w in window_reports.values()),
        'can_support_90_90_90': not overall_blockers, 'blockers': overall_blockers,
        'current_freshness': [_freshness(i['symbol'], i['asset_class'], groups, calendar, now, policies)
                              for i in contract['universe']],
        'qualification_established': False, 'alpha_validation_eligible': False,
        'live_approved': False, 'actual_orders_submitted': 0,
        'strategy_thresholds_changed': False,
        'holdout_prices_exposed': False,
        'limitations': [
            'Coverage is data quality, not strategy performance, statistical qualification or trading authority.',
            'Historical receipt clocks remain historical; a source label or local hash does not authenticate a provider.',
            'The store verifies observation and raw-capture hash chains; this report cannot authenticate supplied records.',
            'A complete declared calendar does not prove exchange authenticity or why an event bucket is absent.',
            'Quote coverage requires quote event and local visibility within 60 seconds before bar close; it is not a fill model.',
            'Current freshness is a separate diagnostic and does not rewrite the coverage of archived windows.',
        ],
    }
