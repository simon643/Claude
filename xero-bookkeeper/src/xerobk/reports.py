"""Dashboard assembly.

Turns a provider into the numbers the UI and CLI display. Kept separate from
both so the same figures back the browser view, the terminal output, and the
JSON export without being recomputed three slightly different ways.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .aging import BUCKET_LABELS, bucket_invoices, bucket_rows
from .models import AgedBuckets, Invoice, InvoiceType, Organisation
from .money import ZERO, fmt, pct, quantize
from .providers import XeroProvider


@dataclass(frozen=True, slots=True)
class DebtorSummary:
    name: str
    email: str | None
    total: Decimal
    invoice_count: int
    oldest_days: int
    share: Decimal

    def to_dict(self, currency: str = "AUD") -> dict[str, Any]:
        return {
            "name": self.name,
            "email": self.email,
            "total": str(self.total),
            "total_display": fmt(self.total, currency),
            "invoice_count": self.invoice_count,
            "oldest_days": self.oldest_days,
            "share": str(self.share),
        }


@dataclass(frozen=True, slots=True)
class Dashboard:
    organisation: Organisation
    as_of: date
    cash_balance: Decimal
    receivables: Decimal
    payables: Decimal
    receivable_buckets: AgedBuckets
    payable_buckets: AgedBuckets
    top_debtors: tuple[DebtorSummary, ...]
    overdue_invoices: tuple[Invoice, ...]
    due_soon: tuple[Invoice, ...]
    draft_count: int
    source: str = ""
    can_write: bool = False

    @property
    def currency(self) -> str:
        return self.organisation.base_currency

    @property
    def net_position(self) -> Decimal:
        return quantize(self.cash_balance + self.receivables - self.payables)

    @property
    def overdue_total(self) -> Decimal:
        return self.receivable_buckets.overdue

    @property
    def overdue_share(self) -> Decimal:
        return pct(self.receivable_buckets.overdue, self.receivable_buckets.total)

    def to_dict(self) -> dict[str, Any]:
        currency = self.currency
        return {
            "organisation": {
                "name": self.organisation.name,
                "legal_name": self.organisation.legal_name,
                "country": self.organisation.country,
                "currency": currency,
                "type": self.organisation.organisation_type,
                "line_of_business": self.organisation.line_of_business,
                "financial_year_start": (
                    self.organisation.financial_year_start.isoformat()
                    if self.organisation.financial_year_start else None
                ),
                "financial_year_end": (
                    self.organisation.financial_year_end.isoformat()
                    if self.organisation.financial_year_end else None
                ),
            },
            "as_of": self.as_of.isoformat(),
            "source": self.source,
            "can_write": self.can_write,
            "headline": {
                "cash": str(self.cash_balance),
                "cash_display": fmt(self.cash_balance, currency),
                "receivables": str(self.receivables),
                "receivables_display": fmt(self.receivables, currency),
                "payables": str(self.payables),
                "payables_display": fmt(self.payables, currency),
                "net_position": str(self.net_position),
                "net_position_display": fmt(self.net_position, currency),
                "overdue": str(self.overdue_total),
                "overdue_display": fmt(self.overdue_total, currency),
                "overdue_share": str(self.overdue_share),
                "draft_count": self.draft_count,
            },
            "receivable_buckets": [
                {
                    "key": key,
                    "label": BUCKET_LABELS[key],
                    "value": str(value),
                    "display": fmt(value, currency),
                    "share": str(pct(value, self.receivable_buckets.total)),
                }
                for key, value in self.receivable_buckets.as_dict().items()
            ],
            "payable_buckets": [
                {
                    "key": key,
                    "label": BUCKET_LABELS[key],
                    "value": str(value),
                    "display": fmt(value, currency),
                    "share": str(pct(value, self.payable_buckets.total)),
                }
                for key, value in self.payable_buckets.as_dict().items()
            ],
            "top_debtors": [d.to_dict(currency) for d in self.top_debtors],
            "overdue_invoices": [_invoice_row(i, self.as_of, currency) for i in self.overdue_invoices],
            "due_soon": [_invoice_row(i, self.as_of, currency) for i in self.due_soon],
        }


def _invoice_row(invoice: Invoice, as_of: date, currency: str) -> dict[str, Any]:
    return {
        "invoice_id": invoice.invoice_id,
        "invoice_number": invoice.invoice_number,
        "reference": invoice.reference,
        "contact": invoice.contact.name,
        "email": invoice.contact.email,
        "amount_due": str(invoice.amount_due),
        "amount_due_display": fmt(invoice.amount_due, currency),
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "days_overdue": invoice.days_overdue(as_of),
        "type": invoice.invoice_type.value,
        "status": invoice.status.value,
    }


def build_dashboard(
    provider: XeroProvider,
    as_of: date | None = None,
    due_soon_days: int = 14,
    top_n: int = 8,
) -> Dashboard:
    """Assemble every headline figure in one pass over the ledger."""
    organisation = provider.organisation()
    invoices = provider.invoices()

    receivable_report = provider.aged_receivables(as_of)
    payable_report = provider.aged_payables(as_of)
    # Prefer the provider's own report date so a stale snapshot ages correctly.
    when = as_of or receivable_report.as_of or date.today()
    cash = provider.cash_position(when)

    sales = [i for i in invoices if i.invoice_type is InvoiceType.ACCREC]
    bills = [i for i in invoices if i.invoice_type is InvoiceType.ACCPAY]
    outstanding_sales = [i for i in sales if i.is_outstanding]

    # Fall back to computing buckets when the snapshot carried no aged report.
    receivable_buckets = receivable_report.buckets
    if receivable_buckets.total == 0 and outstanding_sales:
        receivable_buckets = bucket_invoices(outstanding_sales, when)
    payable_buckets = payable_report.buckets
    outstanding_bills = [i for i in bills if i.is_outstanding]
    if payable_buckets.total == 0 and outstanding_bills:
        payable_buckets = bucket_invoices(outstanding_bills, when)

    receivables_total = receivable_buckets.total or cash.amount_owed
    payables_total = payable_buckets.total or cash.amount_due

    grouped: dict[str, list[Invoice]] = defaultdict(list)
    for invoice in outstanding_sales:
        grouped[invoice.contact.name].append(invoice)

    debtors: list[DebtorSummary] = []
    for name, group in grouped.items():
        total = sum((i.amount_due for i in group), ZERO)
        email = next((i.contact.email for i in group if i.contact.email), None)
        debtors.append(
            DebtorSummary(
                name=name,
                email=email,
                total=total,
                invoice_count=len(group),
                oldest_days=max((i.days_overdue(when) for i in group), default=0),
                share=pct(total, receivables_total),
            )
        )
    debtors.sort(key=lambda d: d.total, reverse=True)

    overdue = sorted(
        (i for i in outstanding_sales if i.is_overdue(when)),
        key=lambda i: i.days_overdue(when),
        reverse=True,
    )
    horizon = when + timedelta(days=due_soon_days)
    due_soon = sorted(
        (
            i for i in outstanding_sales
            if i.due_date and when <= i.due_date <= horizon
        ),
        key=lambda i: i.due_date or date.max,
    )

    from .models import InvoiceStatus

    draft_count = sum(
        1 for i in invoices if i.status in (InvoiceStatus.DRAFT, InvoiceStatus.SUBMITTED)
    )

    return Dashboard(
        organisation=organisation,
        as_of=when,
        cash_balance=cash.cash_balance,
        receivables=receivables_total,
        payables=payables_total,
        receivable_buckets=receivable_buckets,
        payable_buckets=payable_buckets,
        top_debtors=tuple(debtors[:top_n]),
        overdue_invoices=tuple(overdue),
        due_soon=tuple(due_soon),
        draft_count=draft_count,
        source=provider.name,
        can_write=provider.can_write,
    )


def aged_detail(provider: XeroProvider, as_of: date | None = None, payables: bool = False) -> dict[str, Any]:
    """Full aged report with per-invoice rows, for the detail views."""
    report = provider.aged_payables(as_of) if payables else provider.aged_receivables(as_of)
    when = as_of or report.as_of
    rows = list(report.rows)
    if not rows:
        wanted = InvoiceType.ACCPAY if payables else InvoiceType.ACCREC
        rows = bucket_rows(
            [i for i in provider.invoices(wanted) if i.is_outstanding], when
        )
    return {
        "as_of": when.isoformat(),
        "currency": report.currency,
        "total": str(report.buckets.total),
        "overdue": str(report.buckets.overdue),
        "buckets": {k: str(v) for k, v in report.buckets.as_dict().items()},
        "rows": [_stringify(row) for row in rows],
    }


def _stringify(row: Any) -> Any:
    """JSON-safe conversion that keeps Decimals exact by rendering them as text."""
    if isinstance(row, dict):
        return {k: _stringify(v) for k, v in row.items()}
    if isinstance(row, (list, tuple)):
        return [_stringify(v) for v in row]
    if isinstance(row, Decimal):
        return str(row)
    if isinstance(row, date):
        return row.isoformat()
    return row
