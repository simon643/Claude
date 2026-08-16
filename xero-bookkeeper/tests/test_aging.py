"""Aging buckets.

The boundary cases here are transcribed from a real Xero aged receivables
report, so a regression in the bucket rules fails against observed behaviour
rather than against an assumption.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from xerobk.aging import bucket_for, bucket_invoices, bucket_rows
from xerobk.models import InvoiceStatus

from .conftest import make_invoice

AS_OF = date(2026, 8, 16)


class TestBucketBoundaries:
    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (-30, "current"),
            (0, "current"),
            (1, "less_than_one_month"),
            (30, "less_than_one_month"),
            (31, "one_month"),
            (60, "one_month"),
            (61, "two_months"),
            (90, "two_months"),
            (91, "three_months"),
            (120, "three_months"),
            (121, "older_than_three_months"),
            (400, "older_than_three_months"),
        ],
    )
    def test_boundaries(self, days: int, expected: str) -> None:
        assert bucket_for(days) == expected

    @pytest.mark.parametrize(
        ("due", "expected"),
        [
            # Observed rows from a real Xero report dated 2026-08-16.
            ("2026-08-21", "current"),                    # not yet due
            ("2026-07-17", "less_than_one_month"),        # 30 days
            ("2026-07-10", "one_month"),                  # 37 days
            ("2026-06-04", "two_months"),                 # 73 days
            ("2026-05-13", "three_months"),               # 95 days
            ("2026-04-27", "three_months"),               # 111 days
            ("2026-04-16", "older_than_three_months"),    # 122 days
            ("2026-03-19", "older_than_three_months"),    # 150 days
        ],
    )
    def test_against_real_report(self, due: str, expected: str) -> None:
        invoice = make_invoice(due=due, amount="100.00")
        assert bucket_for(invoice.days_overdue(AS_OF)) == expected


class TestBucketing:
    def test_only_outstanding_invoices_count(self) -> None:
        invoices = [
            make_invoice("A", amount="100.00", due="2026-08-01"),
            make_invoice("B", amount="200.00", due="2026-08-01", status=InvoiceStatus.PAID),
            make_invoice("C", amount="300.00", due="2026-08-01", status=InvoiceStatus.DRAFT),
        ]
        buckets = bucket_invoices(invoices, AS_OF)
        assert buckets.total == Decimal("100.00")

    def test_buckets_sum_to_total(self) -> None:
        invoices = [
            make_invoice("A", amount="100.00", due="2026-08-20"),
            make_invoice("B", amount="250.00", due="2026-08-01"),
            make_invoice("C", amount="400.00", due="2026-05-01"),
        ]
        buckets = bucket_invoices(invoices, AS_OF)
        assert buckets.total == Decimal("750.00")
        assert sum(buckets.as_dict().values()) == buckets.total

    def test_overdue_excludes_current(self) -> None:
        invoices = [
            make_invoice("A", amount="100.00", due="2026-09-30"),  # current
            make_invoice("B", amount="250.00", due="2026-07-01"),  # overdue
        ]
        buckets = bucket_invoices(invoices, AS_OF)
        assert buckets.current == Decimal("100.00")
        assert buckets.overdue == Decimal("250.00")

    def test_invoice_with_no_due_date_is_current(self) -> None:
        # No due date means days_overdue is 0, so it can never age. The close
        # checklist reports this separately as a data-quality problem.
        invoice = make_invoice("A", amount="100.00", due="")
        buckets = bucket_invoices([invoice], AS_OF)
        assert buckets.current == Decimal("100.00")

    def test_rows_sorted_oldest_first(self) -> None:
        invoices = [
            make_invoice("NEW", amount="100.00", due="2026-08-10"),
            make_invoice("OLD", amount="100.00", due="2026-01-10"),
            make_invoice("MID", amount="100.00", due="2026-06-10"),
        ]
        rows = bucket_rows(invoices, AS_OF)
        assert [row["document_number"] for row in rows] == ["OLD", "MID", "NEW"]

    def test_row_places_amount_in_exactly_one_bucket(self) -> None:
        invoice = make_invoice("A", amount="500.00", due="2026-06-01")
        row = bucket_rows([invoice], AS_OF)[0]
        placed = [
            key for key in
            ("current", "less_than_one_month", "one_month", "two_months", "three_months", "older_than_three_months")
            if row[key] != 0
        ]
        assert placed == ["two_months"]
