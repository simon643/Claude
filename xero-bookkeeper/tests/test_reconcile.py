"""Matching and coding suggestions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from xerobk.models import BankLine, InvoiceType
from xerobk.reconcile import (
    CONFIDENCE_AUTO,
    find_batch_payment,
    score_match,
    suggest,
    suggest_all,
    summarise,
)
from xerobk.rules import Direction, Rule, RuleSet, learn_rules

from .conftest import make_invoice


def line(description: str, amount: str, when: str = "2026-07-02", reference: str = "") -> BankLine:
    return BankLine(
        line_id=f"L-{description[:8]}-{amount}",
        date=date.fromisoformat(when),
        description=description,
        amount=Decimal(amount),
        reference=reference,
    )


class TestScoring:
    def test_exact_amount_plus_invoice_number_is_conclusive(self) -> None:
        invoice = make_invoice("INV-0031", "Kestrel Build Group Pty Ltd", "11000.00", due="2026-07-24")
        score, reasons = score_match(line("DIRECT CREDIT KESTREL BUILD INV-0031", "11000.00"), invoice)
        assert score >= CONFIDENCE_AUTO
        assert any("invoice number" in r for r in reasons)

    def test_invoice_number_written_without_punctuation(self) -> None:
        invoice = make_invoice("INV-0031", amount="11000.00", due="2026-07-24")
        score, _ = score_match(line("PAYMENT INV0031", "11000.00"), invoice)
        assert score >= CONFIDENCE_AUTO

    def test_bare_digits_still_match(self) -> None:
        invoice = make_invoice("INV-0031", amount="11000.00", due="2026-07-24")
        score, reasons = score_match(line("TRANSFER REF 0031", "11000.00"), invoice)
        assert any("invoice number" in r for r in reasons)
        assert score > 0

    def test_wrong_amount_scores_nothing(self) -> None:
        invoice = make_invoice("INV-0031", amount="11000.00")
        score, _ = score_match(line("PAYMENT INV-0031", "99999.00"), invoice)
        assert score == 0

    def test_part_payment_is_low_confidence_not_zero(self) -> None:
        invoice = make_invoice("INV-0031", amount="11000.00", due="2026-07-24")
        score, reasons = score_match(line("PART PAYMENT KESTREL BUILD", "5000.00"), invoice)
        assert 0 < score < CONFIDENCE_AUTO
        assert any("part payment" in r for r in reasons)

    def test_direction_mismatch_is_penalised(self) -> None:
        bill = make_invoice("BILL-1", "Supplier", "500.00", invoice_type=InvoiceType.ACCPAY, due="2026-07-01")
        # Money IN cannot be settling a bill we owe.
        score, reasons = score_match(line("DEPOSIT", "500.00"), bill)
        assert any("supplier bill" in r for r in reasons)
        assert score < CONFIDENCE_AUTO


class TestSuggest:
    def test_ambiguity_is_surfaced_not_guessed(self) -> None:
        # Two identical invoices for the same customer: the evidence genuinely
        # cannot distinguish them, so both must be offered.
        invoices = [
            make_invoice("INV-1", "Ashgrove Property Trust", "44000.00", due="2026-06-25"),
            make_invoice("INV-2", "Ashgrove Property Trust", "44000.00", due="2026-06-30"),
        ]
        result = suggest(line("OSKO PAYMENT ASHGROVE TRUST", "44000.00", "2026-07-11"), invoices)
        assert result.ambiguous
        assert len(result.invoices) == 2
        assert result.confidence < CONFIDENCE_AUTO
        assert not result.is_confident

    def test_unambiguous_match_is_confident(self) -> None:
        invoices = [
            make_invoice("INV-1", "Ashgrove Property Trust", "44000.00", due="2026-06-25"),
            make_invoice("INV-2", "Kestrel Group", "31000.00", due="2026-06-30"),
        ]
        result = suggest(line("OSKO ASHGROVE TRUST INV-1", "44000.00", "2026-06-26"), invoices)
        assert result.is_confident
        assert not result.ambiguous
        assert result.invoices[0].invoice_number == "INV-1"

    def test_falls_back_to_rules_when_no_invoice_matches(self) -> None:
        ruleset = RuleSet(rules=[Rule("Bank fees", "404", contains=("bank fee",), direction=Direction.OUT)])
        result = suggest(line("MONTHLY BANK FEE", "-15.00"), [], ruleset)
        assert result.kind == "code"
        assert result.account_code == "404"

    def test_unknown_when_nothing_applies(self) -> None:
        result = suggest(line("MYSTERY TRANSFER", "-482.30"), [], RuleSet())
        assert result.kind == "unknown"
        assert result.confidence == 0


class TestBatchPayments:
    def test_sums_to_one_transfer(self) -> None:
        invoices = [
            make_invoice("A", "Harbourline", "165000.00", due="2026-05-28"),
            make_invoice("B", "Harbourline", "132000.00", due="2026-08-11"),
        ]
        found = find_batch_payment(line("DIRECT CREDIT HARBOURLINE", "297000.00"), invoices)
        assert found is not None
        assert {i.invoice_number for i in found} == {"A", "B"}

    def test_will_not_combine_across_contacts(self) -> None:
        # A coincidental cross-customer sum is far more likely wrong than right.
        invoices = [
            make_invoice("A", "Harbourline", "165000.00"),
            make_invoice("B", "Kestrel", "132000.00"),
        ]
        assert find_batch_payment(line("DIRECT CREDIT", "297000.00"), invoices) is None

    def test_no_false_positive_when_nothing_sums(self) -> None:
        invoices = [
            make_invoice("A", "Harbourline", "100.00"),
            make_invoice("B", "Harbourline", "200.00"),
        ]
        assert find_batch_payment(line("PAYMENT", "999.00"), invoices) is None


class TestSuggestAll:
    def test_an_invoice_is_not_claimed_twice(self) -> None:
        # Two identical payments must not both be matched to the one invoice.
        invoices = [make_invoice("INV-1", "Acme", "1000.00", due="2026-07-01")]
        lines = [
            line("PAYMENT ACME INV-1", "1000.00", "2026-07-02"),
            line("PAYMENT ACME INV-1 AGAIN", "1000.00", "2026-07-03"),
        ]
        results = suggest_all(lines, invoices)
        confident = [r for r in results if r.is_confident and r.kind == "match"]
        assert len(confident) == 1

    def test_stats_add_up(self) -> None:
        invoices = [make_invoice("INV-1", "Acme", "1000.00", due="2026-07-01")]
        lines = [
            line("PAYMENT ACME INV-1", "1000.00"),
            line("MYSTERY", "-33.00"),
        ]
        stats = summarise(suggest_all(lines, invoices, RuleSet()))
        assert stats.total == 2
        assert stats.confident + stats.review + stats.unknown == stats.total


class TestRules:
    def test_a_rule_with_no_conditions_matches_nothing(self) -> None:
        # Otherwise it would swallow every transaction.
        assert not Rule("catch all", "999").matches(line("ANYTHING", "-10.00"))

    def test_all_contains_terms_must_be_present(self) -> None:
        rule = Rule("Uber Eats", "420", contains=("uber", "eats"))
        assert rule.matches(line("UBER EATS SYDNEY", "-32.00"))
        assert not rule.matches(line("UBER TRIP SYDNEY", "-24.00"))

    def test_direction_filter(self) -> None:
        rule = Rule("Interest", "270", contains=("interest",), direction=Direction.IN)
        assert rule.matches(line("INTEREST PAID", "5.00"))
        assert not rule.matches(line("INTEREST CHARGED", "-5.00"))

    def test_amount_bounds(self) -> None:
        rule = Rule("Small fees", "404", contains=("fee",), max_amount=Decimal("50.00"))
        assert rule.matches(line("ACCOUNT FEE", "-15.00"))
        assert not rule.matches(line("ACCOUNT FEE", "-500.00"))

    def test_malformed_regex_does_not_explode(self) -> None:
        rule = Rule("Broken", "404", regex="[unclosed")
        assert rule.matches(line("ANYTHING", "-10.00")) is False

    def test_more_specific_rule_wins_at_equal_priority(self) -> None:
        ruleset = RuleSet(rules=[
            Rule("Generic", "400", contains=("payment",), priority=100),
            Rule("Specific", "489", contains=("telstra", "payment"), priority=100),
        ])
        matched = ruleset.match(line("TELSTRA BILL PAYMENT", "-89.00"))
        assert matched is not None and matched.account_code == "489"

    def test_lower_priority_number_wins(self) -> None:
        ruleset = RuleSet(rules=[
            Rule("Late", "400", contains=("fee",), priority=900),
            Rule("Early", "404", contains=("fee",), priority=10),
        ])
        matched = ruleset.match(line("BANK FEE", "-15.00"))
        assert matched is not None and matched.account_code == "404"

    def test_roundtrip_through_json(self, tmp_path) -> None:
        original = RuleSet.defaults()
        path = original.save(tmp_path / "rules.json")
        reloaded = RuleSet.load(path)
        assert len(reloaded.rules) == len(original.rules)
        assert {r.name for r in reloaded.rules} == {r.name for r in original.rules}


class TestLearning:
    def test_learns_a_consistent_merchant(self) -> None:
        history = [(line(f"TELSTRA BILL {i}", "-89.00"), "489") for i in range(3)]
        learned = learn_rules(history, min_occurrences=3)
        assert any(r.account_code == "489" and "telstra" in r.contains for r in learned)

    def test_refuses_to_learn_ambiguous_tokens(self) -> None:
        # The same merchant coded two different ways teaches nothing reliable.
        history = [
            (line("BUNNINGS WAREHOUSE", "-120.00"), "310"),
            (line("BUNNINGS WAREHOUSE", "-120.00"), "310"),
            (line("BUNNINGS WAREHOUSE", "-120.00"), "463"),
        ]
        learned = learn_rules(history, min_occurrences=2)
        assert not any("bunnings" in r.contains for r in learned)

    def test_below_threshold_learns_nothing(self) -> None:
        history = [(line("TELSTRA BILL", "-89.00"), "489")]
        assert learn_rules(history, min_occurrences=3) == []

    def test_noise_words_are_not_learned(self) -> None:
        history = [(line("DIRECT DEBIT PAYMENT", "-50.00"), "400") for _ in range(5)]
        learned = learn_rules(history, min_occurrences=3)
        tokens = {token for rule in learned for token in rule.contains}
        assert not tokens & {"direct", "debit", "payment"}

    def test_learned_rules_never_outrank_explicit_ones(self) -> None:
        history = [(line("TELSTRA BILL", "-89.00"), "999") for _ in range(3)]
        learned = learn_rules(history, min_occurrences=3)
        ruleset = RuleSet(rules=[Rule("Explicit telstra", "489", contains=("telstra",)), *learned])
        matched = ruleset.match(line("TELSTRA BILL PAYMENT", "-89.00"))
        assert matched is not None and matched.account_code == "489"
