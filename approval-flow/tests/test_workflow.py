"""The approval engine: sequencing, controls, and change detection."""

from __future__ import annotations

import dataclasses

import pytest

from apflow.budget import BREACH
from apflow.config import BUDGET_BLOCK, BUDGET_OFF, Settings
from apflow.matrix import ApprovalMatrix, Rule, StepSpec
from apflow.models import Decision, Quorum, RequestState
from apflow.workflow import Engine, WorkflowError
from tests.conftest import make_document


def test_a_small_bill_is_auto_approved_and_still_logged(engine: Engine) -> None:
    result = engine.submit(make_document(total="120.00"), requested_by="priya")
    assert result.outcome == "auto_approved"
    view = engine.request_view(result.request_id)
    assert view["state"] == str(RequestState.APPROVED)
    assert any(e["kind"] == "auto_approved" for e in view["events"])


def test_steps_run_in_order_and_only_one_is_open(engine: Engine) -> None:
    result = engine.submit(make_document(total="4000.00"), requested_by="priya")
    view = engine.request_view(result.request_id)
    assert [s["state"] for s in view["steps"]] == ["pending", "pending"]

    with pytest.raises(WorkflowError) as excinfo:
        engine.decide(result.request_id, "jo", Decision.APPROVE)
    assert "not their turn" in str(excinfo.value)

    engine.decide(result.request_id, "dana", Decision.APPROVE)
    view = engine.request_view(result.request_id)
    assert [s["state"] for s in view["steps"]] == ["approved", "pending"]


def test_full_approval_closes_the_request(engine: Engine) -> None:
    result = engine.submit(make_document(total="4000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    final = engine.decide(result.request_id, "jo", Decision.APPROVE)
    assert final.request_state is RequestState.APPROVED
    assert "ready to post" in final.message


def test_a_rejection_ends_the_request_and_cancels_later_steps(engine: Engine) -> None:
    result = engine.submit(make_document(total="4000.00"), requested_by="priya")
    outcome = engine.decide(result.request_id, "dana", Decision.REJECT, comment="wrong supplier")
    assert outcome.request_state is RequestState.REJECTED
    view = engine.request_view(result.request_id)
    assert [s["state"] for s in view["steps"]] == ["rejected", "cancelled"]


def test_a_rejection_needs_a_reason(engine: Engine) -> None:
    result = engine.submit(make_document(total="4000.00"), requested_by="priya")
    with pytest.raises(WorkflowError) as excinfo:
        engine.decide(result.request_id, "dana", Decision.REJECT)
    assert "reason" in str(excinfo.value)


def test_the_submitter_cannot_approve_their_own_request(engine: Engine) -> None:
    result = engine.submit(make_document(total="4000.00"), requested_by="dana")
    with pytest.raises(WorkflowError) as excinfo:
        engine.decide(result.request_id, "dana", Decision.APPROVE)
    assert "separation of duties" in str(excinfo.value)


def test_separation_of_duties_can_be_switched_off_deliberately(engine: Engine) -> None:
    engine.settings = dataclasses.replace(engine.settings, separation_of_duties=False)
    result = engine.submit(make_document(total="4000.00"), requested_by="dana")
    assert engine.decide(result.request_id, "dana", Decision.APPROVE).step_closed


def test_an_approver_cannot_clear_above_their_limit(engine: Engine) -> None:
    result = engine.submit(make_document(total="40000.00"), requested_by="priya")
    with pytest.raises(WorkflowError) as excinfo:
        engine.decide(result.request_id, "dana", Decision.APPROVE)
    message = str(excinfo.value)
    assert "limit is" in message
    # The refusal has to say who CAN, or the queue just stops.
    assert "Alex" in message


def test_limits_are_checked_in_base_currency(engine: Engine) -> None:
    """USD 2,000 is inside Priya's AUD 2,500 limit; at 1.52 it is not."""
    matrix = ApprovalMatrix(
        rules=[Rule(name="R", steps=(StepSpec(name="Only step", approvers=["priya", "cfo"]),))]
    )
    engine = dataclasses.replace(engine, matrix=matrix)
    result = engine.submit(
        make_document(total="2000.00", currency="USD", rate="1.52"), requested_by="jo"
    )
    with pytest.raises(WorkflowError) as excinfo:
        engine.decide(result.request_id, "priya", Decision.APPROVE)
    assert "3,040.00" in str(excinfo.value)


def test_an_approver_cannot_approve_spend_going_to_their_own_address(engine: Engine) -> None:
    document = make_document(total="4000.00", contact="Dana Consulting", email="dana@example.com")
    result = engine.submit(document, requested_by="priya")
    with pytest.raises(WorkflowError) as excinfo:
        engine.decide(result.request_id, "dana", Decision.APPROVE)
    assert "counterparty" in str(excinfo.value)


def test_a_stranger_cannot_decide(engine: Engine) -> None:
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    with pytest.raises(WorkflowError) as excinfo:
        engine.decide(result.request_id, "priya", Decision.APPROVE)
    assert "not an approver" in str(excinfo.value)


def test_an_unknown_user_cannot_decide(engine: Engine) -> None:
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    with pytest.raises(WorkflowError):
        engine.decide(result.request_id, "ghost", Decision.APPROVE)


def test_the_same_person_cannot_vote_twice_on_one_version(store, directory, budget) -> None:
    """On an 'all' step the request stays open, so a second vote from the same
    person would otherwise count towards a quorum they are only one of."""
    matrix = ApprovalMatrix(
        rules=[
            Rule(name="Joint",
                 steps=(StepSpec(name="Both", approvers=["dana", "jo"], quorum=Quorum.ALL),))
        ]
    )
    engine = Engine(store=store, matrix=matrix, directory=directory, settings=Settings(),
                    budget=budget)
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    with pytest.raises(WorkflowError) as excinfo:
        engine.decide(result.request_id, "dana", Decision.APPROVE)
    assert "already recorded" in str(excinfo.value)


def test_a_closed_request_cannot_be_decided_again(engine: Engine) -> None:
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    with pytest.raises(WorkflowError) as excinfo:
        engine.decide(result.request_id, "jo", Decision.APPROVE)
    assert "already approved" in str(excinfo.value)


def test_an_all_quorum_step_needs_everyone(store, directory, budget) -> None:
    matrix = ApprovalMatrix(
        rules=[
            Rule(
                name="Joint",
                steps=(StepSpec(name="Both", approvers=["dana", "jo"], quorum=Quorum.ALL),),
            )
        ]
    )
    engine = Engine(store=store, matrix=matrix, directory=directory, settings=Settings(),
                    budget=budget)
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")

    first = engine.decide(result.request_id, "dana", Decision.APPROVE)
    assert first.request_state is RequestState.PENDING
    assert "still needs other approvers" in first.message

    second = engine.decide(result.request_id, "jo", Decision.APPROVE)
    assert second.request_state is RequestState.APPROVED


def test_delegation_records_who_it_was_covered_for(engine: Engine) -> None:
    result = engine.submit(make_document(total="4000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    outcome = engine.decide(result.request_id, "jo", Decision.APPROVE)
    assert outcome.acting_for == "sam"
    view = engine.request_view(result.request_id)
    assert view["steps"][1]["decisions"][0]["acting_for"] == "sam"


# -- change detection ------------------------------------------------------


def test_editing_a_document_invalidates_the_approvals_it_already_had(engine: Engine) -> None:
    original = make_document(number="B-77", total="4000.00")
    result = engine.submit(original, requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)

    edited = make_document(number="B-77", total="4400.00")
    again = engine.submit(edited, requested_by="priya")

    assert again.outcome == "superseded"
    assert again.request_id == result.request_id
    view = engine.request_view(result.request_id)
    assert view["state"] == str(RequestState.PENDING)
    assert [s["state"] for s in view["steps"]] == ["pending", "pending"]
    # The superseded decision is kept and labelled, never deleted.
    decision = view["steps"][0]["decisions"][0]
    assert decision["decision"] == "approve" and decision["current"] is False


def test_an_edit_that_crosses_a_threshold_reroutes_under_a_new_request(engine: Engine) -> None:
    result = engine.submit(make_document(number="B-88", total="4000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)

    again = engine.submit(make_document(number="B-88", total="40000.00"), requested_by="priya")
    assert again.outcome == "rerouted"
    assert again.request_id != result.request_id

    old = engine.request_view(result.request_id)
    new = engine.request_view(again.request_id)
    assert old["state"] == str(RequestState.CANCELLED)
    assert [s["name"] for s in new["steps"]] == ["Budget holder", "Finance", "CFO"]
    assert any(e["kind"] == "superseded_by" for e in old["events"])
    assert any(e["kind"] == "supersedes" for e in new["events"])


def test_an_unchanged_document_is_a_no_op(engine: Engine) -> None:
    document = make_document(number="B-99", total="4000.00")
    first = engine.submit(document, requested_by="priya")
    second = engine.submit(document, requested_by="priya")
    assert second.outcome == "unchanged"
    assert second.request_id == first.request_id


def test_an_already_decided_document_is_not_re_raised(engine: Engine) -> None:
    """A bill stays DRAFT in the ledger between approval and posting, and a
    rejected bill stays DRAFT for good. Neither may come back as pending."""
    document = make_document(number="B-100", total="1000.00")
    first = engine.submit(document, requested_by="priya")
    engine.decide(first.request_id, "dana", Decision.REJECT, comment="not ours")

    again = engine.submit(document, requested_by="priya")
    assert again.outcome == "already_rejected"
    assert again.request_id == first.request_id
    assert len(engine.store.list_requests()) == 1


def test_a_rejected_document_that_is_fixed_gets_a_fresh_request(engine: Engine) -> None:
    first = engine.submit(make_document(number="B-101", total="1000.00"), requested_by="priya")
    engine.decide(first.request_id, "dana", Decision.REJECT, comment="wrong coding")

    fixed = engine.submit(make_document(number="B-101", total="1000.00", account="429"),
                          requested_by="priya")
    assert fixed.outcome == "created"
    assert fixed.request_id != first.request_id
    view = engine.request_view(fixed.request_id)
    assert any(e["kind"] == "resubmitted" for e in view["events"])


def test_editing_an_approved_but_unposted_document_reopens_it(engine: Engine) -> None:
    document = make_document(number="B-102", total="1000.00")
    first = engine.submit(document, requested_by="priya")
    engine.decide(first.request_id, "dana", Decision.APPROVE)
    assert engine.request_view(first.request_id)["state"] == str(RequestState.APPROVED)

    edited = engine.submit(make_document(number="B-102", total="1500.00"), requested_by="priya")
    assert edited.request_id == first.request_id
    assert engine.request_view(first.request_id)["state"] == str(RequestState.PENDING)


# -- budget ----------------------------------------------------------------


def test_a_budget_breach_is_recorded_but_allowed_through_by_default(engine: Engine) -> None:
    result = engine.submit(
        make_document(total="5500.00", account="429", dated="2026-09-05"), requested_by="priya"
    )
    assert result.outcome == "created"
    assert result.budget is not None and result.budget.status == BREACH
    view = engine.request_view(result.request_id)
    assert view["budget_status"] == BREACH


def test_blocking_enforcement_refuses_the_submission(engine: Engine) -> None:
    engine.settings = dataclasses.replace(engine.settings, budget_enforcement=BUDGET_BLOCK)
    result = engine.submit(
        make_document(total="5500.00", account="429", dated="2026-09-05"), requested_by="priya"
    )
    assert result.outcome == "blocked"
    assert not result.ok
    assert engine.store.list_requests() == []


def test_enforcement_off_skips_the_check_entirely(engine: Engine) -> None:
    engine.settings = dataclasses.replace(engine.settings, budget_enforcement=BUDGET_OFF)
    result = engine.submit(make_document(total="5500.00", account="429"), requested_by="priya")
    assert result.budget is None


# -- withdrawal ------------------------------------------------------------


def test_the_requester_can_withdraw(engine: Engine) -> None:
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    engine.cancel(result.request_id, "priya", "duplicate")
    assert engine.request_view(result.request_id)["state"] == str(RequestState.CANCELLED)


def test_an_approver_cannot_make_an_awkward_request_disappear(engine: Engine) -> None:
    """Withdrawing instead of rejecting would keep the decision off the record."""
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    with pytest.raises(WorkflowError) as excinfo:
        engine.cancel(result.request_id, "dana", "rather not")
    assert "only the requester" in str(excinfo.value)


def test_unlimited_authority_can_withdraw(engine: Engine) -> None:
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    engine.cancel(result.request_id, "cfo", "superseded by PO")
    assert engine.request_view(result.request_id)["state"] == str(RequestState.CANCELLED)


# -- notifications ---------------------------------------------------------


def test_a_broken_notifier_cannot_lose_a_decision(engine: Engine) -> None:
    class Exploding:
        def on_opened(self, request_id: int, **kwargs: object) -> None:
            raise RuntimeError("mail server on fire")

        def on_decided(self, request_id: int, **kwargs: object) -> None:
            raise RuntimeError("still on fire")

        def on_closed(self, request_id: int, **kwargs: object) -> None:
            raise RuntimeError("very on fire")

    engine.notifier = Exploding()
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    outcome = engine.decide(result.request_id, "dana", Decision.APPROVE)
    assert outcome.request_state is RequestState.APPROVED
    view = engine.request_view(result.request_id)
    assert any(e["kind"] == "notification_failed" for e in view["events"])
