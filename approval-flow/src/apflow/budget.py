"""Budget checking.

A budget here is a set of lines, each keyed by ``(period, account, tracking)``
and carrying an amount for that period. A document is checked by allocating
each of its own lines to the matching budget key and asking whether the
remaining balance covers it.

The part that matters is what counts as *spent*. Checking a document against
actuals alone is the standard way to blow a budget with a compliant process:
two approvers working the same queue at the same time each see $50,000
remaining, each clears a $40,000 bill, and the budget is $30,000 over with
every control having passed. So the balance here is consumed by **commitments**
— every request that is pending or approved, whether or not it has reached the
ledger — and a pending request holds its allocation from the moment it is
raised. Cancel or reject it and the allocation comes back.

Enforcement is a policy setting, not a property of the budget: ``off``, ``warn``
(record the breach, let it through, show it to the approver) or ``block``
(refuse submission). Most finance teams want ``warn`` — a hard block on a
budget line is how shadow approval processes get started.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import Document
from .money import ZERO, fmt, quantize, to_money

MONTHLY = "monthly"
QUARTERLY = "quarterly"
ANNUAL = "annual"

OK = "ok"
NO_BUDGET = "no_budget"
WARN = "warn"
BREACH = "breach"


class BudgetError(ValueError):
    """Raised when a budget file is malformed."""


def period_key(when: date | None, granularity: str = MONTHLY, fy_start_month: int = 7) -> str:
    """The budget period a document falls in.

    ``fy_start_month`` defaults to July, the Australian financial year. An
    annual budget keyed on the calendar year would misfile every July-December
    document for an organisation whose year starts in July.
    """
    if when is None:
        return ""
    if granularity == ANNUAL:
        fy = when.year if when.month >= fy_start_month else when.year - 1
        return f"FY{fy + 1}"
    if granularity == QUARTERLY:
        # Quarters run from the financial year start, so Q1 is Jul-Sep for an
        # AU organisation rather than Jan-Mar.
        offset = (when.month - fy_start_month) % 12
        quarter = offset // 3 + 1
        fy = when.year if when.month >= fy_start_month else when.year - 1
        return f"FY{fy + 1}-Q{quarter}"
    return f"{when.year:04d}-{when.month:02d}"


@dataclass(frozen=True, slots=True)
class BudgetLine:
    period: str
    account: str
    amount: Decimal
    tracking: str = ""
    name: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.period, self.account, self.tracking)

    @property
    def label(self) -> str:
        base = self.name or f"account {self.account}"
        return f"{base} · {self.period}" + (f" · {self.tracking}" if self.tracking else "")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetLine:
        account = str(data.get("account") or data.get("account_code") or "").strip()
        period = str(data.get("period") or "").strip()
        if not account or not period:
            raise BudgetError(f"budget line needs both `period` and `account`: {data!r}")
        return cls(
            period=period,
            account=account,
            amount=to_money(data.get("amount")),
            tracking=str(data.get("tracking") or "").strip(),
            name=str(data.get("name") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"period": self.period, "account": self.account,
                               "amount": str(self.amount)}
        if self.tracking:
            out["tracking"] = self.tracking
        if self.name:
            out["name"] = self.name
        return out


@dataclass(frozen=True, slots=True)
class Impact:
    """What one document does to one budget line."""

    line: BudgetLine
    committed: Decimal
    this_document: Decimal

    @property
    def remaining_before(self) -> Decimal:
        return quantize(self.line.amount - self.committed)

    @property
    def remaining_after(self) -> Decimal:
        return quantize(self.remaining_before - self.this_document)

    @property
    def is_breach(self) -> bool:
        return self.remaining_after < 0

    @property
    def utilisation(self) -> Decimal:
        if self.line.amount == 0:
            return ZERO
        return quantize((self.committed + self.this_document) / self.line.amount * Decimal("100"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.line.label,
            "period": self.line.period,
            "account": self.line.account,
            "tracking": self.line.tracking,
            "budget": str(self.line.amount),
            "committed": str(self.committed),
            "this_document": str(self.this_document),
            "remaining_before": str(self.remaining_before),
            "remaining_after": str(self.remaining_after),
            "utilisation": str(self.utilisation),
            "breach": self.is_breach,
        }


@dataclass(frozen=True, slots=True)
class BudgetCheck:
    status: str
    impacts: tuple[Impact, ...] = ()
    unbudgeted: tuple[str, ...] = ()
    message: str = ""

    @property
    def is_breach(self) -> bool:
        return self.status == BREACH

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "impacts": [i.as_dict() for i in self.impacts],
            "unbudgeted": list(self.unbudgeted),
        }


@dataclass
class Budget:
    """Budget lines plus the granularity they are keyed on."""

    lines: list[BudgetLine] = field(default_factory=list)
    granularity: str = MONTHLY
    fy_start_month: int = 7

    def __bool__(self) -> bool:
        return bool(self.lines)

    def _index(self) -> dict[tuple[str, str, str], BudgetLine]:
        return {line.key: line for line in self.lines}

    def line_for(self, period: str, account: str, tracking: str = "") -> BudgetLine | None:
        index = self._index()
        # A tracking-specific line wins over the account-wide one, so a
        # department budget can sit inside a company-wide account budget
        # without either being double-counted.
        if tracking and (period, account, tracking) in index:
            return index[(period, account, tracking)]
        return index.get((period, account, ""))

    def allocate(self, document: Document) -> dict[tuple[str, str, str], Decimal]:
        """Split a document across budget keys, in base currency.

        Lines carry the split. A document with no line-level coding at all is
        allocated whole to an empty account code, which will find no budget and
        surface as unbudgeted rather than silently passing.
        """
        rate = document.currency_rate if document.currency_rate > 0 else Decimal("1")
        period = period_key(document.document_date, self.granularity, self.fy_start_month)
        out: dict[tuple[str, str, str], Decimal] = {}
        if not document.line_items:
            out[(period, "", "")] = quantize(abs(document.amount_total) * rate)
            return out
        for item in document.line_items:
            tracking = ""
            if item.tracking:
                category, option = sorted(item.tracking.items())[0]
                tracking = f"{category}:{option}"
            # Budgets are set ex-tax: GST on a bill is recoverable and never
            # lands in the expense account the budget is written against.
            net = abs(item.line_amount) - abs(item.tax_amount)
            amount = quantize(net * rate)
            line = self.line_for(period, item.account_code, tracking)
            key = line.key if line else (period, item.account_code, tracking)
            out[key] = out.get(key, ZERO) + amount
        return out

    def check(
        self,
        document: Document,
        commitments: dict[tuple[str, str, str], Decimal] | None = None,
    ) -> BudgetCheck:
        """Test one document against the budget, given existing commitments."""
        if not self.lines:
            return BudgetCheck(status=NO_BUDGET, message="no budget configured")

        commitments = commitments or {}
        index = self._index()
        impacts: list[Impact] = []
        unbudgeted: list[str] = []

        for key, amount in sorted(self.allocate(document).items()):
            line = index.get(key)
            if line is None:
                period, account, tracking = key
                unbudgeted.append(
                    f"{fmt(amount)} against account {account or '(uncoded)'}"
                    + (f" / {tracking}" if tracking else "")
                    + f" in {period or 'an undated period'}"
                )
                continue
            impacts.append(
                Impact(line=line, committed=commitments.get(key, ZERO), this_document=amount)
            )

        if not impacts:
            return BudgetCheck(
                status=NO_BUDGET,
                unbudgeted=tuple(unbudgeted),
                message="nothing on this document maps to a budget line",
            )

        breaches = [i for i in impacts if i.is_breach]
        if breaches:
            worst = min(breaches, key=lambda i: i.remaining_after)
            return BudgetCheck(
                status=BREACH,
                impacts=tuple(impacts),
                unbudgeted=tuple(unbudgeted),
                message=(
                    f"{worst.line.label} would go {fmt(abs(worst.remaining_after))} over "
                    f"({fmt(worst.remaining_before)} left, this document is "
                    f"{fmt(worst.this_document)})"
                ),
            )
        tight = [i for i in impacts if i.line.amount > 0 and i.utilisation >= Decimal("90")]
        if tight:
            worst = max(tight, key=lambda i: i.utilisation)
            return BudgetCheck(
                status=WARN,
                impacts=tuple(impacts),
                unbudgeted=tuple(unbudgeted),
                message=f"{worst.line.label} would be {worst.utilisation}% used",
            )
        return BudgetCheck(status=OK, impacts=tuple(impacts), unbudgeted=tuple(unbudgeted))

    # -- persistence ------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "granularity": self.granularity,
            "fy_start_month": self.fy_start_month,
            "lines": [line.as_dict() for line in self.lines],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.as_dict(), indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> Budget:
        """Load a budget file; a missing file is an empty budget, not an error."""
        target = Path(path)
        if not target.exists():
            return cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BudgetError(f"{target} is not valid JSON: {exc}") from exc
        rows = raw.get("lines") if isinstance(raw, dict) else raw
        granularity = str((raw.get("granularity") if isinstance(raw, dict) else "") or MONTHLY)
        if granularity not in (MONTHLY, QUARTERLY, ANNUAL):
            raise BudgetError(f"unknown budget granularity {granularity!r}")
        return cls(
            lines=[BudgetLine.from_dict(row) for row in (rows or [])],
            granularity=granularity,
            fy_start_month=int((raw.get("fy_start_month") if isinstance(raw, dict) else 7) or 7),
        )


def commitments_from_store(
    budget: Budget, store: Any, exclude_request_id: int | None = None
) -> dict[tuple[str, str, str], Decimal]:
    """Total already committed per budget key by live requests.

    ``exclude_request_id`` leaves a request's own allocation out, which is what
    re-checking an existing request needs: counting itself would make every
    re-check look like a doubling of its own spend.
    """
    totals: dict[tuple[str, str, str], Decimal] = {}
    for row in store.commitments():
        if exclude_request_id is not None and int(row["id"]) == exclude_request_id:
            continue
        try:
            document = Document.from_dict(json.loads(row["document"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not document.kind.is_spend:
            continue
        for key, amount in budget.allocate(document).items():
            totals[key] = totals.get(key, ZERO) + amount
    return totals
