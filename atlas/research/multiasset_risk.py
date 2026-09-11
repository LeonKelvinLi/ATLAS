"""Shared $100 research portfolio; simulation only, with no network methods.

ATR budgets include modeled round-trip costs. They are planning limits, not
loss guarantees: gaps, stale prices and unavailable exits can exceed a stop.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal as D
import json
import math
import os
from pathlib import Path
import statistics

from atlas.domain import decimal, financial_context, json_value
from atlas.research.replay import ExecutionModel, ReplayEngine, OPEN, _hash


BUCKETS = {"SPY": "us_equity_beta", "QQQ": "us_equity_beta", "ES": "us_equity_beta",
           "NQ": "us_equity_beta", "IWM": "us_equity_beta", "DIA": "us_equity_beta",
           "BTC-USD": "crypto", "ETH-USD": "crypto", "GLD": "metals", "SLV": "metals",
           "USO": "energy", "BNO": "energy"}


@dataclass(frozen=True)
class PortfolioRisk:
    risk_fraction: D = D('.005')
    max_total_risk: D = D('.02')
    max_cluster_risk: D = D('.01')
    max_cluster_weight: D = D('.35')
    max_position_weight: D = D('.35')
    max_drawdown: D = D('.10')
    max_daily_loss: D = D('.02')
    correlation_threshold: float = .8
    correlation_window: int = 60
    correlation_min_pairs: int = 20
    unknown_correlations_linked: bool = True

    def __post_init__(self):
        for key in ('risk_fraction', 'max_total_risk', 'max_cluster_risk', 'max_cluster_weight',
                    'max_position_weight', 'max_drawdown', 'max_daily_loss'):
            value = decimal(getattr(self, key), positive=True)
            if value > 1:
                raise ValueError('risk settings must be fractions no greater than one')
            object.__setattr__(self, key, value)
        if self.risk_fraction not in (D('.0025'), D('.005'), D('.01')):
            raise ValueError('predeclared risk experiments are 0.25%, 0.5%, 1%')
        if not 0 < self.correlation_threshold <= 1 or not math.isfinite(self.correlation_threshold):
            raise ValueError('invalid correlation threshold')
        if (type(self.correlation_window) is not int or type(self.correlation_min_pairs) is not int
                or not 3 <= self.correlation_min_pairs <= self.correlation_window <= 1000):
            raise ValueError('invalid correlation observation limits')
        if type(self.unknown_correlations_linked) is not bool:
            raise ValueError('unknown correlation policy must be explicit')

    def public(self):
        return json_value(asdict(self))


def venue_models(bars, *, cost_multiplier=1, latency_seconds=1):
    """Declared scenarios, not broker fee quotations or promises of fills."""
    factor = decimal(cost_multiplier, positive=True)
    result = {}
    for bar in bars:
        symbol = bar['symbol']
        crypto = bar['asset_class'] == 'crypto'
        result[symbol] = ExecutionModel(
            name='coinbase_spot_cost_scenario' if crypto else 'alpaca_equity_cost_scenario',
            fee_bps=(D('120') if crypto else D('1')) * factor,
            spread_bps=(D('10') if crypto else D('5')) * factor,
            slippage_bps=D('5') * factor, latency_seconds=latency_seconds,
            max_quote_age_seconds=bar['interval_seconds'] + 5,
            participation_rate=D('.01'), quantity_increment=D('.000001'),
            minimum_quantity=D('.000001'), max_position_weight=D('.35'))
    return result


class MultiassetReplay(ReplayEngine):
    """Venue-specific cost models, shared reservations, causal correlation and stops.

    Persistent extension state resides inside the base engine's transactional
    checkpoint. All profiles/rules/outages are bound to its content identity.
    """

    def __init__(self, bars, decisions, *, profiles=None, risk=None, outages=None,
                 evidence_class='SYNTHETIC', state_path=None, create_state=False):
        bars = list(bars)
        self.profiles = profiles or venue_models(bars)
        self.risk = risk or PortfolioRisk()
        if not isinstance(self.risk, PortfolioRisk) or set(self.profiles) != {b['symbol'] for b in bars}:
            raise ValueError('explicit model for each market symbol required')
        if any(not isinstance(p, ExecutionModel) for p in self.profiles.values()):
            raise ValueError('invalid venue profile')
        self.outages = {}
        for symbol, windows in (outages or {}).items():
            if symbol != '*' and symbol not in self.profiles:
                raise ValueError('outage symbol not in dataset')
            self.outages[symbol] = []
            for start, end in windows:
                if type(start) is not int or type(end) is not int or not 0 <= start < end:
                    raise ValueError('invalid outage window')
                self.outages[symbol].append([start, end])
        context = {'version': 'shared-risk-v1', 'risk': self.risk.public(),
                   'profiles': {k: v.public() for k, v in sorted(self.profiles.items())},
                   'outages': self.outages, 'live_approved': False}
        if state_path is not None:
            path = Path(state_path).expanduser()
            if not path.is_absolute() or path.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
                raise ValueError('external absolute research state required')
            marker = path.with_suffix(path.suffix + '.created')
            if create_state:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if path.exists():
                    raise ValueError('state already exists; resume explicitly')
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'w') as handle:
                    handle.write('Research state identity reserved; never reseed if database is lost.\n')
                    handle.flush()
                    os.fsync(handle.fileno())
            elif not path.is_file() or not marker.is_file():
                raise ValueError('missing research checkpoint or creation marker; explicit recovery required')
        super().__init__(bars, decisions, model=next(iter(self.profiles.values())), initial_cash='100',
                         evidence_class=evidence_class, state_path=state_path, context_metadata=context,
                         require_existing_state=state_path is not None and not create_state)

    @contextmanager
    def _venue(self, symbol):
        before = self.model
        self.model = self.profiles[symbol]
        try:
            yield
        finally:
            self.model = before

    def _prices(self, bar):
        with self._venue(bar['symbol']):
            return super()._prices(bar)

    def _stale(self, now):
        return sorted({p['symbol'] for p in self.state['positions'].values()
                       if now - self.state['quotes'][p['symbol']]['event_time'] > self.profiles[p['symbol']].max_quote_age_seconds})

    def _reserved_cash(self, exclude=None):
        return sum((D(o['remaining_quantity']) * D(o['reservation_price']) *
                    (1 + self.profiles[o['symbol']].fee_bps / 10000)
                    for key, o in self.state['orders'].items() if key != exclude and
                    o['status'] in OPEN and o['side'] == 'buy'), D(0))

    def _risk_state(self):
        return self.state.setdefault('multiasset_risk', {'drawdown_halt': False, 'daily_halts': [],
             'day': None, 'day_start_equity': '100', 'last_observed': None, 'history': {}, 'matrix': {}})

    def _down(self, symbol, now):
        return any(start <= now < end for key in ('*', symbol) for start, end in self.outages.get(key, []))

    def _observe(self, now):
        state = self._risk_state()
        if state['last_observed'] == now:
            return
        state['last_observed'] = now
        for symbol, bar in self.state['quotes'].items():
            history = state['history'].setdefault(symbol, [])
            if not history or bar['event_time'] > history[-1][0]:
                history.append([bar['event_time'], bar['close']])
                del history[:-self.risk.correlation_window - 1]
        returns = {symbol: {(a[0], b[0]): float(D(b[1]) / D(a[1]) - 1)
                            for a, b in zip(rows, rows[1:])}
                   for symbol, rows in state['history'].items()}
        matrix = {}
        for a in sorted(self.profiles):
            matrix[a] = {}
            for b in sorted(self.profiles):
                keys = sorted(set(returns.get(a, {})) & set(returns.get(b, {})))
                value = 1.0 if a == b else None
                if a != b and len(keys) >= self.risk.correlation_min_pairs:
                    x, y = [returns[a][k] for k in keys], [returns[b][k] for k in keys]
                    if statistics.pstdev(x) and statistics.pstdev(y):
                        value = statistics.correlation(x, y)
                matrix[a][b] = {'correlation': value, 'paired_observations': len(keys), 'as_of': now}
        state['matrix'] = matrix
        self._limits(now)

    def _limits(self, now):
        state, equity = self._risk_state(), self._equity()
        day = str(now // 86400)
        if state['day'] != day:
            state['day'] = day
            # Include an overnight gap relative to the last previous mark.
            state['day_start_equity'] = self.state['equity_curve'][-1]['equity'] if self.state['equity_curve'] else str(self.initial_cash)
        peak = max(D(self.state['peak_equity']), equity)
        if peak > 0 and 1 - equity / peak >= self.risk.max_drawdown and not state['drawdown_halt']:
            state['drawdown_halt'] = True
            self._event('risk_halt', now, reason='portfolio_drawdown')
        baseline = D(state['day_start_equity'])
        if baseline > 0 and 1 - equity / baseline >= self.risk.max_daily_loss and day not in state['daily_halts']:
            state['daily_halts'].append(day)
            self._event('risk_halt', now, reason='daily_marked_loss', utc_day=day)

    def _cluster(self, symbol):
        matrix = self._risk_state()['matrix']
        found = {symbol}
        while True:
            expanded = set(found)
            for a in found:
                for b in self.profiles:
                    corr = matrix.get(a, {}).get(b, {}).get('correlation')
                    same_bucket = BUCKETS.get(a, a) == BUCKETS.get(b, b)
                    if same_bucket or (corr is None and self.risk.unknown_correlations_linked) or (corr is not None and corr >= self.risk.correlation_threshold):
                        expanded.add(b)
            if expanded == found:
                return found
            found = expanded

    def _unit_risk(self, order, price):
        stop = decimal(order.get('metadata', {}).get('stop_distance'), positive=True)
        model = self.profiles[order['symbol']]
        cost = price * (2 * model.fee_bps + model.spread_bps + 2 * model.slippage_bps) / 10000
        return stop + cost

    def _exposure(self, symbols=None, exclude=None):
        risk, value = D(0), D(0)
        for pos in self.state['positions'].values():
            if symbols is not None and pos['symbol'] not in symbols:
                continue
            qty = D(pos['quantity'])
            midpoint = self._prices(self.state['quotes'][pos['symbol']])[2]
            # Never release risk just because a trailing mark moved favorably.
            risk += qty * D(pos.get('risk_per_unit', str(midpoint)))
            value += qty * midpoint
        for key, order in self.state['orders'].items():
            if key == exclude or order['status'] not in OPEN or order['side'] != 'buy' or (symbols is not None and order['symbol'] not in symbols):
                continue
            qty, price = D(order['remaining_quantity']), D(order['reservation_price'])
            risk += qty * self._unit_risk(order, price)
            value += qty * price
        return risk, value

    def _buy_veto(self, order, now):
        self._limits(now)
        state = self._risk_state()
        if state['drawdown_halt']:
            return 'portfolio_drawdown_halt'
        if str(now // 86400) in state['daily_halts']:
            return 'daily_marked_loss_halt'
        if self._down(order['symbol'], now):
            return 'venue_downtime'
        if order.get('metadata', {}).get('regime') in {'crash', 'high_volatility', 'high_vol', 'unavailable'}:
            return 'regime_entry_disabled'
        if not order.get('metadata', {}).get('stop_distance'):
            return 'missing_atr_stop'
        equity = self._equity()
        quote = self.state['quotes'][order['symbol']]
        price = self._prices(quote)[1] * (1 + self.model.slippage_bps / 10000)
        qty = D(order['remaining_quantity'])
        unit = self._unit_risk(order, price)
        # Recheck the full remaining order at the new market; no silent risk upsizing.
        existing_risk, _ = self._exposure(exclude=order['signal_id'])
        cluster_risk, cluster_value = self._exposure(self._cluster(order['symbol']), order['signal_id'])
        if existing_risk + qty * unit > equity * self.risk.max_total_risk:
            return 'portfolio_risk_cap'
        if cluster_risk + qty * unit > equity * self.risk.max_cluster_risk:
            return 'cluster_risk_cap'
        if cluster_value + qty * price > equity * self.risk.max_cluster_weight:
            return 'cluster_exposure_cap'
        sleeve = self._position(order)
        if D(sleeve['quantity']) * D(sleeve.get('risk_per_unit', '0')) + qty * unit > D(order['metadata']['risk_budget_usd']):
            return 'trade_risk_budget'
        return super()._buy_veto(order, now)

    def _reject_signal(self, decision, now, reason):
        order = {**decision, 'order_id': _hash(decision), 'status': 'rejected', 'side': 'buy',
                 'quantity': '0', 'remaining_quantity': '0', 'filled_quantity': '0', 'fees': '0',
                 'reservation_price': '0', 'average_fill_price': None, 'reason': reason}
        self.state['orders'][order['signal_id']] = order
        self._event('rejected', now, signal_id=order['signal_id'], reason=reason)

    @financial_context
    def _decision(self, decision, now):
        if decision['action'] == 'cancel':
            return super()._decision(decision, now)
        with self._venue(decision['symbol']):
            self._observe(now)
            position = self._position(decision)
            want_long = D(decision.get('target_weight', '0')) > 0
            if 'target_weight' not in decision:
                return self._reject_signal(decision, now, 'direction_signals_required')
            if want_long and position.get('stop_triggered'):
                self._event('protective_exit_pending', now, signal_id=decision['signal_id'])
                return
            for order in list(self.state['orders'].values()):
                if order['status'] in OPEN and self._sleeve(order) == self._sleeve(decision) and (order['side'] == 'buy') != want_long:
                    self._cancel(order, now, 'direction_changed')
            if want_long and D(position['quantity']) > 0:
                self._event('hold_existing_sleeve', now, signal_id=decision['signal_id'])
                return
            if not want_long:
                if D(position['quantity']) == 0:
                    self._event('flat_sleeve_noop', now, signal_id=decision['signal_id'])
                    return
                return super()._decision(decision, now)
            quote = self.state['quotes'].get(decision['symbol'])
            if quote is None:
                return self._reject_signal(decision, now, 'missing_market_data')
            metadata = dict(decision.get('metadata', {}))
            try:
                price = self._prices(quote)[1] * (1 + self.model.slippage_bps / 10000)
                unit = self._unit_risk(decision, price)
            except (ValueError, TypeError):
                return self._reject_signal(decision, now, 'invalid_atr_stop')
            equity = self._equity()
            budget = equity * self.risk.risk_fraction
            total, _ = self._exposure()
            cluster_risk, cluster_value = self._exposure(self._cluster(decision['symbol']))
            symbol_value = sum(D(p['quantity']) * price for p in self.state['positions'].values() if p['symbol'] == decision['symbol'])
            qty = self._floor(max(D(0), min(
                budget / unit, (equity * self.risk.max_total_risk - total) / unit,
                (equity * self.risk.max_cluster_risk - cluster_risk) / unit,
                (equity * self.risk.max_cluster_weight - cluster_value) / price,
                (equity * min(self.risk.max_position_weight, self.model.max_position_weight) - symbol_value) / price,
                (D(self.state['cash']) - self._reserved_cash()) / (price * (1 + self.model.fee_bps / 10000)))))
            if qty < self.model.minimum_quantity:
                return self._reject_signal(decision, now, 'no_shared_risk_headroom')
            metadata.update(risk_budget_usd=str(budget), risk_fraction=str(self.risk.risk_fraction),
                            risk_per_unit_at_decision=str(unit), correlation_as_of=now)
            sized = {k: v for k, v in decision.items() if k != 'target_weight'}
            sized.update(side='buy', quantity=str(qty), metadata=metadata)
            super()._decision(sized, now)

    def _stop_orders(self, bar, now):
        for sleeve, pos in list(self.state['positions'].items()):
            if pos['symbol'] != bar['symbol'] or pos.get('stop_price') is None:
                continue
            if not pos.get('stop_triggered') and D(bar['low']) > D(pos['stop_price']):
                continue
            pos['stop_triggered'] = True
            for order in list(self.state['orders'].values()):
                if order['status'] in OPEN and self._sleeve(order) == sleeve:
                    if order['side'] == 'sell':
                        break
                    self._cancel(order, now, 'atr_stop_triggered')
            else:
                decision = {'action': 'order', 'signal_id': 'atr-stop/' + _hash([sleeve, now])[:32],
                            'strategy_id': pos['strategy_id'], 'strategy_version': pos['strategy_version'],
                            'symbol': pos['symbol'], 'decision_time': now,
                            'expires_at': now + max(86400 * 7, bar['interval_seconds'] * 3),
                            'target_weight': '0', 'order_type': 'market',
                            'metadata': {'reason': 'observed_bar_low_crossed_atr_stop', 'stop_price': pos['stop_price']}}
                super()._decision(decision, now)

    @financial_context
    def _match(self, bar, now):
        with self._venue(bar['symbol']):
            self._observe(now)
            self._stop_orders(bar, now)
            if self._down(bar['symbol'], now):
                self._event('venue_downtime_no_fill', now, symbol=bar['symbol'])
                return
            super()._match(bar, now)

    def _fill(self, order, qty, price, executable, midpoint, now, bar):
        previous = self._position(order)
        super()._fill(order, qty, price, executable, midpoint, now, bar)
        if order['side'] == 'buy':
            pos = self.state['positions'][self._sleeve(order)]
            stop = D(order['metadata']['stop_distance'])
            pos.update(stop_price=str(max(D(previous.get('stop_price', '0')), max(D(0), price - stop))),
                       risk_per_unit=str(max(D(previous.get('risk_per_unit', '0')), self._unit_risk(order, price))),
                       stop_distance=str(stop))
        self._limits(now)

    def result(self):
        result = super().result()
        result.update(initial_cash='100', risk_policy=self.risk.public(),
                      venue_profiles={k: v.public() for k, v in self.profiles.items()},
                      risk_state=json.loads(json.dumps(self.state.get('multiasset_risk', {}))), live_approved=False)
        result['limitations'] += [
            'ATR stop observed only at bar publication; exit uses a subsequent available bar and can gap beyond risk budget.',
            'Entry budgets include modeled round-trip fees/spread/slippage; they do not bound tail losses.',
            'Unknown pair correlations are linked conservatively by default; shared static equity beta bucket is always enforced.',
            'Stale held equity marks conservatively block mixed-venue fills during market closures.',
            'Venue fees are explicit dated research scenarios; no broker order is submitted.',
            'Drawdown halts new exposure persistently; daily marked loss halts entries until the next UTC day.']
        return result
