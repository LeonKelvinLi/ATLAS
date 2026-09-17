#!/usr/bin/env python3
"""Serialize the unchanged read-only checker and retain terminal evidence once."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import time
import uuid

SCRIPTS = Path('/usr/local/libexec/atlas-checks-20260917-diagnostics')
WORK = Path('/var/lib/atlas-operator-20260917-diagnostics')
STATE = Path('/var/lib/atlas-final-check-20260917-diagnostics')
LOCK = Path('/run/lock/atlas-validation-20260917-diagnostics.lock')
BASELINE = WORK / 'baseline.json'
TERMINAL = STATE / 'terminal.json'
MAX_JSON = 16 * 1024 * 1024
CHECK_TIMEOUT = 420


def checked(path, uid=0, mode=None, directory=False):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('operator_path_not_plain_absolute')
    info = path.stat()
    if (info.st_uid != uid or mode is not None and stat.S_IMODE(info.st_mode) != mode
            or not (stat.S_ISDIR(info.st_mode) if directory else
                    stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
        raise ValueError('operator_owner_mode_or_type_invalid')
    return info


def validate_paths():
    if os.geteuid() != 0:
        raise PermissionError('operator_requires_root')
    checked(STATE, mode=0o700, directory=True)
    checked(SCRIPTS, mode=0o755, directory=True)
    for name in ('run_unattended_check.py', 'check_unattended.py',
                 'unattended_service_history.py', 'run_guarded_check.py',
                 'native_final_check.py'):
        info = checked(SCRIPTS / name)
        if stat.S_IMODE(info.st_mode) not in {0o644, 0o755}:
            raise ValueError('operator_script_mode_invalid')


@contextmanager
def operator_lock():
    checked(LOCK, mode=0o600)
    fd = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError('operator_lock_invalid')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def read_private(path, uid=0, limit=MAX_JSON):
    if checked(path, uid=uid, mode=0o600).st_size > limit:
        raise ValueError('operator_json_bound')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('operator_json_bound')
    result = json.loads(raw)
    if type(result) is not dict:
        raise ValueError('operator_json_object_required')
    return result, hashlib.sha256(raw).hexdigest()


def save_new(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    if len(raw) > 256 * 1024:
        raise ValueError('operator_receipt_bound')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def baseline():
    owner = pwd.getpwnam('atlas-collector').pw_uid
    value, digest = read_private(BASELINE, uid=owner)
    started = value.get('captured_at')
    if type(started) not in (int, float) or not math.isfinite(started) or started <= 0:
        raise ValueError('operator_baseline_time_invalid')
    return value, digest


def terminal_cached():
    if TERMINAL.exists():
        result, _ = read_private(TERMINAL, limit=256 * 1024)
        return {**result['result'], 'cached_terminal': True,
                'terminal_recorded_at': result['recorded_at'], 'terminal_path': str(TERMINAL)}
    return None


def terminal(result, *, origin, baseline_sha=None, assessment_sha=None):
    if result.get('infrastructure_24h_status') not in {'PASS', 'FAIL', 'ERROR'}:
        raise ValueError('operator_terminal_status_invalid')
    value = {'version': 1, 'recorded_at': datetime.now(timezone.utc).isoformat(),
             'origin': origin, 'baseline_sha256': baseline_sha,
             'assessment_sha256': assessment_sha, 'result': result,
             'physical_mac_power_state': 'UNKNOWN_REQUIRES_USER_ATTESTATION'}
    save_new(TERMINAL, value)
    return {**result, 'terminal_path': str(TERMINAL)}


def run_wrapper():
    """Bound only this operator process group; never signal the collector."""
    process = subprocess.Popen(['/usr/bin/python3', '-I', str(SCRIPTS / 'run_unattended_check.py')],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        return {'infrastructure_24h_status': 'ERROR', 'operator_error': 'checker_timeout',
                'execution_enabled': False}
    if len(stdout) > 256 * 1024:
        raise ValueError('operator_wrapper_output_bound')
    result = json.loads(stdout)
    if type(result) is not dict or result.get('infrastructure_24h_status') not in {'PASS', 'FAIL', 'PENDING', 'ERROR'}:
        raise ValueError('operator_wrapper_result_invalid')
    if process.returncode and result['infrastructure_24h_status'] != 'ERROR':
        raise ValueError('operator_wrapper_exit_mismatch')
    return result


def check_locked(*, origin):
    cached = terminal_cached()
    if cached:
        return cached
    if not BASELINE.exists():
        return {'infrastructure_24h_status': 'PENDING', 'operator_state': 'baseline_not_created',
                'execution_enabled': False}
    _, base_sha = baseline()
    try:
        result = run_wrapper()
        assessment_sha = None
        if result.get('result_path'):
            path = Path(result['result_path'])
            if path.parent != WORK or not path.name.startswith('check-'):
                raise ValueError('operator_assessment_path_invalid')
            assessment, assessment_sha = read_private(path, uid=pwd.getpwnam('atlas-collector').pw_uid)
            if assessment.get('infrastructure_24h_status') != result['infrastructure_24h_status']:
                raise ValueError('operator_assessment_status_mismatch')
        if result['infrastructure_24h_status'] in {'PASS', 'FAIL'} and not assessment_sha:
            raise ValueError('operator_terminal_assessment_missing')
        if baseline()[1] != base_sha:
            raise ValueError('operator_baseline_changed_during_check')
    except Exception as exc:
        result = {'infrastructure_24h_status': 'ERROR', 'operator_error': 'checker_failed',
                  'error_type': type(exc).__name__ if type(exc).__module__ == 'builtins' else 'Exception',
                  'execution_enabled': False}
        assessment_sha = None
    receipt = STATE / ('guard-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:8] + '.json')
    save_new(receipt, {'origin': origin, 'recorded_at': time.time(), 'baseline_sha256': base_sha,
                       'assessment_sha256': assessment_sha, 'result': result})
    if result['infrastructure_24h_status'] in {'PASS', 'FAIL'}:
        return terminal(result, origin=origin, baseline_sha=base_sha, assessment_sha=assessment_sha)
    return {**result, 'guard_receipt': str(receipt)}


def main():
    try:
        validate_paths()
        with operator_lock():
            result = check_locked(origin='operator_guard')
    except BlockingIOError:
        result = {'infrastructure_24h_status': 'PENDING', 'operator_state': 'another_check_running',
                  'execution_enabled': False}
    except Exception:
        result = {'infrastructure_24h_status': 'ERROR', 'operator_error': 'guard_preflight_failed',
                  'execution_enabled': False}
    print(json.dumps(result, sort_keys=True))
    return int(result['infrastructure_24h_status'] == 'ERROR')


if __name__ == '__main__':
    raise SystemExit(main())
