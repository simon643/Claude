"""Domain models.

Field names follow the shapes the Xero connector returns (snake_case,
``amount_total`` / ``line_amount_gross``) as well as the PascalCase of the raw
Accounting API, so a snapshot captured from either source parses with the same
code.

The load-bearing idea in this module is :attr:`Document.fingerprint`. An
approval is consent to a *specific* document, not to a document number. If a
bill is edited after someone approved it — the amount, the supplier, the
coding, a line — the earlier approval no longer describes what would be posted,
so it must stop counting. Making that a hash over the material fields turns
"was this changed after approval?" from a manual review question into an
equality test.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .money import ZERO, to_money


def parse_date(value: Any) -> date | None:
    """Parse the several date shapes Xero emits.

    The modern connector returns ISO ``YYYY-MM-DD``; the legacy Accounting API
    returns Microsoft JSON dates (``/Date(1728000000000+0000)/``). Both appear
    in the wild depending on endpoint and version.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text.startswith("/Date(") and text.endswith(")/"):
        inner = text[6:-2]
        for sep in ("+", "-"):
            idx = inner.find(sep, 1)
            if idx > 0:
                inner = inner[:idx]
                break
        try:
            return datetime.utcfromtimestamp(int(inner) / 1000).date()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


class DocumentKind(StrEnum):
    """What is being approved.

    Xero models bills and sales invoices as one entity distinguished by
    ``Type``; purchase orders and credit notes are separate endpoints. They are
    separate kinds here because they route to different people: a purchase
    order is approved *before* commitment, a bill *before* payment.
    """

    BILL = "bill"
    INVOICE = "invoice"
    PURCHASE_ORDER = "purchase_order"
    CREDIT_NOTE = "credit_note"

    @classmethod
    def parse(cls, value: Any) -> DocumentKind:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "accpay": cls.BILL,
            "bills": cls.BILL,
            "accrec": cls.INVOICE,
            "invoices": cls.INVOICE,
            "po": cls.PURCHASE_ORDER,
            "purchaseorder": cls.PURCHASE_ORDER,
            "purchase_orders": cls.PURCHASE_ORDER,
            "accpaycredit": cls.CREDIT_NOTE,
            "creditnote": cls.CREDIT_NOTE,
        }
        if text in aliases:
            return aliases[text]
        try:
            return cls(text)
        except ValueError:
            return cls.BILL

    @property
    def xero_type(self) -> str:
        """The Xero ``Type`` this kind maps to, for filtering and writes."""
        return {
            DocumentKind.BILL: "ACCPAY",
            DocumentKind.INVOICE: "ACCREC",
            DocumentKind.PURCHASE_ORDER: "PURCHASEORDER",
            DocumentKind.CREDIT_NOTE: "ACCPAYCREDIT",
        }[self]

    @property
    def is_spend(self) -> bool:
        """Whether approving this commits the organisation to pay money out."""
        return self in (DocumentKind.BILL, DocumentKind.PURCHASE_ORDER)


class DocumentStatus(StrEnum):
    """The document's status in the ledger, not in this tool's workflow."""

    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    AUTHORISED = "AUTHORISED"
    PAID = "PAID"
    BILLED = "BILLED"
    VOIDED = "VOIDED"
    DELETED = "DELETED"

    @classmethod
    def parse(cls, value: Any) -> DocumentStatus:
        try:
            return cls(str(value).upper())
        except ValueError:
            return cls.DRAFT


class RequestState(StrEnum):
    """Where an approval request sits in this tool's workflow."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

    @property
    def is_open(self) -> bool:
        return self is RequestState.PENDING


class StepState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class Quorum(StrEnum):
    """How many approvers on a step must agree before it closes."""

    ANY = "any"
    ALL = "all"

    @classmethod
    def parse(cls, value: Any) -> Quorum:
        text = str(value or "any").strip().lower()
        if text in {"all", "every", "unanimous"}:
            return cls.ALL
        return cls.ANY


@dataclass(frozen=True, slots=True)
class Organisation:
    name: str = ""
    legal_name: str = ""
    country: str = "AU"
    base_currency: str = "AUD"
    timezone: str = "Australia/Sydney"

    @classmethod
    def from_api(cls, org: dict[str, Any]) -> Organisation:
        tz = org.get("timezone") or org.get("Timezone") or {}
        tz_name = tz.get("tz_name") if isinstance(tz, dict) else str(tz)
        return cls(
            name=str(org.get("name") or org.get("Name") or ""),
            legal_name=str(org.get("legal_name") or org.get("LegalName") or ""),
            country=str(org.get("country") or org.get("CountryCode") or org.get("region") or "AU"),
            base_currency=str(org.get("base_currency") or org.get("BaseCurrency") or "AUD"),
            timezone=str(tz_name or "Australia/Sydney"),
        )


@dataclass(frozen=True, slots=True)
class Contact:
    contact_id: str = ""
    name: str = ""
    email: str | None = None
    is_supplier: bool = False
    is_customer: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Contact:
        return cls(
            contact_id=str(data.get("contact_id") or data.get("ContactID") or ""),
            name=str(data.get("name") or data.get("Name") or ""),
            email=(data.get("email") or data.get("EmailAddress")) or None,
            is_supplier=bool(data.get("is_supplier", data.get("IsSupplier", False))),
            is_customer=bool(data.get("is_customer", data.get("IsCustomer", False))),
        )


@dataclass(frozen=True, slots=True)
class Account:
    code: str = ""
    name: str = ""
    account_type: str = ""
    tax_type: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Account:
        return cls(
            code=str(data.get("code") or data.get("Code") or ""),
            name=str(data.get("name") or data.get("Name") or ""),
            account_type=str(data.get("account_type") or data.get("Type") or ""),
            tax_type=str(data.get("tax_type") or data.get("TaxType") or ""),
        )


@dataclass(frozen=True, slots=True)
class LineItem:
    description: str = ""
    quantity: Decimal = ZERO
    unit_amount: Decimal = ZERO
    line_amount: Decimal = ZERO
    tax_amount: Decimal = ZERO
    account_code: str = ""
    tracking: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> LineItem:
        amount = data.get("line_amount_gross")
        if amount is None:
            amount = data.get("line_amount_net")
        if amount is None:
            amount = data.get("LineAmount")
        tracking = data.get("tracking")
        if isinstance(tracking, list):
            # Raw API shape: [{"Name": "Region", "Option": "North"}, ...]
            tracking = {
                str(t.get("Name") or t.get("name") or ""): str(t.get("Option") or t.get("option") or "")
                for t in tracking
                if isinstance(t, dict)
            }
        return cls(
            description=str(data.get("description") or data.get("Description") or ""),
            quantity=to_money(data.get("quantity", data.get("Quantity"))),
            unit_amount=to_money(data.get("unit_amount", data.get("UnitAmount"))),
            line_amount=to_money(amount),
            tax_amount=to_money(data.get("line_tax_amount", data.get("TaxAmount"))),
            account_code=str(data.get("account_code") or data.get("AccountCode") or ""),
            tracking=dict(tracking or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "quantity": str(self.quantity),
            "unit_amount": str(self.unit_amount),
            "line_amount": str(self.line_amount),
            "tax_amount": str(self.tax_amount),
            "account_code": self.account_code,
            "tracking": dict(sorted(self.tracking.items())),
        }


@dataclass(frozen=True, slots=True)
class Document:
    """Something awaiting a decision: a bill, an invoice, a PO, a credit note."""

    document_id: str
    kind: DocumentKind
    number: str = ""
    contact: Contact = field(default_factory=Contact)
    document_date: date | None = None
    due_date: date | None = None
    amount_total: Decimal = ZERO
    amount_tax: Decimal = ZERO
    amount_net: Decimal = ZERO
    currency_code: str = "AUD"
    currency_rate: Decimal = Decimal("1")
    status: DocumentStatus = DocumentStatus.DRAFT
    reference: str = ""
    purchase_order_number: str = ""
    url: str = ""
    line_items: tuple[LineItem, ...] = ()
    updated_at: datetime | None = None

    # -- derived views ----------------------------------------------------

    @property
    def base_amount(self) -> Decimal:
        """Total in the organisation's base currency.

        Approval limits and budgets are denominated in base currency. Comparing
        a USD invoice's face value against an AUD limit would let a USD 9,000
        bill through a AUD 10,000 limit it actually exceeds.
        """
        from .money import quantize

        rate = self.currency_rate if self.currency_rate and self.currency_rate > 0 else Decimal("1")
        return quantize(self.amount_total * rate)

    @property
    def account_codes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for line in self.line_items:
            if line.account_code and line.account_code not in seen:
                seen.append(line.account_code)
        return tuple(seen)

    @property
    def tracking_pairs(self) -> tuple[str, ...]:
        """Tracking as ``Category:Option`` strings, for matrix conditions."""
        pairs: list[str] = []
        for line in self.line_items:
            for category, option in sorted(line.tracking.items()):
                pair = f"{category}:{option}"
                if pair not in pairs:
                    pairs.append(pair)
        return tuple(pairs)

    @property
    def fingerprint(self) -> str:
        """Hash of every field an approver's consent depends on.

        Excluded on purpose: the ledger status (which this tool changes itself
        when it posts an approval) and the ledger's own modified timestamp
        (which moves for reasons that do not change what is being bought, and
        would make every re-pull look like tampering).
        """
        material = {
            "kind": str(self.kind),
            "number": self.number,
            "contact": self.contact.name,
            "date": self.document_date.isoformat() if self.document_date else "",
            "due": self.due_date.isoformat() if self.due_date else "",
            "total": str(self.amount_total),
            "tax": str(self.amount_tax),
            "currency": self.currency_code,
            "rate": str(self.currency_rate),
            "reference": self.reference,
            "po": self.purchase_order_number,
            "lines": [line.as_dict() for line in self.line_items],
        }
        blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def describe_change(self, other: Document) -> list[str]:
        """Human-readable differences against a previous version of itself.

        Used to explain a re-approval rather than merely demand one: "the
        amount went from $4,400 to $6,900" is actionable, "the document
        changed" is not.
        """
        changes: list[str] = []
        if self.amount_total != other.amount_total:
            changes.append(f"total {other.amount_total} -> {self.amount_total}")
        if self.amount_tax != other.amount_tax:
            changes.append(f"tax {other.amount_tax} -> {self.amount_tax}")
        if self.contact.name != other.contact.name:
            changes.append(f"contact {other.contact.name!r} -> {self.contact.name!r}")
        if self.due_date != other.due_date:
            changes.append(f"due {other.due_date} -> {self.due_date}")
        if self.currency_code != other.currency_code:
            changes.append(f"currency {other.currency_code} -> {self.currency_code}")
        if self.reference != other.reference:
            changes.append(f"reference {other.reference!r} -> {self.reference!r}")
        if self.account_codes != other.account_codes:
            changes.append(
                f"coding {'/'.join(other.account_codes) or '—'} -> {'/'.join(self.account_codes) or '—'}"
            )
        if len(self.line_items) != len(other.line_items):
            changes.append(f"{len(other.line_items)} line(s) -> {len(self.line_items)}")
        elif [line.as_dict() for line in self.line_items] != [
            line.as_dict() for line in other.line_items
        ]:
            changes.append("line detail edited")
        if not changes and self.fingerprint != other.fingerprint:
            changes.append("document edited")
        return changes

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "kind": str(self.kind),
            "number": self.number,
            "contact": {"name": self.contact.name, "email": self.contact.email,
                        "contact_id": self.contact.contact_id},
            "document_date": self.document_date.isoformat() if self.document_date else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "amount_total": str(self.amount_total),
            "amount_tax": str(self.amount_tax),
            "amount_net": str(self.amount_net),
            "base_amount": str(self.base_amount),
            "currency_code": self.currency_code,
            # The rate is part of the fingerprint and of every base-currency
            # comparison, so a round trip through the store must preserve it.
            "currency_rate": str(self.currency_rate),
            "status": str(self.status),
            "reference": self.reference,
            "purchase_order_number": self.purchase_order_number,
            "account_codes": list(self.account_codes),
            "tracking": list(self.tracking_pairs),
            "fingerprint": self.fingerprint,
            "line_items": [line.as_dict() for line in self.line_items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Document:
        """Rebuild from :meth:`as_dict` — used to read a stored snapshot back."""
        contact = data.get("contact") or {}
        return cls(
            document_id=str(data.get("document_id") or ""),
            kind=DocumentKind.parse(data.get("kind")),
            number=str(data.get("number") or ""),
            contact=Contact(
                contact_id=str(contact.get("contact_id") or ""),
                name=str(contact.get("name") or ""),
                email=contact.get("email") or None,
            ),
            document_date=parse_date(data.get("document_date")),
            due_date=parse_date(data.get("due_date")),
            amount_total=to_money(data.get("amount_total")),
            amount_tax=to_money(data.get("amount_tax")),
            amount_net=to_money(data.get("amount_net")),
            currency_code=str(data.get("currency_code") or "AUD"),
            currency_rate=to_money(data.get("currency_rate") or 1),
            status=DocumentStatus.parse(data.get("status")),
            reference=str(data.get("reference") or ""),
            purchase_order_number=str(data.get("purchase_order_number") or ""),
            line_items=tuple(
                LineItem(
                    description=str(line.get("description") or ""),
                    quantity=to_money(line.get("quantity")),
                    unit_amount=to_money(line.get("unit_amount")),
                    line_amount=to_money(line.get("line_amount")),
                    tax_amount=to_money(line.get("tax_amount")),
                    account_code=str(line.get("account_code") or ""),
                    tracking=dict(line.get("tracking") or {}),
                )
                for line in (data.get("line_items") or [])
            ),
        )

    @classmethod
    def from_api(cls, data: dict[str, Any], kind: DocumentKind) -> Document:
        contact_data = data.get("contact") or data.get("Contact") or {}
        lines = data.get("line_items") or data.get("LineItems") or []
        identifier = (
            data.get("invoice_id")
            or data.get("InvoiceID")
            or data.get("purchase_order_id")
            or data.get("PurchaseOrderID")
            or data.get("credit_note_id")
            or data.get("CreditNoteID")
            or ""
        )
        number = (
            data.get("invoice_number")
            or data.get("InvoiceNumber")
            or data.get("purchase_order_number")
            or data.get("PurchaseOrderNumber")
            or data.get("credit_note_number")
            or data.get("CreditNoteNumber")
            or ""
        )
        return cls(
            document_id=str(identifier),
            kind=kind,
            number=str(number),
            contact=Contact.from_api(contact_data),
            document_date=parse_date(
                data.get("invoice_date") or data.get("date") or data.get("DateString") or data.get("Date")
            ),
            due_date=parse_date(
                data.get("due_date") or data.get("DueDateString") or data.get("DueDate")
                or data.get("delivery_date") or data.get("DeliveryDate")
            ),
            amount_total=to_money(data.get("amount_total", data.get("Total"))),
            amount_tax=to_money(data.get("amount_tax", data.get("TotalTax"))),
            amount_net=to_money(data.get("amount_net", data.get("SubTotal"))),
            currency_code=str(data.get("currency_code") or data.get("CurrencyCode") or "AUD"),
            currency_rate=to_money(data.get("currency_rate", data.get("CurrencyRate")) or 1),
            status=DocumentStatus.parse(data.get("status") or data.get("Status")),
            reference=str(data.get("reference") or data.get("Reference") or ""),
            purchase_order_number=str(
                data.get("purchase_order_number_ref")
                or data.get("PurchaseOrderNumber")
                or (number if kind is DocumentKind.PURCHASE_ORDER else "")
            ),
            url=str(data.get("url") or data.get("Url") or ""),
            line_items=tuple(LineItem.from_api(line) for line in lines),
        )


@dataclass(frozen=True, slots=True)
class User:
    """Someone who can submit or approve.

    ``approval_limit`` is a delegation of authority in base currency; ``None``
    means unlimited. ``away_until`` and ``delegate_to`` implement out-of-office
    substitution, which exists because an approval queue that stalls for two
    weeks of annual leave is the reason organisations abandon these systems.
    """

    user_id: str
    name: str = ""
    email: str = ""
    role: str = ""
    approval_limit: Decimal | None = None
    delegate_to: str = ""
    away_until: date | None = None
    escalates_to: str = ""
    active: bool = True

    def is_away(self, as_of: date) -> bool:
        return self.away_until is not None and as_of <= self.away_until

    def within_limit(self, amount: Decimal) -> bool:
        if self.approval_limit is None:
            return True
        return abs(amount) <= self.approval_limit

    @property
    def label(self) -> str:
        return self.name or self.user_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.user_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "approval_limit": str(self.approval_limit) if self.approval_limit is not None else None,
            "delegate_to": self.delegate_to,
            "away_until": self.away_until.isoformat() if self.away_until else None,
            "escalates_to": self.escalates_to,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> User:
        from .config import decimal_or_none

        return cls(
            user_id=str(data.get("id") or data.get("user_id") or "").strip(),
            name=str(data.get("name") or ""),
            email=str(data.get("email") or ""),
            role=str(data.get("role") or ""),
            approval_limit=decimal_or_none(data.get("approval_limit")),
            delegate_to=str(data.get("delegate_to") or ""),
            away_until=parse_date(data.get("away_until")),
            escalates_to=str(data.get("escalates_to") or ""),
            active=bool(data.get("active", True)),
        )


class Directory:
    """The people who can act, loaded from ``users.json``."""

    def __init__(self, users: list[User]) -> None:
        self.users = [u for u in users if u.user_id]
        self._by_id = {u.user_id.lower(): u for u in self.users}

    def __contains__(self, user_id: object) -> bool:
        return str(user_id).lower() in self._by_id

    def __iter__(self) -> Any:
        return iter(self.users)

    def __len__(self) -> int:
        return len(self.users)

    def get(self, user_id: str) -> User | None:
        return self._by_id.get(str(user_id).strip().lower())

    def require(self, user_id: str) -> User:
        user = self.get(user_id)
        if user is None:
            known = ", ".join(sorted(u.user_id for u in self.users)) or "(none configured)"
            raise KeyError(f"unknown user {user_id!r}. Known users: {known}")
        return user

    def label_for(self, user_id: str) -> str:
        """Display name for a user id, falling back to the id itself.

        Used everywhere a stored decision or step is rendered: the id in an old
        decision may belong to someone who has since left the directory, and
        showing a blank where their name was would quietly erase them from the
        audit trail.
        """
        user = self.get(user_id)
        return user.label if user is not None else str(user_id)

    def by_email(self, email: str) -> User | None:
        target = (email or "").strip().lower()
        if not target:
            return None
        for user in self.users:
            if user.email.strip().lower() == target:
                return user
        return None

    def resolve_approver(self, user_id: str, as_of: date, _depth: int = 0) -> tuple[User, str]:
        """Follow out-of-office delegation to whoever should actually decide.

        Returns ``(effective_user, delegated_from)``. Delegation chains are
        followed, but a cycle (A delegates to B, B back to A) would loop
        forever, so the walk is depth-capped and falls back to the nominated
        approver — better a stalled step someone can see than an infinite loop.
        """
        user = self.require(user_id)
        if _depth >= 5 or not user.is_away(as_of) or not user.delegate_to:
            return user, ""
        delegate = self.get(user.delegate_to)
        if delegate is None or delegate.user_id == user.user_id:
            return user, ""
        effective, _ = self.resolve_approver(delegate.user_id, as_of, _depth + 1)
        return effective, user.user_id

    def as_list(self) -> list[dict[str, Any]]:
        return [u.as_dict() for u in self.users]

    @classmethod
    def load(cls, path: Any) -> Directory:
        from pathlib import Path

        target = Path(path)
        if not target.exists():
            return cls([])
        raw = json.loads(target.read_text(encoding="utf-8"))
        rows = raw.get("users") if isinstance(raw, dict) else raw
        return cls([User.from_dict(row) for row in (rows or [])])
