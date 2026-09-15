"""Actual process crashes through the isolated engine and synthetic broker.

The exchange fixture is persisted independently before os._exit, so recovery
cannot accidentally obtain the old engine's in-memory permits or claim. Every
request still crosses the real broker parser, risk policy and durable journal.
"""

from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CRASH_EXIT = 73

# A standalone child avoids sharing pytest fixtures, Python objects or cleanup
# handlers with the restarted process. All sockets are denied in both children.
CHILD = r'''
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys

operation, boundary, root, fixture = sys.argv[1:]
sys.path.insert(0, root)

def denied(*args, **kwargs):
    raise AssertionError('crash regression must never use network')
socket.create_connection = denied
socket.socket.connect = denied

from atlas.paper.broker import AlpacaPaperAPI
from atlas.paper.engine import PaperEngine
from atlas.paper.journal import PaperJournal
from atlas.paper.models import client_order_id, decision
from atlas.paper.risk import PaperRiskPolicy
from atlas.paper.simulation import SyntheticAlpaca

fixture = Path(fixture)
from atlas.paper import journal as journal_module
journal_module._account_lock_root = lambda: fixture / 'account-locks'
state = fixture / 'private-paper-state'
broker_path = fixture / 'broker.json'
policy = PaperRiskPolicy(allowed_strategy_ids=('crash_fixture',))

def persist(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)

sim = SyntheticAlpaca() if operation == 'crash' else SyntheticAlpaca.from_state(json.loads(broker_path.read_text()))
start = sim.clock()
reviewed = decision({'signal_id': 'crash-fixture-original', 'strategy_id': 'crash_fixture',
    'strategy_sha256': 'b' * 64, 'data_sha256': 'c' * 64, 'symbol': 'SPY', 'side': 'buy',
    'quantity': '0.9', 'limit_price': '10', 'signal_time': start, 'expires_at': start + 60})

def crash_now():
    persist(broker_path, sim.export_state())
    os._exit(73)

def transport(method, url, headers, payload):
    response = sim.request(method, url, headers, payload)
    if operation == 'crash' and boundary == 'accepted' and method == 'POST':
        assert response.status == 200 and payload['client_order_id'] in sim.orders
        # External acceptance has happened; even the broker client has not yet
        # parsed the response, so no engine acknowledgement can be committed.
        crash_now()
    return response

api = AlpacaPaperAPI(transport=transport, clock=sim.clock)
binding = {'mode': 'SIMULATION', 'account_fingerprint': api.account()['fingerprint'],
           'strategy_sha256': 'b' * 64, 'policy_sha256': policy.fingerprint(), 'allocated_principal': '100'}
with PaperJournal(state, binding, create=operation == 'crash', now=sim.clock()) as journal:
    engine = PaperEngine(api, journal, policy, clock=sim.clock)
    if operation == 'crash':
        engine.initialize()
        original_transaction = journal.transaction

        @contextmanager
        def hooked_transaction(kind, body, *, now):
            with original_transaction(kind, body, now=now):
                yield
            target = {'reservation': 'intent_and_reservation_committed', 'armed': 'submission_armed'}
            if target.get(boundary) == kind:
                # Commit and directory fsync really completed. The process
                # dies before the caller can proceed to its next instruction.
                crash_now()

        journal.transaction = hooked_transaction
        result = engine.submit(reviewed)
        raise AssertionError('intended crash boundary was never reached: ' + json.dumps(result, sort_keys=True))
    before = journal.orders()
    sim.advance(1)
    recovered = engine.reconcile()
    duplicate = engine.submit(reviewed)
    additional = None
    if boundary != 'accepted':
        fresh = {**reviewed, 'signal_id': 'crash-fixture-next', 'signal_time': sim.clock(),
                 'expires_at': sim.clock() + 60}
        additional = engine.submit(fresh)
    result = {'binding': journal.binding, 'expected_client_id': client_order_id(binding, reviewed),
              'before': before, 'after': journal.orders(), 'recovery': recovered, 'duplicate': duplicate,
              'additional': additional, 'integrity': journal.integrity_check(),
              'events': journal.events(), 'status': engine.status(), 'broker': sim.export_state()}
persist(fixture / 'recovery.json', result)
'''


def run_child(operation, boundary, fixture):
    return subprocess.run(
        [sys.executable, '-I', '-B', '-c', CHILD, operation, boundary, str(ROOT), str(fixture)],
        capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize('boundary,local_state,wire_authorized,posts', [
    ('reservation', 'reserved', False, 0),
    ('armed', 'submitting', False, 0),
    ('accepted', 'submitting', True, 1),
])
def test_process_crash_recovers_exact_intent_without_duplicate_submission(
        tmp_path, boundary, local_state, wire_authorized, posts):
    crashed = run_child('crash', boundary, tmp_path)
    assert crashed.returncode == CRASH_EXIT, crashed.stdout + crashed.stderr
    frozen_broker = json.loads((tmp_path / 'broker.json').read_text())
    assert frozen_broker['provenance'] == 'TEST'
    assert len([call for call in frozen_broker['calls'] if call['method'] == 'POST']) == posts
    assert len(frozen_broker['orders']) == posts

    restarted = run_child('recover', boundary, tmp_path)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    recovered = json.loads((tmp_path / 'recovery.json').read_text())
    cid = recovered['expected_client_id']
    assert recovered['integrity'] is True
    assert len(recovered['before']) == len(recovered['after']) == 1
    before, after = recovered['before'][0], recovered['after'][0]
    assert before['state'] == local_state and before['wire_authorized'] is wire_authorized
    assert before['observation'] is None
    assert before['client_order_id'] == after['client_order_id'] == cid
    assert before['decision_sha256'] == after['decision_sha256']
    assert before['payload'] == after['payload']
    assert before['reservation_cash'] == after['reservation_cash']
    assert Decimal(after['reservation_cash']) == Decimal('9.009')
    assert before['risk_at_reservation']['passed'] is True
    assert recovered['duplicate']['replayed'] is True
    assert recovered['duplicate']['new_submission'] is False
    assert recovered['duplicate']['order']['client_order_id'] == cid

    mutations = [call for call in recovered['broker']['calls'] if call['method'] != 'GET']
    assert len(mutations) == posts
    assert len(recovered['broker']['orders']) == posts
    if posts:
        assert mutations[0]['method'] == 'POST'
        assert mutations[0]['payload']['client_order_id'] == cid
    assert not any(event['kind'] == 'broker_submission_observed' for event in recovered['events'])
    assert recovered['status']['mode'] == 'SIMULATION'
    assert recovered['status']['provenance'] == 'TEST'
    assert recovered['status']['actual_orders_submitted'] == 0
    assert recovered['status']['live_enabled'] is False

    if boundary == 'accepted':
        assert recovered['recovery']['reconciled'] is True
        assert after['state'] == 'new' and after['observation']['filled_qty'] == '0'
        assert after['observation']['id'] == recovered['broker']['orders'][cid]['id']
        assert recovered['status']['positions'] == [] and recovered['status']['fill_count'] == 0
        assert recovered['additional'] is None
    else:
        # Absence at the broker does not grant a new process the original
        # ephemeral authority to send this reservation or clear its risk hold.
        assert recovered['recovery']['reconciled'] is False
        assert 'paper_order_missing_requires_reconciliation' in recovered['recovery']['reasons']
        assert 'paper_unresolved_order' in recovered['recovery']['reasons']
        assert after['state'] == local_state and after['observation'] is None
        assert recovered['additional']['new_submission'] is False
        assert recovered['additional']['risk']['passed'] is False
        assert recovered['additional']['risk']['reasons'] == ['paper_reconciliation_required']
