"""Budget periods, allocation, and commitment accounting."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from apflow.budget import (
    ANNUAL,
    BREACH,
    NO_BUDGET,
    OK,
    QUARTERLY,
    WARN,
    Budget,
    BudgetError,
    BudgetLine,
    commitments_from_store,
    period_key,
)
from apflow.models import DocumentKind
from apflow.money import ZERO
from tests.conftest import make_document


def test_monthly_period_key() -> None:
    assert period_key(date(2026, 9, 5)) == "2026-09"
    assert period_key(None) == ""


def test_annual_period_follows_the_financial_year_not_the_calendar() -> None:
    """July-December must not be filed against the previous financial year."""
    assert period_key(date(2026, 8, 1), ANNUAL) == "FY2027"
    assert period_key(date(2026, 6, 30), ANNUAL) == "FY2026"


def test_quarters_start_at_the_financial_year_start() -> None:
    assert period_key(date(2026, 7, 15), QUARTERLY) == "FY2027-Q1"
    assert period_key(date(2026, 10, 15), QUARTERLY) == "FY2027-Q2"
    assert period_key(date(2026, 6, 15), QUARTERLY) == "FY2026-Q4"


def test_allocation_is_ex_tax_because_gst_is_recoverable(budget: Budget) -> None:
    document = make_document(total="1100.00", account="420", dated="2026-09-05")
    allocated = budget.allocate(document)
    assert allocated[("2026-09", "420", "")] == Decimal("1000.00")


def test_allocation_converts_to_base_currency(budget: Budget) -> None:
    document = make_document(total="1100.00", account="420", currency="USD", rate="1.50",
                             dated="2026-09-05")
    assert budget.allocate(document)[("2026-09", "420", "")] == Decimal("1500.00")


def test_a_tracking_specific_line_wins_over_the_account_wide_one() -> None:
    budget = Budget(
        lines=[
            BudgetLine(period="2026-09", account="420", amount=Decimal("10000.00")),
            BudgetLine(period="2026-09", account="420", tracking="Department:Marketing",
                       amount=Decimal("2000.00")),
        ]
    )
    document = make_document(total="1100.00", account="420", dated="2026-09-05",
                             tracking={"Department": "Marketing"})
    allocated = budget.allocate(document)
    assert ("2026-09", "420", "Department:Marketing") in allocated


def test_within_budget_is_ok(budget: Budget) -> None:
    check = budget.check(make_document(total="1100.00", account="420", dated="2026-09-05"))
    assert check.status == OK
    assert check.impacts[0].remaining_after == Decimal("9000.00")


def test_spending_the_budget_exactly_is_not_a_breach(budget: Budget) -> None:
    """11,000 gross is 10,000 net — the budget to the cent. A breach is an
    overspend, and off-by-one here would reject a correct payment."""
    check = budget.check(make_document(total="11000.00", account="420", dated="2026-09-05"))
    assert check.status == WARN
    assert check.impacts[0].remaining_after == ZERO


def test_near_the_line_warns(budget: Budget) -> None:
    warn = budget.check(make_document(total="10450.00", account="420", dated="2026-09-05"))
    assert warn.status == WARN
    assert "95" in warn.message


def test_commitments_consume_headroom(budget: Budget) -> None:
    """Two approvers must not each spend the same remaining budget."""
    document = make_document(total="5500.00", account="420", dated="2026-09-05")
    assert budget.check(document).status == OK
    committed = {("2026-09", "420", ""): Decimal("6000.00")}
    breached = budget.check(document, committed)
    assert breached.status == BREACH
    assert "over" in breached.message


def test_an_unbudgeted_account_is_reported_not_silently_passed(budget: Budget) -> None:
    check = budget.check(make_document(total="1100.00", account="999", dated="2026-09-05"))
    assert check.status == NO_BUDGET
    assert check.unbudgeted and "999" in check.unbudgeted[0]


def test_no_budget_configured_is_not_a_failure() -> None:
    assert Budget().check(make_document()).status == NO_BUDGET


def test_commitments_count_pending_and_approved_only(engine, budget: Budget) -> None:
    engine.submit(make_document(number="B-1", total="1100.00", account="420", dated="2026-09-05"))
    result = engine.submit(
        make_document(number="B-2", total="2200.00", account="420", dated="2026-09-05")
    )
    engine.cancel(result.request_id, "cfo", "not needed")
    totals = commitments_from_store(budget, engine.store)
    assert totals[("2026-09", "420", "")] == Decimal("1000.00")


def test_commitments_ignore_money_coming_in(engine, budget: Budget) -> None:
    engine.submit(
        make_document(number="INV-1", kind=DocumentKind.INVOICE, total="5500.00",
                      account="420", dated="2026-09-05")
    )
    assert commitments_from_store(budget, engine.store) == {}


def test_excluding_a_request_stops_it_counting_against_itself(engine, budget: Budget) -> None:
    result = engine.submit(
        make_document(number="B-9", total="1100.00", account="420", dated="2026-09-05")
    )
    totals = commitments_from_store(budget, engine.store, exclude_request_id=result.request_id)
    assert totals == {}


def test_round_trips_through_json(tmp_path, budget: Budget) -> None:
    path = budget.save(tmp_path / "budget.json")
    assert Budget.load(path).as_dict() == budget.as_dict()


def test_a_missing_budget_file_is_an_empty_budget(tmp_path) -> None:
    assert not Budget.load(tmp_path / "nope.json")


def test_a_malformed_budget_line_is_rejected() -> None:
    with pytest.raises(BudgetError):
        BudgetLine.from_dict({"amount": "100.00"})
