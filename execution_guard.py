"""Legacy binary-contract check, retained for offline compatibility tests.

This helper is not the platform execution gateway and must not be exposed
through MCP. New execution uses atlas.service.AtlasService and its durable
proposal, risk, approval and reconciliation path. Legacy REAL CLIs refuse.
"""

import math

from instruments import InstrumentSpec, verify_contract


def guarded_buy(
    client,
    call,
    manifest: dict,
    spec: InstrumentSpec,
    amount: float,
    direction: str,
    timeout: int = 60,
):
    """Verify the bundle contract, then (and only then) place the order.

    `call` is the timeout wrapper (run_once._call) so the broker call keeps
    the same hang protection as every other library call."""
    verify_contract(
        manifest,
        expiry_seconds=spec.expiry_minutes * 60,
        order_active=spec.order_active,
        option_kind=spec.option_kind,
    )
    if direction not in ("call", "put"):
        raise ValueError(f"direction must be 'call' or 'put', got {direction!r}")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        raise ValueError("amount must be finite and positive")
    return call(
        client.buy, amount, spec.order_active, direction, spec.expiry_minutes, timeout=timeout
    )
