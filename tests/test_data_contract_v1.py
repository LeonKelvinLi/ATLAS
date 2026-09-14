"""Adversarial schema and availability tests; no feeds, accounts or files."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

import pytest

from atlas.research.data_contract_v1 import (
    DAY, VERSION, DataContractError, as_of_records, default_contract, validate_contract, validate_record,
)
from atlas.research.multiasset_validation import DEFAULT_THRESHOLDS


CREATED = int(datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp())
NOW = CREATED + DAY


def contract(created=CREATED):
    return default_contract('data-v1-fixture', created - 180 * DAY, created)


def bar(*, equity=False, event=CREATED + 300):
    return {
        'contract_version': VERSION, 'record_type': 'bar',
        'symbol': 'SPY' if equity else 'BTC-USD', 'asset_class': 'equity' if equity else 'crypto',
        'venue': 'alpaca-iex' if equity else 'coinbase-exchange', 'source_id': 'fixture-source',
        'event_time': event, 'publication_time': event + 1, 'available_time': event + 2,
        'ingestion_time': event + 3, 'revision': 0, 'previous_observation_hash': None,
        'raw_capture_sha256': 'a' * 64, 'acquisition_kind': 'prospective_observation',
        'revision_basis': 'locally_observed_versions', 'interval_seconds': 300, 'session': None,
        'payload': {'open': '100.00', 'high': '101', 'low': '99.000', 'close': '100.50',
                    'volume': '0.00', 'volume_unit': 'shares' if equity else 'base_asset',
                    'currency': 'USD', 'price_adjustment': 'unadjusted',
                    'corporate_actions_status': 'unknown' if equity else 'not_applicable'},
    }


def session(date='2026-09-11', close_hour=16):
    ny = ZoneInfo('America/New_York')
    y, m, d = (int(part) for part in date.split('-'))
    return {'calendar_id': 'declared-iex-calendar', 'session_date': date,
            'open_time': int(datetime(y, m, d, 9, 30, tzinfo=ny).timestamp()),
            'close_time': int(datetime(y, m, d, close_hour, tzinfo=ny).timestamp()),
            'calendar_verified': False}


def equity_bar(date='2026-09-11', close_hour=16):
    bounds = session(date, close_hour)
    result = bar(equity=True, event=bounds['open_time'] + 300)
    result['session'] = bounds
    return result


def quote(*, equity=False, event=CREATED + 300):
    result = bar(equity=equity, event=event)
    result['record_type'] = 'quote'
    result['interval_seconds'] = 0
    result['payload'] = {'bid': '100.00', 'ask': '100.25', 'bid_size': None, 'ask_size': '0',
                         'size_unit': 'shares' if equity else 'base_asset', 'currency': 'USD',
                         'timestamp_basis': 'provider',
                         'provider_timestamp': datetime.fromtimestamp(event, timezone.utc).isoformat()}
    return result


def checked(record, *, now=NOW, settings=None):
    return validate_record(record, contract() if settings is None else settings, now)


def test_fixed_generation_windows_and_threshold_copy_are_independent():
    value = contract()
    before = deepcopy(value)
    canonical = validate_contract(value)
    assert canonical == before and canonical is not value
    assert value['qualification_thresholds'] == DEFAULT_THRESHOLDS
    windows = value['windows']
    for window in windows.values():
        assert window['end_exclusive'] - window['start'] == 90 * DAY
    assert windows['validation']['start'] - windows['research']['end_exclusive'] == 300
    assert windows['prospective_holdout']['start'] - windows['validation']['end_exclusive'] == 300
    assert windows['prospective_holdout']['start'] >= CREATED
    canonical['qualification_thresholds']['min_sharpe'] = 999
    assert value == before and DEFAULT_THRESHOLDS['min_sharpe'] == 1.2
    assert [item['symbol'] for item in value['universe']] == ['SPY', 'QQQ', 'GLD', 'USO', 'BTC-USD']


@pytest.mark.parametrize('field,replacement', [
    ('version', 'V2'), ('interval_seconds', 60), ('interval_seconds', 300.0),
    ('embargo_seconds', False), ('universe', []), ('generation_id', '../escape'),
    ('created_at', True), ('policies', {}), ('qualification_thresholds', {}),
])
def test_contract_rejects_drift_and_coercions(field, replacement):
    value = contract()
    value[field] = replacement
    with pytest.raises(DataContractError):
        validate_contract(value)


def test_contract_rejects_changed_windows_extra_fields_and_native_type_substitution():
    for mutate in (
        lambda c: c['windows']['prospective_holdout'].__setitem__('start', CREATED - 1),
        lambda c: c.__setitem__('allocation_approved', True),
        lambda c: c.__setitem__('universe', tuple(c['universe'])),
        lambda c: c['qualification_thresholds'].__setitem__('min_sharpe', 1.19),
        lambda c: c['policies'].__setitem__('max_bar_lag_seconds', 901),
    ):
        value = contract()
        mutate(value)
        with pytest.raises(DataContractError):
            validate_contract(value)
    with pytest.raises(DataContractError, match='holdout'):
        default_contract('too-late', CREATED - 181 * DAY, CREATED)


def test_valid_bar_canonicalizes_decimals_without_mutating_input_or_claiming_alpha():
    source = bar()
    before = deepcopy(source)
    result = checked(source)
    assert source == before
    assert result['record']['payload']['open'] == '100'
    assert result['record']['payload']['close'] == '100.5'
    assert result['record']['payload']['volume'] == '0'
    assert result['record']['publication_time'] == source['publication_time']
    quality = result['quality']
    assert quality['valid'] is True
    assert quality['evidence_class'] == 'PROSPECTIVE_OBSERVATION_UNVERIFIED'
    assert quality['qualification_eligible'] is quality['alpha_validation_eligible'] is False
    assert quality['provider_authenticity_verified'] is quality['point_in_time_verified'] is False
    assert quality['revision_chain_verified'] is quality['live_approved'] is False
    assert {'provider_authenticity_not_verified', 'cost_source_not_verified'} <= set(quality['blockers'])
    result['record']['payload']['open'] = '0'
    assert source == before


@pytest.mark.parametrize('field,value', [
    ('open', 'NaN'), ('open', 'Infinity'), ('open', '-1'), ('open', '0'),
    ('volume', '-1'), ('volume', 3.0), ('volume', 3), ('volume', True),
    ('close', Decimal('100')), ('high', '99'), ('low', '101'),
    ('volume', '1e1000000'), ('volume', ' 3'), ('volume', '01'),
])
def test_bad_ohlcv_and_non_string_numbers_are_rejected(field, value):
    record = bar()
    record['payload'][field] = value
    with pytest.raises(DataContractError):
        checked(record)


def test_decimal_normalization_is_exact_even_under_low_precision_context():
    record = bar()
    record['payload']['volume'] = '123456789012345678901234.0012300'
    with localcontext() as context:
        context.prec = 2
        result = checked(record)
    assert result['record']['payload']['volume'] == '123456789012345678901234.00123'


@pytest.mark.parametrize('change', [
    {'event_time': CREATED + 302},
    {'publication_time': CREATED + 299},
    {'publication_time': CREATED + 303},
    {'available_time': CREATED + 304},
    {'ingestion_time': NOW + 1},
    {'event_time': True},
    {'interval_seconds': 60},
    {'interval_seconds': True},
])
def test_impossible_chronology_incomplete_bars_and_wrong_grids_fail(change):
    record = bar()
    record.update(change)
    with pytest.raises(DataContractError):
        checked(record)


def test_unknown_publication_is_preserved_without_inventing_old_ingestion():
    record = bar(event=CREATED - DAY)
    record.update(publication_time=None, available_time=CREATED + 20,
                  ingestion_time=CREATED + 21, acquisition_kind='historical_backfill')
    result = checked(record)
    assert result['record']['publication_time'] is None
    assert result['record']['ingestion_time'] == CREATED + 21
    assert result['quality']['evidence_class'] == 'HISTORICAL_UNVERIFIED'
    assert {'publication_time_unknown', 'not_prospective_observation', 'stale_at_receipt'} <= set(result['quality']['blockers'])
    assert result['quality']['qualification_eligible'] is False
    record['acquisition_kind'] = 'prospective_observation'
    with pytest.raises(DataContractError, match='predates'):
        checked(record)


@pytest.mark.parametrize('kind,label', [('historical_backfill', 'HISTORICAL_UNVERIFIED'),
                                       ('test', 'TEST'),
                                       ('prospective_observation', 'PROSPECTIVE_OBSERVATION_UNVERIFIED')])
def test_acquisition_labels_never_certify_provider_or_qualification(kind, label):
    record = bar()
    record['acquisition_kind'] = kind
    result = checked(record)
    assert result['quality']['evidence_class'] == label
    assert result['quality']['qualification_eligible'] is False
    assert result['quality']['provider_authenticity_verified'] is False


@pytest.mark.parametrize('lag,stale', [(900, False), (901, True)])
def test_bar_staleness_is_at_receipt_and_old_timely_archive_is_not_retroactively_stale(lag, stale):
    record = bar()
    record['ingestion_time'] = record['event_time'] + lag
    quality = checked(record)['quality']
    assert quality['stale_at_receipt'] is stale
    assert ('stale_at_receipt' in quality['blockers']) is stale
    assert quality['stale_at_validation'] is True
    assert quality['acquisition_lag_seconds'] == lag
    assert quality['age_at_validation_seconds'] == NOW - record['event_time']


@pytest.mark.parametrize('date,utc_hour', [('2026-03-06', 14), ('2026-03-09', 13),
                                         ('2026-11-02', 14)])
def test_equity_session_bounds_respect_new_york_dst(date, utc_hour):
    record = equity_bar(date)
    created = record['session']['open_time'] - DAY
    result = checked(record, settings=contract(created), now=record['ingestion_time'])
    assert datetime.fromtimestamp(record['session']['open_time'], timezone.utc).hour == utc_hour
    assert result['record']['session'] == record['session']
    assert {'calendar_authenticity_not_verified', 'corporate_actions_unknown'} <= set(result['quality']['blockers'])


def test_early_close_final_completed_bucket_is_valid_and_following_bar_is_not():
    record = equity_bar(close_hour=13)
    closing = record['session']['close_time']
    record.update(event_time=closing, publication_time=closing, available_time=closing, ingestion_time=closing)
    assert checked(record)['quality']['valid'] is True
    record.update(event_time=closing + 300, publication_time=closing + 300,
                  available_time=closing + 300, ingestion_time=closing + 300)
    with pytest.raises(DataContractError, match='session'):
        checked(record)


@pytest.mark.parametrize('field,value', [('calendar_verified', True), ('session_date', '2026-09-10'),
                                        ('close_time', CREATED + 17 * 3600 + 30 * 60)])
def test_equity_calendar_cannot_be_self_certified_or_misaligned(field, value):
    record = equity_bar()
    record['session'][field] = value
    with pytest.raises(DataContractError):
        checked(record)


def test_equity_requires_bar_session_crypto_requires_null_and_units_stay_unknown():
    with pytest.raises(DataContractError, match='session'):
        checked(bar(equity=True))
    record = bar()
    record['session'] = session()
    with pytest.raises(DataContractError, match='crypto session'):
        checked(record)
    record['session'] = None
    record['payload']['volume_unit'] = 'unknown'
    result = checked(record)
    assert result['record']['payload']['volume_unit'] == 'unknown'
    assert 'volume_unit_unknown' in result['quality']['blockers']


@pytest.mark.parametrize('field,value', [('bid', '0'), ('ask', '-1'), ('bid', '101'),
                                       ('bid_size', '-1'), ('ask_size', 'Infinity'),
                                       ('currency', 'EUR'), ('size_unit', 'round_lots')])
def test_invalid_or_crossed_bbo_is_rejected(field, value):
    record = quote()
    record['payload'][field] = value
    with pytest.raises(DataContractError):
        checked(record)


def test_quote_null_size_is_not_zero_and_equity_round_lots_are_not_assumed_shares():
    record = quote(equity=True)
    record['payload']['size_unit'] = 'round_lots'
    result = checked(record)
    assert result['record']['payload']['bid_size'] is None
    assert result['record']['payload']['ask_size'] == '0'
    assert {'round_lot_conversion_not_verified', 'quote_size_unavailable', 'session_metadata_unknown'} <= set(result['quality']['blockers'])


def test_offhours_quote_is_retained_with_blocker_instead_of_using_bar_session_rule():
    record = quote(equity=True)
    record['session'] = session()
    result = checked(record)
    assert 'quote_outside_declared_session' in result['quality']['blockers']
    assert result['record']['event_time'] == record['event_time']
    assert result['record']['session'] == record['session']
    assert result['record']['payload']['bid'] == '100'


def test_receipt_only_quote_cannot_claim_provider_event_or_known_publication():
    record = quote()
    record['payload'].update(timestamp_basis='receipt_only', provider_timestamp=None)
    record.update(event_time=record['available_time'], publication_time=None)
    result = checked(record)
    assert result['record']['payload']['provider_timestamp'] is None
    assert {'publication_time_unknown', 'quote_provider_timestamp_unknown'} <= set(result['quality']['blockers'])
    record['publication_time'] = record['event_time']
    with pytest.raises(DataContractError):
        checked(record)


def test_provider_fractional_timestamp_must_be_available_after_its_actual_time():
    record = quote()
    event = record['event_time']
    record['payload']['provider_timestamp'] = datetime.fromtimestamp(event, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + '.999999999Z'
    record.update(publication_time=None, available_time=event + 1, ingestion_time=event + 1)
    assert checked(record)['quality']['valid'] is True
    record.update(available_time=event, ingestion_time=event)
    with pytest.raises(DataContractError, match='provider_timestamp'):
        checked(record)


@pytest.mark.parametrize('fraction', ['000000001', '00000001', '0000001', '000001', '999999999'])
def test_nanosecond_event_cannot_be_available_or_published_at_its_floor(fraction):
    record = quote()
    event = record['event_time']
    raw = datetime.fromtimestamp(event, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + '.' + fraction + 'Z'
    record['payload']['provider_timestamp'] = raw
    record.update(publication_time=None, available_time=event+1, ingestion_time=event+1)
    with localcontext() as context:
        context.prec = 3
        result = checked(record)
        assert result['record']['payload']['provider_timestamp'] == raw
        assert result['record']['event_time'] == event
        record['publication_time'] = event
        with pytest.raises(DataContractError, match='provider_timestamp'):
            checked(record)
        record.update(publication_time=None, available_time=event, ingestion_time=event)
        with pytest.raises(DataContractError, match='provider_timestamp'):
            checked(record)


def test_exact_zero_nanoseconds_and_explicit_offset_preserve_original_text():
    record = quote()
    event = record['event_time']
    raw = '2026-09-11T01:05:00.000000000+01:00'
    record['payload']['provider_timestamp'] = raw
    record.update(publication_time=event, available_time=event, ingestion_time=event)
    result = checked(record)
    assert result['record']['payload']['provider_timestamp'] == raw
    assert result['record']['event_time'] == event


@pytest.mark.parametrize('timestamp_text', [
    '2026-09-11T00:05:00.0000000001Z', '2026-09-11T00:05:00.Z',
    '2026-09-11 00:05:00Z', '2026-09-11T00:05:00+00:00:00',
    '2026-09-11T00:05:00-00:00', '2026-09-11T00:05:00,123Z',
])
def test_provider_timestamp_requires_unambiguous_bounded_rfc3339(timestamp_text):
    record = quote()
    record['payload']['provider_timestamp'] = timestamp_text
    with pytest.raises(DataContractError, match='provider_timestamp'):
        checked(record)


@pytest.mark.parametrize('timestamp_text', [None, '', '2026-09-11', 'not-a-time', '2026-09-11T00:05:01Z'])
def test_provider_quote_timestamp_is_required_aware_and_consistent(timestamp_text):
    record = quote()
    record['payload']['provider_timestamp'] = timestamp_text
    with pytest.raises(DataContractError):
        checked(record)


@pytest.mark.parametrize('lag,stale', [(60, False), (61, True)])
def test_quote_receipt_age_has_its_own_sixty_second_bound(lag, stale):
    record = quote()
    record['ingestion_time'] = record['event_time'] + lag
    result = checked(record)
    assert result['quality']['stale_at_receipt'] is stale
    assert result['quality']['max_acquisition_lag_seconds'] == 60


@pytest.mark.parametrize('change', [
    {'symbol': 'AAPL'}, {'venue': 'alpaca-sip'}, {'source_id': ''},
    {'raw_capture_sha256': 'not-a-hash'}, {'raw_capture_sha256': 'A' * 64},
    {'previous_observation_hash': 'b' * 64}, {'revision': -1}, {'revision': True},
    {'revision': 1}, {'revision_basis': 'provider_verified'}, {'acquisition_kind': 'live'},
    {'unexpected': True},
])
def test_identity_provenance_and_revision_metadata_fail_closed(change):
    record = bar()
    record.update(change)
    with pytest.raises(DataContractError):
        checked(record)


def test_later_revision_needs_explicit_link_but_record_alone_cannot_verify_chain():
    record = bar()
    record.update(revision=1, previous_observation_hash='b' * 64)
    result = checked(record)
    assert result['record']['revision'] == 1
    assert result['quality']['revision_chain_verified'] is False


def test_as_of_requires_both_available_and_ingested_and_excludes_future_payloads():
    original = checked(bar())['record']
    revised = deepcopy(original)
    revised.update(revision=1, previous_observation_hash='b' * 64,
                   publication_time=CREATED + 400, available_time=CREATED + 400,
                   ingestion_time=CREATED + 500)
    revised['payload']['close'] = '100.75'
    assert as_of_records([original, revised], as_of=CREATED + 499) == [original]
    assert as_of_records([original, revised], as_of=CREATED + 500) == [revised]
    future = deepcopy(revised)
    future['payload'] = {'malformed_future_data': object()}
    assert as_of_records([original, future], as_of=CREATED + 499) == [original]


def test_as_of_does_not_depend_on_input_order_and_returns_independent_latest_versions():
    original = checked(bar())['record']
    revised = deepcopy(original)
    revised.update(revision=1, previous_observation_hash='b' * 64, publication_time=CREATED + 400,
                   available_time=CREATED + 400, ingestion_time=CREATED + 401)
    assert as_of_records([revised, original], as_of=NOW) == [revised]
    visible = as_of_records([original, revised], as_of=NOW)
    visible[0]['payload']['close'] = '1'
    assert revised['payload']['close'] == '100.5'
    with pytest.raises(DataContractError, match='duplicate'):
        as_of_records([original, original], as_of=NOW)


def test_as_of_historical_filter_is_explicit_without_reclassifying_records():
    record = checked(bar())['record']
    record['acquisition_kind'] = 'historical_backfill'
    assert as_of_records([record], as_of=NOW) == [record]
    assert as_of_records([record], as_of=NOW, include_historical=False) == []
    assert record['acquisition_kind'] == 'historical_backfill'
    with pytest.raises(DataContractError):
        as_of_records([record], as_of=NOW, include_historical=1)
    with pytest.raises(DataContractError, match='1000000'):
        as_of_records([record] * 1_000_001, as_of=NOW)
