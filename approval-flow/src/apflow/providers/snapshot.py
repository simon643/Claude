"""Offline provider backed by a directory of JSON files.

A snapshot directory contains any of these files, all optional:

    organisation.json      the org info block
    contacts.json          {"contacts": [...]}
    accounts.json          {"accounts": [...]}
    bills.json             {"invoices": [...]}          supplier bills (ACCPAY)
    invoices.json          {"invoices": [...]}          sales invoices (ACCREC)
    purchase_orders.json   {"purchase_orders": [...]}
    credit_notes.json      {"credit_notes": [...]}

The JSON shapes are what the Xero connector returns, so a snapshot can be
produced by dumping tool responses verbatim — no transformation step to get
wrong.

Writes are refused rather than faked. A snapshot that pretended to authorise a
bill would leave the local record claiming an approval reached a ledger that
never saw it, which is the one lie this tool must never tell.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import Account, Contact, Document, DocumentKind, DocumentStatus, Organisation
from . import ProviderError, ReadOnlyError

_FILES = {
    DocumentKind.BILL: ("bills.json", ("invoices", "bills", "items")),
    DocumentKind.INVOICE: ("invoices.json", ("invoices", "items")),
    DocumentKind.PURCHASE_ORDER: ("purchase_orders.json", ("purchase_orders", "items")),
    DocumentKind.CREDIT_NOTE: ("credit_notes.json", ("credit_notes", "items")),
}


class SnapshotProvider:
    """Reads a point-in-time snapshot from disk. Never touches the network."""

    can_write = False

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_dir():
            raise ProviderError(f"snapshot directory not found: {self.path}")
        self.name = f"snapshot:{self.path.name}"
        self._cache: dict[str, Any] = {}

    def _load(self, filename: str) -> dict[str, Any]:
        if filename in self._cache:
            return self._cache[filename]  # type: ignore[no-any-return]
        target = self.path / filename
        data: dict[str, Any] = {}
        if target.exists():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ProviderError(f"{target} is not valid JSON: {exc}") from exc
            if isinstance(loaded, dict):
                data = loaded
            elif isinstance(loaded, list):
                data = {"items": loaded}
        self._cache[filename] = data
        return data

    def organisation(self) -> Organisation:
        return Organisation.from_api(self._load("organisation.json"))

    def contacts(self) -> list[Contact]:
        raw = self._load("contacts.json")
        return [Contact.from_api(row) for row in (raw.get("contacts") or raw.get("items") or [])]

    def accounts(self) -> list[Account]:
        raw = self._load("accounts.json")
        return [Account.from_api(row) for row in (raw.get("accounts") or raw.get("items") or [])]

    def documents(self, kind: DocumentKind, status: str | None = None) -> list[Document]:
        filename, keys = _FILES[kind]
        raw = self._load(filename)
        rows: list[dict[str, Any]] = []
        for key in keys:
            if raw.get(key):
                rows = list(raw[key])
                break
        documents = [Document.from_api(row, kind) for row in rows]
        if status:
            wanted = DocumentStatus.parse(status)
            documents = [d for d in documents if d.status is wanted]
        return documents

    def set_document_status(self, document: Document, status: str) -> dict[str, Any]:
        raise ReadOnlyError(
            "This is an offline snapshot — it cannot change document status in Xero. "
            "Set XERO_CLIENT_ID and run `apflow connect --allow-writes` to enable writes."
        )
