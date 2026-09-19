"""Money handling.

Every monetary value in this application is a ``Decimal``. Floats are never
used for money, not even transiently: an approval limit that lets $10,000.00
through because it compared as 9999.999999999998 is not a rounding curiosity,
it is a control failure.

Xero returns amounts as JSON numbers, so a naive parse yields floats.
:func:`to_money` converts at the boundary via ``repr``, so the decimal value is
the one that was printed rather than its binary approximation.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")


def to_money(value: Any) -> Decimal:
    """Coerce an arbitrary API/JSON value to a 2dp Decimal.

    Accepts ``None`` (-> zero), strings, ints, Decimals and floats. Tolerates
    the shapes that turn up in hand-edited config: ``"$1,234.56"``, ``"(45.00)"``
    for a negative, and stray whitespace.
    """
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return quantize(value)
    if isinstance(value, float):
        return quantize(Decimal(repr(value)))
    if isinstance(value, int):
        return quantize(Decimal(value))
    text = str(value).strip()
    if not text:
        return ZERO
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    if not text or text in {"-", "."}:
        return ZERO
    try:
        amount = quantize(Decimal(text))
    except InvalidOperation:
        raise ValueError(f"not a monetary value: {value!r}")
    return -amount if negative else amount


def quantize(value: Decimal) -> Decimal:
    """Round to 2dp using ROUND_HALF_UP.

    Python's default is banker's rounding, which disagrees with what Xero does
    at the half-cent. A one-cent disagreement between the amount an approver
    saw and the amount posted is a re-approval trigger, so it matters here more
    than it looks.
    """
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def fmt(value: Decimal, currency: str = "AUD") -> str:
    """Format for display: ``$1,234.56`` / ``-$1,234.56``."""
    symbol = {"AUD": "$", "NZD": "$", "USD": "$", "GBP": "£", "EUR": "€"}.get(currency, "")
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def pct(part: Decimal, whole: Decimal) -> Decimal:
    """Percentage of ``whole`` that ``part`` represents; zero-safe."""
    if whole == 0:
        return ZERO
    return quantize(part / whole * Decimal("100"))
