"""Documents, fingerprints, and the user directory."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from apflow.models import Directory, Document, DocumentKind, User, parse_date
from tests.conftest import make_document


def test_parses_both_date_shapes_xero_emits() -> None:
    assert parse_date("2026-09-05") == date(2026, 9, 5)
    assert parse_date("/Date(1789171200000+0000)/") is not None
    assert parse_date("") is None
    assert parse_date("not a date") is None


def test_base_amount_applies_the_booked_rate() -> None:
    document = make_document(total="9000.00", currency="USD", rate="1.52")
    assert document.base_amount == Decimal("13680.00")


def test_base_amount_survives_a_missing_rate() -> None:
    document = make_document(total="100.00", rate="0")
    assert document.base_amount == Decimal("100.00")


def test_fingerprint_is_stable_across_a_store_round_trip() -> None:
    """The bug this guards: as_dict() once dropped currency_rate, so every
    foreign-currency document read back from the store looked tampered with."""
    document = make_document(total="9000.00", currency="USD", rate="1.52")
    restored = Document.from_dict(document.as_dict())
    assert restored.fingerprint == document.fingerprint
    assert restored.base_amount == document.base_amount


def test_fingerprint_moves_when_anything_material_moves() -> None:
    base = make_document()
    for changed in (
        make_document(total="1200.00"),
        make_document(contact="Other Pty Ltd"),
        make_document(due="2026-11-05"),
        make_document(account="429"),
        make_document(reference="PO-9"),
    ):
        assert changed.fingerprint != base.fingerprint


def test_fingerprint_ignores_ledger_status() -> None:
    """Approving a bill changes its status. That must not read as tampering."""
    import dataclasses

    from apflow.models import DocumentStatus

    base = make_document()
    authorised = dataclasses.replace(base, status=DocumentStatus.AUTHORISED)
    assert authorised.fingerprint == base.fingerprint


def test_describe_change_names_what_moved() -> None:
    before = make_document(total="1100.00")
    after = make_document(total="7700.00", account="429")
    changes = after.describe_change(before)
    assert any("total" in c for c in changes)
    assert any("coding" in c for c in changes)


def test_kind_parsing_accepts_xero_vocabulary() -> None:
    assert DocumentKind.parse("ACCPAY") is DocumentKind.BILL
    assert DocumentKind.parse("ACCREC") is DocumentKind.INVOICE
    assert DocumentKind.parse("purchase-order") is DocumentKind.PURCHASE_ORDER
    assert DocumentKind.BILL.is_spend and not DocumentKind.INVOICE.is_spend


def test_directory_routes_around_an_absent_approver(directory: Directory) -> None:
    effective, delegated_from = directory.resolve_approver("sam", date(2026, 9, 20))
    assert effective.user_id == "jo"
    assert delegated_from == "sam"


def test_directory_leaves_a_present_approver_alone(directory: Directory) -> None:
    effective, delegated_from = directory.resolve_approver("dana", date(2026, 9, 20))
    assert effective.user_id == "dana"
    assert delegated_from == ""


def test_delegation_cycle_does_not_hang() -> None:
    away = date(2026, 12, 31)
    directory = Directory(
        [
            User(user_id="a", away_until=away, delegate_to="b"),
            User(user_id="b", away_until=away, delegate_to="a"),
        ]
    )
    effective, _ = directory.resolve_approver("a", date(2026, 9, 20))
    assert effective.user_id in {"a", "b"}


def test_unknown_user_names_the_known_ones(directory: Directory) -> None:
    with pytest.raises(KeyError) as excinfo:
        directory.require("nobody")
    assert "dana" in str(excinfo.value)


def test_limits_are_absolute_so_credits_cannot_slip_through() -> None:
    user = User(user_id="x", approval_limit=Decimal("100.00"))
    assert user.within_limit(Decimal("100.00"))
    assert not user.within_limit(Decimal("-250.00"))
