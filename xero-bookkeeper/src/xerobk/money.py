"""Money handling.

Every monetary value in this application is a ``Decimal``. Floats are never
used for money, not even transiently: ``0.1 + 0.2 != 0.3`` is an accounting bug
waiting to be reported to the ATO.

Xero's API returns amounts as JSON numbers, which means they arrive as Python
floats if parsed naively. :func:`to_money` exists to convert at the boundary,
via ``str()``, so the decimal value is the one that was printed rather than the
binary approximation.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")

# Australian GST. Stored as a rate rather than a divisor so the arithmetic below
# reads the way the ATO worksheets describe it.
GST_RATE = Decimal("0.10")


def to_money(value: Any) -> Decimal:
    """Coerce an arbitrary API/CSV value to a 2dp Decimal.

    Accepts ``None`` (-> zero), strings, ints, Decimals, and floats. Floats go
    through ``repr`` so that ``0.1`` becomes ``Decimal("0.1")`` and not
    ``Decimal("0.1000000000000000055511151231257827021181583404541015625")``.
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
    # Tolerate the shapes bank CSVs actually ship: "$1,234.56", "(45.00)" for
    # negatives, and a trailing "CR"/"DR" marker.
    negative = False
    if text.upper().endswith(("CR", "DR")):
        # CR is a credit (money in, positive); DR is a debit (money out).
        marker = text[-2:].upper()
        text = text[:-2].strip()
        negative = marker == "DR"
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

    Python's default is banker's rounding (ROUND_HALF_EVEN), which disagrees
    with what Xero and the ATO do at the half-cent. Rounding 0.125 must give
    0.13, not 0.12.
    """
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def gst_from_inclusive(gross: Decimal, rate: Decimal = GST_RATE) -> Decimal:
    """GST contained within a GST-inclusive amount.

    At the standard 10% rate this is the familiar "divide by 11".
    """
    return quantize(gross * rate / (Decimal("1") + rate))


def gst_from_exclusive(net: Decimal, rate: Decimal = GST_RATE) -> Decimal:
    """GST to add on top of a GST-exclusive amount."""
    return quantize(net * rate)


def net_from_inclusive(gross: Decimal, rate: Decimal = GST_RATE) -> Decimal:
    """The ex-GST portion of a GST-inclusive amount."""
    return quantize(gross - gst_from_inclusive(gross, rate))


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
