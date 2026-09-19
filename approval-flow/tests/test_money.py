"""Money is Decimal, always, and rounds the way the ATO does."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apflow.money import ZERO, fmt, pct, quantize, to_money


def test_none_and_blank_are_zero() -> None:
    assert to_money(None) == ZERO
    assert to_money("") == ZERO
    assert to_money("   ") == ZERO


def test_floats_go_through_repr_not_binary_expansion() -> None:
    assert to_money(0.1) == Decimal("0.10")
    assert to_money(1234.565) == Decimal("1234.57")


def test_accepts_the_shapes_config_files_actually_contain() -> None:
    assert to_money("$1,234.56") == Decimal("1234.56")
    assert to_money("(45.00)") == Decimal("-45.00")
    assert to_money(" 99 ") == Decimal("99.00")


def test_rejects_nonsense_loudly() -> None:
    with pytest.raises(ValueError):
        to_money("twelve dollars")


def test_half_cent_rounds_up_not_to_even() -> None:
    # Python's default would give 0.12 here, which is not what Xero does.
    assert quantize(Decimal("0.125")) == Decimal("0.13")
    assert quantize(Decimal("0.135")) == Decimal("0.14")


def test_formatting_keeps_the_sign_outside_the_symbol() -> None:
    assert fmt(Decimal("-1234.5")) == "-$1,234.50"
    assert fmt(Decimal("1234.5"), "EUR") == "€1,234.50"


def test_percentage_is_zero_safe() -> None:
    assert pct(Decimal("5"), ZERO) == ZERO
    assert pct(Decimal("25"), Decimal("200")) == Decimal("12.50")
