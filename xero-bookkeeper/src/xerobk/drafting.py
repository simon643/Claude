"""Invoice and bill drafting.

Builds the payloads Xero's Accounting API expects, and validates them before
anything is sent. Validation matters more than the payload construction: a
rejected POST costs a round trip, but a *accepted* POST with the wrong tax
treatment or a mistyped amount becomes a correcting journal later.

Drafts are always created with ``Status: DRAFT`` unless the caller explicitly
asks to authorise. Approving an invoice puts it in the ledger and on the BAS;
that should be a deliberate second step, not a side effect of drafting one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .models import InvoiceType
from .money import ZERO, gst_from_exclusive, gst_from_inclusive, quantize, to_money

# Xero's tax type codes for an Australian organisation.
AU_TAX_TYPES = {
    "OUTPUT": "GST on Income (10%)",
    "INPUT": "GST on Expenses (10%)",
    "EXEMPTOUTPUT": "GST Free Income",
    "EXEMPTEXPENSES": "GST Free Expenses",
    "BASEXCLUDED": "BAS Excluded",
}

LINE_AMOUNT_TYPES = ("Exclusive", "Inclusive", "NoTax")


class ValidationError(ValueError):
    """Raised when a draft is not fit to send."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class DraftLine:
    description: str
    quantity: Decimal = Decimal("1")
    unit_amount: Decimal = ZERO
    account_code: str = ""
    tax_type: str = ""
    item_code: str = ""
    tracking: dict[str, str] = field(default_factory=dict)

    @property
    def line_amount(self) -> Decimal:
        return quantize(self.quantity * self.unit_amount)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "Description": self.description,
            "Quantity": str(self.quantity),
            "UnitAmount": str(self.unit_amount),
        }
        if self.account_code:
            payload["AccountCode"] = self.account_code
        if self.tax_type:
            payload["TaxType"] = self.tax_type
        if self.item_code:
            payload["ItemCode"] = self.item_code
        if self.tracking:
            payload["Tracking"] = [
                {"Name": name, "Option": option} for name, option in self.tracking.items()
            ]
        return payload


@dataclass
class Draft:
    """A sales invoice or supplier bill, before it is sent to Xero."""

    contact_name: str = ""
    contact_id: str = ""
    lines: list[DraftLine] = field(default_factory=list)
    invoice_type: InvoiceType = InvoiceType.ACCREC
    invoice_date: date | None = None
    due_date: date | None = None
    reference: str = ""
    invoice_number: str = ""
    line_amount_types: str = "Exclusive"
    currency_code: str = "AUD"
    status: str = "DRAFT"

    # -- computed totals --------------------------------------------------

    @property
    def subtotal(self) -> Decimal:
        """Sum of line amounts, always ex-GST regardless of entry mode."""
        total = sum((line.line_amount for line in self.lines), ZERO)
        if self.line_amount_types == "Inclusive":
            return quantize(total - self.total_tax)
        return quantize(total)

    @property
    def total_tax(self) -> Decimal:
        total = ZERO
        for line in self.lines:
            if not _is_taxable(line.tax_type):
                continue
            if self.line_amount_types == "Inclusive":
                total += gst_from_inclusive(line.line_amount)
            elif self.line_amount_types == "Exclusive":
                total += gst_from_exclusive(line.line_amount)
        return quantize(total)

    @property
    def total(self) -> Decimal:
        return quantize(self.subtotal + self.total_tax)

    # -- validation -------------------------------------------------------

    def validate(self, known_accounts: set[str] | None = None) -> list[str]:
        """Return a list of problems; empty means the draft is sendable."""
        errors: list[str] = []

        if not (self.contact_id or self.contact_name.strip()):
            errors.append("a contact is required (contact_id or contact_name)")

        if not self.lines:
            errors.append("at least one line item is required")

        if self.line_amount_types not in LINE_AMOUNT_TYPES:
            errors.append(
                f"line_amount_types must be one of {', '.join(LINE_AMOUNT_TYPES)}, "
                f"got {self.line_amount_types!r}"
            )

        if self.status not in ("DRAFT", "SUBMITTED", "AUTHORISED"):
            errors.append(f"status must be DRAFT, SUBMITTED, or AUTHORISED, got {self.status!r}")

        for index, line in enumerate(self.lines, start=1):
            label = f"line {index}"
            if not line.description.strip():
                errors.append(f"{label}: description is required")
            if line.unit_amount == 0 and line.quantity == 0:
                errors.append(f"{label}: quantity and unit amount are both zero")
            if not line.account_code and not line.item_code:
                # Xero will accept this and post to a default, which is how
                # miscoded lines quietly accumulate in suspense.
                errors.append(f"{label}: needs an account code (or an item code that carries one)")
            if line.tax_type and line.tax_type not in AU_TAX_TYPES and self.currency_code == "AUD":
                errors.append(
                    f"{label}: tax type {line.tax_type!r} is not a standard AU code "
                    f"({', '.join(sorted(AU_TAX_TYPES))})"
                )
            if known_accounts is not None and line.account_code and line.account_code not in known_accounts:
                errors.append(f"{label}: account code {line.account_code} is not in the chart of accounts")

        if self.due_date and self.invoice_date and self.due_date < self.invoice_date:
            errors.append("due date is before the invoice date")

        if self.total < 0:
            errors.append("total is negative — raise a credit note instead of an invoice")

        return errors

    def require_valid(self, known_accounts: set[str] | None = None) -> None:
        errors = self.validate(known_accounts)
        if errors:
            raise ValidationError(errors)

    # -- payload ----------------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        """Build the Xero Accounting API invoice payload."""
        contact: dict[str, Any] = {}
        if self.contact_id:
            contact["ContactID"] = self.contact_id
        if self.contact_name:
            contact["Name"] = self.contact_name

        payload: dict[str, Any] = {
            "Type": self.invoice_type.value,
            "Contact": contact,
            "LineItems": [line.to_payload() for line in self.lines],
            "LineAmountTypes": self.line_amount_types,
            "Status": self.status,
            "CurrencyCode": self.currency_code,
        }
        if self.invoice_date:
            payload["Date"] = self.invoice_date.isoformat()
        if self.due_date:
            payload["DueDate"] = self.due_date.isoformat()
        if self.reference:
            payload["Reference"] = self.reference
        if self.invoice_number:
            payload["InvoiceNumber"] = self.invoice_number
        return payload

    def summary(self) -> str:
        kind = "Invoice" if self.invoice_type is InvoiceType.ACCREC else "Bill"
        who = self.contact_name or self.contact_id or "(no contact)"
        return (
            f"{kind} for {who}: {len(self.lines)} line(s), "
            f"subtotal {self.subtotal}, GST {self.total_tax}, total {self.total}"
        )


def _is_taxable(tax_type: str) -> bool:
    """Whether a tax type attracts 10% GST."""
    return tax_type.upper() in ("OUTPUT", "INPUT")


def with_payment_terms(invoice_date: date, days: int = 14) -> date:
    """Due date from an invoice date and a net-N term."""
    return invoice_date + timedelta(days=days)


def build_draft(
    contact_name: str,
    lines: list[dict[str, Any]],
    invoice_type: InvoiceType = InvoiceType.ACCREC,
    invoice_date: date | None = None,
    payment_terms_days: int = 14,
    reference: str = "",
    line_amount_types: str = "Exclusive",
    currency_code: str = "AUD",
    tax_type: str | None = None,
) -> Draft:
    """Convenience constructor from plain dicts.

    ``tax_type`` sets a default for lines that do not specify one — OUTPUT for
    sales and INPUT for bills, matching the direction of the document.
    """
    when = invoice_date or date.today()
    default_tax = tax_type or ("OUTPUT" if invoice_type is InvoiceType.ACCREC else "INPUT")

    draft_lines = [
        DraftLine(
            description=str(row.get("description") or ""),
            quantity=to_money(row.get("quantity", 1)),
            unit_amount=to_money(row.get("unit_amount", row.get("amount", 0))),
            account_code=str(row.get("account_code") or ""),
            tax_type=str(row.get("tax_type") or default_tax),
            item_code=str(row.get("item_code") or ""),
            tracking=dict(row.get("tracking") or {}),
        )
        for row in lines
    ]

    return Draft(
        contact_name=contact_name,
        lines=draft_lines,
        invoice_type=invoice_type,
        invoice_date=when,
        due_date=with_payment_terms(when, payment_terms_days),
        reference=reference,
        line_amount_types=line_amount_types,
        currency_code=currency_code,
    )
