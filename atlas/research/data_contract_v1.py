"""Pure, frozen market-observation contracts. No storage, strategy or broker I/O.

Acquisition code must round receipt clocks UP to whole UTC seconds. Provider
timestamps remain separate from receipt time; an unknown publication is null.
Local observation and a source label never authenticate a provider or alpha.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from zoneinfo import ZoneInfo

from atlas.collection.primitives import DEFAULT_THRESHOLDS, timestamp


VERSION = 'ATLAS_DATA_CONTRACT_V1'
CONTRACT_VERSION = VERSION
DAY = 86400
MAX_RECORDS = 1_000_000
UNIVERSE = (
    ('SPY', 'equity', 'alpaca-iex'), ('QQQ', 'equity', 'alpaca-iex'),
    ('GLD', 'equity', 'alpaca-iex'), ('USO', 'equity', 'alpaca-iex'),
    ('BTC-USD', 'crypto', 'coinbase-exchange'),
)
POLICIES = {
    'max_bar_lag_seconds': 900, 'max_quote_age_seconds': 60,
    'imputation': 'forbidden', 'historical_ingestion': 'actual_receipt_time',
    'publication_unknown': 'preserve_null_and_block_qualification',
    'unknown_units': 'block_qualification', 'corporate_actions_unknown': 'block_qualification',
    'cost_source_unverified': 'block_qualification', 'revision_basis': 'locally_observed_versions',
    'receipt_timestamp_rounding': 'ceil_to_utc_second',
}
RECORD_FIELDS = {
    'contract_version', 'record_type', 'symbol', 'asset_class', 'venue', 'source_id',
    'event_time', 'publication_time', 'available_time', 'ingestion_time', 'revision',
    'previous_observation_hash', 'raw_capture_sha256', 'acquisition_kind', 'revision_basis',
    'interval_seconds', 'session', 'payload',
}
BAR_FIELDS = {
    'open', 'high', 'low', 'close', 'volume', 'volume_unit', 'currency',
    'price_adjustment', 'corporate_actions_status',
}
QUOTE_FIELDS = {
    'bid', 'ask', 'bid_size', 'ask_size', 'size_unit', 'currency',
    'timestamp_basis', 'provider_timestamp',
}
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')
_GENERATION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_NUMBER = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z')
_PROVIDER_TIME = re.compile(
    r'(?P<whole>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})'
    r'(?:\.(?P<fraction>[0-9]{1,9}))?(?P<offset>Z|[+-][0-9]{2}:[0-9]{2})\Z'
)


class DataContractError(ValueError):
    """The declared contract or a canonical observation is inconsistent."""


def _object(value, fields, name):
    if type(value) is not dict or set(value) != set(fields):
        raise DataContractError(name + ' has missing or unknown fields')


def _native_json(value):
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise DataContractError('JSON keys must be strings')
        for item in value.values():
            _native_json(item)
    elif type(value) is list:
        for item in value:
            _native_json(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise DataContractError('native JSON values required')


def _time(value, name):
    try:
        return timestamp(value, name)
    except ValueError as exc:
        raise DataContractError(str(exc)) from exc


def _identifier(value, name, *, generation=False):
    pattern = _GENERATION if generation else _ID
    if type(value) is not str or not pattern.fullmatch(value):
        raise DataContractError('invalid ' + name)
    return value


def _hash(value, name):
    if type(value) is not str or not _HASH.fullmatch(value):
        raise DataContractError('invalid ' + name)


def _decimal(value, name, *, positive=False):
    # Strings avoid silently converting a provider decimal through binary floats.
    if type(value) is not str or len(value) > 96 or not _NUMBER.fullmatch(value):
        raise DataContractError(name + ' requires a finite decimal string')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise DataContractError('invalid ' + name) from exc
    if (not number.is_finite() or number < 0 or (positive and number == 0)
            or abs(number.adjusted()) > 30 or len(number.as_tuple().digits) > 60):
        raise DataContractError(name + ' requires a bounded nonnegative decimal' + (' above zero' if positive else ''))
    if number == 0:
        return '0'
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def default_contract(generation_id, research_start, created_at):
    """Freeze one 90/90/90 calendar-day generation with two 300s embargoes."""
    generation_id = _identifier(generation_id, 'generation_id', generation=True)
    start = _time(research_start, 'research_start')
    created = _time(created_at, 'created_at')
    research_end = _time(start + 90 * DAY, 'research end')
    validation_start = research_end + 300
    validation_end = validation_start + 90 * DAY
    holdout_start = validation_end + 300
    holdout_end = _time(holdout_start + 90 * DAY, 'holdout end')
    if holdout_start < created:
        raise DataContractError('prospective holdout must start at or after contract creation')
    return {
        'version': VERSION, 'generation_id': generation_id, 'created_at': created,
        'universe': [{'symbol': s, 'asset_class': a, 'venue': v} for s, a, v in UNIVERSE],
        'interval_seconds': 300, 'embargo_seconds': 300,
        'windows': {
            'research': {'start': start, 'end_exclusive': research_end},
            'validation': {'start': validation_start, 'end_exclusive': validation_end},
            'prospective_holdout': {'start': holdout_start, 'end_exclusive': holdout_end},
        },
        'policies': dict(POLICIES), 'qualification_thresholds': dict(DEFAULT_THRESHOLDS),
    }


def validate_contract(contract):
    """Return an independent canonical fixed contract; reject policy drift."""
    _object(contract, {'version', 'generation_id', 'created_at', 'universe', 'interval_seconds',
                       'embargo_seconds', 'windows', 'policies', 'qualification_thresholds'}, 'contract')
    windows = contract['windows']
    _object(windows, {'research', 'validation', 'prospective_holdout'}, 'windows')
    for name, window in windows.items():
        _object(window, {'start', 'end_exclusive'}, name + ' window')
        _time(window['start'], name + ' start')
        _time(window['end_exclusive'], name + ' end')
    expected = default_contract(contract['generation_id'], windows['research']['start'], contract['created_at'])
    try:
        _native_json(contract)
        # JSON type distinctions prevent True/1 and 300.0/300 policy substitutions.
        encoded = json.dumps(contract, sort_keys=True, allow_nan=False, separators=(',', ':'))
        fixed = json.dumps(expected, sort_keys=True, allow_nan=False, separators=(',', ':'))
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise DataContractError('contract must be finite native JSON') from exc
    if encoded != fixed:
        raise DataContractError('fixed V1 contract fields, windows and policies cannot change')
    return expected


def _session(value):
    _object(value, {'calendar_id', 'session_date', 'open_time', 'close_time', 'calendar_verified'}, 'session')
    _identifier(value['calendar_id'], 'calendar_id')
    if value['calendar_verified'] is not False:
        raise DataContractError('declared calendar cannot self-certify verification')
    opening = _time(value['open_time'], 'session open_time')
    closing = _time(value['close_time'], 'session close_time')
    ny = ZoneInfo('America/New_York')
    begin, end = (datetime.fromtimestamp(t, ny) for t in (opening, closing))
    date = value['session_date']
    if (type(date) is not str or begin.date().isoformat() != date or end.date().isoformat() != date
            or begin.weekday() > 4 or opening >= closing
            or (begin.hour, begin.minute, begin.second) != (9, 30, 0)
            or (end.hour, end.minute, end.second) not in {(13, 0, 0), (16, 0, 0)}):
        raise DataContractError('invalid regular-session calendar or DST-adjusted bounds')
    return deepcopy(value)


def validate_record(record, contract, now):
    """Normalize a record while retaining valid but insufficient raw evidence.

    Staleness is measured at acquisition, not at a later archival review. The
    validation-clock age is also reported, for callers needing current quotes.
    Revision linkage/authenticity and whole-session gaps require the store and
    coverage layer; one record cannot establish those properties.
    """
    contract = validate_contract(contract)
    now = _time(now, 'now')
    if now < contract['created_at']:
        raise DataContractError('validation now precedes contract creation')
    _object(record, RECORD_FIELDS, 'record')
    try:
        _native_json(record)
    except RecursionError as exc:
        raise DataContractError('record nesting is invalid') from exc
    if record['contract_version'] != VERSION:
        raise DataContractError('unsupported contract_version')
    kind = record['record_type']
    if kind not in ('bar', 'quote'):
        raise DataContractError('record_type must be bar or quote')
    identity = (record['symbol'], record['asset_class'], record['venue'])
    if identity not in UNIVERSE:
        raise DataContractError('record instrument is outside the fixed universe/venue')
    _identifier(record['source_id'], 'source_id')
    _hash(record['raw_capture_sha256'], 'raw_capture_sha256')
    revision = record['revision']
    if type(revision) is not int or not 0 <= revision <= 1_000_000:
        raise DataContractError('revision must be a bounded nonnegative integer')
    if revision == 0:
        if record['previous_observation_hash'] is not None:
            raise DataContractError('original revision must have a null previous_observation_hash')
    else:
        _hash(record['previous_observation_hash'], 'previous_observation_hash')
    if record['revision_basis'] != 'locally_observed_versions':
        raise DataContractError('revision_basis must preserve locally observed versions')
    acquisition = record['acquisition_kind']
    if acquisition not in ('historical_backfill', 'prospective_observation', 'test'):
        raise DataContractError('invalid acquisition_kind')
    event, available, ingested = (_time(record[key], key) for key in
                                  ('event_time', 'available_time', 'ingestion_time'))
    published = record['publication_time']
    if published is not None:
        published = _time(published, 'publication_time')
        if not event <= published <= available:
            raise DataContractError('event/publication/availability chronology is inconsistent')
    if not event <= available <= ingested <= now:
        raise DataContractError('event/availability/ingestion/now chronology is inconsistent')
    if acquisition == 'prospective_observation' and event < contract['created_at']:
        raise DataContractError('prospective observation predates contract creation')
    interval = 300 if kind == 'bar' else 0
    if type(record['interval_seconds']) is not int or record['interval_seconds'] != interval:
        raise DataContractError('bar interval must be 300 seconds and quote interval zero')
    canonical = deepcopy(record)
    blockers = ['provider_authenticity_not_verified', 'cost_source_not_verified']
    if published is None:
        blockers.append('publication_time_unknown')
    if acquisition != 'prospective_observation':
        blockers.append('not_prospective_observation')
    session = record['session']
    equity = record['asset_class'] == 'equity'
    if not equity and session is not None:
        raise DataContractError('crypto session must be null; its calendar is UTC 24/7')
    if equity:
        if session is None:
            if kind == 'bar':
                raise DataContractError('equity bars require declared session bounds')
            blockers.append('session_metadata_unknown')
        else:
            canonical['session'] = _session(session)
        blockers.append('calendar_authenticity_not_verified')
    payload = record['payload']
    _object(payload, BAR_FIELDS if kind == 'bar' else QUOTE_FIELDS, kind + ' payload')
    canonical['payload'] = normalized = deepcopy(payload)
    if payload['currency'] != 'USD':
        raise DataContractError('fixed USD accounting required')
    if kind == 'bar':
        for key in ('open', 'high', 'low', 'close', 'volume'):
            normalized[key] = _decimal(payload[key], key, positive=key != 'volume')
        o, h, low, close = (Decimal(normalized[k]) for k in ('open', 'high', 'low', 'close'))
        if low > min(o, close) or h < max(o, close) or low > h:
            raise DataContractError('inconsistent OHLC extrema')
        unit = 'shares' if equity else 'base_asset'
        if payload['volume_unit'] not in (unit, 'unknown'):
            raise DataContractError('volume unit does not match the instrument')
        if payload['volume_unit'] == 'unknown':
            blockers.append('volume_unit_unknown')
        if payload['price_adjustment'] != 'unadjusted':
            raise DataContractError('V1 preserves unadjusted provider prices only')
        if payload['corporate_actions_status'] != ('unknown' if equity else 'not_applicable'):
            raise DataContractError('unsupported corporate_actions_status; verification cannot be fabricated')
        if equity:
            blockers.append('corporate_actions_unknown')
            opening, closing = session['open_time'], session['close_time']
            if not opening <= event - interval < event <= closing or (event - opening) % interval:
                raise DataContractError('bar is incomplete, off-grid or outside its declared session')
        elif event < interval or event % interval:
            raise DataContractError('crypto bar closes must align to the UTC 300-second grid')
    else:
        for key in ('bid', 'ask'):
            normalized[key] = _decimal(payload[key], key, positive=True)
        if Decimal(normalized['bid']) > Decimal(normalized['ask']):
            raise DataContractError('crossed bid/ask quote')
        for key in ('bid_size', 'ask_size'):
            if payload[key] is not None:
                normalized[key] = _decimal(payload[key], key)
        units = ('shares', 'round_lots', 'unknown') if equity else ('base_asset', 'unknown')
        if payload['size_unit'] not in units:
            raise DataContractError('quote size unit does not match instrument')
        if payload['size_unit'] == 'unknown':
            blockers.append('quote_size_unit_unknown')
        if payload['size_unit'] == 'round_lots':
            blockers.append('round_lot_conversion_not_verified')
        if payload['bid_size'] is None or payload['ask_size'] is None:
            blockers.append('quote_size_unavailable')
        basis, provider_time = payload['timestamp_basis'], payload['provider_timestamp']
        if basis == 'receipt_only':
            if provider_time is not None or event != available or published is not None:
                raise DataContractError('receipt-only quote requires null provider timestamp and event equal to receipt availability')
            blockers.append('quote_provider_timestamp_unknown')
        elif basis == 'provider':
            if type(provider_time) is not str or not 1 <= len(provider_time) <= 100:
                raise DataContractError('provider quote timestamp must be preserved as text')
            try:
                match = _PROVIDER_TIME.fullmatch(provider_time)
                if match is None or match['offset'] == '-00:00':
                    # RFC3339 -00:00 declares an unknown local offset.
                    raise ValueError('known RFC3339 timezone and at most nanoseconds required')
                parsed = datetime.fromisoformat((match['whole'] + match['offset']).replace('Z', '+00:00'))
                whole = int(parsed.timestamp())
                # Parsing through datetime or a binary float discards nanoseconds.
                # Construct the Decimal directly to avoid active-context rounding.
                actual_time = Decimal(str(whole) + '.' + (match['fraction'] or '0'))
                if (whole != event or actual_time > available
                        or (published is not None and actual_time > published)):
                    raise ValueError('timestamp does not match UTC event second')
            except (ValueError, OverflowError) as exc:
                raise DataContractError('provider_timestamp must match its UTC event second') from exc
        else:
            raise DataContractError('invalid quote timestamp_basis')
        if equity and session is not None and not session['open_time'] <= event <= session['close_time']:
            blockers.append('quote_outside_declared_session')
    max_lag = contract['policies']['max_bar_lag_seconds' if kind == 'bar' else 'max_quote_age_seconds']
    lag = ingested - event
    if lag > max_lag:
        blockers.append('stale_at_receipt')
    return {'record': canonical, 'quality': {
        'valid': True, 'blockers': sorted(set(blockers)), 'evidence_class': {
            'historical_backfill': 'HISTORICAL_UNVERIFIED', 'test': 'TEST',
            'prospective_observation': 'PROSPECTIVE_OBSERVATION_UNVERIFIED',
        }[acquisition],
        'qualification_eligible': False, 'alpha_validation_eligible': False,
        'provider_authenticity_verified': False, 'point_in_time_verified': False,
        'live_approved': False, 'acquisition_lag_seconds': lag,
        'max_acquisition_lag_seconds': max_lag, 'stale_at_receipt': lag > max_lag,
        'age_at_validation_seconds': now - event, 'stale_at_validation': now - event > max_lag,
        'revision_chain_verified': False,
    }}


def as_of_records(records, *, as_of, include_historical=True):
    """Select latest locally visible versions from already validated records.

    Visibility needs both availability AND ingestion. Future record payloads
    are never inspected. The append store owns predecessor hash verification;
    this selector rejects duplicate visible versions and never repairs them.
    """
    as_of = _time(as_of, 'as_of')
    if type(include_historical) is not bool:
        raise DataContractError('include_historical must be boolean')
    if type(records) is not list or len(records) > MAX_RECORDS:
        raise DataContractError('as-of input must contain at most 1000000 records')
    selected, seen = {}, set()
    for row in records:
        if type(row) is not dict:
            raise DataContractError('as-of records must be objects')
        available = _time(row.get('available_time'), 'available_time')
        ingested = _time(row.get('ingestion_time'), 'ingestion_time')
        if max(available, ingested) > as_of:
            continue
        if not include_historical and row.get('acquisition_kind') == 'historical_backfill':
            continue
        event = _time(row.get('event_time'), 'event_time')
        if not event <= available <= ingested:
            raise DataContractError('visible record chronology is inconsistent')
        _object(row, RECORD_FIELDS, 'visible record')
        identity = tuple(row[k] for k in ('record_type', 'symbol', 'asset_class', 'venue', 'event_time', 'interval_seconds'))
        revision = row['revision']
        if type(revision) is not int or revision < 0:
            raise DataContractError('invalid visible revision')
        key = identity + (revision,)
        if key in seen:
            raise DataContractError('duplicate visible observation revision')
        seen.add(key)
        old = selected.get(identity)
        if old is None or revision > old['revision']:
            selected[identity] = row
    return [deepcopy(row) for row in sorted(selected.values(), key=lambda row:
            (row['available_time'], row['event_time'], row['symbol'], row['record_type']))]
