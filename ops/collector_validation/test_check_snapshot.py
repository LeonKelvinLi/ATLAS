"""Offline regression evidence for detached archive verification; no broker I/O."""
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_unattended as checker
from atlas.collection.archive import acquire_collection_once, verify_collection_archive
from atlas.collection.config import validate_config
from atlas.collection.health import report
from atlas.collection.primitives import HTTPResponse, _encoded
from atlas.collection.runtime import init_runtime
from atlas.collection.state import connection

NOW = 1789430700


def collect(config, *, price=100, now=NOW):
    def transport(url, *args, **kwargs):
        body = ([[NOW - 300, price, price, price, price, 1]] if '/candles?' in url else
                {'sequence': 1, 'bids': [[str(price), '1', 1]], 'asks': [[str(price + 1), '1', 1]]})
        return HTTPResponse(200, _encoded(body))
    return acquire_collection_once(config['archive_dir'], provider='coinbase',
        start=NOW-300, end=NOW, transport=transport, clock=lambda: now)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state = tmp_path.resolve() / 'state'
    config = validate_config({'version': 1, 'mode': 'data_collection', 'live_trading': False,
        'state_dir': str(state), 'archive_dir': str(state / 'archives' / 'v1'),
        'generation_id': 'v1', 'research_start': NOW - 179 * 86400,
        'providers': ['coinbase'], 'alpaca_key_file': None})
    init_runtime(config, clock=lambda: NOW)
    fd = os.open(state / ".snapshot.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    collect(config)
    work = tmp_path.resolve() / 'work'
    work.mkdir(mode=0o700)
    monkeypatch.setattr(checker, 'WORK', work)
    monkeypatch.setattr(checker.time, 'time', lambda: NOW)
    return config, work


def capture(config, *, verifier=verify_collection_archive, baseline=None):
    return checker._capture_consistent(config, baseline, report=report,
        connection=connection, verifier=verifier)


def digests(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_full_verification_releases_writer_barrier_and_preserves_all_source_bytes(setup):
    config, work = setup
    source = Path(config['archive_dir'])
    before = digests(source)
    snapshots = []
    def verify(snapshot, **kwargs):
        snapshots.append(snapshot)
        assert snapshot != source and snapshot.is_relative_to(work)
        with open(Path(config['state_dir']) / '.snapshot.lock', 'rb') as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return verify_collection_archive(snapshot, **kwargs)
    now, health, *_ = capture(config, verifier=verify)
    assert now == NOW
    assert health['archive']['integrity'] == 'PASS'
    assert health['archive']['records'] == 2
    assert health['archive']['verification_source'] == 'private_consistent_snapshot'
    assert health['archive']['snapshot_barrier_seconds'] < 5
    assert health['archive']['snapshot_bytes'] > 0
    # Initial stopped state remains a blocker; integrity is not health.
    assert health['status'] == 'UNHEALTHY'
    assert 'collector_process_stopped' in health['blockers']
    assert digests(source) == before
    assert all(not p.exists() for p in snapshots)
    assert list(work.iterdir()) == []


def test_live_append_after_unlock_does_not_change_snapshot_evidence(setup):
    config, work = setup
    snapshots = []
    def verify(snapshot, **kwargs):
        snapshots.append(snapshot)
        before = digests(snapshot)
        with open(Path(config['state_dir']) / '.snapshot.lock', 'rb') as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            collect(config, price=110, now=NOW+1)
        assert digests(snapshot) == before
        assert kwargs['clock']() == NOW
        return verify_collection_archive(snapshot, **kwargs)
    _, health, *_ = capture(config, verifier=verify)
    assert health['archive']['records'] == 2 and health['archive']['batches'] == 1
    assert verify_collection_archive(config['archive_dir'], clock=lambda: NOW+1)['record_count'] == 4
    assert list(work.iterdir()) == []


def test_corrupt_snapshot_integrity_fails_closed_and_cleans_up(setup):
    config, work = setup
    source = Path(config['archive_dir'])
    before = digests(source)
    def corrupt(snapshot, **kwargs):
        next((snapshot/'days').glob('*/archive.sqlite3')).write_bytes(b'invalid sqlite')
        return verify_collection_archive(snapshot, **kwargs)
    with pytest.raises((ValueError, OSError)):
        capture(config, verifier=corrupt)
    assert digests(source) == before and list(work.iterdir()) == []


@pytest.mark.parametrize('field,value', [
    ('record_count', 123), ('batch_count', 123), ('generation_id', 'wrong'),
    ('contract_sha256', 'wrong'), ('started_at', NOW-1), ('latest_timestamp', NOW-1),
    ('acquisition_kinds', {'test': 0}), ('integrity_verified', False),
    ('verification_scope', 'operational_counts_only'),
])
def test_full_scan_must_match_locked_operational_evidence(setup, field, value):
    config, work = setup
    def mismatch(snapshot, **kwargs):
        result = verify_collection_archive(snapshot, **kwargs)
        result[field] = value
        return result
    with pytest.raises(ValueError, match='snapshot_archive_metadata_mismatch'):
        capture(config, verifier=mismatch)
    assert list(work.iterdir()) == []


def test_failed_operational_report_cannot_be_overridden_by_good_full_scan(setup):
    config, work = setup
    def failed_report(*args, **kwargs):
        value = report(*args, **kwargs)
        value['archive']['integrity'] = 'FAIL'
        return value
    with pytest.raises(ValueError, match='snapshot_archive_metadata_mismatch'):
        checker._capture_consistent(config, None, report=failed_report,
            connection=connection, verifier=verify_collection_archive)
    assert list(work.iterdir()) == []


def test_pending_check_never_copies_or_scans_archive(setup, monkeypatch):
    config, work = setup
    def forbidden(*args, **kwargs):
        pytest.fail('intermediate check must not copy or fully scan archive')
    monkeypatch.setattr(checker, '_copy_archive_snapshot', forbidden)
    _, health, *_ = capture(config, verifier=forbidden,
        baseline={'captured_at': NOW-10, 'event_watermark': 0})
    assert health['archive']['integrity'] == 'NOT_CHECKED'
    assert list(work.iterdir()) == []


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'public', 'sidecar'])
def test_unsafe_archive_entries_are_rejected_without_source_mutation(setup, kind):
    config, work = setup
    source = Path(config['archive_dir'])
    if kind == 'symlink':
        (source/'extra').symlink_to(source/'registration.json')
    elif kind == 'hardlink':
        os.link(source/'registration.json', source/'extra')
    elif kind == 'public':
        (source/'registration.json').chmod(0o644)
    else:
        (source/'catalog.sqlite3-journal').write_bytes(b'')
        (source/'catalog.sqlite3-journal').chmod(0o600)
    before = digests(source)
    with pytest.raises(ValueError, match='snapshot_'):
        capture(config)
    assert digests(source) == before and list(work.iterdir()) == []


def test_work_symlink_rejected(setup, monkeypatch):
    config, work = setup
    link = work.parent/'linked-work'
    link.symlink_to(work, target_is_directory=True)
    monkeypatch.setattr(checker, 'WORK', link)
    with pytest.raises(ValueError, match='snapshot_symlink_refused'):
        capture(config)
    assert list(work.iterdir()) == []


@pytest.mark.parametrize('setting', ['SNAPSHOT_MAX_FILE_BYTES', 'SNAPSHOT_MAX_TOTAL_BYTES', 'SNAPSHOT_MAX_ENTRIES'])
def test_copy_limits_fail_closed_with_cleanup(setup, monkeypatch, setting):
    config, work = setup
    monkeypatch.setattr(checker, setting, 1)
    with pytest.raises(ValueError, match='snapshot_.*bound_exceeded'):
        capture(config)
    assert list(work.iterdir()) == []


def test_disk_reserve_kept_and_partial_copy_cleaned(setup, monkeypatch):
    config, work = setup
    monkeypatch.setattr(checker.shutil, 'disk_usage', lambda path: SimpleNamespace(
        used=0, free=config['min_free_bytes'], total=config['min_free_bytes']))
    with pytest.raises(ValueError, match='snapshot_disk_reserve_exhausted'):
        capture(config)
    assert list(work.iterdir()) == []


def test_deadline_refuses_full_scan_and_releases_barrier(setup, monkeypatch):
    config, work = setup
    monkeypatch.setattr(checker, 'SNAPSHOT_LOCK_SECONDS', 0)
    with pytest.raises(ValueError, match='snapshot_copy_budget_exceeded'):
        capture(config)
    with open(Path(config['state_dir']) / '.snapshot.lock', 'rb') as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert list(work.iterdir()) == []


def test_registration_mutation_before_scan_is_detected(setup, monkeypatch):
    config, work = setup
    original = checker._verify_snapshot
    def corrupt(config, health, snapshot, **kwargs):
        with open(snapshot/'registration.json', 'ab') as stream:
            stream.write(b' ')
        return original(config, health, snapshot, **kwargs)
    monkeypatch.setattr(checker, '_verify_snapshot', corrupt)
    with pytest.raises(ValueError, match='snapshot_registration_changed'):
        capture(config)
    assert list(work.iterdir()) == []


def test_assess_function_source_is_unchanged():
    source = Path(checker.__file__).read_text()
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'assess')
    # Frozen original function, before the snapshot-copy repair.
    assert hashlib.sha256(ast.get_source_segment(source, function).encode()).hexdigest() == (
        '1cb3856cccffa6d60b3e2d0a9fc5128396b0e87b67a68d139665f4f005959918')


def test_budget_exhaustion_during_partial_copy_cleans_up_and_keeps_source(setup, monkeypatch):
    config, work = setup
    source = Path(config['archive_dir'])
    before = digests(source)
    ticks = [0]
    def monotonic():
        ticks[0] += 0.1
        return ticks[0]
    monkeypatch.setattr(checker.time, 'monotonic', monotonic)
    monkeypatch.setattr(checker, 'SNAPSHOT_CHUNK_BYTES', 1)
    with pytest.raises(ValueError, match='snapshot_copy_budget_exceeded'):
        capture(config)
    assert digests(source) == before and list(work.iterdir()) == []
