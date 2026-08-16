"""Live Xero Accounting API provider.

Built on ``urllib`` so the package keeps zero runtime dependencies.

Two behaviours matter more than the endpoint plumbing:

**Rate limits.** Xero enforces 60 calls/minute and 5,000/day per tenant, and
answers a breach with HTTP 429 plus a ``Retry-After`` header. This client
paces itself against the minute limit locally and honours ``Retry-After`` when
it is told to back off, rather than hammering until the daily limit is burnt.

**Aging is computed, not fetched.** The raw Accounting API's
``AgedReceivablesByContact`` report is per-contact, so building an
organisation-wide aged report from it would cost one call per customer. The
outstanding invoices are fetched once and bucketed locally with
:mod:`xerobk.aging` — the same code path the offline provider uses, so the two
cannot drift apart.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any

from ..aging import bucket_invoices, bucket_rows
from ..config import XERO_API_BASE, Settings
from ..models import (
    Account,
    BankLine,
    CashPosition,
    Contact,
    Invoice,
    InvoiceType,
    Organisation,
)
from ..money import ZERO, to_money
from . import AgedReport, AuthError, ProviderError, ReadOnlyError
from .oauth import TokenSet, refresh

# Xero allows 60 calls per rolling minute. Stay under it with a small margin.
_MAX_CALLS_PER_MINUTE = 55
_PAGE_SIZE = 100


def financial_year_bounds(
    end_month: int, end_day: int, today: date | None = None
) -> tuple[date | None, date | None]:
    """Derive the current financial year from Xero's stored FY *end* month/day.

    Xero records only when the year ends (for an Australian org, 30 June), so
    the enclosing year has to be inferred: if today is on or before that day
    this financial year ends in the current calendar year, otherwise it ends in
    the next one. The start is the day after the previous year's end.

    Returns ``(None, None)`` if the stored month/day is not a real date.
    """
    when = today or date.today()
    end_year = when.year if (when.month, when.day) <= (end_month, end_day) else when.year + 1
    try:
        fy_end = date(end_year, end_month, end_day)
        fy_start = date(end_year - 1, end_month, end_day) + timedelta(days=1)
    except ValueError:
        return None, None
    return fy_start, fy_end


class LiveProvider:
    """Talks to the Xero Accounting API on behalf of one connected tenant."""

    def __init__(self, settings: Settings, tokens: TokenSet | None = None) -> None:
        self.settings = settings
        self.tokens = tokens or TokenSet.load()
        if not self.tokens.refresh_token and not self.tokens.access_token:
            raise AuthError("not connected to Xero — run `xerobk connect` first")
        self.can_write = settings.allow_writes
        self.name = f"xero:{self.tokens.tenant_name or self.tokens.tenant_id[:8]}"
        self._call_times: list[float] = []
        self._cache: dict[str, Any] = {}

    # -- transport --------------------------------------------------------

    def _throttle(self) -> None:
        """Block if 55 calls have already gone out in the trailing minute."""
        now = time.time()
        self._call_times = [t for t in self._call_times if now - t < 60]
        if len(self._call_times) >= _MAX_CALLS_PER_MINUTE:
            sleep_for = 60 - (now - self._call_times[0]) + 0.5
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._call_times = [t for t in self._call_times if time.time() - t < 60]
        self._call_times.append(time.time())

    def _access_token(self) -> str:
        if self.tokens.is_expired:
            if not self.settings.client_id:
                raise AuthError("XERO_CLIENT_ID is not set; cannot refresh the access token")
            self.tokens = refresh(self.settings.client_id, self.tokens)
        return self.tokens.access_token

    def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        _attempt: int = 0,
    ) -> dict[str, Any]:
        if method != "GET" and not self.can_write:
            raise ReadOnlyError(
                "This connection is read-only. Re-run `xerobk connect --allow-writes` "
                "to grant write scopes."
            )

        self._throttle()
        url = f"{XERO_API_BASE}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"

        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Xero-Tenant-Id": self.tokens.tenant_id,
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if payload else {}),
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and _attempt < 3:
                # Xero tells us exactly how long to wait; obey it rather than
                # guessing, and never retry more than three times.
                wait = int(exc.headers.get("Retry-After") or 60)
                time.sleep(min(wait, 300) + 1)
                return self._request(path, params, method, body, _attempt + 1)
            if exc.code == 401 and _attempt < 1:
                # Token rejected despite looking fresh; force one refresh.
                self.tokens.expires_at = 0
                return self._request(path, params, method, body, _attempt + 1)
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise ProviderError(f"Xero API {method} {path} failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"could not reach Xero: {exc.reason}") from exc

    def _paged(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Walk Xero's page-numbered collections until a short page arrives."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request(path, {**(params or {}), "page": page, "pageSize": _PAGE_SIZE})
            rows = data.get(key) or []
            out.extend(rows)
            if len(rows) < _PAGE_SIZE:
                return out
            page += 1
            if page > 200:  # hard stop; 20k records is well past any real ledger
                return out

    # -- reads ------------------------------------------------------------

    def organisation(self) -> Organisation:
        if "org" not in self._cache:
            rows = self._request("Organisations").get("Organisations") or [{}]
            raw = rows[0]
            fy_start, fy_end = financial_year_bounds(
                end_month=int(raw.get("FinancialYearEndMonth") or 6),
                end_day=int(raw.get("FinancialYearEndDay") or 30),
            )
            self._cache["org"] = Organisation(
                name=str(raw.get("Name") or ""),
                legal_name=str(raw.get("LegalName") or ""),
                organisation_type=str(raw.get("OrganisationType") or ""),
                country=str(raw.get("CountryCode") or "AU"),
                base_currency=str(raw.get("BaseCurrency") or "AUD"),
                timezone=str(raw.get("Timezone") or "Australia/Sydney"),
                line_of_business=str(raw.get("LineOfBusiness") or ""),
                registration_number=str(raw.get("RegistrationNumber") or ""),
                financial_year_start=fy_start,
                financial_year_end=fy_end,
            )
        return self._cache["org"]  # type: ignore[no-any-return]

    def contacts(self) -> list[Contact]:
        rows = self._paged("Contacts", "Contacts")
        return [Contact.from_api(row) for row in rows]

    def accounts(self) -> list[Account]:
        rows = self._request("Accounts").get("Accounts") or []
        return [Account.from_api(row) for row in rows]

    def invoices(self, invoice_type: InvoiceType | None = None) -> list[Invoice]:
        where_parts = ['Status!="DELETED"', 'Status!="VOIDED"']
        if invoice_type is not None:
            where_parts.append(f'Type=="{invoice_type.value}"')
        rows = self._paged("Invoices", "Invoices", {"where": "&&".join(where_parts)})
        return [Invoice.from_api(row) for row in rows]

    def _outstanding(self, invoice_type: InvoiceType) -> list[Invoice]:
        return [inv for inv in self.invoices(invoice_type) if inv.is_outstanding]

    def _aged(self, invoice_type: InvoiceType, as_of: date | None) -> AgedReport:
        when = as_of or date.today()
        invoices = self._outstanding(invoice_type)
        return AgedReport(
            as_of=when,
            buckets=bucket_invoices(invoices, when),
            rows=tuple(bucket_rows(invoices, when)),
            currency=self.organisation().base_currency,
        )

    def aged_receivables(self, as_of: date | None = None) -> AgedReport:
        return self._aged(InvoiceType.ACCREC, as_of)

    def aged_payables(self, as_of: date | None = None) -> AgedReport:
        return self._aged(InvoiceType.ACCPAY, as_of)

    def cash_position(self, as_of: date | None = None) -> CashPosition:
        when = as_of or date.today()
        cash = ZERO
        for account in self.accounts():
            if account.account_type.upper() != "BANK":
                continue
            summary = self._request(
                "Reports/BankSummary", {"fromDate": when.isoformat(), "toDate": when.isoformat()}
            )
            cash += _closing_balance(summary, account.code)
            break  # BankSummary already covers every bank account in one call
        return CashPosition(
            cash_balance=cash,
            amount_owed=self.aged_receivables(when).buckets.total,
            amount_due=self.aged_payables(when).buckets.total,
            snapshot_date=when,
            currency=self.organisation().base_currency,
        )

    def bank_lines(self) -> list[BankLine]:
        """Unreconciled bank statement lines.

        Xero exposes statement lines only through the Bank Feeds API, which
        requires a separate partner-level app. For a desktop bookkeeping tool
        the practical path is importing the bank's own CSV, so this returns
        empty and the CSV importer is the supported route.
        """
        return []

    def bank_accounts(self) -> list[Account]:
        """Bank accounts, which payments and bank transactions must post against."""
        return [a for a in self.accounts() if a.account_type.upper() == "BANK"]

    # -- writes -----------------------------------------------------------

    def create_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("Invoices", method="POST", body={"Invoices": [payload]})

    def create_bank_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("BankTransactions", method="POST", body={"BankTransactions": [payload]})

    def create_payment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a payment to an invoice or bill.

        This is what actually reconciles a receipt against an invoice in Xero;
        creating a bank transaction alone would record the money but leave the
        invoice showing as unpaid.

        Xero's Payments endpoint takes PUT, not POST — a POST is silently
        treated as a different operation and will not do what you expect.
        """
        return self._request("Payments", method="PUT", body={"Payments": [payload]})


def _closing_balance(report: dict[str, Any], account_code: str) -> Any:
    """Pull the closing-balance total out of a BankSummary report payload."""
    total = ZERO
    reports = report.get("Reports") or []
    for section in reports:
        for row in section.get("Rows") or []:
            for inner in row.get("Rows") or []:
                cells = inner.get("Cells") or []
                if len(cells) >= 5:
                    total += to_money(cells[-1].get("Value"))
    return total
