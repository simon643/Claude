"""Domain models.

Field names follow the shapes the Xero connector actually returns (snake_case,
``amount_due`` / ``amount_total`` / ``line_amount_gross``) rather than the
PascalCase of the raw Accounting API, so a snapshot captured from either source
can be parsed by the same code. :mod:`xerobk.providers.live` normalises the raw
API's PascalCase into these models at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .money import ZERO, to_money


def _parse_date(value: Any) -> date | None:
    """Parse the several date shapes Xero emits.

    The modern connector returns ISO ``YYYY-MM-DD``. The legacy Accounting API
    returns Microsoft JSON dates (``/Date(1728000000000+0000)/``). Both appear
    in the wild depending on endpoint and version, so both are handled.
    """
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if text.startswith("/Date(") and text.endswith(")/"):
        inner = text[6:-2]
        for sep in ("+", "-"):
            idx = inner.find(sep, 1)
            if idx > 0:
                inner = inner[:idx]
                break
        try:
            # Xero's epoch offsets are in milliseconds, always UTC.
            return datetime.utcfromtimestamp(int(inner) / 1000).date()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


class InvoiceStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    AUTHORISED = "AUTHORISED"
    PAID = "PAID"
    VOIDED = "VOIDED"
    DELETED = "DELETED"

    @classmethod
    def parse(cls, value: Any) -> InvoiceStatus:
        try:
            return cls(str(value).upper())
        except ValueError:
            return cls.DRAFT


class InvoiceType(StrEnum):
    """ACCREC is a sales invoice (money in); ACCPAY is a bill (money out)."""

    ACCREC = "ACCREC"
    ACCPAY = "ACCPAY"

    @classmethod
    def parse(cls, value: Any) -> InvoiceType:
        try:
            return cls(str(value).upper())
        except ValueError:
            return cls.ACCREC


class TaxTreatment(StrEnum):
    INCLUSIVE = "Inclusive"
    EXCLUSIVE = "Exclusive"
    NO_TAX = "NoTax"

    @classmethod
    def parse(cls, value: Any) -> TaxTreatment:
        text = str(value or "").strip().lower()
        if text.startswith("inc"):
            return cls.INCLUSIVE
        if text.startswith("ex"):
            return cls.EXCLUSIVE
        return cls.NO_TAX


@dataclass(frozen=True, slots=True)
class Organisation:
    name: str = ""
    legal_name: str = ""
    organisation_type: str = ""
    country: str = "AU"
    base_currency: str = "AUD"
    timezone: str = "Australia/Sydney"
    line_of_business: str = ""
    registration_number: str = ""
    financial_year_start: date | None = None
    financial_year_end: date | None = None

    @property
    def is_australian(self) -> bool:
        return self.country.upper() == "AU"

    @classmethod
    def from_api(cls, org: dict[str, Any], fy: dict[str, Any] | None = None) -> Organisation:
        tz = org.get("timezone") or {}
        tz_name = tz.get("tz_name") if isinstance(tz, dict) else str(tz)
        fy = fy or {}
        return cls(
            name=str(org.get("name") or ""),
            legal_name=str(org.get("legal_name") or ""),
            organisation_type=str(org.get("organisation_type") or ""),
            country=str(org.get("country") or org.get("region") or "AU"),
            base_currency=str(org.get("base_currency") or "AUD"),
            timezone=str(tz_name or "Australia/Sydney"),
            line_of_business=str(org.get("line_of_business") or ""),
            registration_number=str(org.get("company_registration_number") or ""),
            financial_year_start=_parse_date(fy.get("financial_year_start_date")),
            financial_year_end=_parse_date(fy.get("financial_year_end_date")),
        )


@dataclass(frozen=True, slots=True)
class Contact:
    contact_id: str
    name: str
    email: str | None = None
    is_customer: bool = False
    is_supplier: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Contact:
        return cls(
            contact_id=str(data.get("contact_id") or data.get("ContactID") or ""),
            name=str(data.get("name") or data.get("Name") or ""),
            email=(data.get("email") or data.get("EmailAddress")) or None,
            is_customer=bool(data.get("is_customer", data.get("IsCustomer", False))),
            is_supplier=bool(data.get("is_supplier", data.get("IsSupplier", False))),
        )


@dataclass(frozen=True, slots=True)
class LineItem:
    description: str = ""
    quantity: Decimal = ZERO
    unit_amount: Decimal = ZERO
    line_amount: Decimal = ZERO
    tax_amount: Decimal = ZERO
    tax_treatment: TaxTreatment = TaxTreatment.NO_TAX
    account_code: str | None = None
    item_code: str | None = None
    tracking: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> LineItem:
        # The connector reports the line total under different keys depending on
        # the tax treatment: gross for inclusive lines, net for exclusive ones.
        amount = data.get("line_amount_gross")
        if amount is None:
            amount = data.get("line_amount_net")
        if amount is None:
            amount = data.get("line_amount_net_base_currency")
        if amount is None:
            amount = data.get("LineAmount")
        return cls(
            description=str(data.get("description") or data.get("Description") or ""),
            quantity=to_money(data.get("quantity", data.get("Quantity"))),
            unit_amount=to_money(data.get("unit_amount", data.get("UnitAmount"))),
            line_amount=to_money(amount),
            tax_amount=to_money(data.get("line_tax_amount", data.get("TaxAmount"))),
            tax_treatment=TaxTreatment.parse(
                data.get("line_tax_treatment") or data.get("LineAmountTypes")
            ),
            account_code=(data.get("account_code") or data.get("AccountCode")) or None,
            item_code=(data.get("item_code") or data.get("ItemCode")) or None,
            tracking=dict(data.get("tracking") or {}),
        )


@dataclass(frozen=True, slots=True)
class Invoice:
    invoice_id: str
    invoice_number: str
    contact: Contact
    invoice_date: date | None
    due_date: date | None
    amount_total: Decimal = ZERO
    amount_due: Decimal = ZERO
    amount_paid: Decimal = ZERO
    amount_credited: Decimal = ZERO
    amount_net: Decimal = ZERO
    amount_tax: Decimal = ZERO
    currency_code: str = "AUD"
    currency_rate: Decimal = Decimal("1")
    status: InvoiceStatus = InvoiceStatus.DRAFT
    invoice_type: InvoiceType = InvoiceType.ACCREC
    reference: str = ""
    line_items: tuple[LineItem, ...] = ()

    @property
    def is_outstanding(self) -> bool:
        return self.status == InvoiceStatus.AUTHORISED and self.amount_due > 0

    def days_overdue(self, as_of: date) -> int:
        """Positive when overdue, negative when not yet due, 0 if undated."""
        if self.due_date is None:
            return 0
        return (as_of - self.due_date).days

    def is_overdue(self, as_of: date) -> bool:
        return self.is_outstanding and self.days_overdue(as_of) > 0

    @classmethod
    def from_api(cls, data: dict[str, Any], default_type: InvoiceType = InvoiceType.ACCREC) -> Invoice:
        contact_data = data.get("contact") or data.get("Contact") or {}
        lines = data.get("line_items") or data.get("LineItems") or []
        return cls(
            invoice_id=str(data.get("invoice_id") or data.get("InvoiceID") or ""),
            invoice_number=str(data.get("invoice_number") or data.get("InvoiceNumber") or ""),
            contact=Contact.from_api(contact_data),
            invoice_date=_parse_date(data.get("invoice_date") or data.get("DateString") or data.get("Date")),
            due_date=_parse_date(data.get("due_date") or data.get("DueDateString") or data.get("DueDate")),
            amount_total=to_money(data.get("amount_total", data.get("Total"))),
            amount_due=to_money(data.get("amount_due", data.get("AmountDue"))),
            amount_paid=to_money(data.get("amount_paid", data.get("AmountPaid"))),
            amount_credited=to_money(data.get("amount_credited", data.get("AmountCredited"))),
            amount_net=to_money(data.get("amount_net", data.get("SubTotal"))),
            amount_tax=to_money(data.get("amount_tax", data.get("TotalTax"))),
            currency_code=str(data.get("currency_code") or data.get("CurrencyCode") or "AUD"),
            currency_rate=to_money(data.get("currency_rate", data.get("CurrencyRate")) or 1),
            status=InvoiceStatus.parse(data.get("status") or data.get("Status")),
            invoice_type=InvoiceType.parse(data.get("type") or data.get("Type") or default_type.value),
            reference=str(data.get("reference") or data.get("Reference") or ""),
            line_items=tuple(LineItem.from_api(line) for line in lines),
        )


@dataclass(frozen=True, slots=True)
class Account:
    """A chart-of-accounts entry."""

    code: str
    name: str
    account_type: str = ""
    tax_type: str = ""
    description: str = ""
    enable_payments: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Account:
        return cls(
            code=str(data.get("code") or data.get("Code") or ""),
            name=str(data.get("name") or data.get("Name") or ""),
            account_type=str(data.get("account_type") or data.get("Type") or ""),
            tax_type=str(data.get("tax_type") or data.get("TaxType") or ""),
            description=str(data.get("description") or data.get("Description") or ""),
            enable_payments=bool(data.get("enable_payments", data.get("EnablePaymentsToAccount", False))),
        )


@dataclass(frozen=True, slots=True)
class BankLine:
    """One line from a bank statement, before it has been coded.

    ``amount`` is signed: positive is money in, negative is money out.
    """

    line_id: str
    date: date | None
    description: str
    amount: Decimal
    balance: Decimal | None = None
    reference: str = ""
    account_id: str = ""

    @property
    def is_credit(self) -> bool:
        """Money in. Likely a customer payment against a sales invoice."""
        return self.amount > 0

    @property
    def is_debit(self) -> bool:
        """Money out. Likely a supplier payment or an expense."""
        return self.amount < 0


@dataclass(frozen=True, slots=True)
class AgedBuckets:
    current: Decimal = ZERO
    less_than_one_month: Decimal = ZERO
    one_month: Decimal = ZERO
    two_months: Decimal = ZERO
    three_months: Decimal = ZERO
    older_than_three_months: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return (
            self.current
            + self.less_than_one_month
            + self.one_month
            + self.two_months
            + self.three_months
            + self.older_than_three_months
        )

    @property
    def overdue(self) -> Decimal:
        """Everything past due, i.e. all buckets except ``current``."""
        return self.total - self.current

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "current": self.current,
            "less_than_one_month": self.less_than_one_month,
            "one_month": self.one_month,
            "two_months": self.two_months,
            "three_months": self.three_months,
            "older_than_three_months": self.older_than_three_months,
        }

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AgedBuckets:
        return cls(
            current=to_money(data.get("current")),
            less_than_one_month=to_money(data.get("less_than_one_month")),
            one_month=to_money(data.get("one_month")),
            two_months=to_money(data.get("two_months")),
            three_months=to_money(data.get("three_months")),
            older_than_three_months=to_money(data.get("older_than_three_months")),
        )


@dataclass(frozen=True, slots=True)
class CashPosition:
    cash_balance: Decimal = ZERO
    amount_owed: Decimal = ZERO  # receivables: owed TO the business
    amount_due: Decimal = ZERO  # payables: owed BY the business
    snapshot_date: date | None = None
    currency: str = "AUD"

    @property
    def net_working_position(self) -> Decimal:
        return self.cash_balance + self.amount_owed - self.amount_due

    @property
    def can_cover_payables(self) -> bool:
        return self.cash_balance >= self.amount_due

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CashPosition:
        return cls(
            cash_balance=to_money(data.get("cash_balance")),
            amount_owed=to_money(data.get("amount_owed")),
            amount_due=to_money(data.get("amount_due")),
            snapshot_date=_parse_date(data.get("snapshot_date")),
            currency=str(data.get("organisation_base_currency") or "AUD"),
        )
