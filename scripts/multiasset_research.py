"""Bounded local research; no broker execution, funding, workers or deployment."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from atlas.research.multiasset_lab import default_plan,run_campaign,synthetic_datasets,write_json
from atlas.research.data_adapters import verify_historical_archive


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--synthetic',action='store_true')
    source.add_argument('--archive',help='existing verified local Alpaca intake archive')
    parser.add_argument('--output-dir',required=True,help='new absolute directory outside the repository')
    parser.add_argument('--train-days',type=int)
    parser.add_argument('--validation-days',type=int)
    parser.add_argument('--test-days',type=int)
    parser.add_argument('--max-folds',type=int,default=3)
    parser.add_argument('--synthetic-days',type=int,default=12)
    parser.add_argument('--assume-archive-close',action='store_true',
        help='derive explicitly unverified retrospective close-time observations; source archive unchanged')
    args=parser.parse_args(argv)
    now=int(time.time())
    if args.synthetic:
        if args.assume_archive_close:parser.error('archive assumption only applies to archive input')
        datasets=synthetic_datasets(days=args.synthetic_days)
    else:
        verify_historical_archive(args.archive,now=now)
        datasets=[json.loads((Path(args.archive)/'dataset.json').read_text())]
    plan=default_plan(train_days=args.train_days or (2 if args.synthetic else 10),
                      validation_days=args.validation_days or (2 if args.synthetic else 5),
                      test_days=args.test_days or (2 if args.synthetic else 5),max_folds=args.max_folds)
    previous=[0]
    def progress(value):
        current=time.monotonic()
        if current-previous[0]>=15:
            print(json.dumps({'progress':value}),flush=True);previous[0]=current
    result=run_campaign(datasets,args.output_dir,plan=plan,now=now,
                        assume_archive_close=args.assume_archive_close,progress=progress)
    print(json.dumps({key:result[key] for key in ('evidence_class','fold_count','hypothesis_count',
        'evaluated_trial_windows','verified_OOS_survivors','synthetic_cash_remaining',
        'actual_orders_submitted','real_money_allocated')},indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
