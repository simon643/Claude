"""Close checklist and Australian period arithmetic."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from xerobk import autax
from xerobk.close import (
    CRITICAL,
    CloseContext,
    check_cash_vs_receivables,
    check_duplicate_contacts,
    check_duplicate_invoices,
    check_gst_consistency,
    check_missing_due_dates,
    check_overdue_debt,
    run_close,
)
from xerobk.models import Contact, InvoiceStatus, InvoiceType, Organisation

from .conftest import make_invoice

AU_ORG = Organisation(name="Test Trust", country="AU", base_currency="AUD")
AS_OF = date(2026, 8, 16)


def context(**kwargs) -> CloseContext:
    kwargs.setdefault("as_of", AS_OF)
    kwargs.setdefault("organisation", AU_ORG)
    return CloseContext(**kwargs)


class TestFinancialYear:
    @pytest.mark.parametrize(
        ("when", "label", "start", "end"),
        [
            (date(2026, 8, 16), "FY27", date(2026, 7, 1), date(2027, 6, 30)),
            (date(2026, 6, 30), "FY26", date(2025, 7, 1), date(2026, 6, 30)),
            (date(2026, 7, 1), "FY27", date(2026, 7, 1), date(2027, 6, 30)),
            (date(2026, 1, 15), "FY26", date(2025, 7, 1), date(2026, 6, 30)),
        ],
    )
    def test_july_to_june(self, when: date, label: str, start: date, end: date) -> None:
        fy = autax.financial_year(when)
        assert (fy.label, fy.start, fy.end) == (label, start, end)


class TestBasQuarters:
    @pytest.mark.parametrize(
        ("when", "label", "start", "end", "due"),
        [
            (date(2026, 8, 16), "FY27 Q1", date(2026, 7, 1), date(2026, 9, 30), date(2026, 10, 28)),
            (date(2026, 11, 5), "FY27 Q2", date(2026, 10, 1), date(2026, 12, 31), date(2027, 2, 28)),
            (date(2026, 2, 3), "FY26 Q3", date(2026, 1, 1), date(2026, 3, 31), date(2026, 4, 28)),
            (date(2026, 5, 20), "FY26 Q4", date(2026, 4, 1), date(2026, 6, 30), date(2026, 7, 28)),
        ],
    )
    def test_quarters(self, when: date, label: str, start: date, end: date, due: date) -> None:
        quarter = autax.bas_quarter(when)
        assert (quarter.label, quarter.start, quarter.end, quarter.due) == (label, start, end, due)

    def test_q2_due_date_crosses_the_calendar_year(self) -> None:
        # Oct-Dec is due the following February, not the same year.
        quarter = autax.bas_quarter(date(2026, 12, 1))
        assert quarter.due == date(2027, 2, 28)

    def test_previous_quarter_is_the_one_being_lodged(self) -> None:
        assert autax.previous_bas_quarter(date(2026, 8, 16)).label == "FY26 Q4"

    def test_every_month_maps_to_a_quarter(self) -> None:
        for month in range(1, 13):
            assert autax.bas_quarter(date(2026, month, 15)) is not None

    def test_month_period_handles_december(self) -> None:
        period = autax.month_period(date(2026, 12, 9))
        assert period.start == date(2026, 12, 1)
        assert period.end == date(2026, 12, 31)


class TestOverdueDebt:
    def test_fires_past_the_threshold(self) -> None:
        ctx = context(
            invoices=[make_invoice("A", amount="10000.00", due="2026-01-01")],
            escalation_days=60,
        )
        findings = check_overdue_debt(ctx)
        assert len(findings) == 1
        assert "60+ days overdue" in findings[0].title

    def test_silent_when_everything_is_current(self) -> None:
        ctx = context(invoices=[make_invoice("A", amount="10000.00", due="2026-09-30")])
        assert check_overdue_debt(ctx) == []

    def test_majority_overdue_is_a_blocker(self) -> None:
        ctx = context(
            invoices=[
                make_invoice("A", amount="10000.00", due="2026-01-01"),
                make_invoice("B", amount="100.00", due="2026-09-30"),
            ],
        )
        assert check_overdue_debt(ctx)[0].severity == CRITICAL


class TestDuplicateContacts:
    def test_catches_a_missing_legal_suffix(self) -> None:
        # This is the real-world case: one customer entered twice, so their
        # balance and statement are split across two records.
        ctx = context(contacts=[
            Contact("1", "Harbourline Constructions Pty Ltd", "a@example.com"),
            Contact("2", "Harbourline Constructions", None),
        ])
        findings = check_duplicate_contacts(ctx)
        assert len(findings) == 1
        assert "Harbourline Constructions" in findings[0].items[0]

    def test_ignores_genuinely_different_names(self) -> None:
        ctx = context(contacts=[
            Contact("1", "Harbourline Constructions Pty Ltd"),
            Contact("2", "Kestrel Build Group"),
        ])
        assert check_duplicate_contacts(ctx) == []

    def test_finds_duplicates_that_only_appear_on_invoices(self) -> None:
        ctx = context(invoices=[
            make_invoice("A", contact="Acme Pty Ltd"),
            make_invoice("B", contact="Acme"),
        ])
        assert len(check_duplicate_contacts(ctx)) == 1


class TestDuplicateInvoices:
    def test_same_contact_amount_and_close_dates(self) -> None:
        ctx = context(invoices=[
            make_invoice("A", "Acme", "44000.00", issued="2026-06-11", due="2026-06-25"),
            make_invoice("B", "Acme", "44000.00", issued="2026-06-16", due="2026-06-30"),
        ])
        assert len(check_duplicate_invoices(ctx)) == 1

    def test_monthly_recurring_is_not_flagged(self) -> None:
        ctx = context(invoices=[
            make_invoice("A", "Acme", "500.00", issued="2026-05-01", due="2026-05-15"),
            make_invoice("B", "Acme", "500.00", issued="2026-06-01", due="2026-06-15"),
        ])
        assert check_duplicate_invoices(ctx) == []

    def test_voided_invoices_are_ignored(self) -> None:
        ctx = context(invoices=[
            make_invoice("A", "Acme", "44000.00", issued="2026-06-11"),
            make_invoice("B", "Acme", "44000.00", issued="2026-06-16", status=InvoiceStatus.VOIDED),
        ])
        assert check_duplicate_invoices(ctx) == []


class TestGstConsistency:
    def test_flags_tax_that_does_not_recompute(self) -> None:
        ctx = context(
            invoices=[make_invoice("A", amount="96800.00", issued="2026-05-09", tax="0.00")],
            period=autax.bas_quarter(date(2026, 5, 1)),
        )
        findings = check_gst_consistency(ctx)
        assert len(findings) == 1
        assert "8,800.00" in findings[0].items[0]

    def test_correct_gst_passes(self) -> None:
        ctx = context(
            invoices=[make_invoice("A", amount="1100.00", issued="2026-05-09")],
            period=autax.bas_quarter(date(2026, 5, 1)),
        )
        assert check_gst_consistency(ctx) == []

    def test_skipped_when_not_gst_registered(self) -> None:
        ctx = context(
            invoices=[make_invoice("A", amount="96800.00", issued="2026-05-09", tax="0.00")],
            period=autax.bas_quarter(date(2026, 5, 1)),
            gst_registered=False,
        )
        assert check_gst_consistency(ctx) == []

    def test_skipped_for_non_australian_orgs(self) -> None:
        ctx = context(
            organisation=Organisation(name="UK Ltd", country="GB", base_currency="GBP"),
            invoices=[make_invoice("A", amount="96800.00", issued="2026-05-09", tax="0.00")],
            period=autax.bas_quarter(date(2026, 5, 1)),
        )
        assert check_gst_consistency(ctx) == []


class TestOtherChecks:
    def test_zero_cash_with_receivables_is_a_blocker(self) -> None:
        ctx = context(
            invoices=[make_invoice("A", amount="10000.00")],
            cash_balance=Decimal("0.00"),
        )
        findings = check_cash_vs_receivables(ctx)
        assert len(findings) == 1
        assert findings[0].severity == CRITICAL
        assert "bank feed" in findings[0].action

    def test_no_finding_when_cash_is_present(self) -> None:
        ctx = context(
            invoices=[make_invoice("A", amount="10000.00")],
            cash_balance=Decimal("5000.00"),
        )
        assert check_cash_vs_receivables(ctx) == []

    def test_missing_due_date(self) -> None:
        ctx = context(invoices=[make_invoice("A", amount="23650.00", due="")])
        findings = check_missing_due_dates(ctx)
        assert len(findings) == 1
        assert findings[0].value == Decimal("23650.00")


class TestRunClose:
    def test_a_failing_check_does_not_abort_the_run(self, monkeypatch) -> None:
        import xerobk.close as close_module

        def exploding(ctx):
            raise RuntimeError("boom")

        monkeypatch.setattr(close_module, "CHECKS", (exploding, check_overdue_debt))
        report = run_close(context(invoices=[make_invoice("A", amount="100.00", due="2026-01-01")]))
        titles = [f.title for f in report.findings]
        assert any("failed to run" in title for title in titles)
        assert any("overdue" in title for title in titles)

    def test_findings_sort_blockers_first(self) -> None:
        report = run_close(context(
            invoices=[make_invoice("A", amount="10000.00", due="2026-01-01")],
            cash_balance=Decimal("0.00"),
        ))
        severities = [f.severity for f in report.findings]
        assert severities == sorted(severities, key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s])

    def test_report_serialises(self) -> None:
        report = run_close(context(invoices=[make_invoice("A", amount="100.00")]))
        payload = report.to_dict()
        assert "period" in payload and "findings" in payload
        assert isinstance(payload["counts"]["critical"], int)


class TestAgainstDemoData:
    def test_demo_snapshot_produces_the_planted_findings(self, demo) -> None:
        from xerobk.close import build_context

        ctx = build_context(demo, as_of=AS_OF, frequency="monthly")
        report = run_close(ctx)
        checks = {f.check for f in report.findings}
        # Each of these is deliberately seeded in data/demo/generate.py.
        assert {
            "duplicate_contacts",
            "duplicate_invoices",
            "gst_consistency",
            "missing_due_dates",
            "contacts_missing_email",
            "overdue_debt",
            "debtor_concentration",
        } <= checks

    def test_demo_bills_are_separated_from_sales(self, demo) -> None:
        sales = demo.invoices(InvoiceType.ACCREC)
        bills = demo.invoices(InvoiceType.ACCPAY)
        assert all(i.invoice_type is InvoiceType.ACCREC for i in sales)
        assert all(i.invoice_type is InvoiceType.ACCPAY for i in bills)
        assert len(demo.invoices()) == len(sales) + len(bills)


class TestLiveFinancialYearDerivation:
    """Xero stores only the FY *end* month/day; the year has to be inferred."""

    def test_australian_year_before_the_end_date(self) -> None:
        from xerobk.providers.live import financial_year_bounds

        start, end = financial_year_bounds(6, 30, today=date(2026, 3, 1))
        assert (start, end) == (date(2025, 7, 1), date(2026, 6, 30))

    def test_australian_year_after_the_end_date(self) -> None:
        from xerobk.providers.live import financial_year_bounds

        start, end = financial_year_bounds(6, 30, today=date(2026, 8, 16))
        assert (start, end) == (date(2026, 7, 1), date(2027, 6, 30))

    def test_the_end_date_itself_closes_the_current_year(self) -> None:
        from xerobk.providers.live import financial_year_bounds

        start, end = financial_year_bounds(6, 30, today=date(2026, 6, 30))
        assert (start, end) == (date(2025, 7, 1), date(2026, 6, 30))

    def test_calendar_year_org(self) -> None:
        from xerobk.providers.live import financial_year_bounds

        start, end = financial_year_bounds(12, 31, today=date(2026, 8, 16))
        assert (start, end) == (date(2026, 1, 1), date(2026, 12, 31))

    def test_impossible_month_day_returns_none(self) -> None:
        from xerobk.providers.live import financial_year_bounds

        assert financial_year_bounds(2, 30, today=date(2026, 8, 16)) == (None, None)

    def test_matches_the_real_org_financial_year(self) -> None:
        # The connected AU org reports FY 2026-07-01 -> 2027-06-30 as at Aug 2026.
        from xerobk.providers.live import financial_year_bounds

        start, end = financial_year_bounds(6, 30, today=date(2026, 8, 16))
        assert start == date(2026, 7, 1)
        assert end == date(2027, 6, 30)
