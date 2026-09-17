#!/usr/bin/env python3
"""Lightweight timer gate, followed by bounded attempts at the unchanged check."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_guarded_check as guard

MIN_ELAPSED = 86460
MAX_FINAL_DELAY = 7200
MAX_ERRORS = 3
MAX_ATTEMPTS = 8
MAX_NO_BASELINE_SECONDS = 172800
BUSY_UNITS = ('atlas-collector-backup.service', 'atlas-collector-offsite.service',
              'apt-daily.service', 'apt-daily-upgrade.service')
PHASE = 'native_preflight'
SAFE_ERROR_TYPES = {'CollectionError', 'CollectionArchiveError', 'DataArchiveError',
                    'PermissionError', 'FileNotFoundError', 'OSError', 'ValueError',
                    'TypeError', 'KeyError', 'OperationalError', 'TimeoutExpired',
                    'CalledProcessError', 'RuntimeError'}
SAFE_ERROR_CODES = {'collector_private_path_required', 'collector_json_invalid',
                    'collector_state_unavailable_or_corrupt', 'archive_storage_unavailable_or_busy',
                    'archive_owner_type_or_permissions_invalid', 'snapshot_busy',
                    'collector_execution_configuration_refused', 'collector_configuration_changed_requires_explicit_migration',
                    'prebaseline_smoke_busy', 'prebaseline_smoke_output_bound',
                    'prebaseline_smoke_refused_after_baseline_or_terminal'}


class OwnedProbeFailure(Exception):
    def __init__(self, details):
        self.details = details


def safe_diagnostic(exc):
    result = {'infrastructure_24h_status': 'ERROR', 'operator_error': 'native_preflight_failed',
              'phase': PHASE, 'requires_attention': True, 'execution_enabled': False}
    name = type(exc).__name__
    result['error_type'] = name if name in SAFE_ERROR_TYPES else 'Exception'
    if isinstance(exc, OwnedProbeFailure):
        result.update(exc.details)
        return result
    if isinstance(exc, subprocess.CalledProcessError):
        result['returncode'] = exc.returncode
        raw = exc.stdout or ''
        if type(raw) is bytes:
            raw = raw.decode(errors='replace')
        if type(raw) is str and len(raw) <= 65536:
            try:
                owned = json.loads(raw)
                if type(owned) is dict:
                    if owned.get('error_type') in SAFE_ERROR_TYPES:
                        result['child_error_type'] = owned['error_type']
                    if owned.get('error_code') in SAFE_ERROR_CODES:
                        result['child_error_code'] = owned['error_code']
                    if owned.get('infrastructure_24h_status') in {'PASS', 'PENDING', 'FAIL', 'ERROR'}:
                        result['child_status'] = owned['infrastructure_24h_status']
            except (ValueError, TypeError):
                pass
    elif isinstance(exc, subprocess.TimeoutExpired):
        result['timed_out'] = True
    elif len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in SAFE_ERROR_CODES:
        result['error_code'] = exc.args[0]
    return result


def capture_probe():
    """Fixed owned diagnostic: no exception text, local variables or raw data."""
    code = """import json,os,sys
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,%r)
out={'child_uid':os.getuid(),'child_euid':os.geteuid(),'child_gid':os.getgid()}
status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
out['child_capabilities']={key:int(status[key].strip(),16) for key in ('CapEff','CapPrm','CapAmb')}
try:
 import check_unattended
 current=check_unattended.capture()
 out.update(probe_status='PASS',health_status=current['health']['status'],archive_integrity=current['health']['archive']['integrity'])
except Exception as exc:
 out['probe_status']='ERROR'
 name=type(exc).__name__
 if name in %r:out['child_error_type']=name
 if len(exc.args)==1 and type(exc.args[0]) is str and exc.args[0] in %r:out['child_error_code']=exc.args[0]
print(json.dumps(out,sort_keys=True))
""" % (str(guard.SCRIPTS), SAFE_ERROR_TYPES, SAFE_ERROR_CODES)
    run = subprocess.run(['/usr/sbin/runuser', '--user', 'atlas-collector', '--',
                          '/usr/bin/python3', '-I', '-c', code],
                         capture_output=True, text=True, timeout=300, check=True)
    if len(run.stdout) > 4096:
        raise ValueError('prebaseline_smoke_output_bound')
    owned = json.loads(run.stdout)
    details = {key: owned[key] for key in ('child_uid', 'child_euid', 'child_gid')
               if type(owned.get(key)) is int and 0 <= owned[key] <= 2**32 - 1}
    caps = owned.get('child_capabilities')
    if (type(caps) is dict and set(caps) == {'CapEff', 'CapPrm', 'CapAmb'}
            and all(type(value) is int and 0 <= value < 2**64 for value in caps.values())):
        details['child_capabilities'] = caps
    if owned.get('child_error_type') in SAFE_ERROR_TYPES:
        details['child_error_type'] = owned['child_error_type']
    if owned.get('child_error_code') in SAFE_ERROR_CODES:
        details['child_error_code'] = owned['child_error_code']
    if owned.get('probe_status') != 'PASS':
        raise OwnedProbeFailure(details)
    return {**details, 'probe_status': 'PASS', 'health_status': owned.get('health_status'),
            'archive_integrity': owned.get('archive_integrity')}


def error(reason, *, base_sha=None):
    return guard.terminal({'infrastructure_24h_status': 'ERROR', 'operator_error': reason,
                           'requires_attention': True, 'execution_enabled': False},
                          origin='native_final_check', baseline_sha=base_sha)


def first_and_second_scheduled_backup(started):
    first = int(started // 86400) * 86400 + 4 * 3600 + 15 * 60
    if first <= started:
        first += 86400
    return first, first + 86400


def busy_units():
    run = subprocess.run(['/usr/bin/systemctl', 'show', *BUSY_UNITS,
        '--no-pager', '--property=Id,LoadState,ActiveState'], check=True,
        capture_output=True, text=True, timeout=10)
    if len(run.stdout) > 16384:
        raise ValueError('operator_unit_output_bound')
    units = {}
    for block in run.stdout.strip().split('\n\n'):
        row = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if row.get('LoadState') != 'loaded':
            raise ValueError('operator_unit_not_loaded')
        units[row['Id']] = row['ActiveState']
    if set(units) != set(BUSY_UNITS):
        raise ValueError('operator_unit_set_mismatch')
    return [name for name, state in units.items() if state not in {'inactive', 'failed'}]


def smoke_locked():
    """Exercise the real checker sandbox before publishing the actual baseline."""
    global PHASE
    PHASE = 'smoke_preflight'
    if guard.BASELINE.exists() or guard.TERMINAL.exists():
        raise ValueError('prebaseline_smoke_refused_after_baseline_or_terminal')
    if busy_units():
        raise ValueError('prebaseline_smoke_busy')
    PHASE = 'smoke_capture_probe'
    probe = capture_probe()
    prefix = 'prebaseline-smoke-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:8]
    smoke_base, capture, history = (guard.WORK / (prefix + '-' + name + '.json')
                                    for name in ('baseline', 'capture', 'history'))
    def invoke(name, args, collector=True):
        command = ['/usr/bin/python3', '-I', str(guard.SCRIPTS / name), *map(str, args)]
        if collector:
            command = ['/usr/sbin/runuser', '--user', 'atlas-collector', '--', *command]
        run = subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
        if len(run.stdout) > 16 * 1024 * 1024:
            raise ValueError('prebaseline_smoke_output_bound')
        return json.loads(run.stdout)
    PHASE = 'smoke_baseline_capture'
    invoke('check_unattended.py', ['--baseline-out', smoke_base])
    PHASE = 'smoke_current_capture'
    invoke('check_unattended.py', ['--baseline', smoke_base, '--capture-out', capture])
    PHASE = 'smoke_service_history'
    exported = invoke('unattended_service_history.py', ['--baseline', smoke_base, '--current', capture], collector=False)
    guard.save_new(history, exported)
    account = pwd.getpwnam('atlas-collector')
    os.chown(history, account.pw_uid, account.pw_gid)
    PHASE = 'smoke_assessment'
    assessment = invoke('check_unattended.py', ['--baseline', smoke_base, '--current', capture,
                                              '--service-history', history])
    original, base_sha = guard.read_private(smoke_base, uid=account.pw_uid)
    result = {'smoke_status': 'PASS' if (assessment['infrastructure_24h_status'] == 'PENDING'
        and not assessment['failures'] and original['health']['archive']['integrity'] == 'PASS') else 'FAIL',
        'assessment_status': assessment['infrastructure_24h_status'], 'failures': assessment['failures'],
        'pending': assessment['pending'], 'baseline_archive_integrity': original['health']['archive']['integrity'],
        'original_baseline_path': str(smoke_base), 'smoke_baseline_sha256': base_sha,
        'actual_baseline_created': False, 'qualification_established': False, 'execution_enabled': False,
        'sandbox_capture_probe': probe}
    receipt = guard.STATE / (prefix + '-result.json')
    guard.save_new(receipt, result)
    return {**result, 'smoke_receipt': str(receipt)}


def native_locked(now=None):
    now = time.time() if now is None else now
    cached = guard.terminal_cached()
    if cached:
        return cached
    activation = guard.STATE / 'native-activation.json'
    if not activation.exists():
        guard.save_new(activation, {'first_run_at': now})
    activated = guard.read_private(activation, limit=4096)[0]['first_run_at']
    if not guard.BASELINE.exists():
        if now - activated > MAX_NO_BASELINE_SECONDS:
            return error('baseline_not_created_within_48_hours')
        return {'infrastructure_24h_status': 'PENDING', 'operator_state': 'baseline_not_created'}
    base, base_sha = guard.baseline()
    identity = guard.STATE / 'native-baseline-identity.json'
    if not identity.exists():
        guard.save_new(identity, {'baseline_sha256': base_sha, 'captured_at': base['captured_at']})
    recorded = guard.read_private(identity, limit=4096)[0]
    if recorded != {'baseline_sha256': base_sha, 'captured_at': base['captured_at']}:
        return error('baseline_identity_changed', base_sha=base_sha)
    started = base['captured_at']
    due = started + MIN_ELAPSED
    _, second = first_and_second_scheduled_backup(started)
    # The original acceptance script rejects a second backup invocation. Leave
    # ten minutes for this bounded check to finish before the earliest second run.
    deadline = min(started + 86400 + MAX_FINAL_DELAY, second - 600)
    if due >= deadline:
        return error('baseline_has_no_safe_final_check_window', base_sha=base_sha)
    if now < due:
        return {'infrastructure_24h_status': 'PENDING', 'operator_state': 'waiting_for_elapsed_day',
                'elapsed_seconds': now - started, 'final_check_not_before': due}
    if now >= deadline:
        return error('final_check_window_expired', base_sha=base_sha)
    attempts = sorted(guard.STATE.glob('native-attempt-*-started.json'))
    if len(attempts) >= MAX_ATTEMPTS:
        return error('native_attempt_limit_reached', base_sha=base_sha)
    errors = 0
    for item in attempts:
        prior, _ = guard.read_private(item, limit=4096)
        completed = item.with_name(item.name.replace('-started.json', '-result.json'))
        status = guard.read_private(completed, limit=256 * 1024)[0]['result']['infrastructure_24h_status'] if completed.exists() else 'ERROR'
        errors += status == 'ERROR'
    if errors >= MAX_ERRORS:
        return error('native_checker_failed_three_times', base_sha=base_sha)
    try:
        busy = busy_units()
    except Exception:
        busy = None
    if busy:
        return {'infrastructure_24h_status': 'PENDING', 'operator_state': 'maintenance_or_backup_busy',
                'busy_units': busy}
    number = len(attempts) + 1
    record = guard.STATE / ('native-attempt-%02d-started.json' % number)
    guard.save_new(record, {'attempt': number, 'started_at': now, 'baseline_sha256': base_sha})
    if busy is None:
        result = {'infrastructure_24h_status': 'ERROR', 'operator_error': 'unit_status_unavailable'}
    else:
        try:
            result = guard.check_locked(origin='native_final_check')
        except Exception:
            result = {'infrastructure_24h_status': 'ERROR', 'operator_error': 'guard_failed'}
    guard.save_new(record.with_name(record.name.replace('-started.json', '-result.json')),
                   {'attempt': number, 'finished_at': time.time(), 'result': result})
    if result['infrastructure_24h_status'] == 'ERROR' and errors + 1 >= MAX_ERRORS:
        return error('native_checker_failed_three_times', base_sha=base_sha)
    return result


def main():
    global PHASE
    try:
        PHASE = 'validate_operator_paths'
        guard.validate_paths()
        PHASE = 'acquire_operator_lock'
        with guard.operator_lock():
            if sys.argv[1:] == ['--smoke']:
                result = smoke_locked()
            elif sys.argv[1:]:
                raise ValueError('native_arguments_invalid')
            else:
                PHASE = 'native_elapsed_gate'
                result = native_locked()
    except BlockingIOError:
        result = {'infrastructure_24h_status': 'PENDING', 'operator_state': 'another_check_running'}
    except Exception as exc:
        result = safe_diagnostic(exc)
        try:
            path = guard.STATE / ('native-error-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:8] + '.json')
            guard.save_new(path, result)
            result['diagnostic_receipt'] = str(path)
        except Exception:
            pass
    print(json.dumps(result, sort_keys=True))
    return int(result.get('infrastructure_24h_status') == 'ERROR' or result.get('smoke_status') == 'FAIL')


if __name__ == '__main__':
    raise SystemExit(main())
