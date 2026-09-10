"""Execution configuration validation requires no credentials or broker."""

import pytest

from execution_mode import resolve_execution


def test_paper_and_practice_never_inherit_real_default():
    env = {"IQ_DEFAULT_BALANCE": "REAL", "IQ_ALLOW_REAL": "1"}
    assert resolve_execution("paper", environ=env).balance_mode == "PRACTICE"
    practice = resolve_execution("practice", environ=env)
    assert practice.balance_mode == "PRACTICE"
    assert practice.amount == 1.0


@pytest.mark.parametrize("amount", [None, 0, -1, True, float("nan"), float("inf"), "bad"])
def test_real_requires_explicit_finite_positive_stake(amount):
    with pytest.raises(ValueError, match="amount"):
        resolve_execution("real", amount, environ={"IQ_ALLOW_REAL": "1"})


@pytest.mark.parametrize("allow", [None, "0", "true", ""])
def test_real_requires_exact_opt_in(allow):
    with pytest.raises(ValueError, match="IQ_ALLOW_REAL=1"):
        resolve_execution("real", 2.5, environ={"IQ_ALLOW_REAL": allow})


def test_explicit_real_config_is_separate_and_pending_marker_blocks(tmp_path):
    env = {"IQ_ALLOW_REAL": "1"}
    config = resolve_execution("real", 2.5, environ=env)
    assert config.amount == 2.5
    assert config.orders_enabled
    assert config.balance_mode == "REAL"
    assert config.log_dir(tmp_path) == tmp_path / "real"
    config.assert_ready(tmp_path, environ=env)
    assert list(tmp_path.iterdir()) == []
    config.pending_path(tmp_path).parent.mkdir()
    config.pending_path(tmp_path).write_text("corrupt or incomplete marker")
    with pytest.raises(ValueError, match="unresolved REAL submission"):
        config.assert_ready(tmp_path, environ=env)
    with pytest.raises(ValueError, match="IQ_ALLOW_REAL"):
        config.assert_ready(tmp_path, environ={})


def test_paper_rejects_an_order_amount():
    with pytest.raises(ValueError, match="only valid"):
        resolve_execution("paper", 1)
