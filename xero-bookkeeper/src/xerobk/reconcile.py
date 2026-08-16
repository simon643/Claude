"""Bank reconciliation.

Given unreconciled bank lines and the outstanding ledger, propose what each
line is. Two kinds of proposal come out:

* **Match** — this receipt pays invoice INV-0084. Evidence: the amount is
  exact, the invoice number appears in the narrative, the date is close.
* **Code** — this payment is not settling any invoice; code it to an account
  using the rules engine.

Every suggestion carries a confidence score and the human-readable reasons
behind it. Nothing is auto-posted: the confidence exists so a bookkeeper can
approve the obvious ones in bulk and spend their attention on the rest.

The matcher deliberately reports ambiguity instead of hiding it. When a
payment could settle any of three same-priced invoices, that is surfaced as an
ambiguous match with all three candidates rather than a confident guess at one
— silently picking wrong here means chasing a customer who already paid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations
from typing import Any

from .models import BankLine, Invoice, InvoiceType
from .money import ZERO
from .rules import Rule, RuleSet

# Confidence bands. `AUTO` is the threshold above which a suggestion is safe to
# approve in bulk; `REVIEW` is the floor for showing a suggestion at all.
CONFIDENCE_AUTO = 90
CONFIDENCE_REVIEW = 40

# How far apart a payment and its invoice's due date can be before proximity
# stops counting as evidence.
_DATE_WINDOW_DAYS = 90


@dataclass(slots=True)
class Suggestion:
    """What we think a bank line is."""

    line: BankLine
    kind: str  # "match" | "code" | "unknown"
    confidence: int = 0
    reasons: list[str] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)
    account_code: str = ""
    tax_type: str = ""
    contact_name: str = ""
    rule_name: str = ""
    ambiguous: bool = False

    @property
    def is_confident(self) -> bool:
        return self.confidence >= CONFIDENCE_AUTO and not self.ambiguous

    @property
    def needs_review(self) -> bool:
        return not self.is_confident

    def summary(self) -> str:
        if self.kind == "match" and self.invoices:
            numbers = ", ".join(inv.invoice_number or inv.invoice_id[:8] for inv in self.invoices)
            verb = "may pay" if self.ambiguous else "pays"
            return f"{verb} {numbers}"
        if self.kind == "code" and self.account_code:
            return f"code to {self.account_code}"
        return "no suggestion"

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line.line_id,
            "date": self.line.date.isoformat() if self.line.date else None,
            "description": self.line.description,
            "amount": str(self.line.amount),
            "kind": self.kind,
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "reasons": list(self.reasons),
            "summary": self.summary(),
            "account_code": self.account_code,
            "tax_type": self.tax_type,
            "contact_name": self.contact_name,
            "rule_name": self.rule_name,
            "invoices": [
                {
                    "invoice_id": inv.invoice_id,
                    "invoice_number": inv.invoice_number,
                    "contact": inv.contact.name,
                    "amount_due": str(inv.amount_due),
                    "due_date": inv.due_date.isoformat() if inv.due_date else None,
                }
                for inv in self.invoices
            ],
        }


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _invoice_number_tokens(number: str) -> list[str]:
    """Forms of an invoice number that might appear in a bank narrative.

    ``INV-0084`` may be written as ``INV-0084``, ``INV0084``, ``0084``, or
    ``84`` depending on who typed the payment reference.
    """
    if not number:
        return []
    raw = number.strip()
    compact = re.sub(r"[^A-Za-z0-9]", "", raw).lower()
    digits = re.sub(r"\D", "", raw)
    tokens = {compact}
    if digits:
        tokens.add(digits)
        tokens.add(digits.lstrip("0") or digits)
    return [t for t in tokens if len(t) >= 3]


def _contact_tokens(name: str) -> list[str]:
    """Distinctive words from a contact name, minus company suffixes."""
    noise = {"pty", "ltd", "limited", "the", "and", "australia", "group", "holdings", "trust", "inc", "co"}
    words = _normalise(name).split()
    return [w for w in words if w not in noise and len(w) >= 4]


def score_match(line: BankLine, invoice: Invoice) -> tuple[int, list[str]]:
    """Score how well a bank line matches one invoice. Returns (0-100, reasons)."""
    reasons: list[str] = []
    score = 0
    magnitude = abs(line.amount)
    haystack = _normalise(f"{line.description} {line.reference}")
    compact_haystack = haystack.replace(" ", "")

    # Amount is the strongest single signal.
    if magnitude == invoice.amount_due:
        score += 55
        reasons.append("amount exactly matches the outstanding balance")
    elif magnitude == invoice.amount_total:
        score += 45
        reasons.append("amount matches the invoice total")
    elif invoice.amount_due and abs(magnitude - invoice.amount_due) <= Decimal("0.05"):
        score += 40
        reasons.append("amount matches within rounding")
    elif invoice.amount_due and magnitude < invoice.amount_due:
        score += 12
        reasons.append("looks like a part payment")
    else:
        return 0, []  # overpayment or unrelated amount; not this invoice

    # An invoice number in the narrative is close to conclusive.
    for token in _invoice_number_tokens(invoice.invoice_number):
        if token in compact_haystack:
            score += 35
            reasons.append(f"narrative contains the invoice number ({invoice.invoice_number})")
            break

    # The customer's name in the narrative.
    contact_hits = [t for t in _contact_tokens(invoice.contact.name) if t in haystack]
    if contact_hits:
        score += 20
        reasons.append(f"narrative mentions the contact ({invoice.contact.name})")

    # A reference the bookkeeper typed onto the invoice, echoed by the payer.
    if invoice.reference:
        ref_tokens = [t for t in _normalise(invoice.reference).split() if len(t) >= 4]
        if ref_tokens and any(t in haystack for t in ref_tokens):
            score += 10
            reasons.append("narrative matches the invoice reference")

    # Date proximity: supporting evidence, never decisive on its own.
    if line.date and invoice.due_date:
        gap = abs((line.date - invoice.due_date).days)
        if gap <= 7:
            score += 10
            reasons.append("paid within a week of the due date")
        elif gap <= 30:
            score += 5
            reasons.append("paid within a month of the due date")
        elif gap > _DATE_WINDOW_DAYS:
            score -= 10
            reasons.append(f"paid {gap} days from the due date")

    # Direction sanity: a receipt should settle a sales invoice, a payment a bill.
    if line.is_credit and invoice.invoice_type is InvoiceType.ACCPAY:
        score -= 30
        reasons.append("money in, but this is a supplier bill")
    if line.is_debit and invoice.invoice_type is InvoiceType.ACCREC:
        score -= 30
        reasons.append("money out, but this is a sales invoice")

    return max(0, min(100, score)), reasons


def find_batch_payment(
    line: BankLine, invoices: list[Invoice], max_invoices: int = 4
) -> list[Invoice] | None:
    """Find a small set of invoices whose balances sum to the payment.

    Customers routinely settle several invoices with one transfer. Only
    same-contact combinations are considered, and only up to ``max_invoices``,
    because the number of subsets explodes and a coincidental sum across
    unrelated customers is far more likely to be wrong than right.
    """
    magnitude = abs(line.amount)
    if magnitude <= 0:
        return None

    by_contact: dict[str, list[Invoice]] = {}
    for invoice in invoices:
        if invoice.amount_due > 0 and invoice.amount_due < magnitude:
            by_contact.setdefault(invoice.contact.contact_id or invoice.contact.name, []).append(invoice)

    for candidates in by_contact.values():
        if len(candidates) < 2:
            continue
        # Cap the search space; 12 invoices over subsets of <=4 is ~800 sums.
        pool = sorted(candidates, key=lambda i: i.amount_due, reverse=True)[:12]
        for size in range(2, min(max_invoices, len(pool)) + 1):
            for combo in combinations(pool, size):
                if sum((i.amount_due for i in combo), ZERO) == magnitude:
                    return list(combo)
    return None


def suggest(
    line: BankLine,
    invoices: list[Invoice],
    ruleset: RuleSet | None = None,
) -> Suggestion:
    """Produce the best suggestion for one bank line."""
    outstanding = [inv for inv in invoices if inv.is_outstanding]

    scored: list[tuple[int, list[str], Invoice]] = []
    for invoice in outstanding:
        score, reasons = score_match(line, invoice)
        if score >= CONFIDENCE_REVIEW:
            scored.append((score, reasons, invoice))
    scored.sort(key=lambda item: item[0], reverse=True)

    if scored:
        best_score, best_reasons, best_invoice = scored[0]
        # Ambiguous when a runner-up is within a few points of the leader:
        # the evidence does not actually distinguish them.
        rivals = [item for item in scored[1:] if best_score - item[0] <= 5]
        if rivals:
            candidates = [best_invoice] + [item[2] for item in rivals]
            return Suggestion(
                line=line,
                kind="match",
                confidence=min(best_score, CONFIDENCE_AUTO - 1),
                reasons=[
                    f"{len(candidates)} invoices match equally well — pick one",
                    *best_reasons,
                ],
                invoices=candidates,
                contact_name=best_invoice.contact.name,
                ambiguous=True,
            )
        return Suggestion(
            line=line,
            kind="match",
            confidence=best_score,
            reasons=best_reasons,
            invoices=[best_invoice],
            contact_name=best_invoice.contact.name,
        )

    batch = find_batch_payment(line, outstanding)
    if batch:
        numbers = ", ".join(inv.invoice_number for inv in batch)
        return Suggestion(
            line=line,
            kind="match",
            confidence=75,
            reasons=[f"balances of {len(batch)} invoices sum exactly to this payment ({numbers})"],
            invoices=batch,
            contact_name=batch[0].contact.name,
        )

    if ruleset:
        rule = ruleset.match(line)
        if rule:
            return Suggestion(
                line=line,
                kind="code",
                confidence=_rule_confidence(rule),
                reasons=[f"matched rule '{rule.name}'"],
                account_code=rule.account_code,
                tax_type=rule.tax_type,
                contact_name=rule.contact_name,
                rule_name=rule.name,
            )

    return Suggestion(
        line=line,
        kind="unknown",
        confidence=0,
        reasons=["no invoice match and no coding rule applied"],
    )


def _rule_confidence(rule: Rule) -> int:
    """Explicit rules are trusted; learned ones are held below the auto bar."""
    if rule.source == "learned":
        return 70
    if rule.source == "default":
        return 80
    return 95


def suggest_all(
    lines: list[BankLine],
    invoices: list[Invoice],
    ruleset: RuleSet | None = None,
) -> list[Suggestion]:
    """Suggest for every line, without proposing the same invoice twice.

    Once a high-confidence match consumes an invoice, that invoice is withheld
    from later lines. Otherwise a duplicated payment amount would be proposed
    against the same invoice repeatedly.
    """
    remaining = [inv for inv in invoices if inv.is_outstanding]
    claimed: set[str] = set()
    results: list[Suggestion] = []

    # Two passes: settle the confident matches first so they get first claim on
    # their invoices, then fall back for everything else.
    ordered = sorted(
        range(len(lines)),
        key=lambda i: -max(
            (score_match(lines[i], inv)[0] for inv in remaining),
            default=0,
        ),
    )
    by_index: dict[int, Suggestion] = {}

    for index in ordered:
        available = [inv for inv in remaining if inv.invoice_id not in claimed]
        suggestion = suggest(lines[index], available, ruleset)
        if suggestion.kind == "match" and not suggestion.ambiguous and suggestion.is_confident:
            for invoice in suggestion.invoices:
                claimed.add(invoice.invoice_id)
        by_index[index] = suggestion

    for index in range(len(lines)):
        results.append(by_index[index])
    return results


@dataclass(frozen=True, slots=True)
class ReconcileStats:
    total: int
    confident: int
    review: int
    unknown: int
    matched_value: Decimal

    @property
    def auto_rate(self) -> int:
        return round(self.confident / self.total * 100) if self.total else 0


def summarise(suggestions: list[Suggestion]) -> ReconcileStats:
    confident = sum(1 for s in suggestions if s.is_confident)
    unknown = sum(1 for s in suggestions if s.kind == "unknown")
    matched_value = sum(
        (abs(s.line.amount) for s in suggestions if s.kind == "match" and s.is_confident), ZERO
    )
    return ReconcileStats(
        total=len(suggestions),
        confident=confident,
        review=len(suggestions) - confident - unknown,
        unknown=unknown,
        matched_value=matched_value,
    )


def unreconciled_window(lines: list[BankLine], as_of: date, days: int = 30) -> list[BankLine]:
    """Lines older than ``days``, which are the ones that hold up a close."""
    cutoff = as_of - timedelta(days=days)
    return [line for line in lines if line.date and line.date < cutoff]
