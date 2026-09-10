"""Trusted human approval receipts; never register the issuer as an AI tool.

The operator approval entrypoint supplies a server-injected secret and shows
an immutable proposal before issuing a receipt. The MCP execution service
receives only verification capability through its configured code path: no
tool may accept a key, call the issuer, or claim human identity. HMAC uses a
shared secret, so this boundary depends on process/host trust; a compromised
server possessing that secret can forge receipts. Stronger deployment
isolation can replace HMAC with operator-private/public-key signatures.
"""

import hashlib
import hmac
import uuid

from atlas.ledger import canonical_json, digest


class ApprovalError(ValueError):
    pass


_FIELDS = frozenset({"version", "proposal_id", "proposal_hash", "issued_at", "expires_at", "nonce"})


def _key(key) -> bytes:
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not isinstance(key, bytes) or len(key) < 32:
        raise ApprovalError("approval key must contain at least 32 bytes and come from trusted secret injection")
    return key


def _time(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApprovalError("approval timestamps must be nonnegative integers")
    return value


def _proposal_times(proposal, now):
    if not isinstance(proposal, dict) or not isinstance(proposal.get("id"), str) or not proposal["id"].strip():
        raise ApprovalError("approval requires an identified proposal")
    created, expires = _time(proposal.get("created_at")), _time(proposal.get("expires_at"))
    now = _time(now)
    if created >= expires or now < created or now >= expires:
        raise ApprovalError("proposal is expired or not yet valid")
    return created, expires


def _signature(claims, key):
    return hmac.new(key, canonical_json(claims).encode("utf-8"), hashlib.sha256).hexdigest()


def issue_approval(proposal: dict, key, *, now: int, expires_at: int | None = None,
                   nonce: str | None = None) -> dict:
    """Operator-only issuer. Approval does not claim or execute the proposal."""
    secret = _key(key)
    _, proposal_expiry = _proposal_times(proposal, now)
    expiry = proposal_expiry if expires_at is None else _time(expires_at)
    if not now < expiry <= proposal_expiry:
        raise ApprovalError("approval expiry must follow approval and not exceed proposal expiry")
    nonce = uuid.uuid4().hex if nonce is None else nonce
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128 or not nonce.strip():
        raise ApprovalError("approval nonce must contain 16 to 128 characters")
    claims = {"version": 1, "proposal_id": proposal["id"], "proposal_hash": digest(proposal),
              "issued_at": now, "expires_at": expiry, "nonce": nonce}
    return {**claims, "signature": _signature(claims, secret)}


def verify_approval(receipt: dict, proposal: dict, key, *, now: int) -> dict:
    """Verify the exact snapshot and expiry; callers must also claim once durably."""
    secret = _key(key)
    created, proposal_expiry = _proposal_times(proposal, now)
    if not isinstance(receipt, dict) or set(receipt) != _FIELDS | {"signature"}:
        raise ApprovalError("approval receipt has an invalid shape")
    claims = {name: receipt[name] for name in _FIELDS}
    signature = receipt["signature"]
    if not isinstance(signature, str) or len(signature) != 64:
        raise ApprovalError("approval signature is invalid")
    try:
        valid = hmac.compare_digest(_signature(claims, secret), signature)
    except (ValueError, TypeError) as exc:
        raise ApprovalError("approval claims are invalid") from exc
    if not valid:
        raise ApprovalError("approval signature does not match")
    if type(claims["version"]) is not int or claims["version"] != 1:
        raise ApprovalError("approval version is unsupported")
    if claims["proposal_id"] != proposal["id"] or claims["proposal_hash"] != digest(proposal):
        raise ApprovalError("approval does not match the immutable proposal")
    issued, expiry = _time(claims["issued_at"]), _time(claims["expires_at"])
    if not created <= issued <= now < expiry <= proposal_expiry:
        raise ApprovalError("approval is expired or not yet valid")
    nonce = claims["nonce"]
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128 or not nonce.strip():
        raise ApprovalError("approval nonce is invalid")
    return claims
