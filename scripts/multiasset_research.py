"""Bounded local research; no broker execution, funding, workers or deployment."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from atlas.research.multiasset_lab import default_plan,run_campaign,synthetic_datasets
from atlas.research.data_adapters import verify_historical_archive
from atlas.research.campaign_preflight import validate_campaign_plan
from atlas.research.campaign_planning import plan_campaign
from atlas.research.imports import _external_path, _read, _json
from atlas.research.market import MAX_DATASET_BYTES, validate_dataset

MAX_PLAN_BYTES = 256 * 1024


def load_plan(path):
    """Bounded, strict, explicitly selected external JSON; no credential discovery."""
    plan = _json(_read(_external_path(path), max_bytes=MAX_PLAN_BYTES))
    validate_campaign_plan(plan)
    return plan


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--synthetic',action='store_true')
    source.add_argument('--archive',help='existing verified local Alpaca intake archive')
    parser.add_argument('--output-dir',help='required for a run; forbidden for read-only preflight')
    parser.add_argument('--preflight',action='store_true',help='print input coverage and fold feasibility without replay or file creation')
    parser.add_argument('--plan',help='explicit external campaign JSON; incompatible with window overrides')
    parser.add_argument('--train-days',type=int)
    parser.add_argument('--validation-days',type=int)
    parser.add_argument('--test-days',type=int)
    parser.add_argument('--max-folds',type=int)
    parser.add_argument('--synthetic-days',type=int)
    parser.add_argument('--assume-archive-close',action='store_true',
        help='derive explicitly unverified retrospective close-time observations; source archive unchanged')
    args=parser.parse_args(argv)
    if args.preflight and args.output_dir is not None:
        parser.error('--output-dir cannot be used with --preflight; the report is printed to stdout')
    if not args.preflight and args.output_dir is None:
        parser.error('--output-dir is required unless --preflight is selected')
    if args.plan and any(v is not None for v in (args.train_days,args.validation_days,args.test_days,args.max_folds)):
        parser.error('--plan cannot be combined with window or fold overrides')
    if args.synthetic and args.assume_archive_close:
        parser.error('archive assumption only applies to archive input')
    if args.archive and args.synthetic_days is not None:
        parser.error('--synthetic-days only applies to synthetic input')
    now=int(time.time())
    try:
        plan=load_plan(args.plan) if args.plan else default_plan(
            train_days=args.train_days if args.train_days is not None else (2 if args.synthetic else 10),
            validation_days=args.validation_days if args.validation_days is not None else (2 if args.synthetic else 5),
            test_days=args.test_days if args.test_days is not None else (2 if args.synthetic else 5),
            max_folds=args.max_folds if args.max_folds is not None else 3)
        validate_campaign_plan(plan)  # Reject an invalid plan before touching the selected archive.
        integrity=None
        if args.synthetic:
            datasets=synthetic_datasets(days=args.synthetic_days if args.synthetic_days is not None else 12)
        else:
            integrity=verify_historical_archive(args.archive,now=now)
            payload=_json(_read(_external_path(Path(args.archive)/'dataset.json'),max_bytes=MAX_DATASET_BYTES))
            # Bind the exact payload being planned/run to the verified archive, even if it changes after verification.
            if validate_dataset(payload,now=now)['dataset_hash'] != integrity['dataset_hash']:
                raise ValueError('historical archive dataset changed after verification')
            datasets=[payload]
        if args.preflight:
            report=plan_campaign(datasets,plan=plan,now=now,assume_archive_close=args.assume_archive_close)
            report['archive_integrity']=integrity
            print(json.dumps(report,indent=2,allow_nan=False))
            return 0
    except (ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
        parser.error(str(exc))
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
