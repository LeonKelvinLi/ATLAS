"""Private single-writer paper journal, intent-before-I/O and checked audit chain.

A fixed per-user account lock prevents alternate state directories from running
the same PAPER account concurrently on one host. A second lock protects journal
ownership. SQLite FULL commits and directory fsync precede each mutation. These
are not distributed locks: one execution host per dedicated account is required.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading

from atlas.ledger import canonical_json, digest, _SECRET_KEYS
from atlas.paper.models import PaperError, amount, identifier, sha256, timestamp

SCHEMA = (
    'CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'CREATE TABLE orders (client_id TEXT PRIMARY KEY, signal_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL)',
    'CREATE TABLE fills (id TEXT PRIMARY KEY, body TEXT NOT NULL)',
    'CREATE TABLE events (id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, previous_hash TEXT NOT NULL, state_hash TEXT NOT NULL, hash TEXT NOT NULL)',
)
_PRIVATE_FIELDS = frozenset(key.replace('_', '') for key in _SECRET_KEYS) | frozenset({
    'apisecret', 'secretkey', 'apisecretkey', 'apcaapikeyid', 'apcaapisecretkey',
    'credentials', 'clientsecret', 'idtoken', 'sessiontoken',
})


def _reject_secrets(value):
    """Reject credential fields and recognizable raw auth text before persistence."""
    if isinstance(value, dict):
        for key, child in value.items():
            if (not isinstance(key, str)
                    or key.lower().replace('-', '').replace('_', '') in _PRIVATE_FIELDS):
                raise PaperError('paper_journal_private_material_refused')
            _reject_secrets(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secrets(child)
    elif isinstance(value, str) and (re.search(r'-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----', value)
            or re.search(r'(?i)\b(?:bearer|basic)\s+\S+', value)):
        raise PaperError('paper_journal_private_material_refused')


def _sync(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_file(path, *, create=False):
    flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(fd)
        raise PaperError('paper_private_state_file_required')
    return fd


def _account_lock_root():
    """Fixed production path; tests replace this internal function explicitly."""
    return Path.home() / '.local/state/atlas-paper-account-locks'


def _account_lock_directory():
    root = _account_lock_root()
    if (not isinstance(root, Path) or not root.is_absolute() or '..' in root.parts
            or any(path.is_symlink() for path in (root, *root.parents))):
        raise PaperError('paper_private_account_lock_directory_required')
    missing, parent = [], root
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass  # Concurrent initializers must validate the same new directory.
        if directory.is_symlink():
            raise PaperError('paper_private_account_lock_directory_required')
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PaperError('paper_private_account_lock_directory_required')
        _sync(directory.parent)
    # Check again after creation, including ancestors, before opening a lock.
    info = root.lstat()
    if (any(path.is_symlink() for path in (root, *root.parents))
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise PaperError('paper_private_account_lock_directory_required')
    return root


def _acquire_account_lock(binding, *, create):
    root = _account_lock_directory()
    path = root / (binding['mode'].lower() + '-' + binding['account_fingerprint'] + '.lock')
    descriptor = None
    created = False
    try:
        if binding['mode'] == 'PAPER' and not create:
            try:
                descriptor = _safe_file(path)
            except FileNotFoundError:
                raise PaperError('paper_account_registration_missing_binding') from None
        else:
            try:
                descriptor = _safe_file(path, create=True)
                created = True
                _sync(root)
            except FileExistsError:
                descriptor = _safe_file(path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PaperError('paper_single_account_writer_already_active') from None
        return descriptor, created
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _register_paper_account(descriptor, created, directory, binding, *, initialize):
    """PAPER sessions cannot discard reservations by selecting a fresh path."""
    if binding['mode'] != 'PAPER':
        return
    expected = {'schema': 1, 'mode': 'PAPER', 'account_fingerprint': binding['account_fingerprint'],
                'binding_sha256': digest(binding), 'journal_directory': str(directory.resolve())}
    canonical = canonical_json(expected).encode('utf-8')
    if len(canonical) > 16384:
        raise PaperError('paper_account_registration_invalid')
    if created:
        if not initialize or os.fstat(descriptor).st_size != 0:
            raise PaperError('paper_account_registration_invalid')
        # Never truncate or rewrite a prior claim. An interrupted write remains
        # invalid and requires explicit recovery instead of becoming a new run.
        written = 0
        while written < len(canonical):
            count = os.write(descriptor, canonical[written:])
            if count <= 0:
                raise OSError('paper_account_registration_write_failed')
            written += count
        os.fsync(descriptor)
        _sync(_account_lock_root())
        return
    try:
        raw = os.pread(descriptor, 16385, 0)
        actual = json.loads(raw)
        if len(raw) > 16384 or not isinstance(actual, dict) or canonical_json(actual).encode('utf-8') != raw:
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise PaperError('paper_account_registration_invalid') from None
    if actual != expected:
        raise PaperError('paper_account_registration_binding_or_path_mismatch')
    if initialize:
        raise PaperError('paper_account_session_already_registered')


def _directory(value, create):
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise PaperError('paper_explicit_unlinked_state_directory_required')
    project = Path(__file__).resolve().parents[2]
    if (path.is_relative_to(project) or any((p / '.git').exists() or (p / '.git').is_symlink()
            for p in (path, *path.parents)) or any(p.name.startswith(('atlas-collector', 'atlas-operator'))
            or p.name in ('ATLAS-research-evidence', '.codex') for p in (path, *path.parents))
            or path.is_relative_to(Path.home() / '.config/atlas/alpaca-paper')):
        raise PaperError('paper_isolated_state_directory_required')
    if not path.exists():
        if not create or not path.parent.is_dir():
            raise PaperError('paper_state_missing_explicit_initialization_required')
        path.mkdir(mode=0o700)
        _sync(path.parent)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PaperError('paper_private_state_directory_required')
    if create and any(path.iterdir()):
        raise PaperError('paper_initialization_requires_empty_new_directory')
    return path


class PaperJournal:
    def __init__(self, directory, binding, *, create=False, now=0):
        self._db, self._lockfd, self._account_lockfd, self._failed = None, None, None, False
        self._thread_lock = threading.RLock()
        if (type(binding) is not dict or set(binding) != {'mode', 'account_fingerprint', 'strategy_sha256',
                'policy_sha256', 'allocated_principal'} or binding['mode'] not in ('SIMULATION', 'PAPER')):
            raise PaperError('paper_journal_binding_invalid')
        for name in ('account_fingerprint', 'strategy_sha256', 'policy_sha256'):
            sha256(binding[name])
        if not 0 < amount(binding['allocated_principal']) <= 100:
            raise PaperError('paper_allocation_exceeds_hard_ceiling')
        self._binding = deepcopy(binding)
        try:
            # This identity is independent of the caller-selected journal path.
            # Acquire it before creating or inspecting any journal state.
            self._account_lockfd, account_lock_created = _acquire_account_lock(binding, create=create)
            self.directory = _directory(directory, create)
            self.path = self.directory / 'paper.sqlite3'
            _register_paper_account(self._account_lockfd, account_lock_created, self.directory,
                                    binding, initialize=create)
            lock = self.directory / '.writer.lock'
            self._lockfd = _safe_file(lock, create=create)
            try:
                fcntl.flock(self._lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PaperError('paper_single_writer_already_active') from None
            fd = _safe_file(self.path, create=create)
            info = os.fstat(fd)
            os.close(fd)
            if not create and info.st_size == 0:
                raise PaperError('paper_existing_journal_empty')
            for suffix in ('-wal', '-shm'):
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    fd = _safe_file(sidecar)
                    os.close(fd)
            self._db = sqlite3.connect(self.path, isolation_level=None, timeout=0, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.execute('PRAGMA synchronous=FULL')
            if create:
                self._db.execute('BEGIN IMMEDIATE')
                for sql in SCHEMA:
                    self._db.execute(sql)
                self._db.execute('PRAGMA user_version=1')
                self.set('binding', binding)
                self.set('initialized', False)
                self._checkpoint('journal_created', {'mode': binding['mode']}, timestamp(now))
                self._db.execute('COMMIT')
                _sync(self.directory)
            self.integrity_check()
            if self.get('binding') != binding:
                raise PaperError('paper_journal_foreign_binding')
            self._db.execute('PRAGMA journal_mode=WAL')
            _sync(self.directory)
        except BaseException:
            self.close()
            raise

    @property
    def binding(self):
        return deepcopy(self._binding)

    def close(self):
        try:
            if self._db is not None:
                database, self._db = self._db, None
                database.close()
        finally:
            try:
                if self._lockfd is not None:
                    descriptor, self._lockfd = self._lockfd, None
                    os.close(descriptor)
            finally:
                if self._account_lockfd is not None:
                    descriptor, self._account_lockfd = self._account_lockfd, None
                    os.close(descriptor)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _state_hash(self):
        state = {table: [list(row) for row in self._db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                 for table in ('metadata', 'orders', 'fills')}
        return digest(state)

    def _checkpoint(self, kind, body, now):
        identifier(kind)
        _reject_secrets(body)
        previous = self._db.execute('SELECT id,hash,at FROM events ORDER BY id DESC LIMIT 1').fetchone()
        if previous is not None and now < previous['at']:
            raise PaperError('paper_journal_clock_regressed')
        item = {'id': 1 if previous is None else previous['id'] + 1, 'at': now, 'kind': kind,
                'body': body, 'previous_hash': '0' * 64 if previous is None else previous['hash'],
                'state_hash': self._state_hash()}
        self._db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',
            (item['id'], now, kind, canonical_json(body), item['previous_hash'], item['state_hash'], digest(item)))

    @contextmanager
    def transaction(self, kind, body, *, now):
        with self._thread_lock:
            if self._failed or self._db is None:
                raise PaperError('paper_journal_unavailable')
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self._checkpoint(kind, body, timestamp(now))
                self._db.execute('COMMIT')
                _sync(self.directory)
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute('ROLLBACK')
                self._failed = True  # Durable-storage failure requires reopening and reconciliation.
                raise

    def event(self, kind, body, *, now):
        with self.transaction(kind, body, now=now):
            pass

    def get(self, key, default=None):
        row = self._db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, key, value):
        if not self._db.in_transaction:
            raise PaperError('paper_durable_transaction_required')
        identifier(key)
        _reject_secrets({key: value})
        if key == 'binding' and self.get(key) not in (None, value):
            raise PaperError('paper_immutable_journal_binding_changed')
        self._db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (key, canonical_json(value)))

    def orders(self):
        return [json.loads(row[0]) for row in self._db.execute('SELECT body FROM orders ORDER BY client_id')]

    def order(self, client_id):
        row = self._db.execute('SELECT body FROM orders WHERE client_id=?', (client_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def by_signal(self, signal_id):
        row = self._db.execute('SELECT body FROM orders WHERE signal_id=?', (signal_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_order(self, order, *, new=False):
        if not self._db.in_transaction:
            raise PaperError('paper_durable_transaction_required')
        _reject_secrets(order)
        text = canonical_json(order)
        if new:
            self._db.execute('INSERT INTO orders VALUES (?,?,?)', (order['client_order_id'], order['decision']['signal_id'], text))
        else:
            existing = self.order(order['client_order_id'])
            if existing is None or any(existing[k] != order[k] for k in ('decision', 'decision_sha256', 'payload', 'created_at')):
                raise PaperError('paper_immutable_intent_changed')
            self._db.execute('UPDATE orders SET body=? WHERE client_id=?', (text, order['client_order_id']))

    def fills(self):
        return [json.loads(row[0]) for row in self._db.execute('SELECT body FROM fills ORDER BY id')]

    def put_fill(self, fill):
        if not self._db.in_transaction:
            raise PaperError('paper_durable_transaction_required')
        _reject_secrets(fill)
        encoded = canonical_json(fill)
        old = self._db.execute('SELECT body FROM fills WHERE id=?', (fill['id'],)).fetchone()
        if old and old[0] != encoded:
            raise PaperError('paper_fill_identity_changed')
        if old is None:
            self._db.execute('INSERT INTO fills VALUES (?,?)', (fill['id'], encoded))

    def events(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise PaperError('paper_audit_limit_invalid')
        return [{**dict(row), 'body': json.loads(row['body'])}
                for row in self._db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,))]

    def integrity_check(self):
        try:
            tables = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if (tables != {'metadata', 'orders', 'fills', 'events'}
                    or self._db.execute('PRAGMA user_version').fetchone()[0] != 1
                    or [r[0] for r in self._db.execute('PRAGMA integrity_check')] != ['ok']):
                raise ValueError
            previous_hash, previous_at, count = '0' * 64, 0, 0
            for row in self._db.execute('SELECT * FROM events ORDER BY id'):
                item = {**dict(row), 'body': json.loads(row['body'])}
                current_hash = item.pop('hash')
                count += 1
                if (item['id'] != count or item['previous_hash'] != previous_hash
                        or timestamp(item['at']) < previous_at or digest(item) != current_hash):
                    raise ValueError
                previous_hash, previous_at = current_hash, item['at']
            if not count or item['state_hash'] != self._state_hash():
                raise ValueError
            for order in self.orders():
                if (digest(order['decision']) != order['decision_sha256']
                        or order['client_order_id'] != order['payload']['client_order_id']):
                    raise ValueError
            return True
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            self._failed = True
            raise PaperError('paper_journal_integrity_failed') from None
