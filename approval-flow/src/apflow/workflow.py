"""The approval engine.

Everything that can change the state of an approval passes through here, so
there is exactly one place where the controls live.

**State is derived, not asserted.** A step is approved because the decisions on
record say so, not because something once wrote "approved" into a column. The
materialised ``state`` columns exist for querying and are recomputed from the
decision log after every change. Any disagreement between them is therefore a
bug in one function, :meth:`Engine._recompute`, rather than a corrupted history.

**Consent is to a document, not to a reference.** Every decision records the
fingerprint it was made against, and only decisions matching the document's
current fingerprint count. Edit an approved bill and its approvals stop
counting the moment the change is pulled — automatically, with no reliance on
anyone noticing.

**Re-routing is honest about identity.** If a change is big enough to alter the
workflow itself (a bill that grows past the CFO threshold), the request is
superseded by a new one with the new routing rather than having its steps
rewritten underneath its own decision history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from .budget import BREACH, NO_BUDGET, Budget, BudgetCheck, commitments_from_store
from .config import BUDGET_BLOCK, BUDGET_OFF, Settings
from .matrix import ApprovalMatrix, MatrixError, Resolution
from .models import (
    Decision,
    Directory,
    Document,
    Quorum,
    RequestState,
    StepState,
    User,
)
from .money import ZERO, fmt, to_money
from .store import Store


class WorkflowError(RuntimeError):
    """A refusal: the action is not permitted, or does not apply."""


@dataclass(frozen=True, slots=True)
class SubmitResult:
    outcome: str  # created | existing | auto_approved | blocked | unchanged | superseded | rerouted
    request_id: int | None = None
    reference: str = ""
    message: str = ""
    budget: BudgetCheck | None = None
    resolution: Resolution | None = None
    changes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome != "blocked"

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "request_id": self.request_id,
            "reference": self.reference,
            "message": self.message,
            "changes": list(self.changes),
            "budget": self.budget.as_dict() if self.budget else None,
            "routing": self.resolution.as_dict() if self.resolution else None,
        }


@dataclass(frozen=True, slots=True)
class DecisionResult:
    request_id: int
    reference: str
    decision: Decision
    step_name: str
    request_state: RequestState
    step_closed: bool
    next_step: str = ""
    acting_for: str = ""
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "reference": self.reference,
            "decision": str(self.decision),
            "step": self.step_name,
            "step_closed": self.step_closed,
            "state": str(self.request_state),
            "next_step": self.next_step,
            "acting_for": self.acting_for,
            "message": self.message,
        }


@dataclass
class Engine:
    """Submits documents, records decisions, and keeps state consistent."""

    store: Store
    matrix: ApprovalMatrix
    directory: Directory
    settings: Settings = field(default_factory=Settings)
    budget: Budget = field(default_factory=Budget)
    notifier: Any | None = None

    # -- submission -------------------------------------------------------

    def submit(
        self,
        document: Document,
        requested_by: str = "system",
        as_of: date | None = None,
        _rerouted_from: str = "",
    ) -> SubmitResult:
        """Route a document into the workflow, or reconcile it with its request.

        Safe to call repeatedly on the same document, which is what makes
        `apflow pull` runnable on a timer. Three cases are handled here and all
        three matter:

        * An unchanged document with a live request is a no-op.
        * An unchanged document whose request was already decided is NOT
          re-raised. A bill stays DRAFT in the ledger between approval and
          posting, and a rejected bill stays DRAFT for good — re-routing either
          one on the next run would resurrect a rejected bill as pending and
          ask for a second approval of something already approved.
        * A document that has changed since it was decided goes back through
          the workflow, because the decision on record no longer describes it.
        """
        when = as_of or date.today()
        existing = self.store.open_request_for_document(document.document_id)
        if existing is not None:
            return self._reconcile(existing, document, when)

        prior = self.store.latest_request_for_document(document.document_id)
        prior_note = ""
        if prior is not None:
            state = str(prior["state"])
            if prior["fingerprint"] == document.fingerprint:
                return SubmitResult(
                    outcome=f"already_{state}",
                    request_id=int(prior["id"]),
                    reference=str(prior["reference"]),
                    message=f"already {state} as {prior['reference']}; not re-raised",
                )
            if state == str(RequestState.APPROVED) and not prior["pushed_at"]:
                # Approved but not yet posted, and now edited: the approvals on
                # record are for a document that no longer exists.
                return self._reconcile(prior, document, when)
            # A reroute already records the supersession on both requests;
            # adding a second note about it would say the same thing twice.
            if not _rerouted_from:
                prior_note = (
                    f"{prior['reference']} was {state}"
                    + (" and posted" if prior["pushed_at"] else "")
                    + "; this is a changed version of the same document"
                )

        resolution = self.matrix.resolve(document, self.directory, when)
        check = self._check_budget(document)

        if (
            self.settings.budget_enforcement == BUDGET_BLOCK
            and check is not None
            and check.status == BREACH
        ):
            self.store.add_event(
                None,
                "submission_blocked",
                actor=requested_by,
                detail=f"{document.kind} {document.number}: {check.message}",
                payload={"document_id": document.document_id, "budget": check.as_dict()},
            )
            return SubmitResult(
                outcome="blocked",
                message=f"over budget — {check.message}",
                budget=check,
                resolution=resolution,
            )

        request_id = self.store.create_request(
            document=document,
            rule_name=resolution.rule.name,
            requested_by=requested_by,
            steps=[
                {
                    "position": step.position,
                    "name": step.name,
                    "quorum": str(step.quorum),
                    "sla_days": step.sla_days,
                    "approvers": [
                        {"user_id": a.user_id, "delegated_from": a.delegated_from}
                        for a in step.approvers
                    ],
                }
                for step in resolution.steps
            ],
            budget_status=check.status if check else NO_BUDGET,
            budget_detail=check.as_dict() if check else {},
        )
        reference = f"AR-{request_id:04d}"

        self.store.add_event(
            request_id,
            "submitted",
            actor=requested_by,
            detail=f"routed by {resolution.rule.name}",
            payload=resolution.as_dict(),
        )
        if prior_note:
            self.store.add_event(
                request_id,
                "resubmitted",
                actor=requested_by,
                detail=prior_note,
                payload={"prior": prior["reference"] if prior else ""},
            )
        for skipped in resolution.skipped:
            self.store.add_event(
                request_id, "step_skipped", detail=f"{skipped.name}: {skipped.reason}"
            )
        if check is not None and check.status in (BREACH, "warn"):
            self.store.add_event(request_id, "budget_" + check.status, detail=check.message)

        if resolution.auto_approve:
            self.store.set_request_state(request_id, RequestState.APPROVED)
            self.store.add_event(
                request_id,
                "auto_approved",
                actor="policy",
                detail=f"rule {resolution.rule.name!r} auto-approves this band",
            )
            self._notify("auto_approved", request_id)
            return SubmitResult(
                outcome="auto_approved",
                request_id=request_id,
                reference=reference,
                message=f"auto-approved by rule {resolution.rule.name!r}",
                budget=check,
                resolution=resolution,
            )

        self._notify("opened", request_id)
        return SubmitResult(
            outcome="created",
            request_id=request_id,
            reference=reference,
            message=f"routed by {resolution.rule.name!r}",
            budget=check,
            resolution=resolution,
        )

    def _reconcile(
        self, existing: dict[str, Any], document: Document, when: date
    ) -> SubmitResult:
        """Handle a re-pull of a document whose request is still actionable.

        That means a pending request, or one that is approved but not yet
        posted — an approval that has not reached the ledger is still live, and
        editing the document behind it has to invalidate it.
        """
        request_id = int(existing["id"])
        reference = str(existing["reference"])
        if existing["fingerprint"] == document.fingerprint:
            return SubmitResult(outcome="unchanged", request_id=request_id, reference=reference)

        previous = self.store.document_for(request_id)
        changes = tuple(document.describe_change(previous)) if previous else ("document edited",)

        try:
            resolution = self.matrix.resolve(document, self.directory, when)
        except MatrixError as exc:
            # The edit pushed the document outside every rule. Refusing to
            # silently keep the old routing is the whole point.
            self.store.add_event(
                request_id, "reroute_failed", detail=str(exc), payload={"changes": list(changes)}
            )
            raise

        if self._routing_signature(resolution) == self._current_signature(request_id):
            # Same people, same order: keep the request and invalidate the
            # approvals collected against the superseded version.
            self.store.update_document(request_id, document)
            self.store.reopen_steps(request_id)
            check = self._check_budget(document, exclude_request_id=request_id)
            if check is not None:
                self.store.set_budget_result(request_id, check.status, check.as_dict())
            self.store.set_request_state(request_id, RequestState.PENDING, closed=False)
            self.store.add_event(
                request_id,
                "document_changed",
                detail="; ".join(changes) + " — approvals reset",
                payload={"changes": list(changes), "fingerprint": document.fingerprint},
            )
            self._recompute(request_id)
            self._notify("changed", request_id)
            return SubmitResult(
                outcome="superseded",
                request_id=request_id,
                reference=reference,
                message="document changed after approval started; approvals reset",
                budget=check,
                resolution=resolution,
                changes=changes,
            )

        # Routing itself changed. Close the old request and raise a new one, so
        # the old decision history keeps describing the document it was about.
        self.store.set_request_state(request_id, RequestState.CANCELLED)
        self.store.add_event(
            request_id,
            "superseded",
            detail="; ".join(changes) + " — re-routed under a new request",
            payload={"changes": list(changes)},
        )
        fresh = self.submit(
            document,
            requested_by=str(existing["requested_by"] or "system"),
            as_of=when,
            _rerouted_from=reference,
        )
        self.store.add_event(
            request_id, "superseded_by", detail=fresh.reference, payload={"request": fresh.reference}
        )
        if fresh.request_id:
            self.store.add_event(
                fresh.request_id, "supersedes", detail=reference, payload={"request": reference}
            )
        return SubmitResult(
            outcome="rerouted",
            request_id=fresh.request_id,
            reference=fresh.reference,
            message=f"{reference} superseded: {'; '.join(changes)}",
            budget=fresh.budget,
            resolution=fresh.resolution,
            changes=changes,
        )

    def _routing_signature(self, resolution: Resolution) -> str:
        return json.dumps(
            [
                [step.name, str(step.quorum), sorted(a.user_id for a in step.approvers)]
                for step in resolution.steps
            ],
            sort_keys=True,
        )

    def _current_signature(self, request_id: int) -> str:
        steps = [s for s in self.store.steps_for(request_id) if s["state"] != str(StepState.SKIPPED)]
        return json.dumps(
            [
                [s["name"], s["quorum"], sorted(a["user_id"] for a in s["approvers"])]
                for s in steps
            ],
            sort_keys=True,
        )

    def _check_budget(
        self, document: Document, exclude_request_id: int | None = None
    ) -> BudgetCheck | None:
        if self.settings.budget_enforcement == BUDGET_OFF or not self.budget:
            return None
        if not document.kind.is_spend:
            return None
        commitments = commitments_from_store(self.budget, self.store, exclude_request_id)
        return self.budget.check(document, commitments)

    # -- decisions --------------------------------------------------------

    def decide(
        self,
        request_id: int,
        user_id: str,
        decision: Decision | str,
        comment: str = "",
        as_of: date | None = None,
    ) -> DecisionResult:
        """Record one approve/reject, enforce the controls, advance the flow."""
        verdict = Decision(str(decision))
        request = self._require_request(request_id)
        reference = str(request["reference"])

        if request["state"] != str(RequestState.PENDING):
            raise WorkflowError(
                f"{reference} is already {request['state']} — it cannot be decided again"
            )

        user = self.directory.get(user_id)
        if user is None:
            raise WorkflowError(f"unknown user {user_id!r}; add them to the user directory")
        if not user.active:
            raise WorkflowError(f"{user.label} is marked inactive and cannot approve")

        step = self._open_step(request_id)
        if step is None:
            raise WorkflowError(f"{reference} has no open step")

        assignment = self._assignment_for(step, user.user_id)
        if assignment is None:
            later = self._later_assignment(request_id, step, user.user_id)
            if later:
                raise WorkflowError(
                    f"{reference} is at step {step['position']} ({step['name']}); "
                    f"{user.label} approves at step {later}. It is not their turn yet."
                )
            raise WorkflowError(f"{user.label} is not an approver on {reference}")

        amount = to_money(request["base_amount"])
        self._enforce_controls(request, step, user, verdict, amount, comment)

        already = [
            d
            for d in self.store.decisions_for(request_id, str(request["fingerprint"]))
            if int(d["step_id"]) == int(step["id"])
            and d["user_id"].lower() == user.user_id.lower()
        ]
        if already:
            raise WorkflowError(
                f"{user.label} already recorded '{already[-1]['decision']}' on this step "
                f"for this version of the document"
            )

        self.store.record_decision(
            request_id=request_id,
            step_id=int(step["id"]),
            user_id=user.user_id,
            decision=verdict,
            fingerprint=str(request["fingerprint"]),
            amount=amount,
            comment=comment.strip(),
            acting_for=str(assignment.get("delegated_from") or ""),
        )
        acting_for = str(assignment.get("delegated_from") or "")
        detail = f"{verdict} at step {step['position']} ({step['name']})"
        if acting_for:
            detail += f", acting for {acting_for}"
        if comment.strip():
            detail += f": {comment.strip()}"
        self.store.add_event(request_id, f"decision_{verdict}", actor=user.user_id, detail=detail)

        state, closed_step, next_step = self._recompute(request_id)
        self._notify("decided", request_id, step_id=int(step["id"]), actor=user.user_id)
        if state is not RequestState.PENDING:
            self._notify("closed", request_id)

        return DecisionResult(
            request_id=request_id,
            reference=reference,
            decision=verdict,
            step_name=str(step["name"]),
            request_state=state,
            step_closed=closed_step,
            next_step=next_step,
            acting_for=acting_for,
            message=self._decision_message(reference, state, closed_step, next_step, verdict),
        )

    def _decision_message(
        self,
        reference: str,
        state: RequestState,
        step_closed: bool,
        next_step: str,
        verdict: Decision,
    ) -> str:
        if state is RequestState.REJECTED:
            return f"{reference} rejected; nothing will be posted"
        if state is RequestState.APPROVED:
            return f"{reference} fully approved and ready to post"
        if verdict is Decision.APPROVE and step_closed and next_step:
            return f"step cleared; {reference} now sits with {next_step}"
        return f"recorded; {reference} still needs other approvers on this step"

    def _enforce_controls(
        self,
        request: dict[str, Any],
        step: dict[str, Any],
        user: User,
        verdict: Decision,
        amount: Decimal,
        comment: str,
    ) -> None:
        """The refusals. Each one is a control an auditor will ask to see."""
        reference = str(request["reference"])

        if verdict is Decision.REJECT and not comment.strip():
            raise WorkflowError(
                "a rejection needs a reason — the requester has to know what to fix"
            )

        if (
            self.settings.separation_of_duties
            and str(request["requested_by"]).lower() == user.user_id.lower()
        ):
            raise WorkflowError(
                f"{user.label} submitted {reference} and cannot also approve it "
                "(separation of duties)"
            )

        if self.settings.block_supplier_self_approval and user.email:
            supplier_email = str(request["contact_email"] or "").strip().lower()
            if supplier_email and supplier_email == user.email.strip().lower():
                raise WorkflowError(
                    f"{user.label} shares an email address with the counterparty on "
                    f"{reference} and cannot approve it"
                )

        if (
            verdict is Decision.APPROVE
            and self.settings.enforce_approval_limits
            and not user.within_limit(amount)
        ):
            limit = user.approval_limit or ZERO
            capable = [
                other.label
                for other in self.directory
                if other.active and other.within_limit(amount) and other.user_id != user.user_id
            ]
            hint = f" Who can: {', '.join(capable[:4])}." if capable else ""
            raise WorkflowError(
                f"{reference} is {fmt(amount, str(request['currency']))} but {user.label}'s "
                f"limit is {fmt(limit)}.{hint}"
            )

    def _assignment_for(self, step: dict[str, Any], user_id: str) -> dict[str, Any] | None:
        for approver in step["approvers"]:
            if str(approver["user_id"]).lower() == user_id.lower():
                return dict(approver)
        return None

    def _later_assignment(
        self, request_id: int, current: dict[str, Any], user_id: str
    ) -> int | None:
        for step in self.store.steps_for(request_id):
            if int(step["position"]) <= int(current["position"]):
                continue
            if self._assignment_for(step, user_id):
                return int(step["position"])
        return None

    def _open_step(self, request_id: int) -> dict[str, Any] | None:
        for step in self.store.steps_for(request_id):
            if step["state"] == str(StepState.PENDING):
                return step
        return None

    # -- state derivation -------------------------------------------------

    def _recompute(self, request_id: int) -> tuple[RequestState, bool, str]:
        """Rebuild step and request state from the decision log.

        Returns ``(request_state, a_step_just_closed, next_step_owner)``.
        """
        request = self._require_request(request_id)
        fingerprint = str(request["fingerprint"])
        decisions = self.store.decisions_for(request_id, fingerprint)
        by_step: dict[int, list[dict[str, Any]]] = {}
        for entry in decisions:
            by_step.setdefault(int(entry["step_id"]), []).append(entry)

        steps = self.store.steps_for(request_id)
        closed_any = False
        request_state = RequestState.APPROVED  # downgraded below if any step is open
        next_owner = ""

        for step in steps:
            step_id = int(step["id"])
            was = step["state"]
            if was == str(StepState.SKIPPED):
                continue
            verdicts = by_step.get(step_id, [])
            rejected = [d for d in verdicts if d["decision"] == str(Decision.REJECT)]
            if rejected:
                self.store.set_step_state(
                    step_id, StepState.REJECTED, note=str(rejected[-1]["comment"])[:500]
                )
                closed_any = closed_any or was != str(StepState.REJECTED)
                self._cancel_remaining(steps, after=int(step["position"]))
                self.store.set_request_state(request_id, RequestState.REJECTED)
                return RequestState.REJECTED, True, ""

            approved_by = {
                d["user_id"].lower() for d in verdicts if d["decision"] == str(Decision.APPROVE)
            }
            required = {a["user_id"].lower() for a in step["approvers"]}
            quorum = Quorum.parse(step["quorum"])
            satisfied = (
                bool(approved_by & required)
                if quorum is Quorum.ANY
                else required.issubset(approved_by)
            )

            if satisfied:
                if was != str(StepState.APPROVED):
                    self.store.set_step_state(step_id, StepState.APPROVED)
                    closed_any = True
                continue

            # First unsatisfied step: this is where the request now sits.
            if was != str(StepState.PENDING):
                self.store.set_step_state(step_id, StepState.PENDING)
            else:
                self.store.set_step_state(step_id, StepState.PENDING, opened=True)
            outstanding = sorted(required - approved_by)
            next_owner = ", ".join(self.directory.label_for(u) for u in outstanding)
            request_state = RequestState.PENDING
            self.store.set_request_state(request_id, RequestState.PENDING, closed=False)
            return request_state, closed_any, next_owner

        self.store.set_request_state(request_id, RequestState.APPROVED)
        self.store.add_event(request_id, "approved", detail="all steps cleared")
        return RequestState.APPROVED, closed_any, ""

    def _cancel_remaining(self, steps: list[dict[str, Any]], after: int) -> None:
        for step in steps:
            if int(step["position"]) > after and step["state"] == str(StepState.PENDING):
                self.store.set_step_state(
                    int(step["id"]), StepState.CANCELLED, note="earlier step rejected"
                )

    # -- other transitions ------------------------------------------------

    def cancel(self, request_id: int, actor: str, reason: str = "") -> None:
        """Withdraw a request.

        Open to the person who raised it and to anyone holding unlimited
        approval authority. An approver cannot cancel a request out from under
        a decision they do not want to make on the record — that would be a
        way to make an inconvenient request disappear without rejecting it.
        """
        request = self._require_request(request_id)
        if request["state"] != str(RequestState.PENDING):
            raise WorkflowError(f"{request['reference']} is already {request['state']}")
        user = self.directory.get(actor)
        is_requester = str(request["requested_by"]).lower() == str(actor).lower()
        unlimited = user is not None and user.active and user.approval_limit is None
        if not (is_requester or unlimited):
            raise WorkflowError(
                f"{actor} cannot cancel {request['reference']}: only the requester "
                "or an unlimited-authority approver may withdraw a request"
            )
        self.store.set_request_state(request_id, RequestState.CANCELLED)
        for step in self.store.steps_for(request_id):
            if step["state"] == str(StepState.PENDING):
                self.store.set_step_state(int(step["id"]), StepState.CANCELLED, note="withdrawn")
        self.store.add_event(
            request_id, "cancelled", actor=str(actor), detail=reason or "withdrawn"
        )
        self._notify("closed", request_id)

    # -- views ------------------------------------------------------------

    def request_view(self, request_id: int) -> dict[str, Any]:
        """Everything about one request, for the CLI, the UI, and the record."""
        request = self._require_request(request_id)
        fingerprint = str(request["fingerprint"])
        steps = self.store.steps_for(request_id)
        decisions = self.store.decisions_for(request_id)
        current = {d["id"] for d in self.store.decisions_for(request_id, fingerprint)}

        step_views: list[dict[str, Any]] = []
        for step in steps:
            step_decisions = [d for d in decisions if int(d["step_id"]) == int(step["id"])]
            step_views.append(
                {
                    "position": int(step["position"]),
                    "name": step["name"],
                    "state": step["state"],
                    "quorum": step["quorum"],
                    "sla_days": int(step["sla_days"]),
                    "opened_at": step["opened_at"],
                    "closed_at": step["closed_at"],
                    "note": step["note"],
                    "approvers": [
                        {
                            "user_id": a["user_id"],
                            "label": self.directory.label_for(a["user_id"]),
                            "delegated_from": a["delegated_from"],
                        }
                        for a in step["approvers"]
                    ],
                    "decisions": [
                        {
                            "user_id": d["user_id"],
                            "decision": d["decision"],
                            "comment": d["comment"],
                            "acting_for": d["acting_for"],
                            "created_at": d["created_at"],
                            # A decision made against a superseded version of
                            # the document is shown, never hidden — but it is
                            # labelled, because it no longer counts.
                            "current": int(d["id"]) in current,
                        }
                        for d in step_decisions
                    ],
                }
            )

        try:
            budget_detail = json.loads(request["budget_detail"] or "{}")
        except json.JSONDecodeError:
            budget_detail = {}

        document = self.store.document_for(request_id)
        return {
            "id": request_id,
            "reference": request["reference"],
            "state": request["state"],
            "kind": request["kind"],
            "number": request["number"],
            "document_id": request["document_id"],
            "contact": request["contact_name"],
            "contact_email": request["contact_email"],
            "currency": request["currency"],
            "amount": request["amount"],
            "base_amount": request["base_amount"],
            "document_date": request["document_date"],
            "due_date": request["due_date"],
            "rule": request["rule_name"],
            "requested_by": request["requested_by"],
            "fingerprint": fingerprint,
            "budget_status": request["budget_status"],
            "budget": budget_detail,
            "created_at": request["created_at"],
            "closed_at": request["closed_at"],
            "pushed_at": request["pushed_at"],
            "push_ref": request["push_ref"],
            "steps": step_views,
            "events": self.store.events_for(request_id),
            "document": document.as_dict() if document else {},
        }

    def _require_request(self, request_id: int) -> dict[str, Any]:
        request = self.store.request_row(request_id)
        if request is None:
            raise WorkflowError(f"no approval request with id {request_id}")
        return request

    # -- notifications ----------------------------------------------------

    def _notify(self, kind: str, request_id: int, **kwargs: Any) -> None:
        if self.notifier is None:
            return
        handler = getattr(self.notifier, f"on_{kind}", None)
        if handler is None:
            return
        # A mail failure must never roll back a recorded approval. The decision
        # is the durable thing; the email is a convenience on top of it.
        try:
            handler(request_id, **kwargs)
        except Exception as exc:
            self.store.add_event(request_id, "notification_failed", detail=str(exc)[:500])
