"""Account selection and order guards use only an in-memory broker."""

import threading

import pytest

import server

THREADED_CALL = server._call


class FakeBroker:
    def __init__(self):
        self.connected = True
        self.mode = "PRACTICE"
        self.events = []
        self.ignore_switch = False

    def connect(self, sms_code):
        self.events.append(("connect", sms_code))
        self.connected = True
        return True, None

    def check_connect(self):
        return self.connected

    def change_balance(self, mode):
        self.events.append(("switch", mode))
        if not self.ignore_switch:
            self.mode = mode

    def get_balance_mode(self):
        return self.mode

    def get_balance(self):
        return 100.0

    def get_currency(self):
        return "TEST"

    def get_server_timestamp(self):
        return 1

    def buy(self, *args):
        self.events.append(("buy", self.mode, args))
        return True, 42

    def buy_order(self, **kwargs):
        self.events.append(("buy_order", self.mode, kwargs))
        return True, 43

    def close_position(self, order_id):
        self.events.append(("close", self.mode, order_id))
        return True

    def cancel_order(self, order_id):
        self.events.append(("cancel", self.mode, order_id))


@pytest.fixture
def broker(monkeypatch):
    fake = FakeBroker()
    monkeypatch.setenv("IQ_DEFAULT_BALANCE", "PRACTICE")
    monkeypatch.setenv("IQ_ALLOW_REAL", "0")
    monkeypatch.setattr(server, "_client", fake)
    monkeypatch.setattr(server, "_selected_balance", None)
    monkeypatch.setattr(server, "_timed_out_calls", set())
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    monkeypatch.setattr(server, "_resolve_asset", lambda asset: asset)
    monkeypatch.setattr(server, "_call", lambda fn, *args, timeout=30, **kw: fn(*args, **kw))
    return fake


def test_default_connect_selects_verified_practice(broker):
    assert server.iq_connect()["balance_mode"] == "PRACTICE"
    assert server._selected_balance == "PRACTICE"
    assert broker.events == [("connect", None), ("switch", "PRACTICE")]


@pytest.mark.parametrize("mode", ["REAL", "TOURNAMENT", "", "UNKNOWN"])
def test_invalid_or_unapproved_default_fails_before_connection(broker, monkeypatch, mode):
    monkeypatch.setenv("IQ_DEFAULT_BALANCE", mode)
    with pytest.raises(server.IQError):
        server.iq_connect()
    assert broker.events == []
    assert server._selected_balance is None


def test_real_default_requires_opt_in_and_verifies_balance(broker, monkeypatch):
    monkeypatch.setenv("IQ_DEFAULT_BALANCE", "REAL")
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    assert server.iq_connect()["balance_mode"] == "REAL"
    assert server._selected_balance == "REAL"
    server.iq_place_binary("TEST", "call", 1)
    assert broker.events[-1][0:2] == ("buy", "REAL")


def test_failed_connect_switch_never_accepts_other_balance(broker, monkeypatch):
    monkeypatch.setenv("IQ_DEFAULT_BALANCE", "REAL")
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    broker.ignore_switch = True
    with pytest.raises(server.IQError, match="Expected REAL"):
        server.iq_connect()
    assert server._selected_balance == "REAL"
    with pytest.raises(server.IQError, match="Expected REAL"):
        server.iq_place_binary("TEST", "call", 1)
    assert not any(event[0] == "buy" for event in broker.events)


def test_real_switch_fails_before_any_connection_without_opt_in(broker):
    broker.connected = False
    with pytest.raises(server.IQError, match="IQ_ALLOW_REAL=1"):
        server.iq_switch_balance("REAL")
    assert broker.events == []


def test_verified_switch_is_preserved_on_reconnect(broker, monkeypatch):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    server.iq_switch_balance("real")
    broker.connected = False
    broker.mode = "PRACTICE"
    assert server.iq_status()["balance_mode"] == "REAL"
    assert broker.events == [("switch", "REAL"), ("connect", None), ("switch", "REAL")]


def test_failed_switch_keeps_requested_selection_and_blocks_previous_balance(broker, monkeypatch):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    server.iq_switch_balance("REAL")
    broker.ignore_switch = True
    with pytest.raises(server.IQError, match="Expected PRACTICE"):
        server.iq_switch_balance("PRACTICE")
    assert server._selected_balance == "PRACTICE"
    with pytest.raises(server.IQError, match="Expected PRACTICE"):
        server.iq_place_binary("TEST", "call", 1)
    assert not any(event[0] == "buy" for event in broker.events)
    broker.ignore_switch = False
    broker.connected = False
    assert server.iq_status()["balance_mode"] == "PRACTICE"


def test_explicit_practice_switch_can_recover_after_real_opt_in_revoked(broker, monkeypatch):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    server.iq_switch_balance("REAL")
    monkeypatch.setenv("IQ_ALLOW_REAL", "0")
    broker.connected = False
    assert server.iq_switch_balance("PRACTICE")["balance_mode"] == "PRACTICE"
    assert server._selected_balance == "PRACTICE"


@pytest.mark.parametrize("actual", [None, "UNKNOWN", "TOURNAMENT", "real", 0])
def test_unknown_balance_is_rejected_even_with_real_opt_in(broker, monkeypatch, actual):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    broker.mode = actual
    with pytest.raises(server.IQError, match="Unrecognized"):
        server.iq_place_binary("TEST", "call", 1)
    assert broker.events == []


def test_unreadable_balance_refuses_submission(broker, monkeypatch):
    def unreadable():
        raise RuntimeError("fake disconnected client")

    monkeypatch.setattr(broker, "get_balance_mode", unreadable)
    with pytest.raises(server.IQError, match="Unable to verify"):
        server.iq_place_binary("TEST", "call", 1)
    assert broker.events == []


ORDER_OPERATIONS = [
    lambda: server.iq_place_binary("TEST", "call", 1),
    lambda: server.iq_place_order("forex", "TEST", "buy", 1),
    lambda: server.iq_close_position(42),
    lambda: server.iq_cancel_order(42),
]


@pytest.mark.parametrize("operation", ORDER_OPERATIONS)
def test_all_mutations_reject_balance_drift_even_when_real_allowed(broker, monkeypatch, operation):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    server.iq_switch_balance("PRACTICE")
    broker.events.clear()
    broker.mode = "REAL"
    with pytest.raises(server.IQError, match="Expected PRACTICE"):
        operation()
    assert broker.events == []


@pytest.mark.parametrize("operation", ORDER_OPERATIONS)
def test_all_mutations_recheck_real_opt_in(broker, monkeypatch, operation):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    server.iq_switch_balance("REAL")
    monkeypatch.setenv("IQ_ALLOW_REAL", "0")
    broker.events.clear()
    with pytest.raises(server.IQError, match="IQ_ALLOW_REAL=1"):
        operation()
    assert broker.events == []


@pytest.mark.parametrize("operation", ORDER_OPERATIONS)
def test_all_mutations_reconnect_to_selected_real_balance(broker, monkeypatch, operation):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    server.iq_switch_balance("REAL")
    broker.connected = False
    broker.mode = "PRACTICE"
    operation()
    assert broker.events[-2] == ("switch", "REAL")
    assert broker.events[-1][1] == "REAL"


def test_switch_cannot_interleave_balance_guard_and_submission(broker, monkeypatch):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    buying = threading.Event()
    release_buy = threading.Event()
    switching = threading.Event()
    errors = []

    def buy(*args):
        buying.set()
        assert release_buy.wait(5), "test did not release the fake order"
        broker.events.append(("buy", broker.mode, args))
        return True, 42

    def order_worker():
        try:
            server.iq_place_binary("TEST", "call", 1)
        except Exception as exc:
            errors.append(exc)

    def switch_worker():
        try:
            switching.set()
            server.iq_switch_balance("REAL")
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(broker, "buy", buy)
    order = threading.Thread(target=order_worker)
    switch = threading.Thread(target=switch_worker)
    order.start()
    try:
        assert buying.wait(5)
        switch.start()
        assert switching.wait(5)
        acquired = server._account_lock.acquire(blocking=False)
        if acquired:
            server._account_lock.release()
        assert not acquired
        assert broker.mode == "PRACTICE"
    finally:
        release_buy.set()
        order.join(5)
        if switch.ident is not None:
            switch.join(5)
    assert not order.is_alive() and not switch.is_alive()
    assert errors == []
    assert [event[:2] for event in broker.events] == [("buy", "PRACTICE"), ("switch", "REAL")]


def test_timed_out_order_blocks_account_changes_until_worker_exits(broker, monkeypatch):
    monkeypatch.setenv("IQ_ALLOW_REAL", "1")
    release_buy = threading.Event()
    started = threading.Event()
    original_buy = broker.buy

    def delayed_buy(*args):
        started.set()
        assert release_buy.wait(5), "test did not release the fake broker worker"
        return original_buy(*args)

    monkeypatch.setattr(broker, "buy", delayed_buy)
    monkeypatch.setattr(
        server, "_call",
        lambda fn, *args, timeout=30, **kw: THREADED_CALL(
            fn, *args, timeout=0.02 if fn is delayed_buy else 5, **kw,
        ),
    )
    done = None
    try:
        with pytest.raises(server.IQError, match="not cancelled.*reconcile broker history"):
            server.iq_place_binary("TEST", "call", 1)
        assert started.wait(5)
        with server._inflight_lock:
            assert len(server._timed_out_calls) == 1
            done = next(iter(server._timed_out_calls))
        for operation in [
            server.iq_connect,
            lambda: server.iq_switch_balance("REAL"),
            server.iq_reset_practice_balance,
            *ORDER_OPERATIONS,
        ]:
            with pytest.raises(server.IQError, match="still in flight"):
                operation()
        assert broker.events == []
        assert server._selected_balance is None
    finally:
        release_buy.set()
        if done is not None:
            assert done.wait(5), "fake broker worker did not finish"
    with server._inflight_lock:
        assert server._timed_out_calls == set()
    # The supposedly timed-out operation did execute. Its unknown outcome is
    # why a caller must reconcile history before deciding whether to retry it.
    assert [event[:2] for event in broker.events] == [("buy", "PRACTICE")]
    assert server.iq_switch_balance("PRACTICE")["balance_mode"] == "PRACTICE"
