"""Data providers.

The rest of the application talks to :class:`XeroProvider` and never to Xero
directly. Two implementations satisfy it:

* :class:`~xerobk.providers.snapshot.SnapshotProvider` reads a local JSON
  snapshot. It needs no credentials and no network, so the whole app — reports,
  reconciliation, close checklist — is usable and testable today.
* :class:`~xerobk.providers.live.LiveProvider` talks to the Xero Accounting API
  over OAuth 2.0. It appears once ``XERO_CLIENT_ID`` is set.

Write operations are a separate, explicitly-capability-gated part of the
interface. A provider that cannot write raises :class:`ReadOnlyError` rather
than silently doing nothing, so a caller can never believe it posted something
it did not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

from ..models import (
    Account,
    AgedBuckets,
    BankLine,
    CashPosition,
    Contact,
    Invoice,
    InvoiceType,
    Organisation,
)


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ReadOnlyError(ProviderError):
    """Raised when a write is attempted against a read-only provider."""


class AuthError(ProviderError):
    """Raised when credentials are missing, invalid, or expired beyond refresh."""


@dataclass(frozen=True, slots=True)
class AgedReport:
    """An aged receivables or payables report."""

    as_of: date
    buckets: AgedBuckets
    rows: tuple[dict[str, Any], ...] = ()
    currency: str = "AUD"

    @property
    def total(self) -> Any:
        return self.buckets.total

    @property
    def overdue(self) -> Any:
        return self.buckets.overdue


@runtime_checkable
class XeroProvider(Protocol):
    """The full surface the application depends on."""

    name: str
    can_write: bool

    def organisation(self) -> Organisation: ...

    def contacts(self) -> list[Contact]: ...

    def accounts(self) -> list[Account]: ...

    def invoices(self, invoice_type: InvoiceType | None = None) -> list[Invoice]: ...

    def aged_receivables(self, as_of: date | None = None) -> AgedReport: ...

    def aged_payables(self, as_of: date | None = None) -> AgedReport: ...

    def cash_position(self, as_of: date | None = None) -> CashPosition: ...

    def bank_lines(self) -> list[BankLine]: ...

    # --- writes (optional; guarded by can_write) -------------------------

    def create_invoice(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def create_bank_transaction(self, payload: dict[str, Any]) -> dict[str, Any]: ...


__all__ = [
    "AgedReport",
    "AuthError",
    "ProviderError",
    "ReadOnlyError",
    "XeroProvider",
]
