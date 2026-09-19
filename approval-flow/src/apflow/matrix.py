"""The approval matrix.

A matrix is an ordered list of rules. The first rule whose condition matches a
document selects the workflow for it; the workflow is a list of steps, run in
order, each with its own approvers and its own optional condition.

Two levels of condition, because they answer different questions:

* **Rule conditions** pick the workflow — "capital expenditure goes through the
  capex chain, everything else through the standard one".
* **Step conditions** tier it — "the CFO step applies only above $50,000".

Collapsing those into one level is the common design mistake. It forces a
separate rule per amount band, and then every change to the base workflow has
to be made in four places, which is how approval matrices drift out of line
with the delegation of authority they are supposed to encode.

First match wins, and matching stops there. A document that matches no rule is
an error, not an implicit approval: :meth:`ApprovalMatrix.resolve` raises, and
:meth:`ApprovalMatrix.validate` warns at configuration time if no catch-all
rule exists.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import decimal_or_none
from .models import Directory, Document, DocumentKind, Quorum, User

MATRIX_VERSION = 1


class MatrixError(ValueError):
    """Raised when a matrix is malformed or cannot route a document."""


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _matches_any(candidates: tuple[str, ...] | list[str], patterns: list[str]) -> bool:
    """Case-insensitive glob match of any candidate against any pattern."""
    lowered = [c.lower() for c in candidates]
    for pattern in patterns:
        needle = pattern.lower()
        for candidate in lowered:
            if fnmatch.fnmatchcase(candidate, needle):
                return True
    return False


@dataclass(frozen=True, slots=True)
class Condition:
    """When a rule or a step applies.

    Every populated field must match (AND); within a field, any entry matching
    is enough (OR). An empty condition matches everything, which is what makes
    the catch-all rule expressible.

    Amount comparisons use the document's BASE-currency total and are
    ``min <= amount < max``. The half-open interval is deliberate: adjacent
    bands written as 0-10,000 and 10,000-50,000 must not both claim a $10,000
    bill, and a closed upper bound would silently give it to whichever rule was
    listed first.
    """

    kinds: list[str] = field(default_factory=list)
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    suppliers: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    tracking: list[str] = field(default_factory=list)
    currencies: list[str] = field(default_factory=list)
    requires_purchase_order: bool | None = None
    """Whether the document carries a purchase-order reference.

    This is the two-way-match control: bills that cite no PO can be routed
    through extra scrutiny. A purchase order itself carries its own number, so
    it satisfies ``True``.
    """

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.kinds,
                self.amount_min is not None,
                self.amount_max is not None,
                self.suppliers,
                self.accounts,
                self.tracking,
                self.currencies,
                self.requires_purchase_order is not None,
            )
        )

    def matches(self, document: Document) -> bool:
        if self.kinds and str(document.kind) not in {k.lower() for k in self.kinds}:
            return False
        amount = abs(document.base_amount)
        if self.amount_min is not None and amount < self.amount_min:
            return False
        if self.amount_max is not None and amount >= self.amount_max:
            return False
        if self.suppliers and not _matches_any((document.contact.name,), self.suppliers):
            return False
        if self.accounts and not _matches_any(document.account_codes, self.accounts):
            return False
        if self.tracking and not _matches_any(document.tracking_pairs, self.tracking):
            return False
        if self.currencies and document.currency_code.upper() not in {
            c.upper() for c in self.currencies
        }:
            return False
        if self.requires_purchase_order is not None:
            has_po = bool(document.purchase_order_number.strip())
            if has_po is not self.requires_purchase_order:
                return False
        return True

    def describe(self) -> str:
        if self.is_empty:
            return "any document"
        parts: list[str] = []
        if self.kinds:
            parts.append("/".join(self.kinds))
        if self.amount_min is not None and self.amount_max is not None:
            parts.append(f"{self.amount_min:,.0f}-{self.amount_max:,.0f}")
        elif self.amount_min is not None:
            parts.append(f"≥ {self.amount_min:,.0f}")
        elif self.amount_max is not None:
            parts.append(f"< {self.amount_max:,.0f}")
        if self.suppliers:
            parts.append("supplier " + ", ".join(self.suppliers))
        if self.accounts:
            parts.append("account " + ", ".join(self.accounts))
        if self.tracking:
            parts.append("tracking " + ", ".join(self.tracking))
        if self.currencies:
            parts.append("in " + ", ".join(self.currencies))
        if self.requires_purchase_order is True:
            parts.append("with a PO")
        elif self.requires_purchase_order is False:
            parts.append("without a PO")
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.kinds:
            out["kinds"] = list(self.kinds)
        if self.amount_min is not None:
            out["amount_min"] = str(self.amount_min)
        if self.amount_max is not None:
            out["amount_max"] = str(self.amount_max)
        if self.suppliers:
            out["suppliers"] = list(self.suppliers)
        if self.accounts:
            out["accounts"] = list(self.accounts)
        if self.tracking:
            out["tracking"] = list(self.tracking)
        if self.currencies:
            out["currencies"] = list(self.currencies)
        if self.requires_purchase_order is not None:
            out["requires_purchase_order"] = self.requires_purchase_order
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Condition:
        data = data or {}
        po = data.get("requires_purchase_order")
        return cls(
            kinds=[str(DocumentKind.parse(k)) for k in _as_list(data.get("kinds") or data.get("kind"))],
            amount_min=decimal_or_none(data.get("amount_min")),
            amount_max=decimal_or_none(data.get("amount_max")),
            suppliers=_as_list(data.get("suppliers") or data.get("supplier")),
            accounts=_as_list(data.get("accounts") or data.get("account")),
            tracking=_as_list(data.get("tracking")),
            currencies=_as_list(data.get("currencies") or data.get("currency")),
            requires_purchase_order=None if po is None else bool(po),
        )


@dataclass(frozen=True, slots=True)
class StepSpec:
    """One stage of a workflow."""

    name: str
    approvers: list[str]
    quorum: Quorum = Quorum.ANY
    condition: Condition = field(default_factory=Condition)
    sla_days: int = 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "approvers": list(self.approvers)}
        if self.quorum is not Quorum.ANY:
            out["quorum"] = str(self.quorum)
        if not self.condition.is_empty:
            out["when"] = self.condition.as_dict()
        if self.sla_days:
            out["sla_days"] = self.sla_days
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepSpec:
        approvers = _as_list(data.get("approvers") or data.get("approver"))
        name = str(data.get("name") or "").strip()
        if not name:
            raise MatrixError("every step needs a name")
        if not approvers:
            raise MatrixError(f"step {name!r} has no approvers")
        return cls(
            name=name,
            approvers=approvers,
            quorum=Quorum.parse(data.get("quorum")),
            condition=Condition.from_dict(data.get("when")),
            sla_days=int(data.get("sla_days") or 0),
        )


@dataclass(frozen=True, slots=True)
class Rule:
    """A named workflow and the documents it applies to."""

    name: str
    condition: Condition = field(default_factory=Condition)
    steps: tuple[StepSpec, ...] = ()
    auto_approve: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if not self.condition.is_empty:
            out["when"] = self.condition.as_dict()
        if self.auto_approve:
            out["auto_approve"] = True
        if self.note:
            out["note"] = self.note
        out["steps"] = [s.as_dict() for s in self.steps]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        name = str(data.get("name") or "").strip()
        if not name:
            raise MatrixError("every rule needs a name")
        auto = bool(data.get("auto_approve"))
        steps = tuple(StepSpec.from_dict(s) for s in (data.get("steps") or []))
        if not steps and not auto:
            raise MatrixError(
                f"rule {name!r} has no steps and is not auto_approve — it would "
                "leave matching documents with nobody to approve them"
            )
        return cls(
            name=name,
            condition=Condition.from_dict(data.get("when")),
            steps=steps,
            auto_approve=auto,
            note=str(data.get("note") or ""),
        )


@dataclass(frozen=True, slots=True)
class ResolvedApprover:
    user_id: str
    delegated_from: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedStep:
    position: int
    name: str
    quorum: Quorum
    approvers: tuple[ResolvedApprover, ...]
    sla_days: int = 0


@dataclass(frozen=True, slots=True)
class SkippedStep:
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class Resolution:
    """What the matrix decided about one document."""

    rule: Rule
    steps: tuple[ResolvedStep, ...]
    skipped: tuple[SkippedStep, ...] = ()
    auto_approve: bool = False

    @property
    def needs_human(self) -> bool:
        return bool(self.steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.name,
            "auto_approve": self.auto_approve,
            "steps": [
                {
                    "position": s.position,
                    "name": s.name,
                    "quorum": str(s.quorum),
                    "sla_days": s.sla_days,
                    "approvers": [
                        {"user_id": a.user_id, "delegated_from": a.delegated_from} for a in s.approvers
                    ],
                }
                for s in self.steps
            ],
            "skipped": [{"name": s.name, "reason": s.reason} for s in self.skipped],
        }


class ApprovalMatrix:
    """An ordered rule set, loaded from JSON."""

    def __init__(self, rules: list[Rule], version: int = MATRIX_VERSION) -> None:
        self.rules = rules
        self.version = version

    # -- routing ----------------------------------------------------------

    def rule_for(self, document: Document) -> Rule | None:
        for rule in self.rules:
            if rule.condition.matches(document):
                return rule
        return None

    def resolve(
        self, document: Document, directory: Directory, as_of: date | None = None
    ) -> Resolution:
        """Pick the workflow for ``document`` and expand its approvers.

        Delegation is resolved here, at submission time, rather than when a
        decision is made. That means an approver who goes on leave *after* a
        request is raised still owns it — which is the correct behaviour: the
        substitution is a property of the routing decision, and re-resolving it
        later would silently move work between people mid-flight.
        """
        when = as_of or date.today()
        rule = self.rule_for(document)
        if rule is None:
            raise MatrixError(
                f"no rule matches {document.kind} {document.number or document.document_id} "
                f"({document.currency_code} {document.amount_total}). Add a catch-all rule "
                "with no `when` clause so nothing can fall through unrouted."
            )

        steps: list[ResolvedStep] = []
        skipped: list[SkippedStep] = []
        position = 1
        for spec in rule.steps:
            if not spec.condition.matches(document):
                skipped.append(SkippedStep(spec.name, f"condition not met: {spec.condition.describe()}"))
                continue
            approvers: list[ResolvedApprover] = []
            for approver_id in spec.approvers:
                try:
                    effective, delegated_from = directory.resolve_approver(approver_id, when)
                except KeyError as exc:
                    raise MatrixError(
                        f"step {spec.name!r} of rule {rule.name!r} names {approver_id!r}, "
                        f"who is not in the user directory"
                    ) from exc
                if not effective.active:
                    continue
                if any(a.user_id == effective.user_id for a in approvers):
                    # Two nominated approvers delegating to the same person must
                    # not create two votes for one human on an "all" step.
                    continue
                approvers.append(ResolvedApprover(effective.user_id, delegated_from))
            if not approvers:
                skipped.append(SkippedStep(spec.name, "no active approver available"))
                continue
            steps.append(
                ResolvedStep(
                    position=position,
                    name=spec.name,
                    quorum=spec.quorum,
                    approvers=tuple(approvers),
                    sla_days=spec.sla_days,
                )
            )
            position += 1

        if rule.auto_approve and not steps:
            return Resolution(rule=rule, steps=(), skipped=tuple(skipped), auto_approve=True)
        if not steps:
            raise MatrixError(
                f"rule {rule.name!r} matched but produced no live step for "
                f"{document.number or document.document_id}: "
                + ("; ".join(f"{s.name} ({s.reason})" for s in skipped) or "no steps defined")
            )
        return Resolution(rule=rule, steps=tuple(steps), skipped=tuple(skipped), auto_approve=False)

    # -- configuration checks ---------------------------------------------

    def validate(self, directory: Directory) -> list[str]:
        """Static problems worth knowing about before a document hits them.

        These are the failure modes that otherwise surface as a stuck queue on
        a Friday afternoon: an approver who left, a band nobody can clear, a
        rule shadowed by a broader one above it, no catch-all at the bottom.
        """
        problems: list[str] = []
        if not self.rules:
            problems.append("the matrix has no rules")

        for index, rule in enumerate(self.rules):
            if rule.condition.is_empty and index < len(self.rules) - 1:
                shadowed = [r.name for r in self.rules[index + 1:]]
                problems.append(
                    f"rule {rule.name!r} matches everything but is not last — "
                    f"it shadows: {', '.join(shadowed)}"
                )
            lower = rule.condition.amount_min
            upper = rule.condition.amount_max
            if lower is not None and upper is not None and lower >= upper:
                problems.append(
                    f"rule {rule.name!r} has an empty amount band "
                    f"({lower} to {upper}) and can never match"
                )
            for step in rule.steps:
                for approver_id in step.approvers:
                    user = directory.get(approver_id)
                    if user is None:
                        problems.append(
                            f"rule {rule.name!r} step {step.name!r} names unknown user {approver_id!r}"
                        )
                        continue
                    if not user.active:
                        problems.append(
                            f"rule {rule.name!r} step {step.name!r} names {user.label}, "
                            "who is marked inactive"
                        )
                problems.extend(self._limit_problems(rule, step, directory))

        if self.rules and not self.rules[-1].condition.is_empty:
            problems.append(
                "no catch-all rule: a document matching nothing will be rejected at "
                "submission rather than routed. Add a final rule with no `when` clause."
            )
        return problems

    def _limit_problems(self, rule: Rule, step: StepSpec, directory: Directory) -> list[str]:
        """Flag a step whose approvers cannot clear the amounts it will see.

        The ceiling a step can see is the tighter of the rule's and the step's
        own upper bound. With no upper bound anywhere, an unlimited approver is
        required — a step of $50k-limited approvers on an unbounded rule stalls
        the first time a $60k bill arrives.
        """
        ceiling = None
        for bound in (rule.condition.amount_max, step.condition.amount_max):
            if bound is not None:
                ceiling = bound if ceiling is None else min(ceiling, bound)

        problems: list[str] = []
        usable: list[User] = []
        for approver_id in step.approvers:
            user = directory.get(approver_id)
            if user is None or not user.active:
                continue
            if user.approval_limit is None:
                return []  # someone here can clear anything
            # With no ceiling anywhere, the step will eventually see a document
            # larger than any finite limit, so a limited approver does not
            # cover it. Treating "no ceiling" as "any limit will do" is how a
            # workflow ends up stalling on its first large bill.
            if ceiling is not None and user.approval_limit >= ceiling:
                usable.append(user)
        if not usable and step.approvers:
            band = f"up to {ceiling:,.0f}" if ceiling is not None else "of any size"
            problems.append(
                f"rule {rule.name!r} step {step.name!r} routes documents {band}, "
                "but no approver on it has a high enough limit"
            )
        return problems

    # -- persistence ------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "rules": [r.as_dict() for r in self.rules]}

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.as_dict(), indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> ApprovalMatrix:
        target = Path(path)
        if not target.exists():
            raise MatrixError(
                f"no approval matrix at {target}. Run `apflow matrix --init` to write a "
                "starter matrix you can edit."
            )
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MatrixError(f"{target} is not valid JSON: {exc}") from exc
        rules_raw = raw.get("rules") if isinstance(raw, dict) else raw
        if not isinstance(rules_raw, list):
            raise MatrixError(f"{target} must contain a list of rules")
        return cls(
            rules=[Rule.from_dict(r) for r in rules_raw],
            version=int(raw.get("version") or MATRIX_VERSION) if isinstance(raw, dict) else MATRIX_VERSION,
        )

    @classmethod
    def starter(cls) -> ApprovalMatrix:
        """A plausible small-company matrix, meant to be edited.

        The shape is the point: one auto-approval band for noise, one tiered
        spend workflow whose steps switch on amount, a receivables workflow,
        and a catch-all so nothing can fall through.
        """
        return cls(
            rules=[
                Rule(
                    name="Low-value bills — auto",
                    condition=Condition(kinds=["bill"], amount_max=Decimal("250.00")),
                    auto_approve=True,
                    note="Under the cost of reviewing it. Still fully logged.",
                ),
                Rule(
                    name="Supplier bills",
                    condition=Condition(kinds=["bill", "purchase_order"]),
                    steps=(
                        StepSpec(name="Budget holder", approvers=["manager"], sla_days=2),
                        StepSpec(
                            name="Finance",
                            approvers=["finance"],
                            condition=Condition(amount_min=Decimal("2500.00")),
                            sla_days=2,
                        ),
                        StepSpec(
                            name="CFO",
                            approvers=["cfo"],
                            condition=Condition(amount_min=Decimal("25000.00")),
                            sla_days=3,
                        ),
                    ),
                ),
                Rule(
                    name="Customer invoices",
                    condition=Condition(kinds=["invoice"]),
                    steps=(StepSpec(name="Finance review", approvers=["finance"], sla_days=1),),
                ),
                Rule(
                    name="Everything else",
                    steps=(StepSpec(name="Finance review", approvers=["finance"], sla_days=2),),
                    note="Catch-all. Nothing reaches the ledger unrouted.",
                ),
            ]
        )
