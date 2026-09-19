"""Storage invariants: the ones an auditor relies on."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apflow.models import Decision, RequestState
from apflow.store import Store, StoreError
from apflow.workflow import Engine
from tests.conftest import make_document


def _steps() -> list[dict]:
    return [{"position": 1, "name": "Only", "quorum": "any", "sla_days": 2,
             "approvers": [{"user_id": "dana"}]}]


def test_one_live_request_per_document(store: Store) -> None:
    """Two pending requests for one bill would let two approvers each clear a
    different copy of the same spend."""
    document = make_document()
    store.create_request(document, "R", "priya", _steps())
    with pytest.raises(StoreError):
        store.create_request(document, "R", "priya", _steps())


def test_a_closed_request_frees_the_document(store: Store) -> None:
    document = make_document()
    first = store.create_request(document, "R", "priya", _steps())
    store.set_request_state(first, RequestState.REJECTED)
    second = store.create_request(document, "R", "priya", _steps())
    assert second != first


def test_a_request_and_its_steps_are_written_atomically(store: Store) -> None:
    """A request row with nobody attached to decide it is worse than none, so a
    failure part-way through writing the steps must take the request with it."""
    with pytest.raises(KeyError):
        store.create_request(
            make_document(),
            "R",
            "priya",
            [{"name": "Missing its position", "approvers": [{"user_id": "dana"}]}],
        )
    assert store.list_requests() == []


def test_decisions_are_append_only(store: Store) -> None:
    request_id = store.create_request(make_document(), "R", "priya", _steps())
    step_id = int(store.steps_for(request_id)[0]["id"])
    store.record_decision(request_id, step_id, "dana", Decision.APPROVE, "fp1", comment="yes")
    store.record_decision(request_id, step_id, "dana", Decision.REJECT, "fp2", comment="no")
    assert [d["decision"] for d in store.decisions_for(request_id)] == ["approve", "reject"]
    assert [d["decision"] for d in store.decisions_for(request_id, "fp1")] == ["approve"]


def test_totals_are_decimal_not_float(store: Store) -> None:
    store.create_request(make_document(total="0.10"), "R", "priya", _steps())
    store.create_request(make_document(number="B-2", total="0.20"), "R", "priya", _steps())
    total = store.totals_by_state()["pending"]["total"]
    assert isinstance(total, Decimal)
    assert total == Decimal("0.30")


def test_references_resolve_several_ways(store: Store) -> None:
    request_id = store.create_request(make_document(number="B-500"), "R", "priya", _steps())
    assert store.resolve_reference(f"AR-{request_id:04d}") == request_id
    assert store.resolve_reference(str(request_id)) == request_id
    assert store.resolve_reference("B-500") == request_id
    assert store.resolve_reference("doc-B-500") == request_id
    assert store.resolve_reference("nonsense") is None


def test_an_inbox_hides_steps_whose_turn_has_not_come(engine: Engine) -> None:
    engine.submit(make_document(total="4000.00"), requested_by="priya")
    assert len(engine.store.pending_steps_for_user("dana")) == 1
    assert engine.store.pending_steps_for_user("jo") == []


def test_an_inbox_stops_nagging_someone_who_has_voted(store, directory, budget) -> None:
    from apflow.config import Settings
    from apflow.matrix import ApprovalMatrix, Rule, StepSpec
    from apflow.models import Quorum

    matrix = ApprovalMatrix(
        rules=[Rule(name="Joint",
                    steps=(StepSpec(name="Both", approvers=["dana", "jo"], quorum=Quorum.ALL),))]
    )
    engine = Engine(store=store, matrix=matrix, directory=directory, settings=Settings(),
                    budget=budget)
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    assert store.pending_steps_for_user("dana") == []
    assert len(store.pending_steps_for_user("jo")) == 1


def test_a_write_is_logged_before_it_is_sent(store: Store) -> None:
    audit_id = store.begin_write("set_status:bill", "doc-1", {"status": "AUTHORISED"})
    assert store.audit_tail()[0]["outcome"] == "pending"
    store.finish_write(audit_id, "ok", "done")
    assert store.audit_tail()[0]["outcome"] == "ok"
