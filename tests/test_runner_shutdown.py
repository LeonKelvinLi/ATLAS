"""Server shutdown using fake broker clients, sockets and captured handlers.

Nothing here opens a broker session or sends an OS signal. Real threads in
the small _call tests only invoke local fixtures and are released in finally.
"""

import json
import signal
import socket
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import live_h2_runner as r
from settle_missing import find_unsettled


def test_call_after_stop_does_not_start_the_callable():
    stop = threading.Event()
    stop.set()
    calls = []
    with pytest.raises(r.ShutdownRequested) as exc:
        r._call(lambda: calls.append("started"), stop_event=stop)
    assert not exc.value.call_started
    assert calls == []


def test_read_wait_is_interruptible_without_claiming_the_thread_cancelled():
    stop, release, finished = (threading.Event() for _ in range(3))

    def blocked_read():
        stop.set()
        try:
            release.wait(2)
        finally:
            finished.set()

    started = time.monotonic()
    try:
        with pytest.raises(r.ShutdownRequested) as exc:
            r._call(blocked_read, timeout=30, stop_event=stop)
        assert exc.value.call_started
        assert time.monotonic() - started < 1
        assert not finished.is_set(), "interrupting a wait does not cancel the library call"
    finally:
        release.set()
        assert finished.wait(2)


def test_buy_acknowledgement_can_finish_during_shutdown_grace():
    stop = threading.Event()

    def fake_buy():
        stop.set()
        time.sleep(0.02)
        return True, 42

    assert r._call(fake_buy, stop_event=stop, shutdown_grace=1) == (True, 42)


def test_inflight_buy_grace_is_bounded():
    stop, release, finished = (threading.Event() for _ in range(3))

    def fake_buy():
        stop.set()
        try:
            release.wait(2)
        finally:
            finished.set()

    started = time.monotonic()
    try:
        with pytest.raises(r.ShutdownRequested) as exc:
            r._call(fake_buy, timeout=60, stop_event=stop, shutdown_grace=0.05)
        assert exc.value.call_started
        assert time.monotonic() - started < 1
    finally:
        release.set()
        assert finished.wait(2)


def test_reconciliation_has_one_budget_and_preserves_unresolved_orders(tmp_path, monkeypatch):
    now = [0.0]
    budgets = []
    records = [{"order_id": oid, "asset": "EURUSD", "ts": oid} for oid in range(1, 4)]
    pending = [(record["order_id"], record) for record in records]
    log = tmp_path / "live_h2.jsonl"
    log.write_text("".join(json.dumps(record) + "\n" for record in records))

    def timed_out(_fn, order_id, *, timeout, stop_event):
        budgets.append(timeout)
        now[0] += timeout
        raise TimeoutError(f"still open: {order_id}")

    monkeypatch.setattr(r.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(r, "_call", timed_out)
    r.settle_open_orders(SimpleNamespace(check_win_v4=object()), pending, log,
                         timeout_budget=8)
    assert len(budgets) == 3
    assert sum(budgets) == pytest.approx(8)
    assert [oid for oid, _ in pending] == [1, 2, 3]
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows == records
    assert find_unsettled(rows) == records


def test_normal_reconciliation_keeps_expiry_wait_but_signal_bounds_the_remainder(
        tmp_path, monkeypatch):
    stop = threading.Event()
    now = [0.0]
    calls = []
    pending = [(oid, {"order_id": oid}) for oid in (1, 2, 3)]

    def interrupted(_fn, order_id, *, timeout, stop_event):
        calls.append((order_id, timeout, stop_event))
        if order_id == 1:
            stop.set()
            raise r.ShutdownRequested(call_started=True)
        now[0] += timeout
        raise TimeoutError("still pending")

    monkeypatch.setattr(r.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(r, "_call", interrupted)
    r.settle_open_orders(SimpleNamespace(check_win_v4=object()), pending,
                         tmp_path / "live_h2.jsonl", stop_event=stop)
    assert calls[0] == (1, r.EXPIRY_MINUTES * 60 + 120, stop)
    assert [oid for oid, _timeout, _event in calls] == [1, 2, 3]
    assert sum(timeout for _oid, timeout, _event in calls[1:]) == r.SHUTDOWN_RECONCILE_S
    assert all(event is None for _oid, _timeout, event in calls[1:])
    assert [oid for oid, _record in pending] == [1, 2, 3]


def test_reconciliation_only_removes_confirmed_finite_verdicts(tmp_path, monkeypatch):
    outcomes = {1: ("win", 0.87), 2: (None, None), 3: ("loose", float("nan"))}
    pending = [(oid, {"order_id": oid, "mode": "trade"}) for oid in outcomes]
    monkeypatch.setattr(r, "_call", lambda _fn, oid, **_kwargs: outcomes[oid])
    log = tmp_path / "live_h2.jsonl"
    r.settle_open_orders(SimpleNamespace(check_win_v4=object()), pending, log)
    assert [oid for oid, _ in pending] == [2, 3]
    assert json.loads(log.read_text()) == {
        "order_id": 1, "mode": "trade", "result": "win", "profit": 0.87,
        "settled": True,
    }


class LoopEvent:
    """No wall-clock waits in main-loop tests."""

    def __init__(self):
        self.stopped = False
        self.on_wait = lambda: None

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, _timeout):
        self.on_wait()
        return self.stopped


@pytest.fixture
def session(monkeypatch, tmp_path):
    handlers = {signal.SIGINT: signal.SIG_DFL, signal.SIGTERM: signal.SIG_DFL}
    initial_handlers = dict(handlers)
    event = LoopEvent()
    watchdog_event = LoopEvent()
    events = iter((event, watchdog_event))
    state = SimpleNamespace(
        event=event, handlers=handlers, initial_handlers=initial_handlers,
        socket_closed=False, client_closed=False, buys=[], feature_calls=0,
        decisions=0, balance_calls=0, bind_error=False, on_balance=lambda: None,
        on_buy=lambda: (True, 42), on_predict=lambda: None,
        on_connect=lambda: (True, None),
        watchdog_event=watchdog_event,
    )

    def signal_swap(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    class FakeSocket:
        def bind(self, address):
            assert address == ("127.0.0.1", 47201)
            if state.bind_error:
                raise OSError("occupied")

        def close(self):
            state.socket_closed = True

    class FakeClient:
        def connect(self):
            return state.on_connect()

        def change_balance(self, mode):
            assert mode == "PRACTICE"

        def get_balance_mode(self):
            state.balance_calls += 1
            if state.balance_calls > 1:
                state.on_balance()
            return "PRACTICE"

        def buy(self, amount, asset, action, expiry):
            state.buys.append((amount, asset, action, expiry))
            return state.on_buy()

        def check_win_v4(self, _order_id):
            raise TimeoutError("not closed yet")

        def close(self):
            state.client_closed = True

    def direct_call(fn, *args, timeout=60, stop_event=None, shutdown_grace=0, **kwargs):
        r._check_shutdown(stop_event)
        return fn(*args, **kwargs)

    def features(_client, _horizon, _basket, **_kwargs):
        state.feature_calls += 1
        return pd.DataFrame([
            {"asset": "EURUSD", "to_ts": int(time.time()), "fixture_feature": 1.0},
            {"asset": "USDJPY", "to_ts": int(time.time()), "fixture_feature": 2.0},
        ])

    def predict(frame):
        state.on_predict()
        return np.full(len(frame), 0.8)

    real_decide = r.decide_action

    def decide(*args):
        state.decisions += 1
        return real_decide(*args)

    client = FakeClient()
    monkeypatch.setattr(r.signal, "signal", signal_swap)
    monkeypatch.setattr(r, "threading", SimpleNamespace(Event=lambda: next(events)))
    monkeypatch.setattr(socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(r, "_LOCK_SOCK", None)
    monkeypatch.setattr(r, "LOGS_DIR", tmp_path / "state" / "logs")
    monkeypatch.setattr(r, "_load_env", lambda: None)
    monkeypatch.setenv("IQ_EMAIL", "fixture@example.invalid")
    monkeypatch.setenv("IQ_PASSWORD", "test-fixture-only")
    monkeypatch.setitem(sys.modules, "iqoptionapi", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "iqoptionapi.stable_api",
                        SimpleNamespace(IQ_Option=lambda *_args: client))
    monkeypatch.setattr(r, "_call", direct_call)
    monkeypatch.setattr(r, "load_frozen_model", lambda: (
        "h2-fixture.pkl", SimpleNamespace(predict_proba_up=predict),
        {"horizon_bars": 15, "feature_columns": ["fixture_feature"], "rows": 2,
         "assets": ["EURUSD", "USDJPY"], "data_end_ts": r.REGISTERED_CUTOFF_TS},
    ))
    monkeypatch.setattr(r, "load_meta_filter", lambda: None)
    monkeypatch.setattr(r, "load_shadow_h4", lambda: None)
    monkeypatch.setattr(r, "_start_cycle_watchdog", lambda **_kwargs: None)
    monkeypatch.setattr(r, "latest_feature_rows", features)
    monkeypatch.setattr(r, "live_quotes", lambda *_args, **_kwargs: {
        r.INSTRUMENTS[asset].quote_key: {"binary": 0.87}
        for asset in ("EURUSD", "USDJPY")
    })
    monkeypatch.setattr(r, "decide_action", decide)
    monkeypatch.setattr(sys, "argv", ["live_h2_runner.py", "--minutes", "1", "--trade"])
    state.log = r.LOGS_DIR / "live_h2.jsonl"
    return state


def assert_cleaned(session):
    assert session.event.is_set()
    assert session.watchdog_event.is_set()
    assert session.socket_closed
    assert session.client_closed
    assert r._LOCK_SOCK is None
    assert session.handlers == session.initial_handlers


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_interrupts_minute_wait_without_deciding_or_buying(session, signum):
    session.event.on_wait = lambda: session.handlers[signum](signum, None)
    assert r.main() == 0
    assert session.feature_calls == session.decisions == 0
    assert session.buys == []
    assert not session.log.exists()
    assert_cleaned(session)


def test_signal_during_prediction_stops_before_any_new_decision(session):
    session.on_predict = lambda: session.handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert r.main() == 0
    assert session.feature_calls == 1
    assert session.decisions == 0
    assert session.buys == []
    assert_cleaned(session)


def test_signal_after_balance_check_never_starts_buy(session):
    session.on_balance = lambda: session.handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert r.main() == 0
    assert session.buys == []
    row = json.loads(session.log.read_text())
    assert row["trade_skipped"] == "shutdown_requested"
    assert not row.get("order_submission_unknown")
    assert_cleaned(session)


def test_acknowledged_order_on_shutdown_remains_recoverable_without_fake_settlement(session):
    def acknowledge():
        session.handlers[signal.SIGTERM](signal.SIGTERM, None)
        return True, 42

    session.on_buy = acknowledge
    assert r.main() == 0
    assert len(session.buys) == 1
    rows = [json.loads(line) for line in session.log.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["order_id"] == 42
    assert not rows[0].get("settled")
    assert find_unsettled(rows) == rows
    assert_cleaned(session)


def test_unknown_submission_is_logged_without_claiming_cancellation(session):
    def unacknowledged():
        session.handlers[signal.SIGTERM](signal.SIGTERM, None)
        raise r.ShutdownRequested(call_started=True)

    session.on_buy = unacknowledged
    assert r.main() == 0
    assert len(session.buys) == 1
    row = json.loads(session.log.read_text())
    assert row["order_submission_unknown"] is True
    assert row["order_error"] == "shutdown_before_acknowledgement"
    assert row["order_id"] is None
    assert "trade_skipped" not in row
    assert not row.get("settled")
    assert_cleaned(session)


def test_buy_timeout_logs_unknown_submission_and_stops_before_next_buy(session):
    def timed_out():
        raise TimeoutError("acknowledgement absent")

    session.on_buy = timed_out
    assert r.main() == 1
    assert len(session.buys) == 1
    row = json.loads(session.log.read_text())
    assert row["order_submission_unknown"] is True
    assert row["order_error"] == "acknowledgement_timeout"
    assert row["order_id"] is None
    assert "trade_skipped" not in row
    assert not row.get("settled")
    assert_cleaned(session)


def test_login_failure_disconnects_and_releases_lock(session):
    session.on_connect = lambda: (False, "fixture failure")
    with pytest.raises(SystemExit, match="login failed"):
        r.main()
    assert_cleaned(session)


def test_cycle_failure_exit_still_disconnects_and_releases_lock(session, monkeypatch):
    def failed_features(*_args, **_kwargs):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(r, "latest_feature_rows", failed_features)
    with pytest.raises(SystemExit) as exc:
        r.main()
    assert exc.value.code == 1
    assert_cleaned(session)


def test_cleanup_failure_cannot_keep_the_lock_or_handlers(session, monkeypatch):
    session.event.on_wait = lambda: session.handlers[signal.SIGTERM](signal.SIGTERM, None)

    def failed_reconciliation(*_args, **_kwargs):
        raise OSError("fixture log failure")

    monkeypatch.setattr(r, "settle_open_orders", failed_reconciliation)
    with pytest.raises(OSError, match="fixture log failure"):
        r.main()
    assert_cleaned(session)


def test_occupied_lock_closes_its_socket_without_constructing_client(session):
    session.bind_error = True
    assert r.main() == 0
    assert session.socket_closed
    assert r._LOCK_SOCK is None
    assert session.handlers == session.initial_handlers
    assert not session.client_closed


def test_broker_api_disconnect_is_bounded(monkeypatch):
    calls = []

    def fake_close():
        pass

    close = fake_close
    client = SimpleNamespace(api=SimpleNamespace(close=close))
    monkeypatch.setattr(r, "_call", lambda fn, **kwargs: calls.append((fn, kwargs)))
    r._close_client(client)
    assert calls == [(close, {"timeout": r.SHUTDOWN_CLOSE_S})]
