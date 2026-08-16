"""Shared fixtures."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from xerobk.models import Contact, Invoice, InvoiceStatus, InvoiceType, LineItem, TaxTreatment
from xerobk.providers.snapshot import SnapshotProvider

DEMO_DIR = Path(__file__).resolve().parents[1] / "data" / "demo"


@pytest.fixture
def demo() -> SnapshotProvider:
    return SnapshotProvider(DEMO_DIR)


def make_invoice(
    number: str = "INV-001",
    contact: str = "Acme Pty Ltd",
    amount: str = "1100.00",
    due: str = "2026-06-01",
    issued: str = "2026-05-18",
    status: InvoiceStatus = InvoiceStatus.AUTHORISED,
    invoice_type: InvoiceType = InvoiceType.ACCREC,
    paid: str = "0.00",
    reference: str = "",
    email: str | None = "ap@acme.example",
    tax: str | None = None,
) -> Invoice:
    """Build an invoice without going through the API parsing layer."""
    total = Decimal(amount)
    gst = Decimal(tax) if tax is not None else (total / Decimal("11")).quantize(Decimal("0.01"))
    already_paid = Decimal(paid)
    return Invoice(
        invoice_id=f"id-{number}",
        invoice_number=number,
        contact=Contact(contact_id=f"c-{contact}", name=contact, email=email),
        invoice_date=date.fromisoformat(issued) if issued else None,
        due_date=date.fromisoformat(due) if due else None,
        amount_total=total,
        amount_due=total - already_paid,
        amount_paid=already_paid,
        amount_net=total - gst,
        amount_tax=gst,
        status=status,
        invoice_type=invoice_type,
        reference=reference,
        line_items=(
            LineItem(
                description=reference or number,
                quantity=Decimal("1"),
                unit_amount=total,
                line_amount=total,
                tax_amount=gst,
                tax_treatment=TaxTreatment.INCLUSIVE,
            ),
        ),
    )
