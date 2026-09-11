from dataclasses import replace
from decimal import Decimal as D
import json

import pytest

from atlas.research.multiasset_risk import MultiassetReplay, PortfolioRisk, venue_models
from atlas.research.replay import ExecutionModel


def bar(t, price='100', symbol='SPY', **extra):
    return dict(symbol=symbol, asset_class='crypto' if symbol == 'BTC-USD' else 'equity',
                venue='TEST', currency='USD', event_time=t, interval_seconds=60,
                information_available_time=t, open=price, high=price, low=price,
                close=price, volume='10000', volume_unit='base_asset' if symbol == 'BTC-USD' else 'shares',
                revision=0, **extra)


def signal(t=60, symbol='SPY', weight='1', key=None, **meta):
    return dict(signal_id=key or f'{symbol}-{t}', symbol=symbol, strategy_id=symbol,
                strategy_version='test', decision_time=t, expires_at=t+1000,
                target_weight=weight, metadata={'stop_distance': '2', 'regime': 'range', **meta})


def profiles(bars, **extra):
    return {b['symbol']: ExecutionModel(fee_bps='0', spread_bps='0', slippage_bps='0',
                quantity_increment=D('.000001'), participation_rate=D('.5'),
                max_position_weight=D('.35'), **extra) for b in bars}


def replay(bars, decisions, **kwargs):
    engine = MultiassetReplay(bars, decisions, profiles=kwargs.pop('profiles', profiles(bars)), **kwargs)
    try:
        return engine.advance()
    finally:
        engine.close()


def test_atr_sizes_dollar_risk_and_never_fills_decision_event():
    rows = [bar(t) for t in (60,120,180)]
    result = replay(rows, [signal()])
    assert D(result['fills'][0]['quantity']) == D('.25')
    assert result['fills'][0]['fill_time'] == 120
    assert D(result['portfolio']['cash']) == 75
    assert result['portfolio']['positions'][0]['stop_price'] == '98'
    assert result['live_approved'] is False


@pytest.mark.parametrize('fraction,quantity', [('.0025','.125'),('.005','.25'),('.01','.35')])
def test_predeclared_risk_experiments_are_clipped_by_position_cap(fraction, quantity):
    rows = [bar(t) for t in (60,120)]
    result = replay(rows, [signal()], risk=PortfolioRisk(risk_fraction=D(fraction)))
    assert D(result['fills'][0]['quantity']) == D(quantity)


def test_pending_equity_beta_orders_share_headroom_and_cash():
    rows = [bar(t, symbol=s) for t in (60,120) for s in ('SPY','QQQ')]
    result = replay(rows, [signal(symbol='SPY'), signal(symbol='QQQ')])
    assert sum(D(f['quantity'])*D(f['fill_price']) for f in result['fills']) <= 35
    assert D(result['portfolio']['cash']) >= 65
    first = result['equity_curve'][0]
    assert D(first['reserved_cash']) <= 35


def test_gap_veto_prevents_increasing_predeclared_risk():
    rows = [bar(60), bar(120,'200')]
    result = replay(rows, [signal()])
    assert not result['fills']
    assert result['orders'][0]['reason'] == 'cluster_exposure_cap'


def test_per_venue_fee_reservations_and_cost_aware_sizing():
    rows = [bar(t,symbol=s) for t in (60,120) for s in ('SPY','BTC-USD')]
    models = profiles(rows)
    models['BTC-USD'] = replace(models['BTC-USD'],fee_bps=D('120'))
    result = replay(rows, [signal(symbol='BTC-USD'),signal(symbol='SPY')], profiles=models)
    btc = next(f for f in result['fills'] if f['symbol']=='BTC-USD')
    assert D(btc['quantity']) <= D('.5') / D('4.4')
    assert D(btc['fees']) == D(btc['quantity'])*D('100')*D('.012')
    assert D(result['portfolio']['cash']) >= 0


def test_stop_gap_exit_has_subsequent_bar_fill_and_can_exceed_budget():
    rows = [bar(60),bar(120),bar(180,'90'),bar(240,'85')]
    result = replay(rows,[signal()])
    sells = [f for f in result['fills'] if f['side']=='sell']
    assert sells[0]['decision_time'] == 180 and sells[0]['fill_time'] == 240
    assert D(sells[0]['realized_pnl']) == D('-3.75')
    assert not result['portfolio']['positions']
    assert result['risk_state']['daily_halts'] == ['0']


def test_drawdown_halt_survives_price_recovery_and_restart(tmp_path):
    rows = [bar(60),bar(120),bar(180,'50'),bar(240,'100'),bar(300,'100')]
    decisions = [signal(),signal(240,key='again')]
    path = tmp_path/'state.sqlite3'
    engine = MultiassetReplay(rows, decisions, profiles=profiles(rows),state_path=path,create_state=True)
    engine.advance(max_events=3)
    assert engine.state['multiasset_risk']['drawdown_halt']
    engine.close()
    restored = MultiassetReplay(rows, decisions, profiles=profiles(rows),state_path=path)
    report = restored.advance()
    restored.close()
    assert report['risk_state']['drawdown_halt']
    assert report['orders'][-1]['reason']=='portfolio_drawdown_halt'
    uninterrupted = replay(rows,decisions)
    assert report == uninterrupted


def test_resume_refuses_lost_state_and_changed_risk(tmp_path):
    rows, decisions = [bar(60),bar(120)], [signal()]
    path=tmp_path/'state.sqlite3'
    engine=MultiassetReplay(rows,decisions,profiles=profiles(rows),state_path=path,create_state=True)
    engine.close()
    with pytest.raises(ValueError,match='changed'):
        MultiassetReplay(rows,decisions,profiles=profiles(rows),state_path=path,risk=PortfolioRisk(risk_fraction=D('.01')))
    path.unlink()
    with pytest.raises(ValueError,match='missing'):
        MultiassetReplay(rows,decisions,profiles=profiles(rows),state_path=path)
    with pytest.raises(FileExistsError):
        MultiassetReplay(rows,decisions,profiles=profiles(rows),state_path=path,create_state=True)


def test_downtime_is_not_a_fill_and_reconnect_can_fill_unexpired_order():
    rows=[bar(t) for t in (60,120,180)]
    report=replay(rows,[signal()],outages={'SPY':[(100,160)]})
    assert [f['fill_time'] for f in report['fills']]==[180]
    assert any(e['kind']=='venue_downtime_no_fill' for e in report['events'])


def test_partial_fills_share_capacity_and_resume_identically(tmp_path):
    rows=[bar(t) for t in (60,120,180,240)]
    for row in rows:
        row['volume']='.1'
    path=tmp_path/'state.sqlite3'
    engine=MultiassetReplay(rows,[signal()],profiles=profiles(rows),state_path=path,create_state=True)
    engine.advance(max_events=2)
    engine.close()
    resumed=MultiassetReplay(rows,[signal()],profiles=profiles(rows),state_path=path)
    result=resumed.advance()
    resumed.close()
    assert result==replay(rows,[signal()])
    assert [D(f['quantity']) for f in result['fills']]==[D('.05')]*3
    assert result['orders'][0]['status']=='partially_filled'


def test_correlation_uses_only_published_same_interval_pairs():
    rows=[bar(t, str(100+(t//60)%7),symbol=s) for t in range(60,1801,60) for s in ('SPY','QQQ')]
    a=MultiassetReplay(rows,[signal(1500)],profiles=profiles(rows))
    a.advance(max_events=20)
    before=json.loads(json.dumps(a.state['multiasset_risk']['matrix']))
    assert before['SPY']['QQQ']['paired_observations']==19
    assert before['SPY']['QQQ']['correlation'] is None
    a.advance(max_events=2)
    after=a.state['multiasset_risk']['matrix']['SPY']['QQQ']
    assert after['correlation']==pytest.approx(1) and after['as_of']==1320
    a.close()


def test_stale_held_venue_blocks_cross_venue_execution():
    rows=[bar(60),bar(120),bar(600,symbol='BTC-USD'),bar(660,symbol='BTC-USD')]
    report=replay(rows,[signal(),signal(600,symbol='BTC-USD')])
    assert not [f for f in report['fills'] if f['symbol']=='BTC-USD']
    assert report['portfolio']['stale_symbols']==['SPY']


@pytest.mark.parametrize('fraction',['0','.02','-1','1'])
def test_invalid_risk_experiments(fraction):
    with pytest.raises(ValueError):
        PortfolioRisk(risk_fraction=D(fraction))


def test_protective_stop_cannot_be_cancelled_by_still_long_signal():
    rows=[bar(60),bar(120),bar(180),bar(240)]
    rows[2]['low']='95'
    report=replay(rows,[signal(),signal(180)])
    sells=[f for f in report['fills'] if f['side']=='sell']
    assert len(sells)==1 and sells[0]['fill_time']==240
    assert not report['portfolio']['positions']
    assert any(e['kind']=='protective_exit_pending' for e in report['events'])


def test_checkpoint_disappearance_during_open_never_reseeds(tmp_path, monkeypatch):
    import sqlite3
    rows=[bar(60),bar(120)]
    path=tmp_path/'state.sqlite3'
    e=MultiassetReplay(rows,[signal()],profiles=profiles(rows),state_path=path,create_state=True)
    e.close()
    from atlas.research.replay import ReplayEngine
    original=ReplayEngine._open_state
    def disappear(self, target):
        target.unlink()
        return original(self,target)
    monkeypatch.setattr(ReplayEngine,'_open_state',disappear)
    with pytest.raises(sqlite3.OperationalError):
        MultiassetReplay(rows,[signal()],profiles=profiles(rows),state_path=path)
    assert not path.exists()


def test_partial_protective_exit_survives_downtime_long_signals_and_restart(tmp_path):
    rows=[bar(t) for t in (60,120,180,240,300,360)]
    rows[2]['low']='95'
    for row in rows[2:5]:
        row['volume']='.1'
    decisions=[signal(),signal(180),signal(240),signal(300)]
    outages={'SPY':[(240,300)]}
    path=tmp_path/'partial-stop.sqlite3'
    engine=MultiassetReplay(rows,decisions,profiles=profiles(rows),outages=outages,
                           state_path=path,create_state=True)
    engine.advance(max_events=5)
    position=next(iter(engine.state['positions'].values()))
    assert position['stop_triggered'] is True
    assert D(position['quantity'])==D('.20')
    pending=[o for o in engine.state['orders'].values() if o['status']=='partially_filled']
    assert len(pending)==1 and pending[0]['side']=='sell'
    engine.close()
    resumed=MultiassetReplay(rows,decisions,profiles=profiles(rows),outages=outages,state_path=path)
    report=resumed.advance()
    resumed.close()
    assert report==replay(rows,decisions,outages=outages)
    sells=[f for f in report['fills'] if f['side']=='sell']
    assert [f['fill_time'] for f in sells]==[300,360]
    assert sum(D(f['quantity']) for f in sells)==D('.25')
    stop_orders=[o for o in report['orders'] if o['signal_id'].startswith('atr-stop/')]
    assert len(stop_orders)==1 and stop_orders[0]['status']=='filled'
    assert not any(e['kind']=='cancelled' and e.get('signal_id','').startswith('atr-stop/')
                   for e in report['events'])
    assert not report['portfolio']['positions']


def test_expired_protective_stop_reissues_after_price_bounce_until_flat(tmp_path):
    expiry=180+7*86400
    rows=[bar(t) for t in (60,120,180,expiry,expiry+60)]
    rows[2]['low']='95'
    decisions=[signal(),signal(180),signal(expiry)]
    outages={'SPY':[(180,expiry)]}
    path=tmp_path/'expired-stop.sqlite3'
    engine=MultiassetReplay(rows,decisions,profiles=profiles(rows),outages=outages,
                           state_path=path,create_state=True)
    engine.advance(max_events=3)
    assert next(iter(engine.state['positions'].values()))['stop_triggered'] is True
    engine.close()
    resumed=MultiassetReplay(rows,decisions,profiles=profiles(rows),outages=outages,state_path=path)
    report=resumed.advance()
    resumed.close()
    stops=[o for o in report['orders'] if o['signal_id'].startswith('atr-stop/')]
    assert len(stops)==2
    assert stops[0]['status']=='cancelled' and stops[0]['reason']=='signal_expired'
    assert stops[1]['status']=='filled' and stops[1]['decision_time']==expiry
    assert [f['fill_time'] for f in report['fills'] if f['side']=='sell']==[expiry+60]
    assert not report['portfolio']['positions']
    assert report==replay(rows,decisions,outages=outages)


def test_partial_entry_then_fee_risk_gap_cancels_remaining_without_resizing_up():
    rows=[bar(t,symbol='BTC-USD') for t in (60,120,180,240)]
    rows[1]['volume']='.02'
    for row in rows[2:]:
        row.update(open='110',high='110',low='110',close='110')
    models=profiles(rows)
    models['BTC-USD']=replace(models['BTC-USD'],fee_bps=D('120'))
    report=replay(rows,[signal(symbol='BTC-USD')],profiles=models)
    buys=[f for f in report['fills'] if f['side']=='buy']
    assert len(buys)==1 and D(buys[0]['quantity'])==D('.01')
    order=report['orders'][0]
    assert order['status']=='cancelled' and order['reason']=='trade_risk_budget'
    assert D(order['filled_quantity'])==D('.01') and D(order['remaining_quantity'])>0
    assert D(report['portfolio']['positions'][0]['quantity'])==D('.01')
    assert D(report['portfolio']['cash'])==D('98.988')
    assert D(report['equity_curve'][-1]['reserved_cash'])==0


def test_duplicate_long_requests_never_create_two_pending_orders_for_one_sleeve():
    rows=[bar(t) for t in (60,120,180)]
    rows[1]['volume']='0'
    report=replay(rows,[signal(),signal(120,key='same-direction')])
    assert len(report['fills'])==1 and D(report['fills'][0]['quantity'])==D('.25')
    assert report['fills'][0]['fill_time']==180
    assert len([o for o in report['orders'] if o['status']=='filled'])==1
    assert report['orders'][1]['status']=='rejected'
    assert report['orders'][1]['reason']=='sleeve_has_open_order'


def test_correlation_prefix_ignores_future_prices_and_later_historical_revision():
    symbols=('SPY','QQQ')
    prefix=[bar(t,str(100+(t//60)%5),symbol=s) for t in range(60,1321,60) for s in symbols]
    future=[bar(t,str(1000+(t//60)%11),symbol=s) for t in range(1380,1801,60) for s in symbols]
    revision=dict(prefix[4])
    revision.update(open='9999',high='9999',low='9999',close='9999',revision=1,information_available_time=1321)
    a=MultiassetReplay(prefix,[],profiles=profiles(prefix))
    b=MultiassetReplay(prefix+future+[revision],[],profiles=profiles(prefix+future))
    try:
        a.advance()
        b.advance(max_events=22)
        assert a.state['multiasset_risk']==b.state['multiasset_risk']
        before=json.loads(json.dumps(b.state['multiasset_risk']))
        b.advance(max_events=1)
        after=b.state['multiasset_risk']
        assert after['history']==before['history']
        for first in symbols:
            for second in symbols:
                assert after['matrix'][first][second]['correlation']==before['matrix'][first][second]['correlation']
                assert after['matrix'][first][second]['paired_observations']==before['matrix'][first][second]['paired_observations']
        assert any(e['kind']=='revision_not_executable' for e in b.state['events'])
    finally:
        a.close()
        b.close()
