"""Draft construction and validation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from xerobk.drafting import Draft, DraftLine, ValidationError, build_draft, with_payment_terms
from xerobk.models import InvoiceType


def valid_line(**kwargs) -> DraftLine:
    defaults = {
        "description": "Consulting",
        "quantity": Decimal("1"),
        "unit_amount": Decimal("1000.00"),
        "account_code": "200",
        "tax_type": "OUTPUT",
    }
    return DraftLine(**{**defaults, **kwargs})


class TestTotals:
    def test_exclusive_adds_gst_on_top(self) -> None:
        draft = Draft(contact_name="Acme", lines=[valid_line()], line_amount_types="Exclusive")
        assert draft.subtotal == Decimal("1000.00")
        assert draft.total_tax == Decimal("100.00")
        assert draft.total == Decimal("1100.00")

    def test_inclusive_extracts_gst_from_within(self) -> None:
        draft = Draft(
            contact_name="Acme",
            lines=[valid_line(unit_amount=Decimal("1100.00"))],
            line_amount_types="Inclusive",
        )
        assert draft.total_tax == Decimal("100.00")
        assert draft.subtotal == Decimal("1000.00")
        assert draft.total == Decimal("1100.00")

    def test_bas_excluded_lines_attract_no_gst(self) -> None:
        draft = Draft(
            contact_name="Acme",
            lines=[valid_line(tax_type="BASEXCLUDED")],
            line_amount_types="Exclusive",
        )
        assert draft.total_tax == Decimal("0.00")
        assert draft.total == Decimal("1000.00")

    def test_gst_free_lines_attract_no_gst(self) -> None:
        draft = Draft(
            contact_name="Acme",
            lines=[valid_line(tax_type="EXEMPTOUTPUT")],
            line_amount_types="Exclusive",
        )
        assert draft.total_tax == Decimal("0.00")

    def test_quantity_multiplies(self) -> None:
        draft = Draft(
            contact_name="Acme",
            lines=[valid_line(quantity=Decimal("3"), unit_amount=Decimal("250.00"))],
        )
        assert draft.subtotal == Decimal("750.00")
        assert draft.total == Decimal("825.00")

    def test_mixed_tax_treatments(self) -> None:
        draft = Draft(
            contact_name="Acme",
            lines=[valid_line(), valid_line(tax_type="BASEXCLUDED", unit_amount=Decimal("500.00"))],
            line_amount_types="Exclusive",
        )
        assert draft.subtotal == Decimal("1500.00")
        assert draft.total_tax == Decimal("100.00")
        assert draft.total == Decimal("1600.00")


class TestValidation:
    def test_a_good_draft_has_no_errors(self) -> None:
        draft = Draft(contact_name="Acme", lines=[valid_line()])
        assert draft.validate() == []

    def test_contact_is_required(self) -> None:
        draft = Draft(lines=[valid_line()])
        assert any("contact" in error for error in draft.validate())

    def test_at_least_one_line_is_required(self) -> None:
        draft = Draft(contact_name="Acme")
        assert any("line item" in error for error in draft.validate())

    def test_account_code_is_required(self) -> None:
        # Xero would accept this and post to a default, which is how miscoded
        # lines quietly accumulate.
        draft = Draft(contact_name="Acme", lines=[valid_line(account_code="")])
        assert any("account code" in error for error in draft.validate())

    def test_item_code_substitutes_for_account_code(self) -> None:
        draft = Draft(contact_name="Acme", lines=[valid_line(account_code="", item_code="WIDGET")])
        assert not any("account code" in error for error in draft.validate())

    def test_unknown_account_code_is_rejected_when_chart_is_known(self) -> None:
        draft = Draft(contact_name="Acme", lines=[valid_line(account_code="999")])
        assert draft.validate() == []  # no chart supplied, so nothing to check
        assert any("not in the chart" in e for e in draft.validate(known_accounts={"200", "400"}))

    def test_nonstandard_au_tax_type_is_rejected(self) -> None:
        draft = Draft(contact_name="Acme", lines=[valid_line(tax_type="VAT20")])
        assert any("not a standard AU code" in error for error in draft.validate())

    def test_due_before_invoice_date(self) -> None:
        draft = Draft(
            contact_name="Acme",
            lines=[valid_line()],
            invoice_date=date(2026, 8, 10),
            due_date=date(2026, 8, 1),
        )
        assert any("due date is before" in error for error in draft.validate())

    def test_negative_total_suggests_a_credit_note(self) -> None:
        draft = Draft(contact_name="Acme", lines=[valid_line(unit_amount=Decimal("-500.00"))])
        assert any("credit note" in error for error in draft.validate())

    def test_require_valid_raises_with_all_errors(self) -> None:
        draft = Draft()
        try:
            draft.require_valid()
        except ValidationError as exc:
            assert len(exc.errors) >= 2
        else:
            raise AssertionError("expected ValidationError")


class TestPayload:
    def test_defaults_to_draft_status(self) -> None:
        # Approving puts an invoice in the ledger and on the BAS; that must be
        # a deliberate second step.
        payload = Draft(contact_name="Acme", lines=[valid_line()]).to_payload()
        assert payload["Status"] == "DRAFT"

    def test_shape_matches_the_accounting_api(self) -> None:
        draft = Draft(
            contact_name="Acme",
            lines=[valid_line()],
            invoice_date=date(2026, 8, 1),
            due_date=date(2026, 8, 15),
            reference="PO-4471",
        )
        payload = draft.to_payload()
        assert payload["Type"] == "ACCREC"
        assert payload["Contact"]["Name"] == "Acme"
        assert payload["Date"] == "2026-08-01"
        assert payload["DueDate"] == "2026-08-15"
        assert payload["Reference"] == "PO-4471"
        assert payload["LineItems"][0]["AccountCode"] == "200"
        assert payload["LineItems"][0]["TaxType"] == "OUTPUT"

    def test_contact_id_is_preferred_when_present(self) -> None:
        draft = Draft(contact_id="abc-123", contact_name="Acme", lines=[valid_line()])
        assert draft.to_payload()["Contact"]["ContactID"] == "abc-123"

    def test_tracking_categories_are_carried(self) -> None:
        draft = Draft(contact_name="Acme", lines=[valid_line(tracking={"Region": "VIC"})])
        tracking = draft.to_payload()["LineItems"][0]["Tracking"]
        assert tracking == [{"Name": "Region", "Option": "VIC"}]


class TestBuildDraft:
    def test_sales_default_to_output_tax(self) -> None:
        draft = build_draft("Acme", [{"description": "Work", "unit_amount": "1000"}])
        assert draft.lines[0].tax_type == "OUTPUT"

    def test_bills_default_to_input_tax(self) -> None:
        draft = build_draft(
            "Supplier",
            [{"description": "Survey", "unit_amount": "500"}],
            invoice_type=InvoiceType.ACCPAY,
        )
        assert draft.lines[0].tax_type == "INPUT"

    def test_payment_terms_set_the_due_date(self) -> None:
        draft = build_draft(
            "Acme",
            [{"description": "Work", "unit_amount": "1000"}],
            invoice_date=date(2026, 8, 1),
            payment_terms_days=30,
        )
        assert draft.due_date == date(2026, 8, 31)

    def test_with_payment_terms(self) -> None:
        assert with_payment_terms(date(2026, 8, 1), 14) == date(2026, 8, 15)
