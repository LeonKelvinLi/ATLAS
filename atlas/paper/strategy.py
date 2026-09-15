"""Offline bridge from the existing causal strategy library to reviewed intents.

Only explicit TEST or HISTORICAL_UNVERIFIED bars are accepted. This is not an
authenticated prospective intake adapter, execution authority, or qualification
evidence. A caller must still pass the decision through the engine's complete
broker reconciliation and risk checks. The bridge does not tune or size orders.

Strategy identity hashes the normalized spec and the exact local source files
listed in SOURCE_FILES. They cover strategy features, aggregation, session rules,
decimal helpers, decision validation, hashing, and this bridge. Python's standard
library and the host timezone database are not hashed; a full reproducibility
manifest must bind those separately. No research artifacts or broker data are read.
"""

from copy import deepcopy
import hashlib
from pathlib import Path

from atlas.ledger import digest
from atlas.paper.models import PaperError, amount, decision, sha256, timestamp
from atlas.research.multiasset_strategies import StrategyError, normalize_spec, signals_for


VERSION = 'paper-existing-strategy-bridge-v1'
MAX_INPUT_BARS = 10_000
SOURCE_FILES = (
    'atlas/domain.py', 'atlas/ledger.py', 'atlas/paper/models.py', 'atlas/paper/strategy.py',
    'atlas/research/market.py', 'atlas/research/multiasset_strategies.py',
)
BAR_FIELDS = frozenset({'symbol', 'asset_class', 'venue', 'source_id', 'event_time',
    'information_available_time', 'ingestion_time', 'interval_seconds', 'session_date',
    'session', 'revision', 'open', 'high', 'low', 'close', 'volume', 'currency', 'volume_unit'})
PROVENANCE_FIELDS = frozenset({'provenance', 'evidence_class', 'acquisition_kind'})


def strategy_identity(spec):
    """Bind one explicitly selected spec to the local implementation; no search."""
    try:
        normalized = normalize_spec(deepcopy(spec))
        if normalized['family'] not in ('equity_mean_reversion', 'commodity_proxy_trend'):
            raise PaperError('paper_strategy_equity_etfs_only')
        root = Path(__file__).resolve().parents[2]
        sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
        spec_hash, source_hash = digest(normalized), digest(sources)
        return {'strategy_id': normalized['strategy_id'], 'spec': normalized, 'source_files': sources,
                'spec_sha256': spec_hash, 'source_sha256': source_hash,
                'strategy_sha256': digest({'version': VERSION, 'spec_sha256': spec_hash, 'source_sha256': source_hash})}
    except PaperError:
        raise
    except (StrategyError, TypeError, ValueError, OSError):
        raise PaperError('paper_strategy_identity_invalid') from None


def _inputs(bars, calendar, symbol, now, evidence_class):
    if evidence_class not in ('TEST', 'HISTORICAL_UNVERIFIED'):
        raise PaperError('paper_strategy_offline_provenance_required')
    if type(bars) is not list or not 1 <= len(bars) <= MAX_INPUT_BARS:
        raise PaperError('paper_strategy_bounded_explicit_bars_required')
    if (type(calendar) is not dict or 'sessions' not in calendar
            or set(calendar) - {'sessions', 'calendar_id', 'timezone', 'version', 'source_id'}
            or type(calendar['sessions']) is not list or not 1 <= len(calendar['sessions']) <= 1000
            or any(type(row) is not dict or set(row) != {'session_date', 'open_time', 'close_time'}
                   for row in calendar['sessions'])
            or any(type(value) is not str or not 1 <= len(value) <= 128
                   for key, value in calendar.items() if key != 'sessions')
            or calendar.get('timezone', 'America/New_York') != 'America/New_York'):
        raise PaperError('paper_strategy_explicit_calendar_required')
    for row in bars:
        if (type(row) is not dict or not BAR_FIELDS <= set(row) or set(row) - BAR_FIELDS - PROVENANCE_FIELDS
                or row['symbol'] != symbol or row['asset_class'] != 'equity'
                or row['currency'] != 'USD' or row['volume_unit'] != 'shares' or row['session'] != 'regular'):
            raise PaperError('paper_strategy_equity_bar_contract_invalid')
        for name in ('provenance', 'evidence_class'):
            if name in row and row[name] != evidence_class:
                raise PaperError('paper_strategy_provenance_conflict')
        if 'acquisition_kind' in row and row['acquisition_kind'] != (
                'test' if evidence_class == 'TEST' else 'historical_backfill'):
            raise PaperError('paper_strategy_provenance_conflict')
        if any(type(row[key]) is not int for key in ('event_time', 'information_available_time', 'ingestion_time')):
            raise PaperError('paper_strategy_evidence_time_invalid')
        event, available, ingested = (timestamp(row[key]) for key in
                                     ('event_time', 'information_available_time', 'ingestion_time'))
        if not event <= available <= ingested <= now:
            raise PaperError('paper_strategy_future_or_inconsistent_evidence')
        for key in ('open', 'high', 'low', 'close', 'volume'):
            amount(row[key], positive=key != 'volume')
    # Hash every supplied row, including provenance, without depending on input
    # list order. Each bounded canonical record is hashed independently.
    ordered = sorted(bars, key=lambda row: (row['information_available_time'], row['event_time'], row['revision']))
    return digest({'version': VERSION, 'bar_sha256': [digest(row) for row in ordered],
                   'calendar_sha256': digest(calendar), 'evidence_class': evidence_class})


def build_decision(*, bars, calendar, spec, symbol, quantity, limit_price, held_quantity,
                   now, evidence_class, expected_strategy_sha256, ttl_seconds=300):
    """Return ``{decision, evidence}`` for one explicit symbol and position state.

    Long while empty maps to buy. Flat while holding maps to an explicit sell
    quantity no larger than current holdings. Already-long and already-flat
    actions are refused. Holdings are only an input here; the engine must verify
    them independently. Bars must contain this symbol alone and be known by the
    original signal time. Expiry is capped from that time, never from ``now``.
    """
    try:
        now = timestamp(now)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise PaperError('paper_strategy_ttl_invalid')
        identity = strategy_identity(spec)
        if sha256(expected_strategy_sha256) != identity['strategy_sha256']:
            raise PaperError('paper_strategy_source_or_spec_binding_mismatch')
        if symbol not in identity['spec']['symbols']:
            raise PaperError('paper_strategy_symbol_not_selected')
        held, qty = amount(held_quantity), amount(quantity, positive=True)
        data_hash = _inputs(bars, calendar, symbol, now, evidence_class)
        signals = signals_for(bars, identity['spec'], calendar=calendar)
        if not signals:
            raise PaperError('paper_strategy_complete_signal_unavailable')
        signal = signals[-1]
        metadata = signal['metadata']
        start = timestamp(signal['decision_time'])
        expires = min(timestamp(signal['expires_at']), start + ttl_seconds)
        event = timestamp(metadata['feature_event_time'])
        if not event <= start <= now < expires or now - event >= ttl_seconds:
            raise PaperError('paper_strategy_original_signal_stale')
        if any(row['ingestion_time'] > start for row in bars):
            raise PaperError('paper_strategy_evidence_received_after_signal')
        if (metadata['features'] is None or metadata['atr'] is None or amount(metadata['atr']) == 0
                or metadata['explicit_session_calendar'] is not True
                or identity['spec']['variant'] == 'opening_range' and not metadata['features']['opening_complete']):
            raise PaperError('paper_strategy_features_insufficient')
        if signal['direction'] == 'long':
            if held != 0:
                raise PaperError('paper_strategy_already_long')
            side = 'buy'
        elif signal['direction'] == 'flat':
            if held == 0:
                raise PaperError('paper_strategy_already_flat')
            if qty > held:
                raise PaperError('paper_strategy_reduction_exceeds_inventory')
            side = 'sell'
        else:
            raise PaperError('paper_strategy_direction_unsupported')
        reviewed = decision({'signal_id': signal['signal_id'], 'strategy_id': identity['strategy_id'],
            'strategy_sha256': identity['strategy_sha256'], 'data_sha256': data_hash,
            'symbol': symbol, 'side': side, 'quantity': quantity, 'limit_price': limit_price,
            'signal_time': start, 'expires_at': expires})
        return {'decision': reviewed, 'evidence': {'version': VERSION, 'evidence_class': evidence_class,
            'provenance': evidence_class, 'strategy': identity, 'data_sha256': data_hash,
            'source_signal': deepcopy(signal), 'original_decision_time': start,
            'feature_event_time': event, 'maximum_ingestion_time': max(row['ingestion_time'] for row in bars),
            'decision_expires_at': expires, 'simulation_only': True, 'prospective_feed_authenticated': False,
            'execution_authorized': False, 'qualification_established': False, 'live_enabled': False}}
    except PaperError:
        raise
    except (StrategyError, KeyError, TypeError, ValueError, ArithmeticError):
        raise PaperError('paper_strategy_inputs_invalid') from None
