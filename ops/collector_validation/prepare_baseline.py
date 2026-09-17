import argparse,fcntl,hashlib,json,os,re,subprocess,sys,time
from pathlib import Path
ROOT=Path('/usr/local/libexec/atlas-checks-20260917-diagnostics')
WORK=Path('/var/lib/atlas-operator-20260917-diagnostics')
STATE=Path('/var/lib/atlas-final-check-20260917-diagnostics')
BASELINE=WORK/'baseline.json'
UNIT='atlas-validation-final-20260917-diagnostics'
parser=argparse.ArgumentParser(description='Create one immutable post-maintenance validation baseline.')
parser.add_argument('--expected-release', required=True)
args=parser.parse_args()
if not re.fullmatch(r'[0-9a-f]{16}', args.expected_release):
    parser.error('--expected-release must be the verified 16-character release identity')
if BASELINE.exists() or (STATE/'terminal.json').exists():
    raise SystemExit('baseline_or_terminal_already_exists')
if Path('/opt/atlas-collector/current').resolve().name!=args.expected_release:
    raise SystemExit('release_mismatch')
if Path('/var/run/reboot-required').exists():
    raise SystemExit('reboot_still_required')
if subprocess.check_output(['timedatectl','show','--property=NTPSynchronized','--value'],text=True).strip()!='yes':
    raise SystemExit('ntp_not_synchronized')
for unit in ('atlas-collector-backup.service','atlas-collector-offsite.service','apt-daily.service','apt-daily-upgrade.service',UNIT+'-smoke.service'):
    if subprocess.check_output(['systemctl','show',unit,'-p','ActiveState','--value'],text=True).strip()!='inactive':
        raise SystemExit('busy:'+unit)
reports=[json.loads(p.read_text()) for p in STATE.glob('prebaseline-smoke-*-result.json')]
if not any(r.get('smoke_status')=='PASS' and not r.get('failures') for r in reports):
    raise SystemExit('sandbox_smoke_pass_required')
# The timer is installed and tested before the immutable actual baseline.
subprocess.run(['systemctl','start',UNIT+'.service'],check=True,timeout=30)
subprocess.run(['systemctl','enable','--now',UNIT+'.timer'],check=True,timeout=30)
lockfd=os.open('/run/lock/atlas-validation-20260917-diagnostics.lock',os.O_RDWR|os.O_NOFOLLOW)
fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
now=time.time(); next_backup=int(now//86400)*86400+4*3600+15*60
if 0<next_backup-now<1800: raise SystemExit('wait_until_daily_backup_offsite_finish')
code="import sys,json;sys.path.insert(0,'/usr/local/libexec/atlas-checks-20260917-diagnostics');import check_unattended;print(json.dumps(check_unattended.capture()))"
current=json.loads(subprocess.check_output(['/usr/sbin/runuser','--user','atlas-collector','--','/usr/bin/python3','-I','-c',code],text=True,timeout=180))
if (current['health']['status']!='HEALTHY' or current['health']['archive']['integrity']!='PASS'
    or not current['capture_stable']
    or any(v['consecutive_failures'] or v['catchup_pending'] for v in current['health']['providers'].values())):
    raise SystemExit('baseline_preflight_not_healthy')
subprocess.run(['/usr/sbin/runuser','--user','atlas-collector','--','/usr/bin/python3','-I',str(ROOT/'check_unattended.py'),'--baseline-out',str(BASELINE)],check=True,capture_output=True,text=True,timeout=180)
raw=BASELINE.read_bytes(); baseline=json.loads(raw)
if (baseline['health']['status']!='HEALTHY' or baseline['health']['archive']['integrity']!='PASS'
    or not baseline['capture_stable']
    or set(baseline['health']['providers'])!={'alpaca','coinbase'}
    or any(v['consecutive_failures'] or v['catchup_pending'] for v in baseline['health']['providers'].values())
    or baseline['boot_id']!=current['boot_id'] or baseline['config_sha256']!=current['config_sha256']
    or baseline['collector']['InvocationID']!=current['collector']['InvocationID']):
    raise SystemExit('recorded_baseline_requires_review_do_not_overwrite')
print(json.dumps({'baseline_path':str(BASELINE),'sha256':hashlib.sha256(raw).hexdigest(),'baseline':baseline,'timer_enabled':True,'earliest_finish_epoch':baseline['captured_at']+86400,'native_final_not_before_epoch':baseline['captured_at']+86460}))
