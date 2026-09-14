"""Bounded accelerated, synthetic collection benchmark; never contacts providers."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import resource
import sys
import time
from urllib.parse import parse_qs,urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from atlas.collection.archive import init_collection_archive,acquire_collection_once,verify_collection_archive
from atlas.collection.primitives import HTTPResponse,_encoded
from atlas.research.data_contract_v1 import default_contract


def rss_bytes():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform=='darwin' else 1024)


def run(output_dir, minutes=120):
    if not 1<=minutes<=1440:
        raise ValueError('benchmark_minutes_bound')
    output=Path(output_dir)
    if not output.is_absolute() or output.exists():
        raise ValueError('benchmark_requires_new_absolute_directory')
    output.mkdir(mode=0o700)
    # Monday September 14, 2026 at 09:30 New York, a declared synthetic session.
    opening=1789392600
    current=[opening]
    root=output/'archive'
    idle=rss_bytes()
    init_collection_archive(root,default_contract('synthetic-capacity',opening//86400*86400,opening),clock=lambda:current[0])
    total_response=0
    def get(url,headers,**limits):
        query=parse_qs(urlsplit(url).query)
        now=current[0]
        if '/calendar?' in url:
            body=[{'date':'2026-09-14','open':'09:30','close':'16:00'}]
        elif '/quotes/latest?' in url:
            body={'quotes':{s:{'bp':100,'ap':101,'bs':1,'as':1,'t':datetime.fromtimestamp(now,timezone.utc).isoformat()} for s in ('GLD','QQQ','SPY','USO')}}
        elif '/book?' in url:
            body={'sequence':now,'bids':[['100','1',1]],'asks':[['101','1',1]],'time':datetime.fromtimestamp(now,timezone.utc).isoformat()}
        else:
            start=int(datetime.fromisoformat(query['start'][0].replace('Z','+00:00')).timestamp())
            end=int(datetime.fromisoformat(query['end'][0].replace('Z','+00:00')).timestamp())
            if '/stocks/bars?' in url:
                rows=[{'t':datetime.fromtimestamp(t,timezone.utc).isoformat(),'o':100,'h':101,'l':99,'c':100,'v':20} for t in range(max(start,opening),min(end+1,opening+23400),300)]
                body={'bars':{s:rows for s in ('GLD','QQQ','SPY','USO')},'next_page_token':None}
            else:
                body=[[t,99,101,100,100,20] for t in range(start,end,300)]
        return HTTPResponse(200,_encoded(body))
    start_wall=time.monotonic()
    start_cpu=time.process_time()
    latencies=[]
    for minute in range(1,minutes+1):
        current[0]=opening+minute*60
        end=current[0]//300*300
        began=time.monotonic()
        for provider in ('alpaca','coinbase'):
            result=acquire_collection_once(root,provider=provider,start=end-900,end=end,
                transport=get,clock=lambda:current[0])
            if not result['collection_complete']:
                raise ValueError('benchmark_fixture_collection_failed')
            total_response+=result['response_bytes']
        latencies.append(time.monotonic()-began)
    elapsed=time.monotonic()-start_wall
    cpu=time.process_time()-start_cpu
    summary=verify_collection_archive(root,clock=lambda:current[0])
    disk=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
    result={'mode':'accelerated_synthetic_no_network','simulated_minutes':minutes,'idle_peak_rss_bytes':idle,
            'collection_peak_rss_bytes':rss_bytes(),'elapsed_wall_seconds':elapsed,'cpu_seconds':cpu,
            'cpu_seconds_per_simulated_minute':cpu/minutes,'mean_cpu_core_fraction_at_60_second_poll':cpu/(minutes*60),
            'max_two_provider_cycle_seconds':max(latencies),'last_two_provider_cycle_seconds':latencies[-1],
            'archive_bytes':disk,'estimated_archive_bytes_per_day':disk*1440/minutes,
            'estimated_archive_bytes_per_30_days':disk*43200/minutes,'synthetic_response_bytes':total_response,
            'estimated_response_bytes_per_day':total_response*1440/minutes,'actual_provider_network_bytes':0,
            'estimate_limitations':['synthetic active-session responses; not observed VPS uptime','linear disk projection excludes historical imports and backups','RSS is process peak; idle value is import baseline, not machine RAM','growing-shard append cost is measured only through this simulated duration'],
            'archive_summary':summary,'orders_submitted':0}
    (output/'benchmark.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result,indent=2,sort_keys=True))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--minutes',type=int,default=120)
    args=parser.parse_args()
    run(args.output_dir,args.minutes)
