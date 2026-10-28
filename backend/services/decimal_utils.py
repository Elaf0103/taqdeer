"""
Decimal utilities for score arithmetic
=======================================
All grading, aggregation, and export code must import and use ``D()`` for
every value that participates in score arithmetic.

Why this exists
---------------
SQLAlchemy ``Numeric(6, 2)`` columns return ``decimal.Decimal`` objects.
LLM-parsing helpers, ``_round_quarter``, and old code paths return plain
Python ``float``.  Python raises::

    TypeError: unsupported operand type(s) for +: 'decimal.Decimal' and 'float'

the moment a Decimal accumulator encounters a float operand — even inside
Python's built-in ``sum()``.

``D()`` is the single, safe conversion gate:

- ``None``    → ``Decimal("0")``
- ``Decimal`` → returned as-is  (no copy, no string round-trip overhead)
- ``int``     → ``Decimal(int_value)``   (exact, lossless)
- ``float``   → ``Decimal(str(float_value))``  (str-route avoids binary
              precision artifacts like ``Decimal(0.1) == Decimal("0.1000000000…")``)
- anything else → ``Decimal(str(value))``

Usage
-----
::

    from services.decimal_utils import D
    from decimal import Decimal

    total = sum((D(r.score) for r in rows), Decimal("0"))
    avg   = total / len(rows) if rows else Decimal("0")
"""

from decimal import Decimal, InvalidOperation
from typing import Union


_Number = Union[int, float, Decimal, None, str]


def D(value: _Number) -> Decimal:
    """
    Safely coerce *value* to ``Decimal``.

    Never raises — bad values silently become ``Decimal("0")`` and a
    ``[DECIMAL-WARN]`` line is printed so bad callers are visible in logs.
    """
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # str-route: avoids Decimal(0.1) → 0.1000000000000000055511151231257827021181583404541015625
        return Decimal(str(value))
    # str / unknown
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        print(f"[DECIMAL-WARN] Could not convert {value!r} ({type(value).__name__}) to Decimal — using 0")
        return Decimal("0")


def safe_avg(total: Decimal, count: int) -> Decimal:
    """Return ``total / count`` rounded to 2 dp, or ``Decimal("0")`` if count is 0."""
    if not count:
        return Decimal("0")
    return round(D(total) / count, 2)
