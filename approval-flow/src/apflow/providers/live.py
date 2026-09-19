"""Live Xero Accounting API provider.

Built on ``urllib`` so the package keeps zero runtime dependencies.

Two behaviours matter more than the endpoint plumbing:

**Rate limits.** Xero enforces 60 calls/minute and 5,000/day per tenant, and
answers a breach with HTTP 429 plus a ``Retry-After`` header. This client paces
itself against the minute limit locally and obeys ``Retry-After`` when told to
back off, rather than burning the daily allowance on retries.

**Writes name the document by id and change one field.** Approving a bill sets
its status and nothing else. Posting a full document body back would silently
overwrite any edit made in Xero since the pull — including, in the worst case,
reinstating the version the approver rejected.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..config import XERO_API_BASE, Settings
from ..models import Account, Contact, Document, DocumentKind, Organisation
from . import AuthError, ProviderError, ReadOnlyError
from .oauth import TokenSet, refresh

_MAX_CALLS_PER_MINUTE = 55
_PAGE_SIZE = 100

# Endpoint and payload key per document kind. Purchase orders and credit notes
# are separate endpoints in Xero, not variants of Invoices.
_ENDPOINTS: dict[DocumentKind, tuple[str, str]] = {
    DocumentKind.BILL: ("Invoices", "Invoices"),
    DocumentKind.INVOICE: ("Invoices", "Invoices"),
    DocumentKind.PURCHASE_ORDER: ("PurchaseOrders", "PurchaseOrders"),
    DocumentKind.CREDIT_NOTE: ("CreditNotes", "CreditNotes"),
}

_ID_FIELD: dict[DocumentKind, str] = {
    DocumentKind.BILL: "InvoiceID",
    DocumentKind.INVOICE: "InvoiceID",
    DocumentKind.PURCHASE_ORDER: "PurchaseOrderID",
    DocumentKind.CREDIT_NOTE: "CreditNoteID",
}


class LiveProvider:
    """Talks to the Xero Accounting API on behalf of one connected tenant."""

    def __init__(self, settings: Settings, tokens: TokenSet | None = None) -> None:
        self.settings = settings
        self.tokens = tokens or TokenSet.load()
        if not self.tokens.refresh_token and not self.tokens.access_token:
            raise AuthError("not connected to Xero — run `apflow connect` first")
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
                "This connection is read-only. Re-run `apflow connect --allow-writes` "
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
            if page > 200:  # hard stop; 20k documents is past any real queue
                return out

    # -- reads ------------------------------------------------------------

    def organisation(self) -> Organisation:
        if "org" not in self._cache:
            rows = self._request("Organisations").get("Organisations") or [{}]
            self._cache["org"] = Organisation.from_api(rows[0])
        return self._cache["org"]  # type: ignore[no-any-return]

    def contacts(self) -> list[Contact]:
        return [Contact.from_api(row) for row in self._paged("Contacts", "Contacts")]

    def accounts(self) -> list[Account]:
        return [Account.from_api(row) for row in (self._request("Accounts").get("Accounts") or [])]

    def documents(self, kind: DocumentKind, status: str | None = None) -> list[Document]:
        path, key = _ENDPOINTS[kind]
        params: dict[str, Any] = {}
        where: list[str] = []
        if path == "Invoices":
            where.append(f'Type=="{kind.xero_type}"')
        if status:
            where.append(f'Status=="{status.upper()}"')
        if where:
            params["where"] = "&&".join(where)
        rows = self._paged(path, key, params)

        # Xero's collection endpoints omit line items; only the single-document
        # endpoint returns them. Lines drive coding, budget allocation and the
        # fingerprint, so a document fetched without them would produce a
        # fingerprint that changes the moment anyone looks at it properly.
        documents: list[Document] = []
        for row in rows:
            if not (row.get("LineItems") or row.get("line_items")):
                identifier = row.get(_ID_FIELD[kind])
                if identifier:
                    detail = self._request(f"{path}/{identifier}").get(key) or []
                    if detail:
                        row = detail[0]
            documents.append(Document.from_api(row, kind))
        return documents

    # -- writes -----------------------------------------------------------

    def set_document_status(self, document: Document, status: str) -> dict[str, Any]:
        """Change one document's status, naming it by id and nothing else."""
        path, key = _ENDPOINTS[document.kind]
        body = {key: [{_ID_FIELD[document.kind]: document.document_id, "Status": status.upper()}]}
        return self._request(path, method="POST", body=body)
