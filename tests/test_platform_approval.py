"""Approval verification is deterministic and broker independent."""

import copy

import pytest

from atlas.approval import ApprovalError, issue_approval, verify_approval
from atlas.ledger import Ledger

KEY = b"fixture-only-approval-key-32-bytes!"
PROPOSAL = {
    "id": "proposal-1", "signal_id": "signal-1", "mode": "live-assisted",
    "account_fingerprint": "a" * 64, "policy_hash": "b" * 64,
    "order": {"symbol": "TEST", "quantity": "2", "limit_price": "10"},
    "evidence": {"source": "fixture"}, "created_at": 100, "expires_at": 200,
}


def test_receipt_binds_snapshot_and_does_not_contain_key():
    receipt = issue_approval(PROPOSAL, KEY, now=110)
    claims = verify_approval(receipt, PROPOSAL, KEY, now=150)
    assert claims["proposal_id"] == "proposal-1"
    assert claims["expires_at"] == 200
    assert KEY.decode() not in str(receipt)
    assert "signature" not in claims


@pytest.mark.parametrize("key", [None, "short", b"", b"a" * 31])
def test_approval_needs_trusted_strong_key(key):
    with pytest.raises(ApprovalError, match="32 bytes"):
        issue_approval(PROPOSAL, key, now=110)


@pytest.mark.parametrize("field,value", [("id", "proposal-2"), ("account_fingerprint", "c" * 64),
                                        ("mode", "live-auto"), ("policy_hash", "d" * 64),
                                        ("expires_at", 300)])
def test_receipt_cannot_authorize_changed_proposal(field, value):
    receipt = issue_approval(PROPOSAL, KEY, now=110)
    changed = copy.deepcopy(PROPOSAL)
    changed[field] = value
    with pytest.raises(ApprovalError, match="immutable proposal"):
        verify_approval(receipt, changed, KEY, now=120)


def test_receipt_cannot_authorize_changed_order_quantity():
    receipt = issue_approval(PROPOSAL, KEY, now=110)
    changed = copy.deepcopy(PROPOSAL)
    changed["order"]["quantity"] = "200"
    with pytest.raises(ApprovalError, match="immutable proposal"):
        verify_approval(receipt, changed, KEY, now=120)


@pytest.mark.parametrize("now", [99, 200, 201])
def test_proposal_cannot_be_approved_outside_lifetime(now):
    with pytest.raises(ApprovalError, match="expired or not yet valid"):
        issue_approval(PROPOSAL, KEY, now=now)


def test_approval_expiry_is_stricter_than_proposal_expiry():
    receipt = issue_approval(PROPOSAL, KEY, now=110, expires_at=150)
    verify_approval(receipt, PROPOSAL, KEY, now=149)
    with pytest.raises(ApprovalError, match="expired"):
        verify_approval(receipt, PROPOSAL, KEY, now=150)
    with pytest.raises(ApprovalError, match="not yet valid"):
        verify_approval(receipt, PROPOSAL, KEY, now=109)
    with pytest.raises(ApprovalError, match="not exceed"):
        issue_approval(PROPOSAL, KEY, now=110, expires_at=201)


@pytest.mark.parametrize("field,value", [("nonce", "another-fixture-nonce"), ("issued_at", 100),
                                        ("expires_at", 199), ("version", 2)])
def test_receipt_claim_tampering_fails_signature(field, value):
    receipt = issue_approval(PROPOSAL, KEY, now=110)
    receipt[field] = value
    with pytest.raises(ApprovalError, match="signature"):
        verify_approval(receipt, PROPOSAL, KEY, now=120)


def test_caller_cannot_self_declare_human_approval_or_change_key():
    receipt = issue_approval(PROPOSAL, KEY, now=110)
    with pytest.raises(ApprovalError, match="signature"):
        verify_approval(receipt, PROPOSAL, b"other-fixture-key-with-32-bytes!!!", now=120)
    receipt["approved_by"] = "human"
    with pytest.raises(ApprovalError, match="invalid shape"):
        verify_approval(receipt, PROPOSAL, KEY, now=120)


def test_receipt_replay_still_requires_one_durable_intent(tmp_path):
    receipt = issue_approval(PROPOSAL, KEY, now=110)
    with Ledger(tmp_path / "execution.db", "live") as ledger:
        ledger.save_proposal(PROPOSAL, "key-1")
        verify_approval(receipt, PROPOSAL, KEY, now=120)
        first = ledger.claim_intent("proposal-1", 120, "mcp:fixture")
        verify_approval(receipt, PROPOSAL, KEY, now=121)
        again = ledger.claim_intent("proposal-1", 121, "mcp:fixture")
        assert again["replayed"]
        assert first["client_order_id"] == again["client_order_id"]
        assert len(ledger.list_intents()) == 1
