"""Offline provider backed by a directory of JSON files.

A snapshot directory contains any of these files, all optional:

    organisation.json      the org info + financial year block
    contacts.json          {"contacts": [...]}
    accounts.json          {"accounts": [...]}
    invoices.json          {"invoices": [...]}     sales invoices (ACCREC)
    bills.json             {"invoices": [...]}     supplier bills (ACCPAY)
    aged_receivables.json  the aged receivables report
    aged_payables.json     the aged payables report
    cash_position.json     the cash position summary
    bank_lines.csv         bank statement lines to be coded

The JSON shapes are exactly what the Xero connector returns, so a snapshot can
be produced by dumping tool responses verbatim — no transformation step to get
wrong.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

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
from . import AgedReport, ProviderError, ReadOnlyError
from .bankcsv import read_bank_csv

_READ_ONLY = (
    "This is an offline snapshot — it cannot post {what} to Xero. "
    "Set XERO_CLIENT_ID and run `xerobk connect --allow-writes` to enable writes."
)


class SnapshotProvider:
    """Reads a point-in-time snapshot from disk. Never touches the network."""

    can_write = False

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_dir():
            raise ProviderError(f"snapshot directory not found: {self.path}")
        self.name = f"snapshot:{self.path.name}"
        self._cache: dict[str, Any] = {}

    # -- file access ------------------------------------------------------

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
                # Tolerate a bare list where a wrapper object was expected.
                data = {"items": loaded}
        self._cache[filename] = data
        return data

    def _snapshot_date(self) -> date:
        """The date the snapshot represents, for correct aging arithmetic.

        Using ``today`` against a stale snapshot would silently shift every
        invoice into an older bucket, so the snapshot's own date wins.
        """
        for filename, key in (
            ("aged_receivables.json", "as_of_date"),
            ("cash_position.json", "snapshot_date"),
        ):
            raw = self._load(filename).get(key)
            if raw:
                try:
                    return date.fromisoformat(str(raw)[:10])
                except ValueError:
                    continue
        return date.today()

    # -- reads ------------------------------------------------------------

    def organisation(self) -> Organisation:
        raw = self._load("organisation.json")
        # Accept either a flat org blob or one with the FY block nested.
        fy = raw.get("financial_year") if isinstance(raw.get("financial_year"), dict) else raw
        return Organisation.from_api(raw, fy)

    def contacts(self) -> list[Contact]:
        raw = self._load("contacts.json")
        rows = raw.get("contacts") or raw.get("items") or []
        return [Contact.from_api(row) for row in rows]

    def accounts(self) -> list[Account]:
        raw = self._load("accounts.json")
        rows = raw.get("accounts") or raw.get("items") or []
        return [Account.from_api(row) for row in rows]

    def bank_accounts(self) -> list[Account]:
        return [a for a in self.accounts() if a.account_type.upper() == "BANK"]

    def invoices(self, invoice_type: InvoiceType | None = None) -> list[Invoice]:
        out: list[Invoice] = []
        wanted = (
            [(("invoices.json"), InvoiceType.ACCREC), ("bills.json", InvoiceType.ACCPAY)]
            if invoice_type is None
            else [
                ("invoices.json", InvoiceType.ACCREC)
                if invoice_type is InvoiceType.ACCREC
                else ("bills.json", InvoiceType.ACCPAY)
            ]
        )
        for filename, default_type in wanted:
            raw = self._load(filename)
            rows = raw.get("invoices") or raw.get("items") or []
            out.extend(Invoice.from_api(row, default_type) for row in rows)
        return out

    def _aged(self, filename: str) -> AgedReport:
        raw = self._load(filename)
        buckets = raw.get("age_buckets") or {}
        rows = raw.get("aged_receivables") or raw.get("aged_payables") or raw.get("items") or []
        as_of_raw = raw.get("as_of_date")
        try:
            as_of = date.fromisoformat(str(as_of_raw)[:10]) if as_of_raw else self._snapshot_date()
        except ValueError:
            as_of = self._snapshot_date()
        return AgedReport(
            as_of=as_of,
            buckets=AgedBuckets.from_api(buckets),
            rows=tuple(rows),
            currency=str(raw.get("organisation_base_currency") or "AUD"),
        )

    def aged_receivables(self, as_of: date | None = None) -> AgedReport:
        return self._aged("aged_receivables.json")

    def aged_payables(self, as_of: date | None = None) -> AgedReport:
        return self._aged("aged_payables.json")

    def cash_position(self, as_of: date | None = None) -> CashPosition:
        return CashPosition.from_api(self._load("cash_position.json"))

    def bank_lines(self) -> list[BankLine]:
        csv_path = self.path / "bank_lines.csv"
        if csv_path.exists():
            return read_bank_csv(csv_path)
        return []

    # -- writes -----------------------------------------------------------

    def create_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise ReadOnlyError(_READ_ONLY.format(what="invoices"))

    def create_bank_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise ReadOnlyError(_READ_ONLY.format(what="bank transactions"))

    def create_payment(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise ReadOnlyError(_READ_ONLY.format(what="payments"))
