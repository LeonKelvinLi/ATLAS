"""Explicit execution selection shared by the runner and supervisor.

Resolving a mode never connects to a broker or creates state. Amounts are in
the selected account's currency, and REAL never inherits the practice stake.
"""

from dataclasses import dataclass
import math
import os
from pathlib import Path

REAL_ORDER_BLOCKED_EXIT = 3
PENDING_ORDER_FILE = "order_submission_pending.json"


class PendingSubmissionError(ValueError):
    """A previous REAL submission needs operator reconciliation."""


@dataclass(frozen=True)
class ExecutionConfig:
    mode: str
    amount: float | None

    @property
    def balance_mode(self) -> str:
        return "REAL" if self.mode == "real" else "PRACTICE"

    @property
    def orders_enabled(self) -> bool:
        return self.mode != "paper"

    def log_dir(self, base: Path) -> Path:
        return base / "real" if self.mode == "real" else base

    def pending_path(self, base: Path) -> Path:
        return self.log_dir(base) / PENDING_ORDER_FILE

    def assert_ready(self, base: Path, environ=None) -> None:
        if self.mode != "real":
            return
        env = os.environ if environ is None else environ
        if env.get("IQ_ALLOW_REAL") != "1":
            raise ValueError("real mode requires IQ_ALLOW_REAL=1")
        if os.path.lexists(self.pending_path(base)):
            raise PendingSubmissionError(
                f"unresolved REAL submission: {self.pending_path(base)}; "
                "reconcile broker history before resuming"
            )


def resolve_execution(mode: str, amount=None, environ=None) -> ExecutionConfig:
    env = os.environ if environ is None else environ
    if mode not in {"paper", "practice", "real"}:
        raise ValueError("mode must be paper, practice, or real")
    if mode == "paper":
        if amount is not None:
            raise ValueError("--amount is only valid for practice or real orders")
        return ExecutionConfig(mode, None)
    if mode == "real":
        if env.get("IQ_ALLOW_REAL") != "1":
            raise ValueError("real mode requires IQ_ALLOW_REAL=1")
        if amount is None:
            raise ValueError("real mode requires an explicit --amount in account currency")
    elif amount is None:
        amount = 1.0
    if isinstance(amount, bool):
        raise ValueError("--amount must be a finite positive number")
    try:
        amount = float(amount)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("--amount must be a finite positive number") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("--amount must be a finite positive number")
    return ExecutionConfig(mode, amount)
