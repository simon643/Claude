"""Month-end / BAS-period close checklist.

Each check is a small function that takes a :class:`CloseContext` and yields
:class:`Finding` objects. Checks are independent and registered in
:data:`CHECKS`, so adding one is a single function plus a list entry.

Findings are advisory. This module never asserts that a return is correct or
that a figure is final — it points at things a human bookkeeper should look at
before signing off. Where a check involves tax, it says what it compared and
leaves the conclusion to the person lodging.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Any

from . import autax
from .aging import BUCKET_LABELS
from .models import (
    BankLine,
    Contact,
    Invoice,
    InvoiceStatus,
    InvoiceType,
    Organisation,
    TaxTreatment,
)
from .money import ZERO, fmt, gst_from_exclusive, gst_from_inclusive, pct, quantize, to_money

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

_SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing worth a human's attention."""

    check: str
    severity: str
    title: str
    detail: str
    action: str = ""
    items: tuple[str, ...] = ()
    value: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "action": self.action,
            "items": list(self.items),
            "value": str(self.value) if self.value is not None else None,
        }


@dataclass
class CloseContext:
    """Everything the checks need, gathered once."""

    as_of: date
    organisation: Organisation
    invoices: list[Invoice] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    bank_lines: list[BankLine] = field(default_factory=list)
    cash_balance: Decimal = ZERO
    period: autax.Period | None = None
    gst_registered: bool = True
    escalation_days: int = 60
    large_threshold: Decimal = Decimal("10000.00")
    suspense_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.period is None:
            self.period = autax.previous_bas_quarter(self.as_of)

    @property
    def sales(self) -> list[Invoice]:
        return [i for i in self.invoices if i.invoice_type is InvoiceType.ACCREC]

    @property
    def bills(self) -> list[Invoice]:
        return [i for i in self.invoices if i.invoice_type is InvoiceType.ACCPAY]

    def in_period(self, invoices: list[Invoice]) -> list[Invoice]:
        assert self.period is not None
        return [
            i for i in invoices
            if i.invoice_date
            and self.period.contains(i.invoice_date)
            and i.status not in (InvoiceStatus.VOIDED, InvoiceStatus.DELETED)
        ]


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_overdue_debt(ctx: CloseContext) -> list[Finding]:
    """Receivables that have aged past the escalation threshold."""
    outstanding = [i for i in ctx.sales if i.is_outstanding]
    if not outstanding:
        return []

    total = sum((i.amount_due for i in outstanding), ZERO)
    # Credit notes carry a negative balance and an old date, so they would
    # otherwise appear on the chase list as ancient "debts" nobody owes.
    escalate = [
        i for i in outstanding
        if not i.is_credit and i.days_overdue(ctx.as_of) >= ctx.escalation_days
    ]
    if not escalate:
        return []

    escalate.sort(key=lambda i: i.days_overdue(ctx.as_of), reverse=True)
    value = sum((i.amount_due for i in escalate), ZERO)
    share = pct(value, total)
    severity = CRITICAL if share >= 50 else WARNING

    return [
        Finding(
            check="overdue_debt",
            severity=severity,
            title=f"{len(escalate)} invoices are {ctx.escalation_days}+ days overdue",
            detail=(
                f"{fmt(value, ctx.organisation.base_currency)} of "
                f"{fmt(total, ctx.organisation.base_currency)} outstanding ({share}%) is past "
                f"{ctx.escalation_days} days. The oldest is "
                f"{escalate[0].days_overdue(ctx.as_of)} days overdue."
            ),
            action="Chase or escalate these, and consider whether any should be provisioned as doubtful.",
            items=tuple(
                f"{i.invoice_number} — {i.contact.name} — "
                f"{fmt(i.amount_due, ctx.organisation.base_currency)} — "
                f"{i.days_overdue(ctx.as_of)} days"
                for i in escalate[:15]
            ),
            value=value,
        )
    ]


def check_debtor_concentration(ctx: CloseContext) -> list[Finding]:
    """A single customer carrying too much of the receivables book."""
    outstanding = [i for i in ctx.sales if i.is_outstanding]
    total = sum((i.amount_due for i in outstanding), ZERO)
    if total <= 0:
        return []

    by_contact: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for invoice in outstanding:
        by_contact[invoice.contact.name] += invoice.amount_due

    top_name, top_value = max(by_contact.items(), key=lambda kv: kv[1])
    share = pct(top_value, total)
    if share < 40:
        return []

    return [
        Finding(
            check="debtor_concentration",
            severity=CRITICAL if share >= 60 else WARNING,
            title=f"{top_name} is {share}% of outstanding receivables",
            detail=(
                f"{fmt(top_value, ctx.organisation.base_currency)} of "
                f"{fmt(total, ctx.organisation.base_currency)} is owed by one customer. "
                "Concentration at this level makes the cash forecast dependent on a single payer."
            ),
            action="Confirm the balance with the customer and check the credit terms still fit the exposure.",
            value=top_value,
        )
    ]


def check_duplicate_contacts(ctx: CloseContext) -> list[Finding]:
    """Contacts that are probably the same entity recorded twice.

    Splitting one customer across two contact records silently splits their
    aged balance and their statement, so neither is right.
    """
    seen: dict[str, list[str]] = defaultdict(list)
    for contact in ctx.contacts:
        key = _contact_key(contact.name)
        if key:
            seen[key].append(contact.name)

    # Contacts that only appear on invoices are worth catching too.
    for invoice in ctx.invoices:
        name = invoice.contact.name
        key = _contact_key(name)
        if key and name not in seen[key]:
            seen[key].append(name)

    dupes = {key: names for key, names in seen.items() if len(set(names)) > 1}
    if not dupes:
        return []

    items = tuple(" / ".join(sorted(set(names))) for names in dupes.values())
    return [
        Finding(
            check="duplicate_contacts",
            severity=WARNING,
            title=f"{len(dupes)} possible duplicate contact{'s' if len(dupes) > 1 else ''}",
            detail=(
                "These contact names normalise to the same entity once legal suffixes and "
                "punctuation are removed. If they are the same customer, their balances and "
                "statements are currently split across two records."
            ),
            action="Merge the duplicates in Xero (Contacts > select both > Merge), keeping the record with the correct ABN and email.",
            items=items,
        )
    ]


def _contact_key(name: str) -> str:
    """Normalise a contact name for duplicate detection."""
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    noise = {"pty", "ltd", "limited", "inc", "incorporated", "the", "co", "company", "trust", "trustee", "group", "australia", "aust"}
    words = [w for w in text.split() if w and w not in noise]
    return " ".join(words)


def _reference_key(reference: str) -> str:
    """Normalise an invoice reference for comparison."""
    return re.sub(r"[^a-z0-9]+", " ", (reference or "").lower()).strip()


def check_duplicate_invoices(ctx: CloseContext) -> list[Finding]:
    """Same contact, same amount, same reference, dates close together."""
    buckets: dict[tuple[str, str], list[Invoice]] = defaultdict(list)
    for invoice in ctx.invoices:
        if invoice.status in (InvoiceStatus.VOIDED, InvoiceStatus.DELETED):
            continue
        buckets[(_contact_key(invoice.contact.name), str(invoice.amount_total))].append(invoice)

    suspicious: list[tuple[Invoice, Invoice]] = []
    for group in buckets.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda i: i.invoice_date or date.min)
        for first, second in pairwise(group):
            if not first.invoice_date or not second.invoice_date:
                continue
            # A genuine recurring charge repeats monthly; anything inside a
            # fortnight of an identical amount is more likely a double entry.
            if (second.invoice_date - first.invoice_date).days > 14:
                continue
            # ...unless the references differ, which means they are distinct
            # pieces of work that happen to cost the same. Staged progress
            # claims do this constantly: a builder raising "83 Gordon (Base
            # Stage)" and "11 Jefferson (Base Stage)" for $66,000 each on the
            # same day is billing two properties, not double-entering one.
            # Without this, one real ledger produced 21 pairs and zero were real.
            ref_a, ref_b = _reference_key(first.reference), _reference_key(second.reference)
            if ref_a and ref_b and ref_a != ref_b:
                continue
            suspicious.append((first, second))

    if not suspicious:
        return []

    currency = ctx.organisation.base_currency
    return [
        Finding(
            check="duplicate_invoices",
            severity=WARNING,
            title=f"{len(suspicious)} possible duplicate invoice pair{'s' if len(suspicious) > 1 else ''}",
            detail=(
                "Each pair is the same contact and the same total, dated within 14 days. "
                "Recurring charges look like this legitimately, so confirm before voiding anything."
            ),
            action="Open each pair in Xero and void the duplicate if it is genuinely a double entry.",
            items=tuple(
                f"{a.invoice_number} & {b.invoice_number} — {a.contact.name} — {fmt(a.amount_total, currency)}"
                for a, b in suspicious[:15]
            ),
        )
    ]


def check_draft_invoices(ctx: CloseContext) -> list[Finding]:
    """Drafts sitting unapproved: revenue and GST not yet in the ledger."""
    drafts = [
        i for i in ctx.invoices
        if i.status in (InvoiceStatus.DRAFT, InvoiceStatus.SUBMITTED)
    ]
    if not drafts:
        return []

    currency = ctx.organisation.base_currency
    value = sum((i.amount_total for i in drafts), ZERO)
    in_period = [i for i in ctx.in_period(drafts)]
    severity = WARNING if in_period else INFO

    return [
        Finding(
            check="draft_invoices",
            severity=severity,
            title=f"{len(drafts)} invoice{'s' if len(drafts) > 1 else ''} still in draft",
            detail=(
                f"{fmt(value, currency)} is sitting in draft or awaiting approval"
                + (
                    f", of which {len(in_period)} fall inside {ctx.period.label}. "
                    "Draft invoices are not in the ledger, so both revenue and GST are understated "
                    "for the period until they are approved."
                    if in_period and ctx.period
                    else "."
                )
            ),
            action="Approve the ones that belong in the period, or move their date out of it.",
            items=tuple(
                f"{i.invoice_number or '(no number)'} — {i.contact.name} — {fmt(i.amount_total, currency)}"
                for i in drafts[:15]
            ),
            value=value,
        )
    ]


def check_gst_consistency(ctx: CloseContext) -> list[Finding]:
    """Recompute GST per line and flag invoices whose tax does not reconcile.

    This is arithmetic, not tax advice: it checks that a line marked GST-
    inclusive carries 1/11th tax and an exclusive line carries 10%. A mismatch
    usually means a line is on the wrong tax rate — GST-free where it should be
    taxable, or vice versa — which flows straight into the BAS.
    """
    if not ctx.gst_registered or not ctx.organisation.is_australian:
        return []

    assert ctx.period is not None
    currency = ctx.organisation.base_currency
    mismatches: list[tuple[Invoice, Decimal, Decimal]] = []

    for invoice in ctx.in_period(ctx.invoices):
        if not invoice.line_items:
            continue
        expected = ZERO
        for line in invoice.line_items:
            if line.tax_treatment is TaxTreatment.INCLUSIVE:
                expected += gst_from_inclusive(line.line_amount)
            elif line.tax_treatment is TaxTreatment.EXCLUSIVE:
                expected += gst_from_exclusive(line.line_amount)
        expected = quantize(expected)
        actual = invoice.amount_tax
        # Allow a couple of cents for Xero's per-line rounding.
        if abs(expected - actual) > Decimal("0.02"):
            mismatches.append((invoice, expected, actual))

    if not mismatches:
        return []

    return [
        Finding(
            check="gst_consistency",
            severity=WARNING,
            title=f"{len(mismatches)} invoice{'s' if len(mismatches) > 1 else ''} with GST that does not recompute",
            detail=(
                f"Recomputing GST from the line amounts and their tax treatment gives a different "
                f"total to the invoice's recorded tax, for invoices dated in {ctx.period.label}. "
                "The usual cause is a line on a GST-free or BAS-excluded rate that should be taxable."
            ),
            action="Check the tax rate on each line before lodging the BAS for this period.",
            items=tuple(
                f"{inv.invoice_number} — {inv.contact.name} — recorded {fmt(actual, currency)}, "
                f"recomputes to {fmt(expected, currency)}"
                for inv, expected, actual in mismatches[:15]
            ),
        )
    ]


def check_gst_summary(ctx: CloseContext) -> list[Finding]:
    """An indicative GST position for the period, for cross-checking the BAS."""
    if not ctx.gst_registered or not ctx.organisation.is_australian:
        return []

    assert ctx.period is not None
    currency = ctx.organisation.base_currency
    sales = ctx.in_period(ctx.sales)
    bills = ctx.in_period(ctx.bills)

    gst_collected = quantize(sum((i.amount_tax for i in sales), ZERO))
    gst_paid = quantize(sum((i.amount_tax for i in bills), ZERO))
    net = quantize(gst_collected - gst_paid)
    total_sales = quantize(sum((i.amount_total for i in sales), ZERO))

    direction = "payable to the ATO" if net >= 0 else "refundable from the ATO"
    due_note = (
        f" Standard self-lodgement due date is {ctx.period.due.strftime('%d %b %Y')}"
        " (later if you lodge through a registered agent)."
        if ctx.period.due
        else ""
    )

    return [
        Finding(
            check="gst_summary",
            severity=INFO,
            title=f"Indicative GST for {ctx.period.label}: {fmt(abs(net), currency)} {direction}",
            detail=(
                f"G1 total sales {fmt(total_sales, currency)} · "
                f"1A GST on sales {fmt(gst_collected, currency)} · "
                f"1B GST on purchases {fmt(gst_paid, currency)}."
                f"{due_note}"
            ),
            action=(
                "Cross-check against Xero's own GST Reconciliation report before lodging. This figure "
                "is computed on an accruals basis from invoice dates and excludes payroll, "
                "adjustments, and anything not raised as an invoice."
            ),
            value=net,
        )
    ]


def check_cash_vs_receivables(ctx: CloseContext) -> list[Finding]:
    """Zero cash alongside a large debtor book usually means no bank feed."""
    outstanding = sum((i.amount_due for i in ctx.sales if i.is_outstanding), ZERO)
    if ctx.cash_balance > 0 or outstanding <= 0:
        return []

    currency = ctx.organisation.base_currency
    return [
        Finding(
            check="cash_vs_receivables",
            severity=CRITICAL,
            title="Cash balance is zero but there are receivables on the ledger",
            detail=(
                f"The ledger shows {fmt(outstanding, currency)} owed to the business and "
                f"{fmt(ctx.cash_balance, currency)} in cash. A zero cash balance alongside an "
                "active debtor book almost always means no bank account is connected, or the "
                "bank feed has stopped importing — not that the business holds no money."
            ),
            action="Check the bank feed in Xero (Accounting > Bank accounts) and reconnect it if it has lapsed.",
            value=outstanding,
        )
    ]


def check_missing_due_dates(ctx: CloseContext) -> list[Finding]:
    """Invoices with no due date never appear as overdue and never get chased."""
    missing = [
        i for i in ctx.invoices
        if i.is_outstanding and not i.is_credit and i.due_date is None
    ]
    if not missing:
        return []

    currency = ctx.organisation.base_currency
    value = sum((i.amount_due for i in missing), ZERO)
    return [
        Finding(
            check="missing_due_dates",
            severity=WARNING,
            title=f"{len(missing)} outstanding invoice{'s' if len(missing) > 1 else ''} with no due date",
            detail=(
                f"{fmt(value, currency)} has no due date, so it never ages into an overdue bucket "
                "and will not appear on a chase list or a customer statement as late."
            ),
            action="Set payment terms on these invoices so they age correctly.",
            items=tuple(f"{i.invoice_number} — {i.contact.name} — {fmt(i.amount_due, currency)}" for i in missing[:15]),
            value=value,
        )
    ]


def check_contacts_missing_email(ctx: CloseContext) -> list[Finding]:
    """Debtors with no email address cannot be sent a statement."""
    with_debt = {
        i.contact.name
        for i in ctx.sales
        if i.is_outstanding and not i.is_credit and not (i.contact.email or "").strip()
    }
    if not with_debt:
        return []

    return [
        Finding(
            check="contacts_missing_email",
            severity=WARNING,
            title=f"{len(with_debt)} debtor{'s' if len(with_debt) > 1 else ''} with no email address",
            detail=(
                "These contacts owe money but have no email on file, so automated reminders and "
                "statements cannot reach them."
            ),
            action="Add an accounts-payable email to each contact in Xero.",
            items=tuple(sorted(with_debt)[:15]),
        )
    ]


def check_stale_bank_lines(ctx: CloseContext) -> list[Finding]:
    """Bank lines still uncoded well after the period they fall in."""
    if not ctx.bank_lines:
        return []

    assert ctx.period is not None
    stale = [line for line in ctx.bank_lines if line.date and line.date <= ctx.period.end]
    if not stale:
        return []

    currency = ctx.organisation.base_currency
    value = sum((abs(line.amount) for line in stale), ZERO)
    return [
        Finding(
            check="stale_bank_lines",
            severity=CRITICAL,
            title=f"{len(stale)} bank line{'s' if len(stale) > 1 else ''} unreconciled in {ctx.period.label}",
            detail=(
                f"{fmt(value, currency)} of bank activity dated on or before {ctx.period.end:%d %b %Y} "
                "has not been coded. The period cannot be closed while these are outstanding, and any "
                "GST inside them is missing from the BAS."
            ),
            action="Run `xerobk reconcile` to get coding suggestions for these lines.",
            items=tuple(
                f"{line.date:%d %b %Y} — {line.description[:60]} — {fmt(line.amount, currency)}"
                for line in sorted(stale, key=lambda x: x.date or date.min)[:15]
            ),
            value=value,
        )
    ]


def check_large_transactions(ctx: CloseContext) -> list[Finding]:
    """Unusually large items in the period, for a sanity read before sign-off."""
    assert ctx.period is not None
    large = [
        i for i in ctx.in_period(ctx.invoices)
        if i.amount_total >= ctx.large_threshold
    ]
    if not large:
        return []

    currency = ctx.organisation.base_currency
    large.sort(key=lambda i: i.amount_total, reverse=True)
    return [
        Finding(
            check="large_transactions",
            severity=INFO,
            title=f"{len(large)} transaction{'s' if len(large) > 1 else ''} over {fmt(ctx.large_threshold, currency)} in {ctx.period.label}",
            detail="Large items are worth an eyeball before sign-off, for both coding and GST treatment.",
            action="Confirm each is coded to the right account and tax rate.",
            items=tuple(
                f"{i.invoice_number} — {i.contact.name} — {fmt(i.amount_total, currency)} "
                f"({'sale' if i.invoice_type is InvoiceType.ACCREC else 'bill'})"
                for i in large[:15]
            ),
        )
    ]


def check_aging_profile(ctx: CloseContext) -> list[Finding]:
    """Report the aged profile as context for the other receivables findings."""
    from .aging import bucket_invoices

    outstanding = [i for i in ctx.sales if i.is_outstanding]
    if not outstanding:
        return []

    buckets = bucket_invoices(outstanding, ctx.as_of)
    currency = ctx.organisation.base_currency
    parts = [
        f"{BUCKET_LABELS[key]} {fmt(value, currency)}"
        for key, value in buckets.as_dict().items()
        if value > 0
    ]
    overdue_share = pct(buckets.overdue, buckets.total)

    return [
        Finding(
            check="aging_profile",
            severity=INFO,
            title=f"Receivables aged profile — {overdue_share}% overdue",
            detail=" · ".join(parts),
            action="",
            value=buckets.total,
        )
    ]


CHECKS: tuple[Callable[[CloseContext], list[Finding]], ...] = (
    check_cash_vs_receivables,
    check_stale_bank_lines,
    check_overdue_debt,
    check_debtor_concentration,
    check_duplicate_contacts,
    check_duplicate_invoices,
    check_draft_invoices,
    check_gst_consistency,
    check_missing_due_dates,
    check_contacts_missing_email,
    check_large_transactions,
    check_gst_summary,
    check_aging_profile,
)


@dataclass(frozen=True, slots=True)
class CloseReport:
    period: autax.Period
    as_of: date
    organisation: Organisation
    findings: tuple[Finding, ...]

    @property
    def critical(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == CRITICAL)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == WARNING)

    @property
    def info(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == INFO)

    @property
    def is_clean(self) -> bool:
        """No blockers. Warnings still want a look, but do not stop a close."""
        return not self.critical

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": {
                "label": self.period.label,
                "start": self.period.start.isoformat(),
                "end": self.period.end.isoformat(),
                "due": self.period.due.isoformat() if self.period.due else None,
            },
            "as_of": self.as_of.isoformat(),
            "organisation": self.organisation.name,
            "counts": {
                "critical": len(self.critical),
                "warning": len(self.warnings),
                "info": len(self.info),
            },
            "is_clean": self.is_clean,
            "findings": [f.to_dict() for f in self.findings],
        }


def run_close(ctx: CloseContext) -> CloseReport:
    """Run every check and return a sorted report."""
    findings: list[Finding] = []
    for check in CHECKS:
        try:
            findings.extend(check(ctx))
        except Exception as exc:
            findings.append(
                Finding(
                    check=getattr(check, "__name__", "unknown"),
                    severity=WARNING,
                    title=f"Check '{getattr(check, '__name__', 'unknown')}' failed to run",
                    detail=f"{type(exc).__name__}: {exc}",
                    action="This is a bug in xerobk, not a finding about your ledger.",
                )
            )

    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.check))
    assert ctx.period is not None
    return CloseReport(
        period=ctx.period,
        as_of=ctx.as_of,
        organisation=ctx.organisation,
        findings=tuple(findings),
    )


def build_context(
    provider: Any,
    as_of: date | None = None,
    frequency: str = "quarterly",
    gst_registered: bool = True,
    escalation_days: int = 60,
    large_threshold: str = "10000.00",
    suspense_codes: tuple[str, ...] = (),
    bank_lines: list[BankLine] | None = None,
) -> CloseContext:
    """Gather everything the checks need from a provider."""
    when = as_of or date.today()
    return CloseContext(
        as_of=when,
        organisation=provider.organisation(),
        invoices=provider.invoices(),
        contacts=provider.contacts(),
        bank_lines=bank_lines if bank_lines is not None else provider.bank_lines(),
        cash_balance=provider.cash_position(when).cash_balance,
        period=autax.close_period(when, frequency),
        gst_registered=gst_registered,
        escalation_days=escalation_days,
        large_threshold=to_money(large_threshold),
        suspense_codes=suspense_codes,
    )
