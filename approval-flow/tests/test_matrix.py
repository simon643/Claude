"""Routing: which workflow a document gets, and which steps of it apply."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from apflow.matrix import ApprovalMatrix, Condition, MatrixError, Rule, StepSpec
from apflow.models import Directory, DocumentKind, Quorum, User
from tests.conftest import make_document

TODAY = date(2026, 9, 20)


def test_empty_condition_matches_anything() -> None:
    assert Condition().matches(make_document())


def test_amount_band_is_half_open_so_bands_cannot_overlap() -> None:
    """0-10,000 and 10,000-50,000 must not both claim a $10,000 bill."""
    lower = Condition(amount_max=Decimal("10000.00"))
    upper = Condition(amount_min=Decimal("10000.00"), amount_max=Decimal("50000.00"))
    document = make_document(total="10000.00")
    assert not lower.matches(document)
    assert upper.matches(document)


def test_amount_conditions_use_base_currency() -> None:
    """USD 9,000 is under a 10,000 limit at face value and over it at 1.52."""
    condition = Condition(amount_min=Decimal("10000.00"))
    assert condition.matches(make_document(total="9000.00", currency="USD", rate="1.52"))
    assert not condition.matches(make_document(total="9000.00", currency="AUD"))


def test_supplier_and_account_conditions_glob(matrix: ApprovalMatrix) -> None:
    assert Condition(suppliers=["acme*"]).matches(make_document(contact="Acme Pty Ltd"))
    assert Condition(accounts=["42*"]).matches(make_document(account="420"))
    assert not Condition(accounts=["300"]).matches(make_document(account="420"))


def test_tracking_condition_matches_category_and_option() -> None:
    document = make_document(tracking={"Department": "Marketing"})
    assert Condition(tracking=["Department:Marketing"]).matches(document)
    assert not Condition(tracking=["Department:Sales"]).matches(document)


def test_purchase_order_condition_distinguishes_both_directions() -> None:
    matched = make_document(purchase_order="PO-1")
    assert Condition(requires_purchase_order=True).matches(matched)
    assert not Condition(requires_purchase_order=False).matches(matched)
    assert Condition(requires_purchase_order=False).matches(make_document())
    assert Condition(requires_purchase_order=True).matches(
        make_document(kind=DocumentKind.PURCHASE_ORDER, number="PO-1")
    )


def test_first_matching_rule_wins(matrix: ApprovalMatrix, directory: Directory) -> None:
    small = matrix.resolve(make_document(total="120.00"), directory, TODAY)
    assert small.rule.name == "Auto" and small.auto_approve

    large = matrix.resolve(make_document(total="4000.00"), directory, TODAY)
    assert large.rule.name == "Spend"


def test_step_conditions_tier_one_workflow(matrix: ApprovalMatrix, directory: Directory) -> None:
    modest = matrix.resolve(make_document(total="1000.00"), directory, TODAY)
    assert [s.name for s in modest.steps] == ["Budget holder"]

    middling = matrix.resolve(make_document(total="4000.00"), directory, TODAY)
    assert [s.name for s in middling.steps] == ["Budget holder", "Finance"]

    large = matrix.resolve(make_document(total="40000.00"), directory, TODAY)
    assert [s.name for s in large.steps] == ["Budget holder", "Finance", "CFO"]
    assert [s.position for s in large.steps] == [1, 2, 3]


def test_skipped_steps_are_reported_not_silently_dropped(
    matrix: ApprovalMatrix, directory: Directory
) -> None:
    resolution = matrix.resolve(make_document(total="1000.00"), directory, TODAY)
    assert {s.name for s in resolution.skipped} == {"Finance", "CFO"}
    assert all(s.reason for s in resolution.skipped)


def test_delegation_is_applied_at_routing_time(
    matrix: ApprovalMatrix, directory: Directory
) -> None:
    resolution = matrix.resolve(make_document(total="4000.00"), directory, TODAY)
    finance = resolution.steps[1]
    assert [a.user_id for a in finance.approvers] == ["jo"]
    assert finance.approvers[0].delegated_from == "sam"


def test_two_approvers_delegating_to_one_person_get_one_vote() -> None:
    """Otherwise an 'all' step would demand two approvals from one human."""
    away = date(2026, 12, 31)
    directory = Directory(
        [
            User(user_id="a", away_until=away, delegate_to="cover"),
            User(user_id="b", away_until=away, delegate_to="cover"),
            User(user_id="cover"),
        ]
    )
    matrix = ApprovalMatrix(
        rules=[Rule(name="R", steps=(StepSpec(name="S", approvers=["a", "b"], quorum=Quorum.ALL),))]
    )
    resolution = matrix.resolve(make_document(), directory, TODAY)
    assert [a.user_id for a in resolution.steps[0].approvers] == ["cover"]


def test_unroutable_document_raises_rather_than_passing(directory: Directory) -> None:
    matrix = ApprovalMatrix(
        rules=[Rule(name="Invoices only", condition=Condition(kinds=["invoice"]),
                    steps=(StepSpec(name="S", approvers=["jo"]),))]
    )
    with pytest.raises(MatrixError) as excinfo:
        matrix.resolve(make_document(), directory, TODAY)
    assert "catch-all" in str(excinfo.value)


def test_validation_flags_a_missing_catch_all(directory: Directory) -> None:
    matrix = ApprovalMatrix(
        rules=[Rule(name="Bills", condition=Condition(kinds=["bill"]),
                    steps=(StepSpec(name="S", approvers=["jo"]),))]
    )
    assert any("catch-all" in p for p in matrix.validate(directory))


def test_validation_flags_a_shadowed_rule(directory: Directory) -> None:
    matrix = ApprovalMatrix(
        rules=[
            Rule(name="Everything", steps=(StepSpec(name="S", approvers=["jo"]),)),
            Rule(name="Never reached", condition=Condition(kinds=["bill"]),
                 steps=(StepSpec(name="S", approvers=["jo"]),)),
        ]
    )
    assert any("shadows" in p for p in matrix.validate(directory))


def test_validation_flags_a_step_nobody_can_clear(directory: Directory) -> None:
    """An unbounded rule staffed only by limited approvers stalls on the first
    big bill — the sort of thing found on a Friday afternoon otherwise."""
    matrix = ApprovalMatrix(
        rules=[Rule(name="All spend", steps=(StepSpec(name="Approve", approvers=["priya"]),))]
    )
    assert any("high enough limit" in p for p in matrix.validate(directory))


def test_validation_flags_unknown_and_inactive_approvers(directory: Directory) -> None:
    matrix = ApprovalMatrix(
        rules=[
            Rule(name="R", steps=(StepSpec(name="S", approvers=["nobody", "gone", "cfo"]),)),
        ]
    )
    problems = matrix.validate(directory)
    assert any("unknown user" in p for p in problems)
    assert any("inactive" in p for p in problems)


def test_validation_flags_an_impossible_band(directory: Directory) -> None:
    matrix = ApprovalMatrix(
        rules=[
            Rule(
                name="Backwards",
                condition=Condition(amount_min=Decimal("100"), amount_max=Decimal("50")),
                steps=(StepSpec(name="S", approvers=["cfo"]),),
            ),
            Rule(name="Catch", steps=(StepSpec(name="S", approvers=["cfo"]),)),
        ]
    )
    assert any("never match" in p for p in matrix.validate(directory))


def test_a_rule_with_no_steps_and_no_auto_approve_is_rejected() -> None:
    with pytest.raises(MatrixError):
        Rule.from_dict({"name": "Empty", "steps": []})


def test_round_trips_through_json(tmp_path, directory: Directory) -> None:
    original = ApprovalMatrix.starter()
    path = original.save(tmp_path / "matrix.json")
    reloaded = ApprovalMatrix.load(path)
    assert reloaded.as_dict() == original.as_dict()


def test_a_missing_matrix_says_how_to_make_one(tmp_path) -> None:
    with pytest.raises(MatrixError) as excinfo:
        ApprovalMatrix.load(tmp_path / "nope.json")
    assert "matrix --init" in str(excinfo.value)


def test_shipped_demo_matrix_is_valid() -> None:
    from tests.conftest import DEMO_DIR

    matrix = ApprovalMatrix.load(DEMO_DIR / "matrix.json")
    people = Directory.load(DEMO_DIR / "users.json")
    assert matrix.validate(people) == []
