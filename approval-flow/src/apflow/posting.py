"""Writing approved documents back to the ledger.

This is the only module that changes anything outside this machine, and it is
deliberately the smallest one that can do the job.

The control that matters is the re-check. Between the last approval and the
push, the document in Xero can change — that window is exactly where an
approved-then-edited bill would slip through. So every document is re-fetched
immediately before it is posted and its fingerprint compared against the one
the approvers signed off. A mismatch does not push: it feeds the new version
back through the engine, which resets the approvals and puts the request back
in front of the people whose consent is now needed.

The audit row is written before the API call, never after. If a POST succeeds
and the process dies before recording it, an after-the-fact log loses the only
local record of a change that now exists in the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .models import Document, DocumentKind, RequestState
from .money import ZERO, to_money
from .providers import LedgerProvider, ProviderError, ReadOnlyError
from .workflow import Engine

# What "approved" means in the ledger, per document kind. Xero uses the same
# terminal word for all four, but the meaning differs: an authorised bill is
# payable, an authorised purchase order is issuable to the supplier.
_TARGET_STATUS: dict[DocumentKind, str] = {
    DocumentKind.BILL: "AUTHORISED",
    DocumentKind.INVOICE: "AUTHORISED",
    DocumentKind.PURCHASE_ORDER: "AUTHORISED",
    DocumentKind.CREDIT_NOTE: "AUTHORISED",
}


class PostingError(RuntimeError):
    """Raised when a push cannot be attempted at all."""


@dataclass(frozen=True, slots=True)
class PushOutcome:
    reference: str
    kind: str
    number: str
    contact: str
    amount: Decimal
    currency: str
    action: str  # pushed | skipped | changed | failed | would_push
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "kind": self.kind,
            "number": self.number,
            "contact": self.contact,
            "amount": str(self.amount),
            "currency": self.currency,
            "action": self.action,
            "detail": self.detail,
        }


@dataclass
class PushSummary:
    outcomes: list[PushOutcome] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for o in self.outcomes if o.action == action)

    @property
    def value_pushed(self) -> Decimal:
        return sum((o.amount for o in self.outcomes if o.action == "pushed"), ZERO)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pushed": self.count("pushed"),
            "would_push": self.count("would_push"),
            "changed": self.count("changed"),
            "failed": self.count("failed"),
            "skipped": self.count("skipped"),
            "value_pushed": str(self.value_pushed),
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


def _live_index(provider: LedgerProvider, kinds: set[DocumentKind]) -> dict[str, Document]:
    """Fetch the current version of every document kind in play, by id."""
    index: dict[str, Document] = {}
    for kind in kinds:
        try:
            for document in provider.documents(kind):
                index[document.document_id] = document
        except ProviderError:
            # One unreadable kind must not block pushing the others; the
            # documents it would have covered simply fail their re-check below.
            continue
    return index


def push_approved(
    engine: Engine,
    provider: LedgerProvider,
    dry_run: bool = False,
    limit: int | None = None,
) -> PushSummary:
    """Post every approved, unpushed request whose document still matches."""
    if not provider.can_write and not dry_run:
        raise PostingError(
            "This connection is read-only. Run `apflow connect --allow-writes` to grant "
            "write scopes, or use --dry-run to see what would be sent."
        )

    pending = engine.store.list_requests(state=str(RequestState.APPROVED), pushed=False)
    if limit is not None:
        pending = pending[:limit]
    summary = PushSummary()
    if not pending:
        return summary

    kinds = {DocumentKind.parse(row["kind"]) for row in pending}
    live = _live_index(provider, kinds)

    for row in pending:
        request_id = int(row["id"])
        reference = str(row["reference"])
        stored = engine.store.document_for(request_id)
        if stored is None:
            summary.outcomes.append(
                PushOutcome(
                    reference, str(row["kind"]), str(row["number"]), str(row["contact_name"]),
                    to_money(row["amount"]), str(row["currency"]), "failed",
                    "the stored document could not be read back",
                )
            )
            continue

        current = live.get(stored.document_id)
        if current is None:
            summary.outcomes.append(
                PushOutcome(
                    reference, str(stored.kind), stored.number, stored.contact.name,
                    stored.amount_total, stored.currency_code, "skipped",
                    "document not found in the ledger — it may have been deleted",
                )
            )
            continue

        if current.fingerprint != stored.fingerprint:
            changes = "; ".join(current.describe_change(stored)) or "document edited"
            if dry_run:
                # A preview changes nothing, including the approvals it would
                # otherwise reset.
                summary.outcomes.append(
                    PushOutcome(
                        reference, str(stored.kind), stored.number, stored.contact.name,
                        stored.amount_total, stored.currency_code, "changed",
                        f"would not post — {changes}",
                    )
                )
                continue
            engine.store.add_event(
                request_id, "push_blocked", detail=f"changed before posting: {changes}"
            )
            # Feed the new version back through the engine: approvals reset and
            # the request reopens in front of the right people.
            engine.submit(current, requested_by=str(row["requested_by"] or "system"))
            summary.outcomes.append(
                PushOutcome(
                    reference, str(stored.kind), stored.number, stored.contact.name,
                    stored.amount_total, stored.currency_code, "changed",
                    f"not posted — {changes}; approvals reset",
                )
            )
            continue

        target = _TARGET_STATUS[stored.kind]
        if dry_run:
            summary.outcomes.append(
                PushOutcome(
                    reference, str(stored.kind), stored.number, stored.contact.name,
                    stored.amount_total, stored.currency_code, "would_push",
                    f"set status to {target}",
                )
            )
            continue

        audit_id = engine.store.begin_write(
            action=f"set_status:{stored.kind}",
            target=stored.document_id,
            payload={"status": target, "number": stored.number,
                     "fingerprint": stored.fingerprint},
            request_id=request_id,
        )
        try:
            provider.set_document_status(stored, target)
        except (ProviderError, ReadOnlyError) as exc:
            engine.store.finish_write(audit_id, "failed", str(exc))
            engine.store.add_event(request_id, "push_failed", detail=str(exc)[:500])
            summary.outcomes.append(
                PushOutcome(
                    reference, str(stored.kind), stored.number, stored.contact.name,
                    stored.amount_total, stored.currency_code, "failed", str(exc)[:200],
                )
            )
            continue

        engine.store.finish_write(audit_id, "ok", f"status set to {target}")
        engine.store.mark_pushed(request_id, f"{stored.kind}:{stored.document_id}")
        engine.store.add_event(
            request_id, "pushed", actor="apflow", detail=f"status set to {target} in the ledger"
        )
        summary.outcomes.append(
            PushOutcome(
                reference, str(stored.kind), stored.number, stored.contact.name,
                stored.amount_total, stored.currency_code, "pushed", f"status set to {target}",
            )
        )

    return summary
