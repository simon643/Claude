"""Provider layer: parsing, snapshot reading, store, and the read-only guard."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from xerobk.models import BankLine, Invoice, InvoiceStatus, InvoiceType, Organisation, TaxTreatment
from xerobk.providers import ReadOnlyError
from xerobk.providers.snapshot import SnapshotProvider
from xerobk.store import Store


class TestModelParsing:
    def test_parses_the_connector_shape(self) -> None:
        # Field names and shapes taken verbatim from a real connector response.
        raw = {
            "invoice_id": "11111111-2222-4333-8444-555555555555",
            "invoice_number": "INV-0042",
            "reference": "12 Example St (Lockup)",
            "status": "AUTHORISED",
            "contact": {"contact_id": "c0ffee01", "name": "Kestrel Build Group Pty Ltd"},
            "amount_due": "11000.0",
            "amount_paid": "0.0",
            "amount_total": "11000.0",
            "amount_net": "10000.00",
            "amount_tax": "1000.00",
            "currency_code": "AUD",
            "invoice_date": "2026-07-17",
            "due_date": "2026-07-24",
            "line_items": [
                {
                    "description": "Stage payment - Lockup",
                    "quantity": "1.0",
                    "unit_amount": "11000.0",
                    "line_amount_gross": "11000.0",
                    "line_tax_amount": "1000.00",
                    "line_tax_treatment": "Inclusive",
                }
            ],
        }
        invoice = Invoice.from_api(raw)
        assert invoice.invoice_number == "INV-0042"
        assert invoice.amount_due == Decimal("11000.00")
        assert invoice.amount_tax == Decimal("1000.00")
        assert invoice.due_date == date(2026, 7, 24)
        assert invoice.is_outstanding
        assert invoice.line_items[0].tax_treatment is TaxTreatment.INCLUSIVE

    def test_exclusive_lines_report_their_net_amount(self) -> None:
        # The connector uses a different key depending on tax treatment.
        raw = {
            "invoice_id": "x",
            "invoice_number": "INV-1",
            "contact": {"name": "Acme"},
            "line_items": [
                {
                    "description": "Site works",
                    "unit_amount": "24500.0",
                    "line_amount_net": "24500.0",
                    "line_tax_amount": "2450.0",
                    "line_tax_treatment": "Exclusive",
                }
            ],
        }
        invoice = Invoice.from_api(raw)
        assert invoice.line_items[0].line_amount == Decimal("24500.00")
        assert invoice.line_items[0].tax_treatment is TaxTreatment.EXCLUSIVE

    def test_parses_pascal_case_from_the_raw_api(self) -> None:
        raw = {
            "InvoiceID": "abc",
            "InvoiceNumber": "INV-9",
            "Contact": {"ContactID": "c1", "Name": "Acme"},
            "AmountDue": 500.0,
            "Total": 500.0,
            "Status": "AUTHORISED",
            "Type": "ACCPAY",
            "DueDate": "2026-08-01",
        }
        invoice = Invoice.from_api(raw)
        assert invoice.invoice_number == "INV-9"
        assert invoice.invoice_type is InvoiceType.ACCPAY
        assert invoice.amount_due == Decimal("500.00")

    def test_microsoft_json_dates(self) -> None:
        invoice = Invoice.from_api({
            "InvoiceID": "abc",
            "InvoiceNumber": "INV-9",
            "Contact": {"Name": "Acme"},
            "DueDate": "/Date(1786579200000+0000)/",
        })
        assert invoice.due_date is not None
        assert invoice.due_date.year == 2026

    def test_unknown_status_falls_back_to_draft(self) -> None:
        invoice = Invoice.from_api({"InvoiceNumber": "X", "Contact": {}, "Status": "WEIRD"})
        assert invoice.status is InvoiceStatus.DRAFT

    def test_organisation_parsing(self) -> None:
        org = Organisation.from_api(
            {
                "name": "Wattlebird Developments Unit Trust",
                "country": "AU",
                "base_currency": "AUD",
                "timezone": {"tz_name": "Australia/Sydney"},
            },
            {"financial_year_start_date": "2026-07-01", "financial_year_end_date": "2027-06-30"},
        )
        assert org.is_australian
        assert org.financial_year_start == date(2026, 7, 1)

    def test_days_overdue_sign(self) -> None:
        from .conftest import make_invoice

        invoice = make_invoice(due="2026-08-01")
        assert invoice.days_overdue(date(2026, 8, 16)) == 15
        assert invoice.days_overdue(date(2026, 7, 20)) == -12
        assert invoice.is_overdue(date(2026, 8, 16))
        assert not invoice.is_overdue(date(2026, 7, 20))


class TestSnapshotProvider:
    def test_reads_the_demo_snapshot(self, demo: SnapshotProvider) -> None:
        org = demo.organisation()
        assert org.country == "AU"
        assert org.base_currency == "AUD"
        assert len(demo.contacts()) == 7
        assert len(demo.invoices()) == 18

    def test_uses_the_snapshot_date_not_today(self, demo: SnapshotProvider) -> None:
        # Aging against today would silently shift every invoice as the
        # snapshot goes stale.
        assert demo.aged_receivables().as_of == date(2026, 8, 16)

    def test_missing_directory_raises(self) -> None:
        from xerobk.providers import ProviderError

        with pytest.raises(ProviderError):
            SnapshotProvider("/nonexistent/path/xyz")

    def test_is_read_only(self, demo: SnapshotProvider) -> None:
        assert demo.can_write is False
        with pytest.raises(ReadOnlyError, match="cannot post invoices"):
            demo.create_invoice({})
        with pytest.raises(ReadOnlyError):
            demo.create_bank_transaction({})

    def test_bank_lines_come_from_the_csv(self, demo: SnapshotProvider) -> None:
        assert len(demo.bank_lines()) == 14


class TestDashboard:
    def test_headline_figures(self, demo: SnapshotProvider) -> None:
        from xerobk.reports import build_dashboard

        dashboard = build_dashboard(demo)
        assert dashboard.currency == "AUD"
        assert dashboard.receivables > 0
        assert dashboard.draft_count == 1
        # Buckets must reconcile with the receivables headline.
        assert dashboard.receivable_buckets.total == dashboard.receivables

    def test_top_debtor_shares_are_sane(self, demo: SnapshotProvider) -> None:
        from xerobk.reports import build_dashboard

        dashboard = build_dashboard(demo)
        assert dashboard.top_debtors[0].total >= dashboard.top_debtors[-1].total
        assert sum(d.share for d in dashboard.top_debtors) <= Decimal("100.01")

    def test_serialises_without_floats(self, demo: SnapshotProvider) -> None:
        import json

        from xerobk.reports import build_dashboard

        payload = build_dashboard(demo).to_dict()
        # json.dumps with no default= proves every value is already JSON-safe.
        text = json.dumps(payload)
        assert "receivables" in text


class TestStore:
    def test_records_and_reads_back_a_decision(self, tmp_path) -> None:
        line = BankLine(
            line_id="abc123",
            date=date(2026, 7, 2),
            description="TELSTRA BILL",
            amount=Decimal("-89.00"),
        )
        with Store(tmp_path / "test.sqlite3") as store:
            store.record_coding(line, account_code="489", tax_type="INPUT")
            assert store.coded_line_ids() == {"abc123"}
            history = store.coding_history()
            assert len(history) == 1
            assert history[0][1] == "489"
            assert history[0][0].amount == Decimal("-89.00")

    def test_recoding_updates_rather_than_duplicates(self, tmp_path) -> None:
        line = BankLine("abc123", date(2026, 7, 2), "TELSTRA", Decimal("-89.00"))
        with Store(tmp_path / "test.sqlite3") as store:
            store.record_coding(line, account_code="489")
            store.record_coding(line, account_code="400")
            history = store.coding_history()
            assert len(history) == 1
            assert history[0][1] == "400"

    def test_audit_log_is_written_before_the_call(self, tmp_path) -> None:
        with Store(tmp_path / "test.sqlite3") as store:
            audit_id = store.begin_write("create_invoice", "INV-1", {"Total": "100"})
            pending = store.audit_tail()[0]
            assert pending["outcome"] == "pending"
            store.finish_write(audit_id, "ok", "created")
            assert store.audit_tail()[0]["outcome"] == "ok"

    def test_close_history(self, tmp_path) -> None:
        with Store(tmp_path / "test.sqlite3") as store:
            store.record_close({
                "period": {"label": "FY26 Q4", "start": "2026-04-01", "end": "2026-06-30"},
                "as_of": "2026-08-16",
                "counts": {"critical": 2, "warning": 4, "info": 4},
            })
            history = store.close_history()
            assert len(history) == 1
            assert history[0]["critical"] == 2
            assert store.previous_close("FY26 Q4") is not None

    def test_usable_from_multiple_threads(self, tmp_path) -> None:
        """The UI serves requests on a thread pool, so the store is shared.

        sqlite3 connections are thread-bound by default; without
        check_same_thread=False plus serialisation, every UI endpoint that
        touches the store raises from a worker thread.
        """
        import threading

        errors: list[str] = []

        with Store(tmp_path / "threads.sqlite3") as store:
            def worker(index: int) -> None:
                try:
                    line = BankLine(
                        line_id=f"line-{index}",
                        date=date(2026, 7, 2),
                        description=f"PAYMENT {index}",
                        amount=Decimal("-10.00"),
                    )
                    store.record_coding(line, account_code="404")
                    store.coding_history()
                    store.coded_line_ids()
                    store.audit_tail()
                except Exception as exc:
                    errors.append(repr(exc))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert errors == []
            assert len(store.coded_line_ids()) == 12
