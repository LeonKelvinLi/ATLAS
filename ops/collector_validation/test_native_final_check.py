import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import native_final_check as native
import run_guarded_check as guard


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    state, work = tmp_path / 'state', tmp_path / 'work'
    state.mkdir(mode=0o700); work.mkdir(mode=0o700)
    monkeypatch.setattr(guard, 'STATE', state)
    monkeypatch.setattr(guard, 'WORK', work)
    monkeypatch.setattr(guard, 'BASELINE', work / 'baseline.json')
    monkeypatch.setattr(guard, 'TERMINAL', state / 'terminal.json')
    original_checked = guard.checked
    monkeypatch.setattr(guard, 'checked', lambda path, uid=0, **kw:
                        original_checked(path, uid=os.getuid(), **kw))
    monkeypatch.setattr(native, 'busy_units', lambda: [])
    def read_base():
        raw = guard.BASELINE.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(guard, 'baseline', read_base)
    started = 1789516800.0  # 2026-09-16 00:00 UTC, safely before the daily timer.
    def baseline(at=started):
        guard.save_new(guard.BASELINE, {'captured_at': at})
        return at
    return state, baseline, started


def test_before_baseline_does_not_invoke_original_wrapper(sandbox, monkeypatch):
    monkeypatch.setattr(guard, 'check_locked', lambda **_: pytest.fail('must not run'))
    assert native.native_locked(sandbox[2])['operator_state'] == 'baseline_not_created'
    assert not guard.TERMINAL.exists()


def test_before_elapsed_day_is_lightweight(sandbox, monkeypatch):
    _, make, start = sandbox; make()
    monkeypatch.setattr(guard, 'check_locked', lambda **_: pytest.fail('must not run'))
    result = native.native_locked(start + 86459)
    assert result['operator_state'] == 'waiting_for_elapsed_day'
    assert not list(guard.STATE.glob('native-attempt-*'))


def test_final_pass_retained_once_and_then_cached(sandbox, monkeypatch):
    _, make, start = sandbox; make(); calls = []
    def checker(**kwargs):
        calls.append(kwargs)
        return guard.terminal({'infrastructure_24h_status': 'PASS', 'elapsed_seconds': 86460},
                              origin='native_final_check')
    monkeypatch.setattr(guard, 'check_locked', checker)
    assert native.native_locked(start + 86460)['infrastructure_24h_status'] == 'PASS'
    first = guard.TERMINAL.read_bytes()
    assert native.native_locked(start + 200000)['cached_terminal'] is True
    assert len(calls) == 1 and guard.TERMINAL.read_bytes() == first


def test_fail_is_terminal_not_retried(sandbox, monkeypatch):
    _, make, start = sandbox; make()
    monkeypatch.setattr(guard, 'check_locked', lambda **_:
                        guard.terminal({'infrastructure_24h_status': 'FAIL'}, origin='test'))
    assert native.native_locked(start + 86460)['infrastructure_24h_status'] == 'FAIL'
    assert native.native_locked(start + 87000)['cached_terminal'] is True


def test_three_transient_errors_exhaust_retry_budget(sandbox, monkeypatch):
    _, make, start = sandbox; make(); calls = []
    monkeypatch.setattr(guard, 'check_locked', lambda **_:
                        calls.append(1) or {'infrastructure_24h_status': 'ERROR'})
    for offset in (86460, 87360):
        native.native_locked(start + offset)
        assert not guard.TERMINAL.exists()
    result = native.native_locked(start + 88260)
    assert result['operator_error'] == 'native_checker_failed_three_times'
    native.native_locked(start + 89160)
    assert len(calls) == 3


def test_process_crash_reservation_counts_as_error(sandbox, monkeypatch):
    state, make, start = sandbox; make()
    for number in range(1, 4):
        guard.save_new(state / ('native-attempt-%02d-started.json' % number), {'attempt': number})
    monkeypatch.setattr(guard, 'check_locked', lambda **_: pytest.fail('must not run'))
    assert native.native_locked(start + 86460)['operator_error'] == 'native_checker_failed_three_times'


def test_busy_backup_skips_without_spending_attempt(sandbox, monkeypatch):
    _, make, start = sandbox; make()
    monkeypatch.setattr(native, 'busy_units', lambda: ['atlas-collector-offsite.service'])
    monkeypatch.setattr(guard, 'check_locked', lambda **_: pytest.fail('must not run'))
    assert native.native_locked(start + 86460)['operator_state'] == 'maintenance_or_backup_busy'
    assert not list(guard.STATE.glob('native-attempt-*'))


def test_unavailable_unit_status_spends_bounded_error_attempt(sandbox, monkeypatch):
    _, make, start = sandbox; make()
    monkeypatch.setattr(native, 'busy_units', lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(guard, 'check_locked', lambda **_: pytest.fail('must not run'))
    for offset in (86460, 87360, 88260):
        result = native.native_locked(start + offset)
    assert result['operator_error'] == 'native_checker_failed_three_times'


def test_baseline_mutation_refused_without_overwriting(sandbox):
    _, make, start = sandbox; make()
    native.native_locked(start + 60)
    guard.BASELINE.write_text(json.dumps({'captured_at': start + 1}))
    changed = guard.BASELINE.read_bytes()
    assert native.native_locked(start + 86460)['operator_error'] == 'baseline_identity_changed'
    assert guard.BASELINE.read_bytes() == changed


def test_final_window_expires_before_more_full_checks(sandbox, monkeypatch):
    _, make, start = sandbox; make()
    monkeypatch.setattr(guard, 'check_locked', lambda **_: pytest.fail('must not run'))
    assert native.native_locked(start + 93600)['operator_error'] == 'final_check_window_expired'


def test_baseline_too_close_to_daily_backup_is_flagged(sandbox, monkeypatch):
    _, make, start = sandbox; make(start + 4 * 3600 + 14 * 60)
    monkeypatch.setattr(guard, 'check_locked', lambda **_: pytest.fail('must not run'))
    result = native.native_locked(start + 4 * 3600 + 14 * 60)
    assert result['operator_error'] == 'baseline_has_no_safe_final_check_window'


def test_missing_baseline_timeout_is_terminal(sandbox):
    start = sandbox[2]
    native.native_locked(start)
    result = native.native_locked(start + native.MAX_NO_BASELINE_SECONDS + 1)
    assert result['operator_error'] == 'baseline_not_created_within_48_hours'


def test_terminal_cannot_be_overwritten(sandbox):
    guard.terminal({'infrastructure_24h_status': 'FAIL'}, origin='test')
    old = guard.TERMINAL.read_bytes()
    with pytest.raises(FileExistsError):
        guard.terminal({'infrastructure_24h_status': 'PASS'}, origin='test')
    assert guard.TERMINAL.read_bytes() == old


def test_real_flock_serializes_guard_instances(sandbox, monkeypatch):
    path = guard.STATE / 'lock'
    guard.save_new(path, {})
    monkeypatch.setattr(guard, 'LOCK', path)
    original_fstat = os.fstat
    from types import SimpleNamespace
    def root_stat(fd):
        st = original_fstat(fd)
        return SimpleNamespace(st_uid=0, st_mode=st.st_mode, st_nlink=st.st_nlink)
    monkeypatch.setattr(guard.os, 'fstat', root_stat)
    with guard.operator_lock():
        with pytest.raises(BlockingIOError):
            with guard.operator_lock():
                pytest.fail('second lock acquired')
    with guard.operator_lock():
        pass


def test_guard_retains_authenticated_wrapper_assessment(sandbox, monkeypatch):
    _, make, start = sandbox; make()
    from types import SimpleNamespace
    monkeypatch.setattr(guard.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=os.getuid()))
    assessment = guard.WORK / 'check-real-assessment.json'
    data = {'infrastructure_24h_status': 'PASS', 'elapsed_seconds': 86460,
            'scheduled_offsite_proven': True, 'failures': [], 'pending': []}
    guard.save_new(assessment, data)
    monkeypatch.setattr(guard, 'run_wrapper', lambda: {**data, 'result_path': str(assessment)})
    result = guard.check_locked(origin='test')
    assert result['infrastructure_24h_status'] == 'PASS'
    retained, _ = guard.read_private(guard.TERMINAL)
    assert retained['assessment_sha256'] == hashlib.sha256(assessment.read_bytes()).hexdigest()
    monkeypatch.setattr(guard, 'run_wrapper', lambda: pytest.fail('must be cached'))
    assert guard.check_locked(origin='test')['cached_terminal'] is True


def test_guard_missing_assessment_cannot_claim_pass(sandbox, monkeypatch):
    sandbox[1]()
    monkeypatch.setattr(guard, 'run_wrapper', lambda: {'infrastructure_24h_status': 'PASS'})
    result = guard.check_locked(origin='test')
    assert result['infrastructure_24h_status'] == 'ERROR'
    assert not guard.TERMINAL.exists()


def test_guard_assessment_status_mismatch_refuses(sandbox, monkeypatch):
    sandbox[1]()
    from types import SimpleNamespace
    monkeypatch.setattr(guard.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=os.getuid()))
    assessment = guard.WORK / 'check-mismatch.json'
    guard.save_new(assessment, {'infrastructure_24h_status': 'FAIL'})
    monkeypatch.setattr(guard, 'run_wrapper', lambda:
                        {'infrastructure_24h_status': 'PASS', 'result_path': str(assessment)})
    assert guard.check_locked(origin='test')['infrastructure_24h_status'] == 'ERROR'
    assert not guard.TERMINAL.exists()


def test_smoke_refuses_once_real_baseline_exists(sandbox, monkeypatch):
    sandbox[1]()
    monkeypatch.setattr(native, 'busy_units', lambda: pytest.fail('must refuse first'))
    with pytest.raises(ValueError, match='prebaseline_smoke_refused'):
        native.smoke_locked()


def test_diagnostics_keep_owned_error_without_secret_output(monkeypatch):
    import subprocess
    monkeypatch.setattr(native, 'PHASE', 'smoke_baseline_capture')
    exc = subprocess.CalledProcessError(1, ['secret-command'],
        output=json.dumps({'infrastructure_24h_status': 'ERROR', 'error_type': 'CollectionError',
                           'error_code': 'collector_private_path_required', 'secret': 'DO_NOT_EMIT'}),
        stderr='API_KEY=DO_NOT_EMIT')
    result = native.safe_diagnostic(exc)
    assert result['phase'] == 'smoke_baseline_capture' and result['returncode'] == 1
    assert result['child_error_type'] == 'CollectionError'
    assert result['child_error_code'] == 'collector_private_path_required'
    assert 'DO_NOT_EMIT' not in json.dumps(result) and 'secret-command' not in json.dumps(result)


def test_diagnostics_reject_arbitrary_provider_error_names():
    import subprocess
    exc = subprocess.CalledProcessError(1, [], output=json.dumps(
        {'error_type': 'DO_NOT_EMIT', 'error_code': 'DO_NOT_EMIT', 'raw': 'DO_NOT_EMIT'}))
    assert 'DO_NOT_EMIT' not in json.dumps(native.safe_diagnostic(exc))


def test_capture_probe_preserves_child_uid_and_allowlisted_cause(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(native.subprocess, 'run', lambda *_, **__: SimpleNamespace(stdout=json.dumps(
        {'probe_status': 'ERROR', 'child_uid': 999, 'child_euid': 999, 'child_gid': 999,
         'child_error_type': 'CollectionError', 'child_error_code': 'collector_private_path_required'})))
    with pytest.raises(native.OwnedProbeFailure) as error:
        native.capture_probe()
    result = native.safe_diagnostic(error.value)
    assert result['child_euid'] == 999 and result['child_error_code'] == 'collector_private_path_required'


def test_capture_probe_pass_is_structured_without_raw_capture(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(native.subprocess, 'run', lambda *_, **__: SimpleNamespace(stdout=json.dumps(
        {'probe_status': 'PASS', 'child_uid': 999, 'child_euid': 999, 'child_gid': 999,
         'health_status': 'HEALTHY', 'archive_integrity': 'PASS', 'raw': 'DO_NOT_EMIT'})))
    result = native.capture_probe()
    assert result['probe_status'] == 'PASS' and 'DO_NOT_EMIT' not in json.dumps(result)
