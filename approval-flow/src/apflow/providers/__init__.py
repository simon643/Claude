"""Data providers.

The rest of the application talks to :class:`LedgerProvider` and never to Xero
directly. Two implementations satisfy it:

* :class:`~apflow.providers.snapshot.SnapshotProvider` reads a local JSON
  snapshot. It needs no credentials and no network, so the whole approval
  workflow is usable and testable without touching a real ledger.
* :class:`~apflow.providers.live.LiveProvider` talks to the Xero Accounting API
  over OAuth 2.0. It appears once ``XERO_CLIENT_ID`` is set.

Write operations are separate and capability-gated. A provider that cannot
write raises :class:`ReadOnlyError` rather than silently doing nothing, so a
caller can never believe an approval reached the ledger when it did not.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..models import Account, Contact, Document, DocumentKind, Organisation


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ReadOnlyError(ProviderError):
    """Raised when a write is attempted against a read-only provider."""


class AuthError(ProviderError):
    """Raised when credentials are missing, invalid, or expired beyond refresh."""


@runtime_checkable
class LedgerProvider(Protocol):
    """The surface the approval engine depends on."""

    name: str
    can_write: bool

    def organisation(self) -> Organisation: ...

    def contacts(self) -> list[Contact]: ...

    def accounts(self) -> list[Account]: ...

    def documents(self, kind: DocumentKind, status: str | None = None) -> list[Document]: ...

    # --- writes (optional; guarded by can_write) -------------------------

    def set_document_status(self, document: Document, status: str) -> dict[str, Any]: ...


__all__ = ["AuthError", "LedgerProvider", "ProviderError", "ReadOnlyError"]
