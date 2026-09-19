"""Reporting.

Three questions a finance lead actually asks of an approval system, and one an
auditor does:

* What is sitting in the queue, and how much is it worth?
* Who is holding it up?
* How long does this process actually take, per person?
* Show me everything that happened to this document.

The last one lives in :meth:`apflow.workflow.Engine.request_view`; the rest are
here. Everything is computed from the decision log, so the numbers cannot drift
away from the evidence behind them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .models import Directory, Document, RequestState, StepState
from .money import ZERO, quantize, to_money
from .store import Store, parse_ts


def _age_hours(value: Any, now: datetime | None = None) -> float:
    started = parse_ts(value)
    if started is None:
        return 0.0
    return max(0.0, ((now or datetime.now(UTC)) - started).total_seconds() / 3600)


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


@dataclass
class QueueEntry:
    reference: str
    kind: str
    number: str
    contact: str
    amount: Decimal
    """Base-currency value. Limits, budgets and totals are all denominated here."""
    face_amount: Decimal
    """The amount printed on the document, in its own currency."""
    currency: str
    step_position: int
    step_name: str
    waiting_on: list[str]
    age_hours: float
    sla_days: int
    budget_status: str = "not_checked"

    @property
    def is_foreign(self) -> bool:
        return self.face_amount != self.amount

    @property
    def age_days(self) -> float:
        return self.age_hours / 24

    @property
    def breached(self) -> bool:
        """Past its SLA. A step with no SLA cannot breach one."""
        return bool(self.sla_days) and self.age_days > self.sla_days

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "kind": self.kind,
            "number": self.number,
            "contact": self.contact,
            "amount": str(self.amount),
            "face_amount": str(self.face_amount),
            "currency": self.currency,
            "foreign": self.is_foreign,
            "step": {"position": self.step_position, "name": self.step_name},
            "waiting_on": list(self.waiting_on),
            "age_days": round(self.age_days, 2),
            "sla_days": self.sla_days,
            "breached": self.breached,
            "budget_status": self.budget_status,
        }


@dataclass
class Queue:
    entries: list[QueueEntry] = field(default_factory=list)
    unrouted: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((e.amount for e in self.entries), ZERO)

    @property
    def breached(self) -> list[QueueEntry]:
        return [e for e in self.entries if e.breached]

    def by_approver(self) -> dict[str, dict[str, Any]]:
        """Who is holding what, ranked by value.

        A request on an "any of" step appears against every approver who could
        clear it. That is correct for "what is on my desk" and wrong for
        summing across people — so the totals below are per-approver workload,
        not a partition of the queue.
        """
        out: dict[str, dict[str, Any]] = {}
        for entry in self.entries:
            for approver in entry.waiting_on:
                bucket = out.setdefault(
                    approver, {"count": 0, "total": ZERO, "oldest_days": 0.0, "breached": 0}
                )
                bucket["count"] += 1
                bucket["total"] += entry.amount
                bucket["oldest_days"] = max(bucket["oldest_days"], round(entry.age_days, 2))
                bucket["breached"] += int(entry.breached)
        return dict(sorted(out.items(), key=lambda kv: kv[1]["total"], reverse=True))

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.entries),
            "total": str(self.total),
            "breached": len(self.breached),
            "entries": [e.as_dict() for e in self.entries],
            "by_approver": {
                k: {**v, "total": str(v["total"])} for k, v in self.by_approver().items()
            },
        }


def build_queue(store: Store, directory: Directory, now: datetime | None = None) -> Queue:
    """Every request currently waiting on somebody."""
    queue = Queue()
    for request in sorted(store.list_requests(state=str(RequestState.PENDING)), key=lambda r: r["id"]):
        request_id = int(request["id"])
        for step in store.steps_for(request_id):
            if step["state"] != str(StepState.PENDING):
                continue
            decided = {
                d["user_id"].lower()
                for d in store.decisions_for(request_id, str(request["fingerprint"]))
                if int(d["step_id"]) == int(step["id"])
            }
            waiting = [
                directory.label_for(a["user_id"])
                for a in step["approvers"]
                if a["user_id"].lower() not in decided
            ]
            queue.entries.append(
                QueueEntry(
                    reference=str(request["reference"]),
                    kind=str(request["kind"]),
                    number=str(request["number"]),
                    contact=str(request["contact_name"]),
                    amount=to_money(request["base_amount"]),
                    face_amount=to_money(request["amount"]),
                    currency=str(request["currency"]),
                    step_position=int(step["position"]),
                    step_name=str(step["name"]),
                    waiting_on=waiting,
                    age_hours=_age_hours(step["opened_at"] or request["created_at"], now),
                    sla_days=int(step["sla_days"]),
                    budget_status=str(request["budget_status"]),
                )
            )
            break  # only the step that is actually open
    return queue


@dataclass
class ApproverStats:
    user_id: str
    label: str
    decisions: int = 0
    approvals: int = 0
    rejections: int = 0
    hours: list[float] = field(default_factory=list)
    value: Decimal = ZERO

    @property
    def median_hours(self) -> float:
        return round(median(self.hours), 1)

    @property
    def slowest_hours(self) -> float:
        return round(max(self.hours), 1) if self.hours else 0.0

    @property
    def rejection_rate(self) -> Decimal:
        if not self.decisions:
            return ZERO
        return quantize(Decimal(self.rejections) / Decimal(self.decisions) * Decimal("100"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "label": self.label,
            "decisions": self.decisions,
            "approvals": self.approvals,
            "rejections": self.rejections,
            "median_hours": self.median_hours,
            "slowest_hours": self.slowest_hours,
            "rejection_rate": str(self.rejection_rate),
            "value": str(self.value),
        }


def turnaround(store: Store, directory: Directory) -> dict[str, Any]:
    """How long decisions take, overall and per approver.

    Measured from when the step opened to when the decision landed, not from
    when the request was raised: holding step 3 responsible for step 1's delay
    tells you nothing about where to intervene.
    """
    stats: dict[str, ApproverStats] = {}
    all_hours: list[float] = []

    for entry in store.all_decisions():
        user_id = str(entry["user_id"])
        bucket = stats.setdefault(
            user_id.lower(),
            ApproverStats(user_id=user_id, label=directory.label_for(user_id)),
        )
        bucket.decisions += 1
        if entry["decision"] == "approve":
            bucket.approvals += 1
            bucket.value += to_money(entry["amount"])
        else:
            bucket.rejections += 1
        opened = parse_ts(entry["opened_at"])
        decided = parse_ts(entry["created_at"])
        if opened and decided and decided >= opened:
            hours = (decided - opened).total_seconds() / 3600
            bucket.hours.append(hours)
            all_hours.append(hours)

    ranked = sorted(stats.values(), key=lambda s: s.median_hours, reverse=True)
    return {
        "decisions": sum(s.decisions for s in ranked),
        "median_hours": round(median(all_hours), 1),
        "approvers": [s.as_dict() for s in ranked],
    }


def spend_under_approval(store: Store) -> dict[str, Any]:
    """Value in flight, broken down by counterparty and by account code.

    Only spend documents count. A sales invoice awaiting sign-off is a control
    on what goes out the door, not money the organisation has committed.
    """
    by_contact: dict[str, Decimal] = {}
    by_account: dict[str, Decimal] = {}
    total = ZERO

    for row in store.list_requests(state=str(RequestState.PENDING)):
        try:
            document = Document.from_dict(json.loads(row["document"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not document.kind.is_spend:
            continue
        amount = to_money(row["base_amount"])
        total += amount
        by_contact[document.contact.name] = by_contact.get(document.contact.name, ZERO) + amount
        rate = document.currency_rate if document.currency_rate > 0 else Decimal("1")
        for line in document.line_items:
            code = line.account_code or "(uncoded)"
            by_account[code] = by_account.get(code, ZERO) + quantize(abs(line.line_amount) * rate)

    return {
        "total": str(total),
        "by_contact": {
            k: str(v) for k, v in sorted(by_contact.items(), key=lambda kv: kv[1], reverse=True)
        },
        "by_account": {
            k: str(v) for k, v in sorted(by_account.items(), key=lambda kv: kv[1], reverse=True)
        },
    }


def overview(
    store: Store, directory: Directory, now: datetime | None = None
) -> dict[str, Any]:
    """The headline numbers, for `apflow status` and the dashboard."""
    queue = build_queue(store, directory, now)
    totals = store.totals_by_state()
    approved_unpushed = store.list_requests(state=str(RequestState.APPROVED), pushed=False)
    changed = [
        e
        for e in store.recent_events(200)
        if e["kind"] in ("document_changed", "superseded", "push_blocked")
    ]
    return {
        "pending": {
            "count": len(queue.entries),
            "total": str(queue.total),
            "breached": len(queue.breached),
            "oldest_days": round(max((e.age_days for e in queue.entries), default=0.0), 1),
        },
        "ready_to_post": {
            "count": len(approved_unpushed),
            "total": str(sum((to_money(r["base_amount"]) for r in approved_unpushed), ZERO)),
        },
        "by_state": {k: {"count": v["count"], "total": str(v["total"])} for k, v in totals.items()},
        "by_approver": {
            k: {**v, "total": str(v["total"])} for k, v in queue.by_approver().items()
        },
        "budget_flags": [
            e.reference for e in queue.entries if e.budget_status in ("warn", "breach")
        ],
        "recent_changes": [
            {"reference": e.get("reference"), "kind": e["kind"], "detail": e["detail"],
             "at": e["created_at"]}
            for e in changed[:10]
        ],
    }
