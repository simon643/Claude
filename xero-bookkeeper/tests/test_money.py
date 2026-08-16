"""Money arithmetic — the layer where a bug silently produces wrong figures."""

from __future__ import annotations

from decimal import Decimal

import pytest

from xerobk.money import (
    ZERO,
    fmt,
    gst_from_exclusive,
    gst_from_inclusive,
    net_from_inclusive,
    pct,
    quantize,
    to_money,
)


class TestToMoney:
    def test_none_and_empty_are_zero(self) -> None:
        assert to_money(None) == ZERO
        assert to_money("") == ZERO
        assert to_money("   ") == ZERO

    def test_float_goes_through_repr_not_binary(self) -> None:
        # The whole reason to_money exists: Decimal(0.1) is 0.1000000000000000055…
        assert to_money(0.1) == Decimal("0.10")
        assert to_money(1234.56) == Decimal("1234.56")

    def test_float_addition_stays_exact(self) -> None:
        assert to_money(0.1) + to_money(0.2) == Decimal("0.30")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$1,234.56", "1234.56"),
            ("1,234.56", "1234.56"),
            ("(45.00)", "-45.00"),
            ("45.00 CR", "45.00"),
            ("45.00 DR", "-45.00"),
            ("-99.99", "-99.99"),
            ("  12.5  ", "12.50"),
        ],
    )
    def test_bank_csv_shapes(self, raw: str, expected: str) -> None:
        assert to_money(raw) == Decimal(expected)

    def test_rejects_nonsense(self) -> None:
        with pytest.raises(ValueError):
            to_money("not a number")

    def test_lone_dash_is_zero(self) -> None:
        # Bank exports use "-" for a nil cell in a debit/credit column.
        assert to_money("-") == ZERO


class TestRounding:
    def test_half_cent_rounds_up_not_to_even(self) -> None:
        # Python's default is banker's rounding, which would give 0.12 here.
        assert quantize(Decimal("0.125")) == Decimal("0.13")
        assert quantize(Decimal("0.135")) == Decimal("0.14")

    def test_negative_half_cent_rounds_away_from_zero(self) -> None:
        assert quantize(Decimal("-0.125")) == Decimal("-0.13")


class TestGst:
    def test_inclusive_is_one_eleventh(self) -> None:
        assert gst_from_inclusive(Decimal("110.00")) == Decimal("10.00")
        assert gst_from_inclusive(Decimal("1100.00")) == Decimal("100.00")

    def test_exclusive_is_ten_percent(self) -> None:
        assert gst_from_exclusive(Decimal("100.00")) == Decimal("10.00")

    def test_net_plus_gst_reconstructs_gross(self) -> None:
        gross = Decimal("11000.00")
        assert net_from_inclusive(gross) + gst_from_inclusive(gross) == gross

    def test_matches_the_figures_xero_reports(self) -> None:
        # A GST-inclusive invoice splits 11:1, the way Xero shows it.
        gross = Decimal("11000.00")
        assert gst_from_inclusive(gross) == Decimal("1000.00")
        assert net_from_inclusive(gross) == Decimal("10000.00")

    @pytest.mark.parametrize(
        "gross",
        # Amounts that do NOT divide evenly by 11, so the split has to round and
        # the two halves must still add back to the original to the cent.
        ["1234.57", "0.05", "99.99", "45751.23", "7.77", "1000000.01"],
    )
    def test_rounding_never_loses_a_cent(self, gross: str) -> None:
        amount = Decimal(gross)
        assert net_from_inclusive(amount) + gst_from_inclusive(amount) == amount


class TestFormatting:
    def test_thousands_and_sign(self) -> None:
        assert fmt(Decimal("1234.56")) == "$1,234.56"
        assert fmt(Decimal("-1234.56")) == "-$1,234.56"
        assert fmt(ZERO) == "$0.00"

    def test_pct_is_zero_safe(self) -> None:
        assert pct(Decimal("10"), ZERO) == ZERO
        assert pct(Decimal("25"), Decimal("100")) == Decimal("25.00")
