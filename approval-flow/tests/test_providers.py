"""The provider layer, against the bundled snapshot."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apflow.models import DocumentKind
from apflow.providers import ProviderError, ReadOnlyError
from apflow.providers.factory import demo_snapshot_path, describe_source, get_provider
from apflow.providers.snapshot import SnapshotProvider


def test_reads_the_organisation(demo: SnapshotProvider) -> None:
    org = demo.organisation()
    assert org.name == "Harbourview Systems Pty Ltd"
    assert org.base_currency == "AUD"


def test_reads_bills_with_their_lines(demo: SnapshotProvider) -> None:
    bills = demo.documents(DocumentKind.BILL)
    assert bills
    bill = next(b for b in bills if b.number == "B-2203")
    assert bill.contact.name == "Harbourline Consulting"
    assert bill.amount_total == Decimal("38500.00")
    assert bill.account_codes == ("300",)
    assert bill.kind is DocumentKind.BILL


def test_a_foreign_currency_bill_keeps_its_rate(demo: SnapshotProvider) -> None:
    bill = next(b for b in demo.documents(DocumentKind.BILL) if b.number == "B-2206")
    assert bill.currency_code == "USD"
    assert bill.base_amount == Decimal("13680.00")


def test_tracking_is_parsed(demo: SnapshotProvider) -> None:
    bill = next(b for b in demo.documents(DocumentKind.BILL) if b.number == "B-2202")
    assert bill.tracking_pairs == ("Department:Marketing",)


def test_status_filtering(demo: SnapshotProvider) -> None:
    assert demo.documents(DocumentKind.BILL, status="DRAFT")
    assert demo.documents(DocumentKind.BILL, status="PAID") == []


def test_invoices_and_purchase_orders_are_separate_kinds(demo: SnapshotProvider) -> None:
    assert all(d.kind is DocumentKind.INVOICE for d in demo.documents(DocumentKind.INVOICE))
    orders = demo.documents(DocumentKind.PURCHASE_ORDER)
    assert orders and orders[0].purchase_order_number == "PO-0031"


def test_a_snapshot_refuses_to_pretend_it_wrote_to_the_ledger(demo: SnapshotProvider) -> None:
    bill = demo.documents(DocumentKind.BILL)[0]
    with pytest.raises(ReadOnlyError):
        demo.set_document_status(bill, "AUTHORISED")


def test_a_missing_snapshot_directory_is_an_error(tmp_path) -> None:
    with pytest.raises(ProviderError):
        SnapshotProvider(tmp_path / "nope")


def test_malformed_json_names_the_file(tmp_path) -> None:
    (tmp_path / "bills.json").write_text("{not json", encoding="utf-8")
    provider = SnapshotProvider(tmp_path)
    with pytest.raises(ProviderError) as excinfo:
        provider.documents(DocumentKind.BILL)
    assert "bills.json" in str(excinfo.value)


def test_an_empty_snapshot_reads_as_empty(tmp_path) -> None:
    provider = SnapshotProvider(tmp_path)
    assert provider.documents(DocumentKind.BILL) == []
    assert provider.contacts() == []


def test_the_factory_falls_back_to_the_demo_org() -> None:
    provider = get_provider()
    assert provider.name.startswith("snapshot:")
    assert "read-only" in describe_source(provider)
    assert demo_snapshot_path().is_dir()


def test_documents_round_trip_through_the_store_unchanged(demo: SnapshotProvider) -> None:
    """A fingerprint that shifted on a store round trip would make every
    document look edited the moment it was read back."""
    from apflow.models import Document

    for kind in (DocumentKind.BILL, DocumentKind.INVOICE, DocumentKind.PURCHASE_ORDER):
        for document in demo.documents(kind):
            assert Document.from_dict(document.as_dict()).fingerprint == document.fingerprint
