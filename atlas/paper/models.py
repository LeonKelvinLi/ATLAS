"""Strict reviewed decisions and broker-order progress, with no I/O."""
from copy import deepcopy
from decimal import Decimal
import math
import re
from uuid import UUID, uuid5

from atlas.domain import decimal, financial_context
from atlas.ledger import digest

DECISION_FIELDS = frozenset({'signal_id', 'strategy_id', 'strategy_sha256', 'data_sha256',
                            'symbol', 'side', 'quantity', 'limit_price', 'signal_time', 'expires_at'})
TERMINAL = frozenset({'filled', 'canceled', 'expired', 'rejected'})
KNOWN_STATUSES = TERMINAL | frozenset({'new', 'accepted', 'pending_new', 'partially_filled',
    'pending_cancel', 'done_for_day', 'pending_replace', 'replaced', 'suspended', 'stopped',
    'accepted_for_bidding', 'calculated', 'held'})
NAMESPACE = UUID('695c3204-e01c-5c12-8a87-b8058e154a04')


class PaperError(ValueError):
    """Fixed public code; provider text and secrets are never error messages."""


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 32503680000:
        raise PaperError('paper_timestamp_invalid')
    return float(value)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', value):
        raise PaperError('paper_identifier_invalid')
    return value


def sha256(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise PaperError('paper_sha256_invalid')
    return value


def amount(value, *, positive=False):
    if not isinstance(value, str):
        raise PaperError('paper_decimal_string_required')
    try:
        result = decimal(value, positive=positive, nonnegative=True)
        if result > Decimal('1e12'):
            raise ValueError
        return result
    except (ValueError, ArithmeticError):
        raise PaperError('paper_amount_invalid') from None


@financial_context
def decision(value):
    if type(value) is not dict or set(value) != DECISION_FIELDS:
        raise PaperError('paper_decision_schema_invalid')
    for name in ('signal_id', 'strategy_id'):
        identifier(value[name])
    for name in ('strategy_sha256', 'data_sha256'):
        sha256(value[name])
    if not isinstance(value['symbol'], str) or not re.fullmatch(r'[A-Z]{1,8}(?:\.[A-Z])?', value['symbol']):
        raise PaperError('paper_symbol_invalid')
    if value['side'] not in ('buy', 'sell'):
        raise PaperError('paper_long_only_side_required')
    qty, price = amount(value['quantity'], positive=True), amount(value['limit_price'], positive=True)
    if qty % Decimal('.000000001') or price % (Decimal('.01') if price >= 1 else Decimal('.0001')):
        raise PaperError('paper_order_precision_invalid')
    if qty * price > 100:
        raise PaperError('paper_absolute_order_ceiling')
    start, end = timestamp(value['signal_time']), timestamp(value['expires_at'])
    if not 0 < end - start <= 300:
        raise PaperError('paper_decision_ttl_invalid')
    return {**deepcopy(value), 'quantity': str(qty), 'limit_price': str(price),
            'signal_time': start, 'expires_at': end}


def client_order_id(binding, reviewed):
    # Terms are excluded so reuse of the same signal with altered sizing cannot
    # generate a second ID. The durable journal separately rejects altered terms.
    return str(uuid5(NAMESPACE, digest({'account': binding['account_fingerprint'],
        'mode': binding['mode'], 'strategy': reviewed['strategy_sha256'], 'signal': reviewed['signal_id']})))


def payload(reviewed, client_id):
    return {'client_order_id': client_id, 'symbol': reviewed['symbol'], 'side': reviewed['side'],
            'qty': reviewed['quantity'], 'limit_price': reviewed['limit_price'],
            'type': 'limit', 'time_in_force': 'day', 'extended_hours': False}


@financial_context
def validate_order(observation, intended, previous=None):
    """Retain lifecycle status and require monotone, limit-compliant fill economics.

    An unchanged cumulative quantity cannot silently revise its average price.
    For an increased quantity, the additional notional must itself obey the
    original limit; a favorable earlier fill cannot hide a later violation.
    """
    if not isinstance(observation, dict):
        raise PaperError('paper_order_response_invalid')
    try:
        broker_id = observation['id']
        if not isinstance(broker_id, str) or str(UUID(broker_id)) != broker_id:
            raise ValueError
        for key in ('client_order_id', 'symbol', 'side', 'type', 'time_in_force'):
            if observation[key] != intended[key]:
                raise ValueError
        if (observation.get('asset_class') != 'us_equity' or observation['extended_hours'] is not False
                or observation['status'] not in KNOWN_STATUSES
                or amount(observation['qty']) != amount(intended['qty'])
                or amount(observation['limit_price']) != amount(intended['limit_price'])):
            raise ValueError
        filled = amount(observation['filled_qty'])
        qty = amount(intended['qty'])
        average = observation['filled_avg_price']
        if filled > qty or (filled > 0) != (average is not None):
            raise ValueError
        status = observation['status']
        if status == 'filled' and filled != qty or status == 'partially_filled' and not 0 < filled < qty:
            raise ValueError
        if status == 'rejected' and filled != 0:
            raise ValueError
        if average is not None:
            avg, limit = amount(average, positive=True), amount(intended['limit_price'])
            if intended['side'] == 'buy' and avg > limit or intended['side'] == 'sell' and avg < limit:
                raise ValueError
        created, updated = timestamp(observation['created_at']), timestamp(observation['updated_at'])
        if updated < created:
            raise ValueError
        if previous:
            prior_filled = amount(previous['filled_qty'])
            if (previous['id'] != broker_id or filled < prior_filled
                    or updated < timestamp(previous['updated_at'])
                    or created != timestamp(previous['created_at'])):
                raise ValueError
            prior_average = previous['filled_avg_price']
            if (prior_filled > 0) != (prior_average is not None):
                raise ValueError
            prior_avg = amount(prior_average, positive=True) if prior_filled else Decimal(0)
            current_avg = amount(average, positive=True) if filled else Decimal(0)
            if filled == prior_filled:
                if current_avg != prior_avg:
                    raise ValueError
            else:
                incremental_notional = filled * current_avg - prior_filled * prior_avg
                limit_notional = (filled - prior_filled) * amount(intended['limit_price'], positive=True)
                if (incremental_notional <= 0
                        or intended['side'] == 'buy' and incremental_notional > limit_notional
                        or intended['side'] == 'sell' and incremental_notional < limit_notional):
                    raise ValueError
            if previous['status'] in TERMINAL and any(observation[k] != previous[k]
                    for k in ('status', 'filled_qty', 'filled_avg_price', 'replaces', 'replaced_by')):
                raise ValueError
            if prior_filled > 0 and status in ('new', 'accepted', 'pending_new'):
                raise ValueError
        return deepcopy(observation)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise PaperError('paper_order_identity_or_progress_mismatch') from None
