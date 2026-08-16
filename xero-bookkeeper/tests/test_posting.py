"""Posting to Xero.

These tests carry more weight than usual: the write paths cannot be
integration-tested from here (no client id, and OAuth needs a real browser), so
the payload shapes and the safety properties are verified against a recording
fake instead. The first real POST a user makes is the first time this code
touches Xero, so the shapes must be right by construction.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from xerobk.models import BankLine
from xerobk.posting import (
    PostingError,
    bank_transaction_payload,
    payment_payload,
    post_batch,
    post_suggestion,
)
from xerobk.providers import ProviderError
from xerobk.reconcile import Suggestion
from xerobk.store import Store

from .conftest import make_invoice

BANK = "090"


def line(description="PAYMENT", amount="1000.00", when="2026-07-02", reference="", line_id=None):
    return BankLine(
        line_id=line_id or f"L-{description[:6]}-{amount}",
        date=date.fromisoformat(when),
        description=description,
        amount=Decimal(amount),
        reference=reference,
    )


class FakeProvider:
    """Records what would be sent, and can be told to fail."""

    name = "fake"

    def __init__(self, can_write=True, fail_on=None):
        self.can_write = can_write
        self.fail_on = fail_on or set()
        self.payments: list[dict[str, Any]] = []
        self.bank_transactions: list[dict[str, Any]] = []
        self.invoices_created: list[dict[str, Any]] = []

    def create_payment(self, payload):
        if "payment" in self.fail_on:
            raise ProviderError("Xero API PUT Payments failed (400): Invoice not found")
        self.payments.append(payload)
        return {"Payments": [{"PaymentID": f"pay-{len(self.payments)}"}]}

    def create_bank_transaction(self, payload):
        if "bank" in self.fail_on:
            raise ProviderError("Xero API POST BankTransactions failed (400): bad account")
        self.bank_transactions.append(payload)
        return {"BankTransactions": [{"BankTransactionID": f"bt-{len(self.bank_transactions)}"}]}

    def create_invoice(self, payload):
        self.invoices_created.append(payload)
        return {"Invoices": [{"InvoiceID": "inv-1", "InvoiceNumber": "INV-9001"}]}

    def accounts(self):
        return []


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "posting.sqlite3") as s:
        yield s


class TestPaymentPayload:
    def test_shape(self) -> None:
        invoice = make_invoice("INV-1", amount="1100.00")
        payload = payment_payload(invoice, date(2026, 7, 2), Decimal("1100.00"), BANK, "REF")
        assert payload["Invoice"] == {"InvoiceID": "id-INV-1"}
        assert payload["Account"] == {"Code": "090"}
        assert payload["Date"] == "2026-07-02"
        assert payload["Reference"] == "REF"

    def test_amount_is_an_exact_decimal_string_not_a_float(self) -> None:
        # A float here would reintroduce binary rounding at the API boundary,
        # which is the one thing the Decimal discipline exists to prevent.
        payload = payment_payload(
            make_invoice("INV-1"), date(2026, 7, 2), Decimal("0.10"), BANK
        )
        assert payload["Amount"] == "0.10"
        assert isinstance(payload["Amount"], str)

    def test_reference_omitted_when_blank(self) -> None:
        payload = payment_payload(make_invoice("INV-1"), date(2026, 7, 2), Decimal("1"), BANK)
        assert "Reference" not in payload


class TestBankTransactionPayload:
    def test_money_out_is_spend_with_a_positive_amount(self) -> None:
        # Xero carries direction in Type; sending a negative UnitAmount on a
        # SPEND would reverse the sign twice.
        s = Suggestion(line=line("BANK FEE", "-15.00"), kind="code", account_code="404",
                       tax_type="INPUT", contact_name="Bank")
        payload = bank_transaction_payload(s, BANK)
        assert payload["Type"] == "SPEND"
        assert payload["LineItems"][0]["UnitAmount"] == "15.00"

    def test_money_in_is_receive(self) -> None:
        s = Suggestion(line=line("INTEREST", "5.00"), kind="code", account_code="270")
        payload = bank_transaction_payload(s, BANK)
        assert payload["Type"] == "RECEIVE"
        assert payload["LineItems"][0]["UnitAmount"] == "5.00"

    def test_carries_account_and_tax_type(self) -> None:
        s = Suggestion(line=line("TELSTRA", "-89.00"), kind="code",
                       account_code="489", tax_type="INPUT")
        item = bank_transaction_payload(s, BANK)["LineItems"][0]
        assert item["AccountCode"] == "489"
        assert item["TaxType"] == "INPUT"

    def test_bank_account_is_set(self) -> None:
        s = Suggestion(line=line(), kind="code", account_code="404")
        assert bank_transaction_payload(s, BANK)["BankAccount"] == {"Code": "090"}


class TestPostSuggestion:
    def test_match_creates_a_payment_not_a_bank_transaction(self, store) -> None:
        # A bank transaction alone would record the money but leave the invoice
        # showing unpaid in the aged report.
        invoice = make_invoice("INV-1", amount="1000.00")
        s = Suggestion(line=line(amount="1000.00"), kind="match", confidence=100, invoices=[invoice])
        provider = FakeProvider()
        result = post_suggestion(s, provider, store, BANK)
        assert result.ok and result.action == "payment"
        assert len(provider.payments) == 1
        assert provider.bank_transactions == []

    def test_code_creates_a_bank_transaction(self, store) -> None:
        s = Suggestion(line=line("BANK FEE", "-15.00"), kind="code",
                       confidence=95, account_code="404")
        provider = FakeProvider()
        result = post_suggestion(s, provider, store, BANK)
        assert result.ok and result.action == "bank_transaction"
        assert len(provider.bank_transactions) == 1

    def test_ambiguous_is_never_posted(self, store) -> None:
        # Picking one of several candidates would mark the wrong invoice paid.
        s = Suggestion(
            line=line(amount="44000.00"), kind="match", confidence=85, ambiguous=True,
            invoices=[make_invoice("A", amount="44000.00"), make_invoice("B", amount="44000.00")],
        )
        provider = FakeProvider()
        result = post_suggestion(s, provider, store, BANK)
        assert result.action == "skipped"
        assert "ambiguous" in result.detail
        assert provider.payments == []

    def test_batch_payment_splits_across_invoices_without_overpaying(self, store) -> None:
        a = make_invoice("A", "Acme", "165000.00")
        b = make_invoice("B", "Acme", "132000.00")
        s = Suggestion(line=line(amount="297000.00"), kind="match", confidence=95, invoices=[a, b])
        provider = FakeProvider()
        post_suggestion(s, provider, store, BANK)
        amounts = [Decimal(p["Amount"]) for p in provider.payments]
        assert amounts == [Decimal("165000.00"), Decimal("132000.00")]
        assert sum(amounts) == Decimal("297000.00")

    def test_a_payment_never_exceeds_the_invoice_balance(self, store) -> None:
        invoice = make_invoice("A", amount="100.00")
        s = Suggestion(line=line(amount="500.00"), kind="match", confidence=95, invoices=[invoice])
        provider = FakeProvider()
        post_suggestion(s, provider, store, BANK)
        assert Decimal(provider.payments[0]["Amount"]) == Decimal("100.00")

    def test_already_posted_lines_are_skipped(self, store) -> None:
        invoice = make_invoice("INV-1", amount="1000.00")
        bank_line = line(amount="1000.00", line_id="dup-1")
        s = Suggestion(line=bank_line, kind="match", confidence=100, invoices=[invoice])
        provider = FakeProvider()
        result = post_suggestion(s, provider, store, BANK, already_posted={"dup-1"})
        assert result.action == "skipped"
        assert provider.payments == []


class TestAuditTrail:
    def test_the_audit_row_is_written_before_the_request(self, store) -> None:
        """The provider sees a 'pending' row already in the log when called.

        This is the property that makes the log trustworthy: if the process
        dies mid-request, the evidence of the attempt already exists.
        """
        seen: list[str] = []

        class Watching(FakeProvider):
            def create_payment(self, payload):
                seen.extend(e["outcome"] for e in store.audit_tail())
                return super().create_payment(payload)

        s = Suggestion(line=line(amount="1000.00"), kind="match", confidence=100,
                       invoices=[make_invoice("INV-1", amount="1000.00")])
        post_suggestion(s, Watching(), store, BANK)
        assert "pending" in seen

    def test_success_records_the_xero_id(self, store) -> None:
        s = Suggestion(line=line(amount="1000.00"), kind="match", confidence=100,
                       invoices=[make_invoice("INV-1", amount="1000.00")])
        post_suggestion(s, FakeProvider(), store, BANK)
        entry = store.audit_tail()[0]
        assert entry["outcome"] == "ok"
        assert "PaymentID=pay-1" in entry["detail"]

    def test_failure_is_recorded_with_xeros_own_message(self, store) -> None:
        s = Suggestion(line=line(amount="1000.00"), kind="match", confidence=100,
                       invoices=[make_invoice("INV-1", amount="1000.00")])
        result = post_suggestion(s, FakeProvider(fail_on={"payment"}), store, BANK)
        assert not result.ok
        entry = store.audit_tail()[0]
        assert entry["outcome"] == "failed"
        assert "Invoice not found" in entry["detail"]


class TestPostBatch:
    def _suggestions(self):
        return [
            Suggestion(line=line("PAY A", "1000.00", line_id="a"), kind="match", confidence=100,
                       invoices=[make_invoice("INV-1", amount="1000.00")]),
            Suggestion(line=line("BANK FEE", "-15.00", line_id="b"), kind="code",
                       confidence=95, account_code="404"),
            Suggestion(line=line("MYSTERY", "-42.00", line_id="c"), kind="unknown", confidence=0),
        ]

    def test_posts_confident_and_skips_the_rest(self, store) -> None:
        provider = FakeProvider()
        summary = post_batch(self._suggestions(), provider, store, BANK)
        assert len(summary.posted) == 2
        assert len(summary.skipped) == 1
        assert not summary.failed

    def test_one_failure_does_not_abandon_the_batch(self, store) -> None:
        provider = FakeProvider(fail_on={"payment"})
        summary = post_batch(self._suggestions(), provider, store, BANK)
        assert len(summary.failed) == 1
        # The bank transaction still went through.
        assert len(provider.bank_transactions) == 1

    def test_rerunning_does_not_post_twice(self, store) -> None:
        provider = FakeProvider()
        suggestions = self._suggestions()
        post_batch(suggestions, provider, store, BANK)
        first = len(provider.payments) + len(provider.bank_transactions)
        post_batch(suggestions, provider, store, BANK)
        second = len(provider.payments) + len(provider.bank_transactions)
        assert first == second, "a re-run duplicated writes into Xero"

    def test_read_only_provider_is_refused(self, store) -> None:
        with pytest.raises(PostingError, match="read-only"):
            post_batch(self._suggestions(), FakeProvider(can_write=False), store, BANK)

    def test_missing_bank_account_is_refused(self, store) -> None:
        # Xero rejects payments without one; failing here gives a better message.
        with pytest.raises(PostingError, match="No bank account"):
            post_batch(self._suggestions(), FakeProvider(), store, "")

    def test_value_totals_only_what_was_posted(self, store) -> None:
        summary = post_batch(self._suggestions(), FakeProvider(), store, BANK)
        assert summary.value == Decimal("1015.00")
