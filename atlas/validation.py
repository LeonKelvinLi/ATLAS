"""Deterministic chronological contracts for new research, with no I/O."""

import math
from numbers import Integral


def validate_gap(horizon: int, gap: int | None = None) -> int:
    if isinstance(horizon, bool) or not isinstance(horizon, Integral) or horizon < 1:
        raise ValueError("horizon must be a positive integer")
    gap = horizon if gap is None else gap
    if isinstance(gap, bool) or not isinstance(gap, Integral) or gap < horizon:
        raise ValueError("gap must be an integer at least as large as the label horizon")
    return int(gap)


def validate_chronology(timestamps, *, now=None):
    previous = None
    for value in timestamps:
        if isinstance(value, bool) or not isinstance(value, (int, float, Integral)):
            raise ValueError("timestamps must be finite UNIX seconds")
        if not math.isfinite(value) or value < 0 or int(value) != value:
            raise ValueError("timestamps must be nonnegative integer UNIX seconds")
        if previous is not None and value <= previous:
            raise ValueError("timestamps must be strictly increasing and unique")
        if now is not None and value > now:
            raise ValueError("future or unfinished bars are not permitted")
        previous = value


def chronological_folds(n_rows, *, train_size, test_size, horizon=1, gap=None):
    """Expanding training windows; future test labels never enter training.

    Indices address a validated single series. Callers that fit models must
    also purge by actual label resolution timestamps for irregular horizons.
    """
    gap = validate_gap(horizon, gap)
    for value in (n_rows, train_size, test_size):
        if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
            raise ValueError("row/window sizes must be positive integers")
    start = train_size + gap
    while start < n_rows:
        yield range(0, start - gap), range(start, min(start + test_size, n_rows))
        start += test_size
