"""Bounded equity-paper gateway: reconcile, reserve, recheck, submit once.

Only the engine holds mutation permits. Strategy integrations submit a reviewed
Decision mapping to submit(); they receive no broker or journal object. TEST
transport is SIMULATION. Owned PAPER writes require explicit configuration.
"""
from copy import deepcopy
from decimal import Decimal
import math
import threading
import time

from atlas.domain import financial_context
from atlas.ledger import digest
from atlas.paper.broker import AlpacaPaperAPI, PaperBrokerError
from atlas.paper.journal import PaperJournal
from atlas.paper.models import (PaperError, TERMINAL, amount, client_order_id, decision,
                                identifier, payload, timestamp, validate_order)
from atlas.paper.risk import PaperRiskPolicy, evaluate

CLOSED_LOCAL = TERMINAL | {'not_submitted', 'http_rejected'}
UNRESOLVED_LOCAL = {'reserved', 'submitting', 'unknown', 'pending_cancel', 'replaced', 'pending_replace'}


class PaperEngine:
    def __init__(self, api, journal, policy, *, clock=time.time):
        if type(api) is not AlpacaPaperAPI or type(journal) is not PaperJournal or type(policy) is not PaperRiskPolicy:
            raise PaperError('paper_owned_components_required')
        mode = 'SIMULATION' if api.provenance == 'TEST' else 'PAPER'
        if journal.binding['mode'] != mode or journal.binding['policy_sha256'] != policy.fingerprint():
            raise PaperError('paper_mode_or_policy_binding_mismatch')
        self._api, self._journal, self._policy, self._clock = api, journal, policy, clock
        if getattr(journal, '_engine_bound', False):
            raise PaperError('paper_journal_engine_already_bound')
        journal._engine_bound = True
        self._lock, self._permits, self._fresh_claim = threading.RLock(), {}, None
        api.bind_gate(self._authorize)

    def _now(self):
        return timestamp(self._clock())

    def _kill(self):
        try:
            (self._journal.directory / 'STOP').lstat()
            return True
        except FileNotFoundError:
            return self._journal.get('kill_switch', False) is not False
        except OSError:
            return True

    def _require_initialized(self):
        if self._journal.get('initialized') is not True:
            raise PaperError('paper_session_not_initialized')

    @financial_context
    def initialize(self):
        """Bind a new, empty dedicated paper experiment; never reset a journal."""
        with self._lock:
            if self._journal.get('initialized'):
                return self.status()
            if self._journal.orders() or self._kill():
                raise PaperError('paper_initialization_not_clean')
            started = self._now()
            account = self._api.account()
            positions = self._api.positions()
            orders = self._api.orders(status='all')
            activities = self._api.activities()
            refreshed = self._api.account()
            now = self._now()
            if not (started <= timestamp(account['observed_at']) <= timestamp(refreshed['observed_at']) <= now
                    and now - started <= self._policy.max_snapshot_age_seconds):
                raise PaperError('paper_initialization_snapshot_stale')
            principal = amount(self._journal.binding['allocated_principal'], positive=True)
            if (positions or orders or activities or {k:v for k,v in account.items() if k != 'observed_at'} != {k:v for k,v in refreshed.items() if k != 'observed_at'}
                    or account['fingerprint'] != self._journal.binding['account_fingerprint']
                    or account['status'] != 'ACTIVE' or account['blocked'] is not False
                    or account['currency'] != 'USD' or amount(account['multiplier']) != 1
                    or amount(account['short_market_value']) != 0 or amount(account['long_market_value']) != 0
                    or not principal <= amount(account['buying_power']) <= amount(account['cash'])
                    or amount(account['cash']) != principal
                    or amount(account['cash']) != amount(account['equity'])):
                raise PaperError('paper_empty_dedicated_cash_account_required')
            now = self._now()
            with self._journal.transaction('session_initialized', {'account_fingerprint': account['fingerprint'],
                    'allocated_principal': str(principal), 'mode': self._journal.binding['mode'],
                    'provenance': self._api.provenance}, now=now):
                for key, value in {'initialized': True, 'started_at': now, 'day': int(now // 86400),
                        'day_start_equity': account['equity'], 'peak_equity': account['equity'],
                        'last_equity': account['equity'], 'last_account': refreshed, 'starting_cash': account['cash'],
                        'broker_failures': 0, 'mutation_failures': 0, 'cooldown_until': 0, 'kill_switch': False,
                        'entry_halt': False, 'reconciliation_ok': True, 'reconciliation_reasons': [],
                        'positions': [], 'open_orders': [], 'last_reconciled_at': now}.items():
                    self._journal.set(key, value)
            return self.status()

    def _failure(self, code, *, retry_after=None, entry_halt=False, mutation=False):
        now = self._now()
        deferred = code == 'paper_rate_limited'
        failures = self._journal.get('broker_failures', 0) + (0 if deferred else 1)
        invalid = code == 'paper_rate_limit_header_invalid'
        delay = 0 if retry_after is None else retry_after
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay < 32503679999 - now:
            invalid = True
        cooldown = (32503679999 - now if invalid else
                    max(float(delay), 1 if deferred else min(60, 2 ** min(failures, 6))))
        with self._journal.transaction('rate_deferral' if deferred else 'broker_or_reconciliation_failure',
                {'code': identifier(code), 'failure_count': failures, 'retry_after_seconds': cooldown}, now=now):
            self._journal.set('broker_failures', failures)
            if mutation and not deferred:
                self._journal.set('mutation_failures', self._journal.get('mutation_failures', 0) + 1)
            self._journal.set('cooldown_until', max(self._journal.get('cooldown_until', 0), now + cooldown))
            self._journal.set('reconciliation_ok', False)
            self._journal.set('reconciliation_reasons', [code])
            if invalid or entry_halt or failures >= self._policy.max_broker_failures:
                self._journal.set('entry_halt', True)

    @financial_context
    def _anchors(self, account, now):
        if int(now // 86400) != self._journal.get('day'):
            # Last observed pre-boundary equity is conservative across gaps; do
            # not reset the loss anchor to an already-depressed new-day value.
            self._journal.set('day', int(now // 86400))
            self._journal.set('day_start_equity', self._journal.get('last_equity'))
        peak = max(amount(self._journal.get('peak_equity')), amount(account['equity']))
        self._journal.set('peak_equity', str(peak))
        self._journal.set('last_equity', account['equity'])
        self._journal.set('last_account', account)
        loss = amount(self._journal.get('day_start_equity')) - amount(account['equity'])
        if loss >= self._policy.max_daily_loss or (peak - amount(account['equity'])) / peak >= self._policy.max_drawdown_fraction:
            self._journal.set('entry_halt', True)

    @financial_context
    def reconcile(self, *, _fresh=None):
        """Refresh broker truth and reconcile all owned orders, fills and inventory."""
        with self._lock:
            self._require_initialized()
            self._journal.integrity_check()
            if self._now() < self._journal.get('cooldown_until', 0):
                return {'reconciled': False, 'reasons': ['paper_broker_cooldown'], **self.status()}
            try:
                before = self._api.account()
                history = self._api.orders(status='all')
                opens = self._api.orders(status='open')
                positions = self._api.positions()
                activities = self._api.activities()
                account = self._api.account()
                now = self._now()
                if (account['fingerprint'] != self._journal.binding['account_fingerprint']
                        or before['fingerprint'] != account['fingerprint']):
                    raise PaperError('paper_reconciliation_account_mismatch')
                if _fresh is not None and _fresh != self._fresh_claim:
                    raise PaperError('paper_fresh_claim_authority_required')
                local = {row['client_order_id']: row for row in self._journal.orders()}
                remote = {row['client_order_id']: row for row in history}
                if len(remote) != len(history):
                    raise PaperError('paper_duplicate_broker_order_identity')
                reasons = []
                new_rejections = 0
                if any(cid not in local for cid in remote):
                    reasons.append('paper_unmanaged_broker_order')
                if before['cash'] != account['cash'] or before['buying_power'] != account['buying_power']:
                    reasons.append('paper_nonatomic_account_snapshot')
                for cid, row in local.items():
                    observation = remote.get(cid)
                    if cid == _fresh and row['state'] == 'reserved' and observation is None:
                        continue  # Only the original locked pre-send invocation may exclude its own fresh claim.
                    if observation is None and row['state'] not in CLOSED_LOCAL:
                        observation = self._api.order_by_client_id(cid)
                    if observation is not None:
                        observed = validate_order(observation, row['payload'], row.get('observation'))
                        if observed['created_at'] > now or observed['updated_at'] > now:
                            raise PaperError('paper_order_timestamp_future')
                        row['observation'] = observed
                        row['state'] = observed['status']
                        row['updated_at'] = now
                        if row['state'] == 'rejected' and not row.get('rejection_counted'):
                            row['rejection_counted'] = True
                            new_rejections += 1
                        if observed['replaces'] is not None or observed['replaced_by'] is not None or observed['status'] in ('replaced', 'pending_replace'):
                            reasons.append('paper_unmanaged_native_replacement')
                    elif row['state'] not in ('not_submitted', 'http_rejected'):
                        # A historical terminal row disappearing is also unsafe;
                        # absence never clears an uncertain submitted intent.
                        reasons.append('paper_order_missing_requires_reconciliation')
                    if row.get('cancel') and row['state'] not in TERMINAL:
                        reasons.append('paper_cancellation_not_terminal')
                    if row['state'] in UNRESOLVED_LOCAL:
                        reasons.append('paper_unresolved_order')
                    if row['state'] not in CLOSED_LOCAL and now - row['created_at'] > self._policy.max_intent_ttl_seconds:
                        reasons.append('paper_stale_open_order')
                ids = {r['observation']['id']: r for r in local.values() if r.get('observation')}
                fills, totals, notionals, expected_positions = {}, {}, {}, {}
                expected_cash = amount(self._journal.get('starting_cash'))
                for activity in activities:
                    if activity['type'] != 'FILL':
                        reasons.append('paper_nontrade_activity_requires_review')
                        continue
                    fill_id, broker_id = activity['id'], activity['order_id']
                    if fill_id in fills or broker_id not in ids:
                        raise PaperError('paper_unmanaged_or_duplicate_fill')
                    intended = ids[broker_id]['payload']
                    if activity['symbol'] != intended['symbol'] or activity['side'] != intended['side'] or activity['at'] > now:
                        raise PaperError('paper_fill_correlation_mismatch')
                    qty, price = amount(activity['qty'], positive=True), amount(activity['price'], positive=True)
                    if intended['side'] == 'buy' and price > amount(intended['limit_price']) or intended['side'] == 'sell' and price < amount(intended['limit_price']):
                        raise PaperError('paper_fill_limit_violation')
                    fills[fill_id] = activity
                    totals[broker_id] = totals.get(broker_id, Decimal(0)) + qty
                    notionals[broker_id] = notionals.get(broker_id, Decimal(0)) + qty * price
                    expected_cash += (-qty * price if intended['side'] == 'buy' else qty * price)
                    if activity.get('fee') is not None:
                        expected_cash -= amount(activity['fee'])
                for prior in self._journal.fills():
                    if prior['id'] not in fills or fills[prior['id']] != prior:
                        raise PaperError('paper_fill_history_changed_or_missing')
                for broker_id, row in ids.items():
                    obs = row['observation']
                    qty = amount(obs['filled_qty'])
                    if totals.get(broker_id, Decimal(0)) != qty:
                        reasons.append('paper_fill_activity_not_reconciled')
                    if qty > 0 and abs(notionals.get(broker_id, Decimal(0)) - qty * amount(obs['filled_avg_price'])) > Decimal('.01'):
                        reasons.append('paper_fill_notional_not_reconciled')
                    name = row['payload']['symbol']
                    expected_positions[name] = expected_positions.get(name, Decimal(0)) + (qty if row['payload']['side'] == 'buy' else -qty)
                if abs(expected_cash - amount(account['cash'])) > Decimal('.01'):
                    reasons.append('paper_cash_reconciliation_mismatch')
                actual_positions = {r['symbol']: amount(r['qty'], positive=True) for r in positions}
                expected_positions = {k: v for k, v in expected_positions.items() if v != 0}
                if len(actual_positions) != len(positions) or actual_positions != expected_positions:
                    reasons.append('paper_position_reconciliation_mismatch')
                expected_open = {cid for cid, row in local.items() if row.get('observation') and row['state'] not in TERMINAL}
                if {r['client_order_id'] for r in opens} != expected_open or len({r['client_order_id'] for r in opens}) != len(opens):
                    reasons.append('paper_open_order_reconciliation_mismatch')
                for open_row in opens:
                    if open_row['client_order_id'] in local:
                        validate_order(open_row, local[open_row['client_order_id']]['payload'],
                                       local[open_row['client_order_id']].get('observation'))
                if new_rejections:
                    reasons.append('paper_new_order_rejection')
                reasons = sorted(set(reasons))
                with self._journal.transaction('reconciliation', {'reconciled': not reasons, 'reasons': reasons,
                        'order_count': len(local), 'fill_count': len(fills), 'positions': positions,
                        'account': account, 'fees_complete': False}, now=self._now()):
                    for row in local.values():
                        self._journal.put_order(row)
                    for fill in fills.values():
                        self._journal.put_fill(fill)
                    if not reasons:
                        self._anchors(account, self._now())
                    self._journal.set('last_account', account)
                    self._journal.set('positions', positions)
                    self._journal.set('open_orders', opens)
                    self._journal.set('reconciliation_ok', not reasons)
                    self._journal.set('reconciliation_reasons', reasons)
                    self._journal.set('last_reconciled_at', self._now())
                    if new_rejections:
                        mutations = self._journal.get('mutation_failures', 0) + new_rejections
                        failures = max(mutations, self._journal.get('broker_failures', 0) + new_rejections)
                        self._journal.set('mutation_failures', mutations)
                        self._journal.set('broker_failures', failures)
                        self._journal.set('cooldown_until', now + min(60, 2 ** min(failures, 6)))
                        if failures >= self._policy.max_broker_failures:
                            self._journal.set('entry_halt', True)
                    if not reasons:
                        # Healthy reads do not prove that order submission works.
                        self._journal.set('broker_failures', self._journal.get('mutation_failures', 0))
                        self._journal.set('cooldown_until', 0)
                    if any(code in reasons for code in ('paper_unmanaged_broker_order', 'paper_unmanaged_native_replacement',
                                                        'paper_nontrade_activity_requires_review')):
                        self._journal.set('entry_halt', True)
                return {'reconciled': not reasons, 'reasons': reasons, **self.status()}
            except (PaperBrokerError, PaperError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
                code = exc.code if isinstance(exc, PaperBrokerError) else str(exc) if isinstance(exc, PaperError) else 'paper_reconciliation_invalid'
                self._failure(code, retry_after=getattr(exc, 'retry_after', None), entry_halt=isinstance(exc, PaperError))
                return {'reconciled': False, 'reasons': [code], **self.status()}

    @financial_context
    def _risk_state(self, *, exclude=None):
        now, orders = self._now(), self._journal.orders()
        reservations = []
        unresolved = 0
        for row in orders:
            if row['client_order_id'] == exclude:
                continue
            if row['state'] in UNRESOLVED_LOCAL or row.get('cancel') and row['state'] not in TERMINAL:
                unresolved += 1
            if row['state'] in CLOSED_LOCAL:
                continue
            filled = amount((row.get('observation') or {}).get('filled_qty', '0'))
            remaining = amount(row['payload']['qty']) - filled
            if remaining > 0:
                cost = remaining * amount(row['payload']['limit_price']) * (1 + self._policy.fee_buffer_bps / 10000)
                reservations.append({'client_order_id': row['client_order_id'], 'symbol': row['payload']['symbol'],
                    'side': row['payload']['side'], 'remaining_qty': str(remaining),
                    'remaining_cash': str(cost) if row['payload']['side'] == 'buy' else '0'})
        attempts = sorted(r['created_at'] for r in orders if r['client_order_id'] != exclude)
        return {'account_fingerprint': self._journal.binding['account_fingerprint'],
            'strategy_sha256': self._journal.binding['strategy_sha256'],
            'allocated_principal': self._journal.binding['allocated_principal'],
            'day_start_equity': self._journal.get('day_start_equity'), 'peak_equity': self._journal.get('peak_equity'),
            'daily_attempts': sum(int(at // 86400) == int(now // 86400) for at in attempts),
            'recent_attempt_times': [at for at in attempts if now - at < 60], 'reservations': reservations,
            'kill_switch': self._kill(), 'reconciliation_ok': self._journal.get('reconciliation_ok', False),
            'broker_failures': self._journal.get('broker_failures', 0),
            'cooldown_until': self._journal.get('cooldown_until', 0), 'unresolved_orders': unresolved,
            'entry_halt': self._journal.get('entry_halt', False)}

    def _snapshot(self, reviewed, *, fresh=None):
        result = self.reconcile(_fresh=fresh)
        if not result['reconciled']:
            raise PaperError('paper_reconciliation_required')
        clock = self._api.clock_status()
        asset = self._api.asset(reviewed['symbol'])
        quote = self._api.quote(reviewed['symbol'])
        return {'account': self._journal.get('last_account'), 'positions': self._journal.get('positions'),
                'open_orders': self._journal.get('open_orders'), 'clock': clock, 'asset': asset, 'quote': quote,
                'observed_at': self._now()}

    def _authorize(self, method, path, body, permit):
        grant = self._permits.pop(permit, None)
        if grant is None or (method, path, digest(body)) != (grant['method'], grant['path'], grant['body_hash']):
            return False
        now = self._now()
        row = self._journal.order(grant['client_id'])
        if row is None or not grant['issued_at'] <= now < grant['expires_at']:
            return False
        if method == 'POST':
            if (self._kill() or row['state'] != 'submitting' or self._fresh_claim != row['client_order_id']
                    or body != row['payload'] or self._journal.get('reconciliation_ok') is not True):
                return False
        elif method == 'DELETE':
            if not row.get('cancel') or row['cancel']['status'] != 'requested' or row['state'] in TERMINAL:
                return False
            if path != '/v2/orders/' + row['observation']['id']:
                return False
        else:
            return False
        with self._journal.transaction('broker_request_authorized', {'method': method, 'path': path,
                'payload_sha256': digest(body), 'client_order_id': row['client_order_id'],
                'provenance': self._api.provenance}, now=now):
            if method == 'POST':
                row['wire_authorized'] = True
            else:
                row['cancel']['wire_authorized'] = True
            self._journal.put_order(row)
        return (self._now() < grant['expires_at']
                and (method == 'DELETE' or not self._kill()))

    def _permit(self, client_id, method, body, *, expires_at, path='/v2/orders'):
        token = object()
        self._permits[token] = {'client_id': client_id, 'method': method, 'path': path,
            'body_hash': digest(body), 'issued_at': self._now(), 'expires_at': expires_at}
        return token

    @financial_context
    def submit(self, reviewed, *, _parent=None):
        """Consume one immutable signal once; duplicate calls only reconcile it."""
        with self._lock:
            self._require_initialized()
            reviewed = decision(reviewed)
            existing = self._journal.by_signal(reviewed['signal_id'])
            if existing is not None:
                if existing['decision_sha256'] != digest(reviewed):
                    raise PaperError('paper_duplicate_signal_terms_changed')
                self.reconcile()
                return self._order_result(existing['client_order_id'], replayed=True)
            if not self._api.execution_enabled:
                raise PaperError('paper_network_activation_pending_review')
            if self._kill():
                raise PaperError('paper_kill_switch_active')
            try:
                snapshot = self._snapshot(reviewed)
                risk = evaluate(self._policy, reviewed, snapshot, self._risk_state(), now=self._now())
            except PaperBrokerError as exc:
                self._failure(exc.code, retry_after=exc.retry_after)
                return {'new_submission': False, 'risk': {'passed': False, 'reasons': [exc.code]}, **self.status()}
            except PaperError as exc:
                self._journal.event('decision_blocked', {'decision': reviewed, 'code': str(exc)}, now=self._now())
                return {'new_submission': False, 'risk': {'passed': False, 'reasons': [str(exc)]}, **self.status()}
            self._journal.event('strategy_and_risk_decision', {'decision': reviewed, 'risk': risk,
                                'snapshot': snapshot}, now=self._now())
            if not risk['passed']:
                return {'new_submission': False, 'risk': risk, **self.status()}
            cid = client_order_id(self._journal.binding, reviewed)
            row = {'client_order_id': cid, 'decision': reviewed, 'decision_sha256': digest(reviewed),
                'payload': payload(reviewed, cid), 'state': 'reserved', 'observation': None,
                'created_at': self._now(), 'updated_at': self._now(), 'reservation_cash': risk['reservation_cash'],
                'reservation_qty': risk['reservation_qty'], 'risk_at_reservation': risk,
                'cancel': None, 'replacement': None, 'replacement_parent': _parent, 'wire_authorized': False}
            with self._journal.transaction('intent_and_reservation_committed', {'client_order_id': cid,
                    'decision_sha256': row['decision_sha256'], 'reservation_cash': row['reservation_cash'],
                    'reservation_qty': row['reservation_qty'], 'replacement_parent': _parent}, now=self._now()):
                self._journal.put_order(row, new=True)
            self._fresh_claim = cid
            try:
                try:
                    snapshot = self._snapshot(reviewed, fresh=cid)
                    fresh_risk = evaluate(self._policy, reviewed, snapshot, self._risk_state(exclude=cid), now=self._now())
                    current = self._journal.order(cid)
                    if current['state'] != 'reserved':
                        return self._order_result(cid, replayed=True)
                    if not fresh_risk['passed'] or int(self._now() // 86400) != int(row['created_at'] // 86400):
                        raise PaperError('paper_fresh_risk_rejected')
                except (PaperBrokerError, PaperError) as exc:
                    with self._journal.transaction('pre_submission_rejected', {'client_order_id': cid,
                            'code': exc.code if isinstance(exc, PaperBrokerError) else str(exc)}, now=self._now()):
                        row = self._journal.order(cid)
                        row['state'], row['updated_at'] = 'not_submitted', self._now()
                        self._journal.put_order(row)
                    if isinstance(exc, PaperBrokerError):
                        self._failure(exc.code, retry_after=exc.retry_after)
                    return self._order_result(cid)
                with self._journal.transaction('submission_armed', {'client_order_id': cid, 'risk': fresh_risk,
                        'snapshot': snapshot}, now=self._now()):
                    row = self._journal.order(cid)
                    row['state'], row['updated_at'] = 'submitting', self._now()
                    self._journal.put_order(row)
                permit = self._permit(cid, 'POST', row['payload'], expires_at=min(
                    reviewed['expires_at'], reviewed['signal_time'] + self._policy.max_signal_age_seconds,
                    snapshot['account']['observed_at'] + self._policy.max_snapshot_age_seconds,
                    snapshot['clock']['at'] + self._policy.max_snapshot_age_seconds,
                    snapshot['clock']['next_close'],
                    snapshot['quote']['at'] + self._policy.max_quote_age_seconds))
                try:
                    observation = self._api.submit_order(row['payload'], permit=permit)
                    observation = validate_order(observation, row['payload'])
                    if observation['created_at'] > self._now() or observation['updated_at'] > self._now():
                        raise PaperError('paper_order_timestamp_future')
                    state = observation['status']
                    failure = PaperBrokerError('paper_order_rejected') if state == 'rejected' else None
                except PaperBrokerError as exc:
                    observation, state, failure = None, 'unknown' if exc.ambiguous else 'http_rejected', exc
                except Exception:
                    observation, state = None, 'unknown'
                    failure = PaperBrokerError('paper_submission_outcome_unknown', ambiguous=True)
                finally:
                    self._permits.pop(permit, None)
                with self._journal.transaction('broker_submission_observed', {'client_order_id': cid, 'state': state,
                        'observation': observation, 'code': failure.code if failure else None}, now=self._now()):
                    row = self._journal.order(cid)
                    row['state'], row['observation'], row['updated_at'] = state, observation, self._now()
                    if state == 'rejected':
                        row['rejection_counted'] = True
                    self._journal.put_order(row)
                    self._journal.set('reconciliation_ok', False)
                if failure:
                    self._failure(failure.code, retry_after=failure.retry_after, mutation=True)
                return self._order_result(cid, new_submission=row['wire_authorized'])
            finally:
                self._fresh_claim = None

    def cancel(self, client_id):
        """One durable exact-order cancellation; only later status proves closure."""
        with self._lock:
            self._require_initialized()
            row = self._journal.order(client_id)
            if row is None:
                raise PaperError('paper_owned_order_required')
            self.reconcile()
            row = self._journal.order(client_id)
            if row['state'] in CLOSED_LOCAL or row.get('cancel'):
                return self._order_result(client_id, replayed=True)
            if not self._api.execution_enabled:
                raise PaperError('paper_network_activation_pending_review')
            if self._now() < self._journal.get('cooldown_until', 0):
                raise PaperError('paper_broker_cooldown')
            if row.get('observation') is None:
                raise PaperError('paper_cancel_identity_unresolved')
            path = '/v2/orders/' + row['observation']['id']
            with self._journal.transaction('cancellation_committed', {'client_order_id': client_id,
                    'broker_order_id': row['observation']['id']}, now=self._now()):
                row['cancel'] = {'status': 'requested', 'requested_at': self._now()}
                self._journal.put_order(row)
            permit = self._permit(client_id, 'DELETE', None, path=path, expires_at=self._now() + 10)
            try:
                self._api.cancel_order(row['observation']['id'], permit=permit)
                outcome, failure = 'acknowledged', None
            except PaperBrokerError as exc:
                outcome, failure = 'unknown' if exc.ambiguous else 'http_rejected', exc
            except Exception:
                outcome, failure = 'unknown', PaperBrokerError('paper_cancellation_outcome_unknown', ambiguous=True)
            finally:
                self._permits.pop(permit, None)
            with self._journal.transaction('cancellation_observed', {'client_order_id': client_id,
                    'outcome': outcome, 'code': failure.code if failure else None}, now=self._now()):
                row = self._journal.order(client_id)
                row['cancel']['status'] = outcome
                self._journal.put_order(row)
                self._journal.set('reconciliation_ok', False)
            if failure:
                self._failure(failure.code, retry_after=failure.retry_after, mutation=True)
            return {**self._order_result(client_id), 'cancellation_requested': True}

    @financial_context
    def replace(self, client_id, replacement):
        """Cancel, reconcile terminal fills, then risk-check the remaining target.

        replacement.quantity is the desired TOTAL quantity across old and new
        legs, at most the old quantity. The new signal ID and terms are immutable.
        Native PATCH is intentionally unavailable; no two legs can be live here.
        """
        with self._lock:
            replacement = decision(replacement)
            row = self._journal.order(client_id)
            if row is None or row.get('observation') is None:
                raise PaperError('paper_replace_owned_acknowledged_order_required')
            original = row['decision']
            if (any(replacement[k] != original[k] for k in ('symbol', 'side', 'strategy_id', 'strategy_sha256'))
                    or replacement['signal_id'] == original['signal_id']
                    or amount(replacement['quantity']) > amount(original['quantity'])):
                raise PaperError('paper_replacement_cannot_expand_or_change_exposure')
            if row['replacement'] is not None and row['replacement'] != replacement:
                raise PaperError('paper_replacement_terms_changed')
            if row['replacement'] is None:
                with self._journal.transaction('replacement_plan_committed', {'parent_client_order_id': client_id,
                        'replacement': replacement}, now=self._now()):
                    row['replacement'] = replacement
                    self._journal.put_order(row)
            self.cancel(client_id)
            result = self.reconcile()
            row = self._journal.order(client_id)
            if row['state'] not in TERMINAL or not result['reconciled']:
                return {'replacement_stage': 'awaiting_terminal_reconciliation', 'new_submission': False,
                        'parent': self._order_result(client_id)}
            filled = amount(row['observation']['filled_qty'])
            remaining = max(Decimal(0), amount(replacement['quantity']) - filled)
            if remaining == 0:
                return {'replacement_stage': 'target_already_filled', 'new_submission': False,
                        'filled_quantity': str(filled)}
            child = {**replacement, 'quantity': str(remaining)}
            result = self.submit(child, _parent=client_id)
            return {'replacement_stage': 'remaining_quantity_reviewed', 'parent_client_order_id': client_id,
                    'remaining_quantity': str(remaining), **result}

    def halt(self):
        with self._lock:
            with self._journal.transaction('kill_switch_enabled', {'new_orders_blocked': True}, now=self._now()):
                self._journal.set('kill_switch', True)
            return self.status()

    def _order_result(self, client_id, *, replayed=False, new_submission=False):
        row = self._journal.order(client_id)
        return {'order': row, 'replayed': replayed, 'new_submission': new_submission,
                **self._submission_counts(self._journal.orders()), 'provenance': self._api.provenance,
                'live_enabled': False, 'qualification_established': False}

    def _submission_counts(self, orders):
        # Authorization precedes credential loading and I/O. It proves neither
        # delivery nor acceptance; a timeout can hide an accepted broker order.
        authorized = sum(bool(row.get('wire_authorized')) for row in orders)
        observed = sum(row.get('observation') is not None for row in orders)
        return {'post_authorizations': authorized, 'broker_orders_observed': observed,
                'actual_orders_submitted': 0 if self._api.provenance == 'TEST' or authorized == 0 else None}

    def status(self):
        with self._lock:
            self._journal.integrity_check()
            orders = self._journal.orders()
            return {'mode': self._journal.binding['mode'], 'provenance': self._api.provenance,
                'initialized': self._journal.get('initialized', False), 'live_enabled': False,
                'network_paper_activation_enabled': self._api.provenance == 'PAPER_API' and self._api.execution_enabled, 'qualification_established': False,
                'account_fingerprint': self._journal.binding['account_fingerprint'],
                'allocated_principal': self._journal.binding['allocated_principal'],
                'kill_switch': self._kill(), 'entry_halt': self._journal.get('entry_halt', False),
                'reconciliation_ok': self._journal.get('reconciliation_ok', False),
                'reconciliation_reasons': self._journal.get('reconciliation_reasons', []),
                'broker_failures': self._journal.get('broker_failures', 0),
                'mutation_failures': self._journal.get('mutation_failures', 0),
                'cooldown_until': self._journal.get('cooldown_until', 0),
                'orders': [{'client_order_id': r['client_order_id'], 'signal_id': r['decision']['signal_id'],
                            'state': r['state'], 'filled_qty': (r.get('observation') or {}).get('filled_qty', '0')}
                           for r in orders], 'positions': self._journal.get('positions', []),
                'fill_count': len(self._journal.fills()), 'fees_complete': False,
                **self._submission_counts(orders)}
