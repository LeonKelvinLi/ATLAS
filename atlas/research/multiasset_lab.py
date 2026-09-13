"""Reproducible research tournament with explicit provenance and no broker authority.

The CLI's archive experiments are retrospective and never qualify a strategy
for allocation. A historical publication-time assumption is retained in a
separate derived input, never written over the acquisition archive.
"""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import gzip
import hashlib
import json
import os
from pathlib import Path
import random
import time
from zoneinfo import ZoneInfo

from atlas.research.market import validate_dataset
from atlas.research.multiasset_risk import MultiassetReplay, PortfolioRisk, venue_models
from atlas.research.multiasset_strategies import strategy_catalog, normalize_spec, signals_for, StrategyUnavailable
from atlas.research.multiasset_validation import (DEFAULT_THRESHOLDS, metrics_for, gate_result,
    schedule_folds, rank_candidates, pre_cutoff, stress_diagnostics)
from atlas.research.replay import ReplayEngine

DAY=86400
RISK_FRACTIONS=('.0025','.005','.01')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def source_identity():
    root=Path(__file__).resolve().parents[2]
    paths=sorted((root/'atlas').rglob('*.py'))+sorted((root/'scripts').glob('*multiasset*.py'))
    return {str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def write_json(path, value):
    """Exclusive artifacts: a failed campaign is retained and never overwritten."""
    raw=json.dumps(value,sort_keys=True,indent=2,allow_nan=False).encode()+b'\n'
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    return hashlib.sha256(raw).hexdigest()


def research_inputs(datasets, *, now, assume_archive_close=False):
    bars, receipts, classes, sessions = [], [], [], set()
    seen=set()
    for payload in datasets:
        checked=validate_dataset(payload,now=now)
        kind=payload['source']['kind']
        if kind in {'synthetic','test'}:
            evidence='SYNTHETIC' if kind=='synthetic' else 'TEST'
        else:
            evidence='HISTORICAL_UNVERIFIED'
        derived=[]
        for raw in checked['bars']:
            row=dict(raw)
            key=(row['symbol'],row['event_time'],row['revision'])
            if key in seen:
                raise ValueError('overlapping dataset observations are not independent evidence')
            seen.add(key)
            if kind not in {'synthetic','test'} and assume_archive_close:
                if row['revision'] != 0:
                    raise ValueError('archive close assumption cannot assign revision publication history')
                row['original_information_available_time']=row['information_available_time']
                row['information_available_time']=row['event_time']
                row['research_availability_assumption']='completed_bar_close_not_verified_historical_publication'
            derived.append(row)
        receipts.append({'dataset_id':payload['dataset_id'],'dataset_hash':checked['dataset_hash'],
                         'evidence_class':evidence,'quality':checked['quality'],
                         'archive_close_assumption':bool(kind not in {'synthetic','test'} and assume_archive_close),
                         'derived_bars_hash':digest(derived),'original_bars_hash':digest(payload['bars'])})
        bars.extend(derived);classes.append(evidence)
        for session in payload.get('calendar',{}).get('sessions',[]):
            sessions.add(int(datetime.fromisoformat(session['session_date']).replace(tzinfo=timezone.utc).timestamp()))
    if not bars:
        raise ValueError('explicit canonical datasets required')
    evidence='HISTORICAL_UNVERIFIED' if 'HISTORICAL_UNVERIFIED' in classes else 'SYNTHETIC' if 'SYNTHETIC' in classes else 'TEST'
    return sorted(bars,key=lambda b:(b['information_available_time'],b['event_time'],b['symbol'],b['revision'])),receipts,evidence,sorted(sessions)



def equity_calendar(datasets):
    calendars=[d['calendar'] for d in datasets if d['asset_class']=='equity']
    if not calendars:return None
    sessions={}
    for calendar in calendars:
        for session in calendar['sessions']:
            old=sessions.get(session['session_date'])
            if old is not None and old!=session:
                raise ValueError('conflicting session calendars')
            sessions[session['session_date']]=session
    return {**calendars[0],'sessions':[sessions[key] for key in sorted(sessions)]}

def default_plan(*,train_days=10,validation_days=5,test_days=5,embargo_seconds=300,max_folds=3):
    for value in (train_days,validation_days,test_days,max_folds):
        if type(value) is not int or value <= 0:
            raise ValueError('positive whole-day windows and fold limit required')
    return {'version':'multiasset-campaign-v1','initial_capital_usd':'100',
            'research_purpose':'diagnostic','max_replay_evaluations':5000,
            'strategy_specs':strategy_catalog(),'risk_fractions':list(RISK_FRACTIONS),
            'train_seconds':train_days*DAY,'validation_seconds':validation_days*DAY,
            'test_seconds':test_days*DAY,'embargo_seconds':embargo_seconds,
            'label_horizon_seconds':300,'max_folds':max_folds,
            'thresholds':dict(DEFAULT_THRESHOLDS),'bootstrap_resamples':200,'seed':1729,
            'cost_multipliers':['1','2'],'latency_stress_seconds':600,
            'objective':'maximum_validation_CAGR_subject_to_fixed_numeric_constraints',
            'allocation':'only_prior_completed_verified_OOS_survivors; never_current_test_ranking',
            'account_scope':'local_synthetic_100_USD_only','live_approved':False}


def _summary(report, session_days):
    equities=bool(report['venue_profiles']) and all('equity' in p['name'] for p in report['venue_profiles'].values())
    if equities:
        declared=set(session_days)
        full=report['equity_curve']
        kept=[]
        for row in full:
            if row['timestamp']//DAY*DAY in declared:
                kept.append(row)
            elif (not kept or D(row['equity'])!=D(kept[-1]['equity'])
                  or D(row['cash'])!=D(kept[-1]['cash'])):
                raise ValueError('non-session event changed equity or cash')
        if len(kept)!=len(full):
            if any(f['fill_time']//DAY*DAY not in declared for f in report['fills']):
                raise ValueError('equity fill outside declared sessions')
            report['all_event_equity_curve']=full
            report['equity_curve']=kept
            report['valuation_sampling']={
                'excluded_unchanged_non_session_events':len(full)-len(kept),
                'basis':'Non-session expiry bookkeeping with unchanged equity/cash excluded from session statistics; complete event curve retained.'}
    return metrics_for(report,calendar='UTC_SESSION_DAYS' if equities else 'UTC_24_7',
                       session_days=session_days if equities else None)


def evaluate(bars, spec, fraction, start, end, *, evidence_class, session_days,
             cost_multiplier='1',latency_seconds=1,shift_seconds=0,outages=None,precomputed_signals=None,calendar=None):
    spec=normalize_spec(spec)
    prefix=[b for b in bars if b['symbol'] in spec['symbols'] and b['information_available_time'] < end]
    observations=[b for b in prefix if start <= b['information_available_time'] < end]
    if not observations:
        raise StrategyUnavailable('no_market_observations_in_window')
    decisions=signals_for(prefix,spec,calendar=calendar) if precomputed_signals is None else precomputed_signals
    filtered=[]
    for raw in decisions:
        decision=dict(raw)
        decision['decision_time']+=shift_seconds
        decision['expires_at']+=shift_seconds
        if start <= decision['decision_time'] < end:
            # Bars/decisions are bounded before the window ends; warmup has no positions or P&L.
            filtered.append(decision)
    models=venue_models(observations,cost_multiplier=cost_multiplier,latency_seconds=latency_seconds)
    engine=MultiassetReplay(observations,filtered,profiles=models,risk=PortfolioRisk(risk_fraction=D(fraction)),
                            outages=outages,evidence_class=evidence_class)
    try:
        report=engine.advance()
    finally:
        engine.close()
    report['research_metrics']=_summary(report,session_days)
    report['evaluation_window']={'start':start,'end_exclusive':end,'warmup_initial_inventory':'0'}
    counts=Counter(d.get('metadata',{}).get('reason') for d in filtered)
    # Count signals per symbol: cross-symbol interleaving must not manufacture episodes.
    active,entries={},0
    for d in filtered:
        current=d.get('direction')=='long'
        if current and not active.get(d['symbol'],False): entries+=1
        active[d['symbol']]=current
    # A rate requires both the entry and resolution inside the observed window.
    # Do not count a warmup-open position or an unresolved terminal breakout.
    breakout_open={};resolved=failed=0
    for d in filtered:
        reason=d.get('metadata',{}).get('reason')
        if reason=='breakout_entry':
            breakout_open[d['symbol']]=True
        elif breakout_open.get(d['symbol']) and d.get('direction')=='flat':
            resolved+=1
            failed+=reason=='breakout_fakeout_exit'
            breakout_open[d['symbol']]=False
    report['signal_diagnostics']={'reason_counts':dict(counts),'entry_signal_episodes':entries,
        'false_breakout_signals':counts.get('breakout_fakeout_exit',0),
        'resolved_breakout_episodes':resolved,'false_breakouts_with_observed_entry':failed,
        'false_breakout_rate':failed/resolved if resolved else None,
        'basis':'false breakouts divided by resolved signal episodes whose entry is in-window; excludes warmup-open and unresolved terminal episodes; not executed trade success'}
    return report


def benchmark(bars,symbol,start,end,*,evidence_class,session_days):
    rows=[b for b in bars if b['symbol']==symbol and start <= b['information_available_time'] < end]
    if not rows:return {'available':False,'reason':'underlying_data_unavailable','symbol':symbol}
    first=min(b['information_available_time'] for b in rows)
    model=replace(venue_models(rows)[symbol],max_position_weight=D(1))
    decision={'signal_id':'benchmark/'+symbol,'strategy_id':'buy_hold/'+symbol,'strategy_version':'1',
              'symbol':symbol,'decision_time':first,'expires_at':end,'target_weight':'1'}
    engine=ReplayEngine(rows,[decision],model=model,initial_cash='100',evidence_class=evidence_class)
    try:report=engine.advance()
    finally:engine.close()
    if not any(fill['side']=='buy' for fill in report['fills']):
        return {'available':False,'reason':'no_executable_entry_fill','symbol':symbol,
                'ending_cash':report['portfolio']['cash']}
    report['venue_profiles']={symbol:model.public()}
    return {'available':True,'symbol':symbol,'metrics':_summary(report,session_days),
            'basis':'100 USD independent buy-and-hold comparison; same window/costs; next-bar entry; terminal mark; no dividends/interest',
            'fills':report['fills'],'ending_portfolio':report['portfolio']}


def _compact(metrics):
    return {k:v for k,v in metrics.items() if k not in {'daily_curve','daily_returns','round_trips','monthly_returns'}}


def _archive_report(directory, name, report):
    path=directory/(name+'.json.gz')
    raw=json.dumps(report,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    packed=gzip.compress(raw,mtime=0)
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as handle:handle.write(packed)
    return {'path':str(path),'sha256':hashlib.sha256(packed).hexdigest(),'uncompressed_sha256':hashlib.sha256(raw).hexdigest()}


def run_campaign(datasets, output_dir, *, plan=None, now=None, assume_archive_close=False, progress=None):
    from atlas.research.campaign_preflight import validate_campaign_plan
    now=int(time.time()) if now is None else now
    plan=default_plan() if plan is None else plan
    preflight=validate_campaign_plan(plan)
    plan=json.loads(json.dumps(plan,allow_nan=False))
    specs=[normalize_spec(s) for s in plan['strategy_specs']]
    out=Path(output_dir).expanduser()
    if not out.is_absolute() or out.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError('absolute external campaign directory required')
    out.mkdir(mode=0o700,parents=False,exist_ok=False)
    (out/'reports').mkdir(mode=0o700)
    sources=source_identity()
    # Freeze source and declared search space before evaluations or ranking.
    manifest={'created_at_utc':now,'plan':plan,'source_identity':sources,'preflight':preflight,
              'input_payload_hashes':[digest(d) for d in datasets],
              'assume_archive_close':assume_archive_close,'historical_windows_are_retrospective':True,
              'live_approved':False,'actual_orders_submitted':0}
    write_json(out/'manifest.json',manifest)
    bars,receipts,evidence,sessions=research_inputs(datasets,now=now,assume_archive_close=assume_archive_close)
    calendar=equity_calendar(datasets)
    write_json(out/'input-receipts.json',receipts)
    if assume_archive_close:write_json(out/'derived-research-bars.json',bars)
    # Grouped publication clocks drive boundaries; revision histories remain inside signals.
    samples=sorted({b['information_available_time'] for b in bars})
    folds=schedule_folds(samples,train_seconds=plan['train_seconds'],validation_seconds=plan['validation_seconds'],
            test_seconds=plan['test_seconds'],embargo_seconds=plan['embargo_seconds'],
            label_horizon_seconds=plan['label_horizon_seconds'])[:plan['max_folds']]
    prior,fold_results,allocation_decisions=[],[],[]
    evidence_flags={'evidence_class':evidence,'provenance_verified':False,'point_in_time_verified':False,
                    'out_of_sample':False,'session_calendar_verified':False}
    for fold in folds:
        number=fold['fold_id'];ranges=fold['ranges']
        tstart,tend=ranges['test']['start'],ranges['test']['end_exclusive']
        allocation=pre_cutoff(prior,allocation_time=tstart,thresholds=plan['thresholds']) if prior else {
            'allocation_time':tstart,'eligible_ids':[],'checks':[],'live_approved':False,
            'reason':'no_previous_completed_eligible_OOS_evidence'}
        # The input route has no authenticated PIT evidence, so cannot promote an archive.
        if allocation['eligible_ids']:
            raise ValueError('archive/synthetic input cannot authorize even paper tournament allocation')
        trials,selection,prepared=[],[],[]
        # Phase one uses only train/validation prefixes for every candidate.
        # Future test availability cannot remove or reorder a validation entry.
        for index,spec in enumerate(specs):
            if progress:progress({'fold':number,'phase':'selection','strategy':index+1,
                                  'total_strategies':len(specs),'family':spec['family']})
            by_split={}
            unavailable=None
            for split in ('train','validation'):
                boundary=ranges[split]['end_exclusive']
                prefix=[b for b in bars if b['symbol'] in spec['symbols'] and b['information_available_time']<boundary]
                try:by_split[split]=signals_for(prefix,spec,calendar=calendar if spec['family']!='btc_breakout' else None)
                except StrategyUnavailable as exc:unavailable=str(exc);break
            if unavailable:
                trials.append({'strategy_id':spec['strategy_id'],'status':'unavailable','reason':unavailable})
                continue
            for fraction in RISK_FRACTIONS:
                candidate=spec['strategy_id']+'/risk'+fraction
                split_reports={}
                try:
                    for split in ('train','validation'):
                        window=ranges[split]
                        split_reports[split]=evaluate(bars,spec,fraction,window['start'],window['end_exclusive'],
                            evidence_class=evidence,session_days=sessions,precomputed_signals=by_split[split])
                except StrategyUnavailable as exc:
                    trials.append({'candidate_id':candidate,'status':'unavailable','reason':str(exc)});continue
                vm=split_reports['validation']['research_metrics']
                selection.append({'candidate_id':candidate,'split':'validation','evaluated_end':vm['evaluated_end'],'metrics':vm})
                trial={'candidate_id':candidate,'strategy_id':spec['strategy_id'],'family':spec['family'],
                       'variant':spec['variant'],'lookback':spec['lookback'],
                       'timeframe_seconds':spec['timeframe_seconds'],'risk_fraction':fraction,
                       'status':'selection_recorded','train':_compact(split_reports['train']['research_metrics']),
                       'validation':_compact(vm)}
                trials.append(trial)
                prepared.append((index,spec,fraction,trial))
        ranked=rank_candidates(selection,selection_time=tstart-1,thresholds=plan['thresholds'])
        selection_snapshot={'fold_id':number,'selected_at':tstart-1,'test_start':tstart,
                            'ranking_basis':'complete_train_validation_only_before_any_test_signal_or_evaluation',
                            'ranked_validation':ranked,
                            'selection_unavailable':[dict(trial) for trial in trials if trial['status']=='unavailable']}
        selection_path=out/f'fold-{number}-validation-selection.json'
        selection_hash=write_json(selection_path,selection_snapshot)
        ranked_by_id={row['candidate_id']:row for row in ranked}
        for index,spec,fraction,trial in prepared:
            chosen=ranked_by_id[trial['candidate_id']]
            trial['selection']=dict(chosen)
            write_json(out/'reports'/f'{number}-{index}-{fraction}-selection.json',chosen)

        # Phase two may inspect test data, but cannot change the frozen ranking.
        test_signals,test_unavailable={},{}
        for index,spec,fraction,trial in prepared:
            strategy_id=spec['strategy_id']
            if strategy_id not in test_signals and strategy_id not in test_unavailable:
                if progress:progress({'fold':number,'phase':'test','strategy':index+1,
                                      'total_strategies':len(specs),'family':spec['family']})
                if not any(b['symbol'] in spec['symbols'] and tstart<=b['information_available_time']<tend for b in bars):
                    test_unavailable[strategy_id]='no_test_market_observations'
                else:
                    prefix=[b for b in bars if b['symbol'] in spec['symbols'] and b['information_available_time']<tend]
                    try:
                        test_signals[strategy_id]=signals_for(prefix,spec,calendar=calendar if spec['family']!='btc_breakout' else None)
                    except StrategyUnavailable as exc:
                        test_unavailable[strategy_id]=str(exc)
            if strategy_id in test_unavailable:
                trial.update(status='test_unavailable',reason=test_unavailable[strategy_id])
                continue
            decisions=test_signals[strategy_id]
            try:
                test=evaluate(bars,spec,fraction,tstart,tend,evidence_class=evidence,session_days=sessions,
                              precomputed_signals=decisions)
            except StrategyUnavailable as exc:
                trial.update(status='test_unavailable',reason=str(exc))
                continue
            scenarios={
                'double_cost':{'cost_multiplier':'2'},
                'latency_600s':{'latency_seconds':plan['latency_stress_seconds']},
                'entry_delay_60s':{'shift_seconds':60},
                'one_hour_downtime':{'outages':{'*':[(tstart+(tend-tstart)//2,tstart+(tend-tstart)//2+3600)]}},
            }
            scenario_results={}
            for name,parameters in scenarios.items():
                try:
                    report=evaluate(bars,spec,fraction,tstart,tend,evidence_class=evidence,session_days=sessions,
                                    precomputed_signals=decisions,**parameters)
                    scenario_results[name]=_compact(report['research_metrics'])
                except StrategyUnavailable as exc:
                    scenario_results[name]={'available':False,'reason':str(exc)}
            metrics=test['research_metrics']
            gate=gate_result(metrics,plan['thresholds'],evidence_flags)
            report_ref=_archive_report(out/'reports',f'{number}-{index}-{fraction}-test',test)
            stress=stress_diagnostics(test,seed=plan['seed'],resamples=plan['bootstrap_resamples'],
                calendar=metrics['calendar'],session_days=sessions if metrics['calendar']=='UTC_SESSION_DAYS' else None)
            trial.update(status='evaluated',test=_compact(metrics),test_gate=gate,test_report=report_ref,
                         signal_diagnostics=test['signal_diagnostics'],diagnostics=stress,stress=scenario_results)
        if hashlib.sha256(selection_path.read_bytes()).hexdigest()!=selection_hash:
            raise ValueError('validation selection artifact changed during test evaluation')
        # Prior OOS evidence only filters the original validation order.
        indexed={r.get('candidate_id'):r for r in trials}
        prior=[]
        for chosen in ranked:
            trial=indexed[chosen['candidate_id']]
            if not chosen['selection_qualified'] or trial['status']!='evaluated':continue
            prior.append({**chosen,'evaluated_start':tstart,'evaluated_end':tend-1,
                          'metrics':trial['test'],'evidence':evidence_flags})
        benchmarks={symbol:benchmark(bars,symbol,tstart,tend,evidence_class=evidence,session_days=sessions)
                    for symbol in ('SPY','QQQ','BTC-USD','GLD','USO')}
        for trial in trials:
            if trial['status']!='evaluated':continue
            trial['benchmark_excess_return']={symbol:trial['test']['net_return']-entry['metrics']['net_return']
                for symbol,entry in benchmarks.items() if entry['available']}
        sensitivity={}
        for trial in trials:
            if trial['status']!='evaluated':continue
            key='/'.join((trial['family'],trial['variant'],str(trial['timeframe_seconds'])))
            sensitivity.setdefault(key,[]).append({
                'candidate_id':trial['candidate_id'],'lookback':trial['lookback'],
                'risk_fraction':trial['risk_fraction'],
                'net_return':trial['test']['net_return'],'max_drawdown':trial['test']['max_drawdown'],
                'completed_round_trips':trial['test']['completed_round_trips'],
                'double_cost_net_return':trial['stress']['double_cost'].get('net_return'),
                'selection_eligible':False})
        fold_results.append({'fold':fold,'selection_clock_basis':'simulated chronology; source history already acquired',
            'validation_selection_artifact':{'path':str(selection_path),'sha256':selection_hash},
            'ranked_validation':ranked,'numeric_validation_survivors':[r['candidate_id'] for r in ranked if r['selection_qualified']],
            'actual_verified_OOS_survivors':[],'paper_allocation':allocation,'benchmarks':benchmarks,'trials':trials,
            'parameter_sensitivity':{'groups':sensitivity,'basis':'prespecified catalog/risk perturbations; descriptive only; no test-window selection'}})
        write_json(out/f'fold-{number}.json',fold_results[-1])
    # The permanently unallocated cash ledger needs no intraday checkpoints.
    # Full-resolution inputs remain bound by the manifest and experiment receipts.
    if allocation_decisions:
        raise ValueError('cash-only archive tournament cannot contain allocated decisions')
    cashmarks={}
    for bar in bars:
        cashmarks[(bar['information_available_time']//DAY,bar['symbol'])]=bar
    cashbars=sorted(cashmarks.values(),key=lambda b:(b['information_available_time'],b['event_time'],b['symbol'],b['revision']))
    cash_sampling={'basis':'Cash-only ledger samples last observed quotes per UTC day; no positions or allocated strategy signals.',
                   'day_clock':'information_available_time','full_input_bar_count':len(bars),
                   'cash_mark_bar_count':len(cashbars),'full_input_bars_hash':digest(bars),
                   'cash_mark_bars_hash':digest(cashbars),'strategy_and_benchmark_bars_thinned':False}
    engine=MultiassetReplay(cashbars,allocation_decisions,evidence_class=evidence,state_path=out/'paper-tournament.sqlite3',create_state=True)
    try:portfolio=engine.advance()
    finally:engine.close()
    portfolio['cash_only_sampling']=cash_sampling
    portfolio_ref=_archive_report(out/'reports','paper-tournament',portfolio)
    if source_identity()!=sources:raise ValueError('source changed during campaign; results cannot be certified')
    result={'schema_version':1,'evidence_class':evidence,'fold_count':len(folds),'preflight':preflight,
            'hypothesis_count':len(specs),'risk_variants':len(RISK_FRACTIONS),
            'evaluated_trial_windows':sum(r['status']=='evaluated' for f in fold_results for r in f['trials']),
            'verified_OOS_survivors':[],'synthetic_cash_allocated':'0','synthetic_cash_remaining':portfolio['portfolio']['cash'],
            'actual_orders_submitted':0,'real_money_allocated':'0','live_approved':False,
            'folds':fold_results,'paper_tournament_report':portfolio_ref,'source_identity_verified':True,
            'cash_only_sampling':cash_sampling,
            'inputs':receipts,'unavailable_benchmarks':{'60_40':'no verified bond total-return series supplied',
                'T_bill':'no risk-free total-return series supplied; zero-interest cash is separate'},
            'limitations':['Retrospective archives and synthetic data cannot establish a genuinely unseen verified result.',
                'Fixed catalog evaluated descriptively; numerical qualification is distinct from provenance qualification.',
                'Each candidate experiment has independent hypothetical100USD; actual tournament ledger has a single100USD.',
                'Cash-only ledger samples last observed quotes per UTC day; no positions or allocated strategy signals.',
                'GLD/USO ETF proxies do not reproduce futures leverage, expiry, rolls or direct commodity returns.',
                '8h regular-session equity bars unavailable; daily proxy signals require sufficient complete sessions.',
                'LEAN comparison, paid futures/order-book feeds and verified live Alpaca equity paper acknowledgment remain outstanding.',
                'No dividends, corporate actions or interest modeled; raw IEX is not consolidated SIP.',
                'Parameter sensitivity and stress results are diagnostics only, never test-window tuning.']}
    write_json(out/'results.json',result)
    report=render_report(result)
    fd=os.open(out/'REPORT.md',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as handle:handle.write(report)
    return result


def render_report(result):
    rows=['# ATLAS multi-asset research campaign','',
          f"Evidence: **{result['evidence_class']}**. Verified out-of-sample survivors: **0**.",
          f"Real money allocated: **$0**. Actual orders submitted: **0**. Shared synthetic cash remaining: **${result['synthetic_cash_remaining']}**.",'',
          f"Registered {result['hypothesis_count']} strategy specifications × {result['risk_variants']} risk fractions; evaluated {result['evaluated_trial_windows']} candidate test windows across {result['fold_count']} chronological folds.",
          '', '## Interpretation','',
          'This run checks the research pipeline and describes supplied historical prices. It does not demonstrate profitable alpha. Every candidate must meet the fixed sample, net-return, drawdown, Sharpe, profit-factor, cost, calendar and evidence gates before a paper allocation can be proposed. No candidate passed all gates.',
          '', '## Gate and experiment policy','',
          'Selection maximizes validation CAGR only after the fixed numeric gates: ≥30 completed flat-to-flat trades, ≥60 daily marks, positive net return, ≤10% drawdown, Sharpe ≥1.2 and profit factor ≥1.25. Only a selection made before a later test may seek promotion after that test finishes. Archive acquisition is not proof of historical publication timing.',
          '', '0.25%, 0.5% and 1% are account-risk budgets, including modeled round-trip costs. The default shared cluster cap is35% exposure/1% planned risk; total planned risk is2%. Daily2% loss blocks entries for the UTC day and10% drawdown blocks them persistently. ATR exits occur after publication and may gap beyond these planning limits.',
          '', '## Candidate results','',
          '| Fold | Candidate | Test net % | Drawdown % | Trades | Gate |',
          '|---|---|---:|---:|---:|---|']
    if 'preflight' in result:
        preflight=result['preflight']
        rows[8:8]=[
            '', f"Research purpose: {preflight['research_purpose']}. Frozen replay upper bound: {preflight['maximum_total_replays']} of {preflight['max_replay_evaluations']} permitted, including benchmarks and the cash ledger.",
            'Outside-campaign attempt history is unknown; this receipt is not a complete historical multiple-testing correction.',
            'Planning warnings: '+('; '.join(preflight['warnings']) or 'none; actual sample and evidence gates remain required.'), '']
    for fold in result['folds']:
        for trial in fold['trials']:
            if trial['status']!='evaluated':continue
            m=trial['test']
            rows.append(f"| {fold['fold']['fold_id']} | {trial['candidate_id']} | {100*m['net_return']:.4f} | {100*m['max_drawdown']:.4f} | {m['completed_round_trips']} | blocked |")
    rows+=['','## Benchmark and stress interpretation','',
           'Each available SPY/QQQ/BTC/GLD/USO buy-and-hold comparator uses an independent $100, the same test window and modeled venue costs, with entry on a subsequent available bar and terminal marked inventory. Missing symbols are explicitly unavailable. The JSON includes excess returns, CAGR, Sharpe, Sortino, Calmar, volatility, expectancy, turnover, costs, worst periods and recovery durations; insufficient metrics are null, never zero.',
           '', 'All evaluated candidates receive double-cost,600-second latency,60-second entry-delay and one-hour downtime reruns. The three risk budgets and catalog variants provide prespecified sensitivity comparisons. Daily block bootstrap and completed-trade-order Monte Carlo report unavailable when the sample is insufficient. These are conditional diagnostics, not probability forecasts.',
           '', '## Unavailable capabilities and limitations','']
    rows += ['- '+text for text in result['limitations']]
    rows += ['- 60/40 and T-bill benchmarks are unavailable without their return series. Uninvested zero-interest cash is the only cash baseline.',
             '- Alpaca equities integration is fixture-tested and cannot submit broker orders. The previous paper account403 rejection remains unresolved.',
             '', 'Full per-fold metrics, validation rankings, gate reasons, scenario comparisons and benchmark data are in results.json. Gzipped test reports retain every simulated order, fill, event and valuation. manifest.json freezes the plan, input hashes and source hashes before evaluation.','']
    return '\n'.join(rows)


def synthetic_datasets(*,days=12,start='2026-08-17',seed=1729):
    """Engineering fixtures only. Weekdays are invented sessions, not a holiday calendar."""
    if type(days) is not int or not 2<=days<=40:raise ValueError('synthetic days must be2to40')
    randomizer=random.Random(seed);begin=datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    by_asset={'equity':[],'crypto':[]};sessions=[];prices={'SPY':500.,'QQQ':400.,'GLD':250.,'USO':70.,'BTC-USD':90000.}
    ny=ZoneInfo('America/New_York')
    for day in range(days):
        current=begin+timedelta(days=day);date=current.date().isoformat()
        if current.weekday()<5:
            opening=datetime(current.year,current.month,current.day,9,30,tzinfo=ny)
            ot=int(opening.timestamp());ct=ot+23400
            sessions.append({'session_date':date,'open_time':ot,'close_time':ct})
        else:ot=ct=None
        for symbol in prices:
            crypto=symbol=='BTC-USD'
            times=range(int(current.timestamp())+300,int(current.timestamp())+DAY+1,300) if crypto else range(ot+300,ct+1,300) if ot else []
            for index,when in enumerate(times):
                previous=prices[symbol]
                # Alternating range/trend/shock segments deliberately exercise gates; no fitted alpha.
                phase=(day//3)%3
                drift=.00015 if phase==1 else 0
                innovation=randomizer.gauss(0,.001 if phase!=2 else .003)
                if phase==0:innovation += -.0005*((index%20)-10)/10
                price=max(1.,previous*(1+drift+innovation));prices[symbol]=price
                high=max(price,previous)*1.0004;low=min(price,previous)*.9996
                by_asset['crypto' if crypto else 'equity'].append({
                    'symbol':symbol,'asset_class':'crypto' if crypto else 'equity','venue':'SYNTHETIC',
                    'event_time':when,'interval_seconds':300,'information_available_time':when,
                    'ingestion_time':when,'timezone':'UTC','session':'24/7' if crypto else 'regular',
                    'session_date':datetime.fromtimestamp(when-1,timezone.utc).date().isoformat() if crypto else date,
                    'open':f'{previous:.8f}','high':f'{high:.8f}','low':f'{low:.8f}','close':f'{price:.8f}',
                    'volume':str(1000+randomizer.randrange(4000)),'revision':0,'source_id':'multiasset-fixture',
                    'currency':'USD','volume_unit':'base_asset' if crypto else 'shares'})
    datasets=[]
    for asset,rows in by_asset.items():
        payload={'schema_version':'1.0','dataset_id':'multiasset-fixture-'+asset,'source':{
            'source_id':'multiasset-fixture','kind':'synthetic','description':'Deterministic engineering fixture; invented regimes and weekdays.'},
            'asset_class':asset,'symbols':sorted({r['symbol'] for r in rows}),'venue':'SYNTHETIC',
            'price_adjustment':'unadjusted','universe':{'selection_method':'fixed_engineering_fixture','survivorship_bias':'unknown'},
            'availability_basis':'synthetic','revision_policy':'synthetic','currency':'USD',
            'volume_unit':'shares' if asset=='equity' else 'base_asset',
            'bars':sorted(rows,key=lambda row:(row['information_available_time'],row['symbol'])),
            'calendar':{'calendar_id':'SYNTHETIC_WEEKDAYS','timezone':'America/New_York','version':'1',
                        'source_id':'multiasset-fixture','sessions':sessions} if asset=='equity' else {'calendar_id':'UTC_24_7','timezone':'UTC','version':'1'}}
        datasets.append(payload)
    return datasets
