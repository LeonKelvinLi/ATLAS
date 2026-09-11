from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import gzip
import json
from pathlib import Path

import pytest

from atlas.research import multiasset_lab as lab
from atlas.research.multiasset_strategies import StrategyUnavailable, strategy_catalog

NOW=1789158600


def fixtures(days=4):return lab.synthetic_datasets(days=days)


def plan():
    p=lab.default_plan(train_days=1,validation_days=1,test_days=1,max_folds=1)
    p['strategy_specs']=[strategy_catalog()[0]]
    p['bootstrap_resamples']=100
    return p


def test_fixtures_are_valid_reproducible_explicit_synthetic():
    inputs=fixtures()
    assert inputs==fixtures()
    bars,receipts,evidence,sessions=lab.research_inputs(inputs,now=NOW)
    assert evidence=='SYNTHETIC' and len(sessions)==4
    assert {b['symbol'] for b in bars}=={'SPY','QQQ','GLD','USO','BTC-USD'}
    assert all(r['quality']['alpha_validation_eligible'] is False for r in receipts)


def test_archive_availability_assumption_is_separate_and_never_upgrades_evidence():
    data=fixtures()[0]
    data['source']['kind']='real';data['availability_basis']='unknown';data['revision_policy']='latest_only'
    for row in data['bars']:
        row['information_available_time']=NOW-1;row['ingestion_time']=NOW-1
    before=deepcopy(data)
    native,_,evidence,_=lab.research_inputs([data],now=NOW)
    derived,receipts,_,_=lab.research_inputs([data],now=NOW,assume_archive_close=True)
    assert data==before
    assert evidence=='HISTORICAL_UNVERIFIED'
    assert len({r['information_available_time'] for r in native})==1
    assert derived[0]['information_available_time']==derived[0]['event_time']
    assert derived[0]['original_information_available_time']==NOW-1
    assert receipts[0]['archive_close_assumption']


def test_duplicate_observations_and_conflicting_calendars_refused():
    data=fixtures()[0]
    with pytest.raises(ValueError,match='overlap'):
        lab.research_inputs([data,data],now=NOW)
    other=deepcopy(data);other['calendar']['sessions'][0]['close_time']-=3600
    with pytest.raises(ValueError,match='conflicting'):
        lab.equity_calendar([data,other])


def test_global_units_survive_canonical_normalization():
    data=fixtures()[0]
    for row in data['bars']:
        del row['currency'],row['volume_unit']
    bars,*_=lab.research_inputs([data],now=NOW)
    assert all(row['currency']=='USD' and row['volume_unit']=='shares' for row in bars)


def test_benchmark_requires_executed_entry_not_merely_a_bar():
    bars,_,evidence,sessions=lab.research_inputs(fixtures(),now=NOW)
    first=next(b for b in bars if b['symbol']=='SPY')
    result=lab.benchmark([first],'SPY',0,NOW,evidence_class=evidence,session_days=sessions)
    assert result['available'] is False and result['reason']=='no_executable_entry_fill'
    absent=lab.benchmark([first],'QQQ',0,NOW,evidence_class=evidence,session_days=sessions)
    assert absent['reason']=='underlying_data_unavailable'


def test_real_archive_cannot_claim_pit_from_metadata_strings():
    data=fixtures()[0]
    data['source']['kind']='real'
    data['availability_basis']='observed'
    data['revision_policy']='as_observed'
    _,receipts,evidence,_=lab.research_inputs([data],now=NOW)
    assert evidence=='HISTORICAL_UNVERIFIED'
    assert receipts[0]['quality']['alpha_validation_eligible'] is False


def test_end_to_end_keeps_one_cash_ledger_and_insufficient_evidence(tmp_path):
    out=tmp_path/'campaign'
    result=lab.run_campaign(fixtures(),out,plan=plan(),now=NOW)
    assert result['fold_count']==1 and result['evaluated_trial_windows']==3
    assert result['verified_OOS_survivors']==[]
    assert D(result['synthetic_cash_remaining'])==100
    assert result['actual_orders_submitted']==0 and result['real_money_allocated']=='0'
    fold=result['folds'][0]
    assert not fold['numeric_validation_survivors']
    assert not fold['paper_allocation']['eligible_ids']
    assert len(fold['trials'])==3
    for trial in fold['trials']:
        assert trial['test']['sharpe'] is None
        assert trial['test_gate']['eligible'] is False
        assert set(trial['stress'])=={'double_cost','latency_600s','entry_delay_60s','one_hour_downtime'}
        raw=Path(trial['test_report']['path']).read_bytes()
        report=json.loads(gzip.decompress(raw))
        assert report['live_approved'] is False
    assert (out/'REPORT.md').is_file() and (out/'manifest.json').stat().st_mode & 0o777==0o600
    with pytest.raises(FileExistsError):lab.run_campaign(fixtures(),out,plan=plan(),now=NOW)


def test_future_prices_do_not_change_earlier_signals_or_validation(tmp_path):
    inputs=fixtures();bars,_,evidence,sessions=lab.research_inputs(inputs,now=NOW)
    calendar=lab.equity_calendar(inputs);spec=strategy_catalog()[0]
    cutoff=min(row['event_time'] for row in bars)+2*lab.DAY
    first=lab.evaluate(bars,spec,'.005',0,cutoff,evidence_class=evidence,session_days=sessions,calendar=calendar)
    changed=deepcopy(bars)
    for row in changed:
        if row['information_available_time']>=cutoff:
            for field in ('open','high','low','close'):row[field]=str(D(row[field])*3)
    second=lab.evaluate(changed,spec,'.005',0,cutoff,evidence_class=evidence,session_days=sessions,calendar=calendar)
    assert first==second


def test_no_archive_assumption_produces_no_historical_folds(tmp_path):
    data=fixtures()[0]
    data['source']['kind']='real';data['availability_basis']='unknown';data['revision_policy']='latest_only'
    for row in data['bars']:
        row['information_available_time']=NOW-1;row['ingestion_time']=NOW-1
    report=lab.run_campaign([data],tmp_path/'capture-only',plan=plan(),now=NOW)
    assert report['fold_count']==0 and report['evaluated_trial_windows']==0
    assert report['verified_OOS_survivors']==[]


def test_forbidden_live_plan_refused_before_output(tmp_path):
    p=plan();p['live_approved']=True
    with pytest.raises(ValueError,match='no live'):
        lab.run_campaign(fixtures(),tmp_path/'no',plan=p,now=NOW)
    assert not (tmp_path/'no').exists()


def test_global_validation_ranking_is_frozen_before_future_unavailability(tmp_path,monkeypatch):
    inputs=fixtures();p=plan();p['strategy_specs']=strategy_catalog()[:2]
    normal=lab.run_campaign(inputs,tmp_path/'normal',plan=p,now=NOW)
    expected=normal['folds'][0]['ranked_validation']
    test_start=normal['folds'][0]['fold']['ranges']['test']['start']
    real_signals=lab.signals_for
    observed=[False]
    out=tmp_path/'future-unavailable'
    def guarded(rows,spec,**kwargs):
        if any(row['information_available_time']>=test_start for row in rows):
            frozen=json.loads((out/'fold-0-validation-selection.json').read_text())
            assert frozen['ranked_validation']==expected
            observed[0]=True
            raise StrategyUnavailable('future_only_data_refusal')
        return real_signals(rows,spec,**kwargs)
    monkeypatch.setattr(lab,'signals_for',guarded)
    changed=lab.run_campaign(inputs,out,plan=p,now=NOW)
    assert observed[0]
    assert changed['folds'][0]['ranked_validation']==expected
    assert all(t['status']=='test_unavailable' for t in changed['folds'][0]['trials'])
    assert changed['evaluated_trial_windows']==0


def test_false_breakout_rate_counts_only_fully_observed_signal_episodes():
    inputs=fixtures();bars,_,evidence,sessions=lab.research_inputs(inputs,now=NOW)
    rows=[r for r in bars if r['symbol']=='BTC-USD'][:8]
    spec=next(s for s in strategy_catalog() if s['family']=='btc_breakout')
    decisions=[]
    sequence=[('long','breakout_hold_or_exit'),('flat','breakout_fakeout_exit'),
              ('long','breakout_entry'),('flat','breakout_fakeout_exit'),
              ('long','breakout_entry'),('flat','breakout_hold_or_exit'),
              ('long','breakout_entry')]
    for index,(direction,reason) in enumerate(sequence):
        when=rows[index]['event_time']
        decisions.append({'signal_id':f'test-{index}','symbol':'BTC-USD','strategy_id':spec['strategy_id'],
            'strategy_version':'test','decision_time':when,'expires_at':when+1000,
            'target_weight':'1' if direction=='long' else '0','direction':direction,
            'metadata':{'stop_distance':'100','regime':'range','reason':reason}})
    result=lab.evaluate(rows,spec,'.005',0,rows[-1]['event_time']+1,evidence_class=evidence,
                        session_days=sessions,precomputed_signals=decisions)
    stats=result['signal_diagnostics']
    assert stats['false_breakout_signals']==2
    assert stats['resolved_breakout_episodes']==2
    assert stats['false_breakouts_with_observed_entry']==1
    assert stats['false_breakout_rate']==.5


def test_unchanged_weekend_expiry_is_not_a_trading_day_or_lost_evidence():
    inputs=lab.synthetic_datasets(days=8,start='2026-08-20')
    bars,_,evidence,sessions=lab.research_inputs(inputs,now=NOW)
    # Daily Friday signal expires Saturday while Monday observations remain.
    spec=next(s for s in strategy_catalog() if s['family']=='commodity_proxy_trend' and s['timeframe_seconds']==86400)
    report=lab.evaluate(bars,spec,'.005',0,max(b['event_time'] for b in bars)+1,
                        evidence_class=evidence,session_days=sessions,calendar=lab.equity_calendar(inputs))
    assert report['valuation_sampling']['excluded_unchanged_non_session_events']>=1
    assert len(report['all_event_equity_curve'])>len(report['equity_curve'])
    assert all(row['timestamp']//lab.DAY*lab.DAY in sessions for row in report['equity_curve'])
    assert D(report['portfolio']['equity'])==D(report['equity_curve'][-1]['equity'])
    from atlas.research.multiasset_validation import stress_diagnostics
    diagnostics=stress_diagnostics(report,calendar='UTC_SESSION_DAYS',session_days=sessions,resamples=100)
    assert diagnostics['selection_eligible'] is False


def test_non_session_cash_change_cannot_be_silently_filtered():
    report={'venue_profiles':{'SPY':{'name':'alpaca_equity_cost_scenario'}},'fills':[],
            'equity_curve':[{'timestamp':60,'equity':'100','cash':'100'},
                            {'timestamp':lab.DAY+60,'equity':'99','cash':'99'}]}
    with pytest.raises(ValueError,match='non-session'):
        lab._summary(report,[0])
