"""Synthetic causal inputs for the existing-strategy bridge; no evidence reads."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import socket

import pytest

from atlas.ledger import digest
from atlas.paper import broker as broker_module, journal as journal_module
from atlas.paper.broker import AlpacaPaperAPI
from atlas.paper.engine import PaperEngine
from atlas.paper.journal import PaperJournal
from atlas.paper.models import DECISION_FIELDS, PaperError, decision
from atlas.paper.risk import PaperRiskPolicy
from atlas.paper.simulation import SyntheticAlpaca
from atlas.paper.strategy import SOURCE_FILES, build_decision, strategy_identity
from atlas.research.multiasset_strategies import normalize_spec, signals_for


OPEN = int(datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc).timestamp())
CALENDAR = {'sessions': [{'session_date': '2026-09-01', 'open_time': OPEN, 'close_time': OPEN + 23400}]}


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('offline strategy bridge cannot use network or credentials')
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(broker_module, '_credentials', deny)
    monkeypatch.setattr(broker_module, '_owned_http', deny)
    monkeypatch.setattr(journal_module, '_account_lock_root', lambda: tmp_path / 'account-locks')


def bars(prices=(100, 101, 99, 101, 99, 100, 98), *, symbol='SPY', interval=900):
    result = []
    for index, close in enumerate(prices):
        close = Decimal(str(close))
        event = OPEN + (index + 1) * interval
        result.append({'symbol': symbol, 'asset_class': 'equity', 'venue': 'fixture', 'source_id': 'fixture',
            'event_time': event, 'information_available_time': event, 'ingestion_time': event,
            'interval_seconds': interval, 'session_date': '2026-09-01', 'session': 'regular',
            'revision': 0, 'open': str(close), 'high': str(close + Decimal('.05')),
            'low': str(close - Decimal('.05')), 'close': str(close), 'volume': '100',
            'currency': 'USD', 'volume_unit': 'shares'})
    return result


@pytest.fixture
def inputs():
    spec = normalize_spec({'family': 'equity_mean_reversion', 'variant': 'zscore', 'lookback': 5})
    supplied = bars()
    return {'bars': supplied, 'calendar': deepcopy(CALENDAR), 'spec': spec, 'symbol': 'SPY',
            'quantity': '0.09', 'limit_price': '98', 'held_quantity': '0',
            'now': supplied[-1]['event_time'] + 1.5, 'evidence_class': 'TEST',
            'expected_strategy_sha256': strategy_identity(spec)['strategy_sha256']}


def test_long_bridge_preserves_actual_library_signal_exact_engine_contract_and_inputs(inputs):
    before = deepcopy(inputs)
    expected = signals_for(inputs['bars'], inputs['spec'], calendar=inputs['calendar'])[-1]
    result = build_decision(**inputs)
    reviewed, evidence = result['decision'], result['evidence']
    assert inputs == before
    assert set(reviewed) == DECISION_FIELDS and decision(reviewed) == reviewed
    assert expected['direction'] == 'long' and reviewed['side'] == 'buy'
    assert reviewed['signal_id'] == expected['signal_id']
    assert reviewed['strategy_id'] == expected['strategy_id'] and '/' in reviewed['strategy_id']
    assert reviewed['strategy_sha256'] == inputs['expected_strategy_sha256']
    assert reviewed['quantity'] == '0.09' and reviewed['limit_price'] == '98'
    assert reviewed['signal_time'] == expected['decision_time']
    assert reviewed['expires_at'] == expected['decision_time'] + 300
    assert evidence['source_signal'] == expected
    assert reviewed['data_sha256'] == evidence['data_sha256']
    assert evidence['provenance'] == evidence['evidence_class'] == 'TEST'
    assert evidence['simulation_only'] is True
    for field in ('prospective_feed_authenticated', 'execution_authorized', 'qualification_established', 'live_enabled'):
        assert evidence[field] is False


@pytest.mark.parametrize('evidence_class', ['TEST', 'HISTORICAL_UNVERIFIED'])
def test_only_offline_provenance_is_preserved_with_all_source_and_spec_hashes(inputs, evidence_class):
    inputs['evidence_class'] = evidence_class
    inputs['bars'][0]['evidence_class'] = evidence_class
    inputs['bars'][1]['provenance'] = evidence_class
    inputs['bars'][2]['acquisition_kind'] = 'test' if evidence_class == 'TEST' else 'historical_backfill'
    result = build_decision(**inputs)
    assert result['evidence']['evidence_class'] == evidence_class
    assert result['evidence']['provenance'] == evidence_class
    identity = result['evidence']['strategy']
    assert set(identity['source_files']) == set(SOURCE_FILES)
    assert identity['source_sha256'] == digest(identity['source_files'])
    assert identity['spec_sha256'] == digest(normalize_spec(inputs['spec']))
    assert identity['strategy_sha256'] == result['decision']['strategy_sha256']


@pytest.mark.parametrize('provenance', ['PAPER', 'PAPER_API', 'PROSPECTIVE', 'PROSPECTIVE_VERIFIED', 'SYNTHETIC', None])
def test_caller_declarations_never_create_a_prospective_or_paper_feed(inputs, provenance):
    with pytest.raises(PaperError, match='offline_provenance_required'):
        build_decision(**{**inputs, 'evidence_class': provenance})


@pytest.mark.parametrize('field,value', [('provenance', 'HISTORICAL_UNVERIFIED'),
                                       ('evidence_class', 'PROSPECTIVE_VERIFIED'),
                                       ('acquisition_kind', 'prospective_observation')])
def test_conflicting_embedded_provenance_is_not_discarded(inputs, field, value):
    inputs['bars'][0][field] = value
    with pytest.raises(PaperError, match='provenance_conflict'):
        build_decision(**inputs)


def test_hash_binding_changes_with_spec_or_exact_implementation_source(inputs, monkeypatch):
    changed_spec = {**inputs['spec'], 'z_threshold': '1.5'}
    changed_spec.pop('strategy_id')
    assert strategy_identity(changed_spec)['strategy_sha256'] != inputs['expected_strategy_sha256']
    with pytest.raises(PaperError, match='source_or_spec_binding_mismatch'):
        build_decision(**{**inputs, 'spec': changed_spec})
    original_read = Path.read_bytes
    def changed_read(path):
        original = original_read(path)
        return original + b'\n# synthetic source change\n' if path.name == 'multiasset_strategies.py' else original
    monkeypatch.setattr(Path, 'read_bytes', changed_read)
    with pytest.raises(PaperError, match='source_or_spec_binding_mismatch'):
        build_decision(**inputs)


def test_data_hash_binds_calendar_prices_and_provenance(inputs):
    original = build_decision(**inputs)
    changed = deepcopy(inputs)
    changed['bars'][0]['volume'] = '101'
    volume = build_decision(**changed)
    historical = build_decision(**{**inputs, 'evidence_class': 'HISTORICAL_UNVERIFIED'})
    changed = deepcopy(inputs)
    changed['calendar']['calendar_id'] = 'fixture-version-2'
    calendar = build_decision(**changed)
    hashes = {row['decision']['data_sha256'] for row in (original, volume, historical, calendar)}
    assert len(hashes) == 4
    assert build_decision(**{**inputs, 'bars': list(reversed(inputs['bars']))}) == original


def test_build_time_cannot_refresh_original_signal_or_expiry(inputs):
    earlier = build_decision(**inputs)
    later = build_decision(**{**inputs, 'now': inputs['now'] + 100})
    assert later == earlier
    assert later['decision']['signal_time'] < inputs['now']
    assert later['decision']['expires_at'] == inputs['bars'][-1]['event_time'] + 300
    with pytest.raises(PaperError, match='original_signal_stale'):
        build_decision(**{**inputs, 'now': earlier['decision']['expires_at']})


def test_expiry_can_only_be_shortened_from_the_original_signal(inputs):
    start = inputs['bars'][-1]['event_time']
    result = build_decision(**{**inputs, 'ttl_seconds': 60, 'now': start + 59})
    assert result['decision']['signal_time'] == start and result['decision']['expires_at'] == start + 60
    with pytest.raises(PaperError, match='original_signal_stale'):
        build_decision(**{**inputs, 'ttl_seconds': 60, 'now': start + 60})


@pytest.mark.parametrize('ttl', [0, -1, 301, True, 1.5])
def test_ttl_cannot_remove_hard_five_minute_cap(inputs, ttl):
    with pytest.raises(PaperError, match='ttl_invalid'):
        build_decision(**{**inputs, 'ttl_seconds': ttl})


@pytest.mark.parametrize('field', ['event_time', 'information_available_time', 'ingestion_time'])
def test_future_evidence_is_refused_before_strategy_features(inputs, field):
    inputs['bars'][-1][field] = int(inputs['now']) + 10
    with pytest.raises(PaperError, match='future_or_inconsistent_evidence'):
        build_decision(**inputs)


def test_future_bars_cannot_affect_a_current_decision(inputs):
    prefix = build_decision(**inputs)
    later = bars((100, 101, 99, 101, 99, 100, 98, 110))[-1]
    full_signals = signals_for(inputs['bars'] + [later], inputs['spec'], calendar=inputs['calendar'])
    assert [signal for signal in full_signals if signal['signal_id'] == prefix['decision']['signal_id']] == [prefix['evidence']['source_signal']]
    with pytest.raises(PaperError, match='future_or_inconsistent_evidence'):
        build_decision(**{**inputs, 'bars': inputs['bars'] + [later]})


def test_delayed_ingestion_is_not_rewritten_as_historical_availability(inputs):
    inputs['bars'][0]['ingestion_time'] = inputs['bars'][-1]['event_time'] + 1
    with pytest.raises(PaperError, match='evidence_received_after_signal'):
        build_decision(**inputs)


def test_old_bar_with_new_publication_is_stale_even_when_signal_time_is_recent(inputs):
    inputs['now'] += 601
    inputs['bars'][-1]['information_available_time'] += 600
    inputs['bars'][-1]['ingestion_time'] += 600
    with pytest.raises(PaperError, match='original_signal_stale'):
        build_decision(**inputs)


@pytest.mark.parametrize('failure', ['warmup', 'gap', 'incomplete_aggregation', 'zero_atr'])
def test_insufficient_inputs_never_turn_into_flat_sell_authority(inputs, failure):
    inputs['held_quantity'] = '0.09'
    if failure == 'warmup':
        inputs['bars'] = inputs['bars'][:5]
        inputs['now'] = inputs['bars'][-1]['event_time'] + 1
    elif failure == 'gap':
        del inputs['bars'][-2]
    elif failure == 'incomplete_aggregation':
        inputs['bars'] = bars((100, 101), interval=300)
        inputs['now'] = inputs['bars'][-1]['event_time'] + 1
    else:
        for row in inputs['bars']:
            row.update(open='100', high='100', low='100', close='100')
    with pytest.raises(PaperError, match='features_insufficient|complete_signal_unavailable'):
        build_decision(**inputs)


@pytest.mark.parametrize('calendar', [None, {}, {'sessions': []}, {'sessions': CALENDAR['sessions'], 'calendar_verified': True}])
def test_explicit_calendar_is_required_and_cannot_self_authenticate(inputs, calendar):
    with pytest.raises(PaperError, match='explicit_calendar_required'):
        build_decision(**{**inputs, 'calendar': calendar})


def test_long_action_refuses_adding_to_existing_inventory(inputs):
    with pytest.raises(PaperError, match='already_long'):
        build_decision(**{**inputs, 'held_quantity': '0.01'})


def test_flat_action_only_reduces_explicit_existing_inventory(inputs):
    inputs['bars'] = bars((100, 101, 99, 101, 99, 100, 98, 101))
    inputs['now'] = inputs['bars'][-1]['event_time'] + 1
    inputs['held_quantity'] = '0.09'
    result = build_decision(**inputs)
    assert result['evidence']['source_signal']['direction'] == 'flat'
    assert result['decision']['side'] == 'sell' and result['decision']['quantity'] == '0.09'
    with pytest.raises(PaperError, match='already_flat'):
        build_decision(**{**inputs, 'held_quantity': '0'})
    with pytest.raises(PaperError, match='reduction_exceeds_inventory'):
        build_decision(**{**inputs, 'quantity': '0.10'})


@pytest.mark.parametrize('symbol', ['GLD', 'USO'])
def test_existing_commodity_trend_proxies_remain_equity_etfs(inputs, symbol):
    settings = normalize_spec({'family': 'commodity_proxy_trend', 'variant': 'momentum', 'lookback': 5})
    supplied = bars((100, 100.2, 100.4, 100.6, 100.8, 101), symbol=symbol, interval=3600)
    result = build_decision(**{**inputs, 'bars': supplied, 'spec': settings, 'symbol': symbol,
        'now': supplied[-1]['event_time'] + 1, 'limit_price': '101',
        'expected_strategy_sha256': strategy_identity(settings)['strategy_sha256']})
    assert result['decision']['side'] == 'buy' and result['decision']['symbol'] == symbol
    metadata = result['evidence']['source_signal']['metadata']
    assert metadata['instrument_kind'] == 'commodity_proxy_etf' and metadata['actual_futures_contract'] is False


def test_crypto_and_unselected_or_mixed_symbols_are_refused(inputs):
    with pytest.raises(PaperError, match='equity_etfs_only'):
        strategy_identity({'family': 'btc_breakout', 'variant': 'donchian'})
    with pytest.raises(PaperError, match='symbol_not_selected'):
        build_decision(**{**inputs, 'symbol': 'BTC-USD'})
    inputs['bars'][0]['asset_class'] = 'crypto'
    with pytest.raises(PaperError, match='equity_bar_contract_invalid'):
        build_decision(**inputs)
    inputs['bars'][0]['asset_class'] = 'equity'
    inputs['bars'][0]['symbol'] = 'QQQ'
    with pytest.raises(PaperError, match='equity_bar_contract_invalid'):
        build_decision(**inputs)


def test_existing_strategy_decision_crosses_actual_simulation_risk_and_durable_engine(inputs, tmp_path):
    bridged = build_decision(**inputs)
    reviewed = bridged['decision']
    sim = SyntheticAlpaca(now=inputs['now'])
    sim.quotes['SPY'].update(bid='97.99', ask='98')
    api = AlpacaPaperAPI(transport=sim.request, clock=sim.clock)
    policy = PaperRiskPolicy(allowed_strategy_ids=(reviewed['strategy_id'],))
    binding = {'mode': 'SIMULATION', 'account_fingerprint': api.account()['fingerprint'],
        'strategy_sha256': reviewed['strategy_sha256'], 'policy_sha256': policy.fingerprint(),
        'allocated_principal': '100'}
    with PaperJournal(tmp_path / 'paper-state', binding, create=True, now=sim.clock()) as journal:
        engine = PaperEngine(api, journal, policy, clock=sim.clock)
        engine.initialize()
        result = engine.submit(reviewed)
        assert result['new_submission'] is True and result['order']['state'] == 'new'
        assert result['order']['decision'] == reviewed
        assert result['order']['risk_at_reservation']['passed'] is True
        assert journal.integrity_check()
        assert result['provenance'] == 'TEST' and result['actual_orders_submitted'] == 0
    posts = [call for call in sim.calls if call['method'] == 'POST']
    assert len(posts) == 1 and posts[0]['payload']['client_order_id'] == result['order']['client_order_id']
