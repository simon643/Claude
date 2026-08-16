"""Posting reconciliation decisions to Xero.

Turns a :class:`~xerobk.reconcile.Suggestion` into the right Xero write:

* an invoice **match** becomes a *Payment* against that invoice — this is what
  actually marks it paid. Creating a bank transaction instead would record the
  money but leave the invoice sitting in the aged report forever.
* a **code** becomes a *BankTransaction* (SPEND or RECEIVE) against the coding
  account, for money that is not settling a document.

Three properties matter more than the payload construction:

**The audit row is written before the request leaves.** If a POST succeeds and
the process dies before recording it, an after-the-fact log loses the only
local record of a change that now exists in Xero. Writing first means the worst
case is an audit row marked ``pending`` that needs checking — recoverable —
rather than a silent untracked write.

**Nothing posts twice.** Every bank line carries a stable content-derived id,
and a line already marked posted is skipped. Re-running after a partial failure
resumes rather than duplicating.

**Failures are per-line.** One rejected payment does not abandon the rest of
the batch, and each failure is recorded against its line with Xero's own error
text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from .models import Invoice, InvoiceType
from .money import ZERO, fmt
from .providers import ProviderError, ReadOnlyError, XeroProvider
from .reconcile import Suggestion
from .store import Store


class PostingError(RuntimeError):
    """Raised when a batch cannot be attempted at all."""


@dataclass
class PostResult:
    """What happened to one suggestion."""

    line_id: str
    action: str  # "payment" | "bank_transaction" | "skipped" | "failed"
    ok: bool
    detail: str = ""
    xero_id: str = ""
    amount: Decimal = ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "action": self.action,
            "ok": self.ok,
            "detail": self.detail,
            "xero_id": self.xero_id,
            "amount": str(self.amount),
        }


@dataclass
class PostSummary:
    results: list[PostResult] = field(default_factory=list)

    @property
    def posted(self) -> list[PostResult]:
        return [r for r in self.results if r.ok and r.action != "skipped"]

    @property
    def skipped(self) -> list[PostResult]:
        return [r for r in self.results if r.action == "skipped"]

    @property
    def failed(self) -> list[PostResult]:
        return [r for r in self.results if not r.ok]

    @property
    def value(self) -> Decimal:
        return sum((r.amount for r in self.posted), ZERO)

    def to_dict(self) -> dict[str, Any]:
        return {
            "posted": len(self.posted),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
            "value": str(self.value),
            "results": [r.to_dict() for r in self.results],
        }


def payment_payload(
    invoice: Invoice, when: date, amount: Decimal, bank_account_code: str, reference: str = ""
) -> dict[str, Any]:
    """Build a Xero Payment payload for one invoice."""
    payload: dict[str, Any] = {
        "Invoice": {"InvoiceID": invoice.invoice_id},
        "Account": {"Code": bank_account_code},
        "Date": when.isoformat(),
        # Xero parses this as a number; send the exact 2dp string form so the
        # Decimal we reasoned about is the value that arrives.
        "Amount": str(amount),
    }
    if reference:
        payload["Reference"] = reference[:255]
    return payload


def bank_transaction_payload(
    suggestion: Suggestion, bank_account_code: str
) -> dict[str, Any]:
    """Build a Xero BankTransaction payload for a coded line."""
    line = suggestion.line
    # RECEIVE for money in, SPEND for money out. Xero wants the line amount
    # positive in both cases; direction is carried by Type alone.
    is_receive = line.is_credit
    amount = abs(line.amount)

    item: dict[str, Any] = {
        "Description": (line.description or "Bank transaction")[:4000],
        "Quantity": "1.0",
        "UnitAmount": str(amount),
    }
    if suggestion.account_code:
        item["AccountCode"] = suggestion.account_code
    if suggestion.tax_type:
        item["TaxType"] = suggestion.tax_type

    payload: dict[str, Any] = {
        "Type": "RECEIVE" if is_receive else "SPEND",
        "Contact": {"Name": suggestion.contact_name or "Unknown"},
        "Date": line.date.isoformat() if line.date else date.today().isoformat(),
        "LineItems": [item],
        "BankAccount": {"Code": bank_account_code},
    }
    if line.reference:
        payload["Reference"] = line.reference[:255]
    return payload


def _extract_id(response: dict[str, Any], collection: str, key: str) -> str:
    rows = response.get(collection) or []
    if rows and isinstance(rows[0], dict):
        return str(rows[0].get(key) or "")
    return ""


def post_suggestion(
    suggestion: Suggestion,
    provider: XeroProvider,
    store: Store,
    bank_account_code: str,
    already_posted: set[str] | None = None,
) -> PostResult:
    """Post one suggestion to Xero, recording the attempt before sending."""
    line = suggestion.line
    already_posted = already_posted or set()

    if line.line_id in already_posted:
        return PostResult(line.line_id, "skipped", True, "already posted to Xero")

    if suggestion.kind == "match" and suggestion.invoices:
        if suggestion.ambiguous:
            return PostResult(
                line.line_id, "skipped", True,
                f"ambiguous — {len(suggestion.invoices)} candidate invoices, needs a human choice",
            )
        return _post_payments(suggestion, provider, store, bank_account_code)

    if suggestion.kind == "code" and suggestion.account_code:
        return _post_bank_transaction(suggestion, provider, store, bank_account_code)

    return PostResult(line.line_id, "skipped", True, "no confident suggestion to post")


def _post_payments(
    suggestion: Suggestion, provider: XeroProvider, store: Store, bank_account_code: str
) -> PostResult:
    line = suggestion.line
    when = line.date or date.today()
    remaining = abs(line.amount)
    posted_ids: list[str] = []

    # A batch payment settles several invoices from one bank line, so it needs
    # one Payment per invoice, each capped at that invoice's outstanding balance
    # so the last one cannot overpay if the amounts do not divide exactly.
    for invoice in suggestion.invoices:
        if remaining <= 0:
            break
        amount = min(remaining, invoice.amount_due)
        payload = payment_payload(invoice, when, amount, bank_account_code, line.reference)

        audit_id = store.begin_write("create_payment", invoice.invoice_number, payload)
        try:
            response = provider.create_payment(payload)
        except (ProviderError, ReadOnlyError) as exc:
            store.finish_write(audit_id, "failed", str(exc))
            return PostResult(
                line.line_id, "failed", False,
                f"payment for {invoice.invoice_number} rejected: {exc}",
                amount=sum((Decimal(0) for _ in posted_ids), ZERO),
            )

        xero_id = _extract_id(response, "Payments", "PaymentID")
        store.finish_write(audit_id, "ok", f"PaymentID={xero_id}")
        posted_ids.append(xero_id)
        remaining -= amount

    store.record_coding(
        line,
        contact_name=suggestion.contact_name,
        invoice_ids=[i.invoice_id for i in suggestion.invoices],
        decided_by="posted",
        confidence=suggestion.confidence,
        posted=True,
    )
    numbers = ", ".join(i.invoice_number for i in suggestion.invoices)
    return PostResult(
        line.line_id, "payment", True,
        f"paid {numbers}", xero_id=",".join(filter(None, posted_ids)),
        amount=abs(line.amount) - remaining,
    )


def _post_bank_transaction(
    suggestion: Suggestion, provider: XeroProvider, store: Store, bank_account_code: str
) -> PostResult:
    line = suggestion.line
    payload = bank_transaction_payload(suggestion, bank_account_code)

    audit_id = store.begin_write("create_bank_transaction", suggestion.account_code, payload)
    try:
        response = provider.create_bank_transaction(payload)
    except (ProviderError, ReadOnlyError) as exc:
        store.finish_write(audit_id, "failed", str(exc))
        return PostResult(line.line_id, "failed", False, f"rejected: {exc}")

    xero_id = _extract_id(response, "BankTransactions", "BankTransactionID")
    store.finish_write(audit_id, "ok", f"BankTransactionID={xero_id}")

    store.record_coding(
        line,
        account_code=suggestion.account_code,
        tax_type=suggestion.tax_type,
        contact_name=suggestion.contact_name,
        decided_by="posted",
        confidence=suggestion.confidence,
        posted=True,
    )
    return PostResult(
        line.line_id, "bank_transaction", True,
        f"coded to {suggestion.account_code}", xero_id=xero_id, amount=abs(line.amount),
    )


def post_batch(
    suggestions: list[Suggestion],
    provider: XeroProvider,
    store: Store,
    bank_account_code: str,
    confident_only: bool = True,
) -> PostSummary:
    """Post a batch of suggestions, continuing past individual failures."""
    if not provider.can_write:
        raise PostingError(
            "This connection is read-only. Re-run `xerobk connect --allow-writes` "
            "to grant write scopes."
        )
    if not bank_account_code:
        raise PostingError(
            "No bank account configured. Run `xerobk accounts` to list them, then "
            "`xerobk accounts --use <CODE>` to choose the one the money moves through."
        )

    already = store.posted_line_ids()
    summary = PostSummary()
    for suggestion in suggestions:
        if confident_only and not suggestion.is_confident:
            summary.results.append(
                PostResult(
                    suggestion.line.line_id, "skipped", True,
                    f"confidence {suggestion.confidence}% is below the auto bar",
                )
            )
            continue
        summary.results.append(
            post_suggestion(suggestion, provider, store, bank_account_code, already)
        )
    return summary


def describe(summary: PostSummary, currency: str = "AUD") -> str:
    """One-line human summary."""
    return (
        f"{len(summary.posted)} posted ({fmt(summary.value, currency)}), "
        f"{len(summary.skipped)} skipped, {len(summary.failed)} failed"
    )


def post_draft(
    draft: Any, provider: XeroProvider, store: Store
) -> PostResult:
    """Post a drafted invoice or bill, validating it before it is sent."""
    if not provider.can_write:
        raise PostingError(
            "This connection is read-only. Re-run `xerobk connect --allow-writes`."
        )
    known = {a.code for a in provider.accounts()} or None
    draft.require_valid(known)

    payload = draft.to_payload()
    label = "create_bill" if draft.invoice_type is InvoiceType.ACCPAY else "create_invoice"
    audit_id = store.begin_write(label, draft.contact_name, payload)
    try:
        response = provider.create_invoice(payload)
    except (ProviderError, ReadOnlyError) as exc:
        store.finish_write(audit_id, "failed", str(exc))
        return PostResult("", "failed", False, f"rejected: {exc}")

    xero_id = _extract_id(response, "Invoices", "InvoiceID")
    number = _extract_id(response, "Invoices", "InvoiceNumber")
    store.finish_write(audit_id, "ok", f"InvoiceID={xero_id} InvoiceNumber={number}")
    return PostResult(
        "", label, True, f"created {number or xero_id}", xero_id=xero_id, amount=draft.total
    )
