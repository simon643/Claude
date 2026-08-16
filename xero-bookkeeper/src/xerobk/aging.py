"""Receivables / payables aging.

One implementation of the bucket rules, shared by every provider, so an offline
snapshot and a live API pull can never disagree about how old an invoice is.

The boundaries below were derived by reconciling computed buckets against the
aged receivables report Xero itself produced for a real organisation, rather
than assumed from the bucket labels:

    days overdue <= 0   current                  (not yet due)
    1   .. 30           less_than_one_month
    31  .. 60           one_month
    61  .. 90           two_months
    91  .. 120          three_months
    > 120               older_than_three_months

Note that "3 months" is the 91-120 band and anything past 120 days falls into
"3+ months" — an invoice 121 days overdue is in the oldest bucket, not the
"three_months" one. Aging is by DUE date, matching Xero's default
``ageingBy=due`` setting.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from .models import AgedBuckets, Invoice
from .money import ZERO

# Upper bound of each bucket in days overdue, in order. ``None`` means unbounded.
BUCKET_BOUNDS: tuple[tuple[str, int | None], ...] = (
    ("current", 0),
    ("less_than_one_month", 30),
    ("one_month", 60),
    ("two_months", 90),
    ("three_months", 120),
    ("older_than_three_months", None),
)

BUCKET_LABELS: dict[str, str] = {
    "current": "Current",
    "less_than_one_month": "< 1 month",
    "one_month": "1 month",
    "two_months": "2 months",
    "three_months": "3 months",
    "older_than_three_months": "3+ months",
}


def bucket_for(days_overdue: int) -> str:
    """Return the bucket key for a number of days overdue."""
    for key, upper in BUCKET_BOUNDS:
        if upper is None or days_overdue <= upper:
            return key
    return "older_than_three_months"


def bucket_invoices(invoices: Iterable[Invoice], as_of: date) -> AgedBuckets:
    """Aggregate outstanding invoice balances into aged buckets."""
    totals: dict[str, Decimal] = {key: ZERO for key, _ in BUCKET_BOUNDS}
    for invoice in invoices:
        if not invoice.is_outstanding:
            continue
        totals[bucket_for(invoice.days_overdue(as_of))] += invoice.amount_due
    return AgedBuckets(**totals)


def bucket_rows(invoices: Iterable[Invoice], as_of: date) -> list[dict[str, object]]:
    """One report row per outstanding invoice, shaped like Xero's aged report."""
    rows: list[dict[str, object]] = []
    sort_keys: list[tuple[int, str]] = []
    for invoice in invoices:
        if not invoice.is_outstanding:
            continue
        days = invoice.days_overdue(as_of)
        key = bucket_for(days)
        row: dict[str, object] = {
            "contact": {"name": invoice.contact.name, "email": invoice.contact.email},
            "document_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
            "document_number": invoice.invoice_number,
            "document_reference": invoice.reference or None,
            "bucket_label": BUCKET_LABELS[key],
            "days_overdue": days,
            "total": invoice.amount_due,
        }
        sort_keys.append((-days, invoice.invoice_number))
        for bucket_key, _ in BUCKET_BOUNDS:
            row[bucket_key] = invoice.amount_due if bucket_key == key else ZERO
        rows.append(row)
    # Sort on the typed keys collected above rather than re-reading the
    # loosely-typed row dicts.
    return [row for _, row in sorted(zip(sort_keys, rows, strict=True), key=lambda pair: pair[0])]
