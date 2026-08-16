"""Bank statement CSV sniffing across the formats AU banks actually export."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from xerobk.providers.bankcsv import parse_bank_csv, parse_bank_date


class TestDateParsing:
    def test_day_first_is_the_default(self) -> None:
        # 03/04/2026 is 3 April in Australia, not 4 March. Getting this
        # backwards silently mis-dates every transaction before the 13th.
        assert parse_bank_date("03/04/2026") == date(2026, 4, 3)

    def test_iso_and_written_forms(self) -> None:
        assert parse_bank_date("2026-04-03") == date(2026, 4, 3)
        assert parse_bank_date("03-Apr-2026") == date(2026, 4, 3)
        assert parse_bank_date("3 April 2026") == date(2026, 4, 3)

    def test_unparseable_returns_none(self) -> None:
        assert parse_bank_date("Opening Balance") is None
        assert parse_bank_date("") is None


class TestParsing:
    def test_signed_amount_column(self) -> None:
        csv = (
            "Date,Description,Amount,Balance\n"
            "01/07/2026,DIRECT CREDIT ACME,1100.00,5000.00\n"
            "02/07/2026,BANK FEE,-15.00,4985.00\n"
        )
        lines = parse_bank_csv(csv)
        assert len(lines) == 2
        assert lines[0].amount == Decimal("1100.00")
        assert lines[0].is_credit
        assert lines[1].amount == Decimal("-15.00")
        assert lines[1].is_debit

    def test_split_debit_credit_columns(self) -> None:
        # Westpac-style: debits in their own column, written positive.
        csv = (
            "Date,Narrative,Debit Amount,Credit Amount,Balance\n"
            "01/07/2026,PAYMENT RECEIVED,,1100.00,5000.00\n"
            "02/07/2026,SUPPLIER PAYMENT,450.00,,4550.00\n"
        )
        lines = parse_bank_csv(csv)
        assert lines[0].amount == Decimal("1100.00")
        assert lines[1].amount == Decimal("-450.00")

    def test_headerless_positional(self) -> None:
        csv = "01/07/2026,1100.00,DIRECT CREDIT ACME,5000.00\n"
        lines = parse_bank_csv(csv)
        assert len(lines) == 1
        assert lines[0].amount == Decimal("1100.00")
        assert "ACME" in lines[0].description

    def test_preamble_junk_is_skipped(self) -> None:
        csv = (
            "Business Transaction Account\n"
            "Exported 16/08/2026\n"
            "\n"
            "Date,Description,Amount\n"
            "01/07/2026,DIRECT CREDIT ACME,1100.00\n"
        )
        lines = parse_bank_csv(csv)
        assert len(lines) == 1

    def test_currency_symbols_and_brackets(self) -> None:
        csv = (
            "Date,Description,Amount\n"
            '01/07/2026,PAYMENT,"$1,100.00"\n'
            "02/07/2026,REFUND,(45.00)\n"
        )
        lines = parse_bank_csv(csv)
        assert lines[0].amount == Decimal("1100.00")
        assert lines[1].amount == Decimal("-45.00")

    def test_semicolon_delimiter(self) -> None:
        csv = "Date;Description;Amount\n01/07/2026;PAYMENT;1100.00\n"
        lines = parse_bank_csv(csv)
        assert len(lines) == 1
        assert lines[0].amount == Decimal("1100.00")

    def test_rows_without_a_date_are_skipped(self) -> None:
        # Trailing totals lines must not abort the import.
        csv = (
            "Date,Description,Amount\n"
            "01/07/2026,PAYMENT,1100.00\n"
            ",TOTAL,1100.00\n"
        )
        lines = parse_bank_csv(csv)
        assert len(lines) == 1

    def test_empty_input(self) -> None:
        assert parse_bank_csv("") == []
        assert parse_bank_csv("   \n  \n") == []

    def test_line_ids_are_stable_across_reimport(self) -> None:
        csv = "Date,Description,Amount\n01/07/2026,PAYMENT,1100.00\n"
        first = parse_bank_csv(csv)
        second = parse_bank_csv(csv)
        assert [line.line_id for line in first] == [line.line_id for line in second]

    def test_identical_rows_get_distinct_ids(self) -> None:
        # Two genuinely identical transactions on one day must not collapse
        # into one, or the second would be treated as already coded.
        csv = (
            "Date,Description,Amount\n"
            "01/07/2026,PAYMENT,50.00\n"
            "01/07/2026,PAYMENT,50.00\n"
        )
        lines = parse_bank_csv(csv)
        assert len({line.line_id for line in lines}) == 2

    def test_demo_file_parses(self) -> None:
        from pathlib import Path

        from xerobk.providers.bankcsv import read_bank_csv

        path = Path(__file__).resolve().parents[1] / "data" / "demo" / "bank_lines.csv"
        lines = read_bank_csv(path)
        assert len(lines) == 14
        # Four receipts: the Kestrel payment, the Ashgrove payment, the
        # Harbourline batch transfer, and the related-entity transfer.
        assert sum(1 for line in lines if line.is_credit) == 4
        assert sum(1 for line in lines if line.is_debit) == 10
