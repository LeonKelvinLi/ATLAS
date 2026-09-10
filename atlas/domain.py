"""Typed, finite financial data; quantities never travel through binary floats."""

from dataclasses import asdict, dataclass, field
from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
from functools import wraps
from typing import Literal

AssetClass = Literal["equity", "crypto", "fx_binary"]
Mode = Literal["research", "paper", "live-assisted", "live-auto"]

# Input values carry at most 64 significant digits and 36 integer/fractional
# places. Precision 256 covers the bounded monetary products/sums and risk
# comparisons; no caller or library's ambient Decimal context may change them.
FINANCIAL_PRECISION = 256
FINANCIAL_QUANTUM = Decimal("1e-36")


def financial_context(function):
    """Isolate financial arithmetic from caller precision, rounding and traps."""
    @wraps(function)
    def isolated(*args, **kwargs):
        with localcontext(Context(prec=FINANCIAL_PRECISION, rounding=ROUND_HALF_EVEN,
                                  Emin=-999, Emax=999, capitals=1, clamp=0, flags=[],
                                  traps=[InvalidOperation, DivisionByZero, Overflow])):
            return function(*args, **kwargs)
    return isolated


def decimal(value, *, positive=False, nonnegative=False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a financial amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid financial amount") from exc
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError("financial amount outside permitted range")
    sign, digits, exponent = result.as_tuple()
    if not any(digits):
        return Decimal("0")
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    if len(digits) > 64 or max(len(digits) + exponent, 0) > 36 or max(-exponent, 0) > 36:
        raise ValueError("financial amount exceeds 64 significant digits or 36 integer/fractional places")
    if exponent > 0:
        digits.extend([0] * exponent)
        exponent = 0  # Preserve ordinary monetary strings such as '50', not '5E+1'.
    return Decimal((sign, tuple(digits), exponent))


@financial_context
def financial_value(value, *, rounding=ROUND_HALF_EVEN) -> Decimal:
    """Book derived simulator values at 36 fractional places; reject overflow.

    This is an explicit internal accounting convention, not a broker tick or
    currency-unit assumption. Callers estimating fees can round upwards.
    External amounts use decimal() and are never silently rounded here.
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("derived financial value must be a finite Decimal")
    return decimal(value.quantize(FINANCIAL_QUANTUM, rounding=rounding))


def json_value(value):
    if hasattr(value, "__dataclass_fields__"):
        return json_value(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


@dataclass(frozen=True)
class BrokerCapabilities:
    broker: str
    asset_classes: tuple[str, ...]
    account_modes: tuple[str, ...]
    order_types: tuple[str, ...] = ("limit",)
    fractional_quantities: bool = True
    cancellation: bool = True
    client_order_lookup: bool = True
    server_idempotency: bool = False
    official: bool = False
    network_enabled: bool = False
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    market_value: Decimal | None
    cost_basis: Decimal | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str = field(repr=False)
    mode: str
    currency: str
    equity: Decimal | None
    cash: Decimal | None
    buying_power: Decimal | None
    positions: tuple[Position, ...]
    observed_at: int
    daily_realized_pnl: Decimal | None = None
    tradable: bool = True
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Quote:
    symbol: str
    asset_class: str
    bid: Decimal
    ask: Decimal
    timestamp: int
    market_open: bool | None
    available_quantity: Decimal | None
    source: str
    timestamp_basis: str = "provider"
    fee_per_unit: Decimal = Decimal("0")
    quantity_increment: Decimal | None = None
    minimum_quantity: Decimal | None = None
    currency: str = "USD"


@dataclass(frozen=True)
class OrderRequest:
    account_id: str = field(repr=False)
    symbol: str
    asset_class: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    client_order_id: str
    order_type: str = "limit"


@dataclass(frozen=True)
class OrderResult:
    broker_order_id: str | None
    client_order_id: str
    status: str
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    fees: Decimal | None = None
    realized_pnl: Decimal | None = None
    reason_code: str | None = None


class BrokerError(Exception):
    """Safe public error code; never include raw credentials or HTTP bodies."""


class BrokerRejected(BrokerError):
    """A confirmed refusal with no order accepted."""


class UnknownSubmission(BrokerError):
    """The request may have reached the broker; reconciliation is required."""


class UnsupportedCapability(BrokerError):
    """No verified contract exists for the requested capability."""
