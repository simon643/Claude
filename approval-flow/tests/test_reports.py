"""Queue, turnaround, and spend reporting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apflow.models import Decision, DocumentKind
from apflow.reports import build_queue, median, overview, spend_under_approval, turnaround
from apflow.workflow import Engine
from tests.conftest import make_document


def test_median_handles_both_parities_and_empty() -> None:
    assert median([]) == 0.0
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_the_queue_shows_only_the_step_that_is_open(engine: Engine) -> None:
    engine.submit(make_document(total="4000.00"), requested_by="priya")
    queue = build_queue(engine.store, engine.directory)
    assert len(queue.entries) == 1
    assert queue.entries[0].step_name == "Budget holder"
    assert queue.entries[0].waiting_on == ["Dana"]


def test_the_queue_follows_the_request_as_it_advances(engine: Engine) -> None:
    result = engine.submit(make_document(total="4000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    entry = build_queue(engine.store, engine.directory).entries[0]
    assert entry.step_name == "Finance"
    assert entry.waiting_on == ["Jo"]


def test_a_closed_request_leaves_the_queue(engine: Engine) -> None:
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    assert build_queue(engine.store, engine.directory).entries == []


def test_sla_breach_is_measured_against_the_step_not_the_request(engine: Engine) -> None:
    engine.submit(make_document(total="1000.00"), requested_by="priya")
    later = datetime.now(UTC) + timedelta(days=5)
    queue = build_queue(engine.store, engine.directory, now=later)
    assert queue.breached and queue.entries[0].age_days > 4


def test_a_step_with_no_sla_cannot_breach_one(store, directory, budget) -> None:
    from apflow.config import Settings
    from apflow.matrix import ApprovalMatrix, Rule, StepSpec

    matrix = ApprovalMatrix(
        rules=[Rule(name="Untimed", steps=(StepSpec(name="Whenever", approvers=["cfo"]),))]
    )
    engine = Engine(store=store, matrix=matrix, directory=directory, settings=Settings(),
                    budget=budget)
    engine.submit(make_document(total="1000.00"), requested_by="priya")
    later = datetime.now(UTC) + timedelta(days=90)
    entry = build_queue(engine.store, engine.directory, now=later).entries[0]
    assert entry.sla_days == 0 and entry.age_days > 89
    assert not entry.breached


def test_workload_is_per_approver_not_a_partition(engine: Engine) -> None:
    engine.submit(make_document(number="B-1", total="1000.00"), requested_by="priya")
    engine.submit(make_document(number="B-2", total="2000.00"), requested_by="priya")
    workload = build_queue(engine.store, engine.directory).by_approver()
    assert workload["Dana"]["count"] == 2
    assert workload["Dana"]["total"] == Decimal("3000.00")


def test_turnaround_measures_from_when_the_step_opened(engine: Engine) -> None:
    result = engine.submit(make_document(total="4000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    engine.decide(result.request_id, "jo", Decision.REJECT, comment="no")
    data = turnaround(engine.store, engine.directory)
    assert data["decisions"] == 2
    by_label = {a["label"]: a for a in data["approvers"]}
    assert by_label["Dana"]["approvals"] == 1
    assert by_label["Jo"]["rejections"] == 1
    assert by_label["Jo"]["rejection_rate"] == "100.00"


def test_spend_counts_money_going_out_only(engine: Engine) -> None:
    engine.submit(make_document(number="B-1", total="1100.00", account="420"),
                  requested_by="priya")
    engine.submit(
        make_document(number="INV-1", kind=DocumentKind.INVOICE, total="5500.00", account="200"),
        requested_by="priya",
    )
    data = spend_under_approval(engine.store)
    assert data["total"] == "1100.00"
    assert list(data["by_account"]) == ["420"]


def test_overview_separates_pending_from_ready_to_post(engine: Engine) -> None:
    engine.submit(make_document(number="B-1", total="120.00"), requested_by="priya")  # auto
    engine.submit(make_document(number="B-2", total="4000.00"), requested_by="priya")
    summary = overview(engine.store, engine.directory)
    assert summary["pending"]["count"] == 1
    assert summary["ready_to_post"]["count"] == 1
    assert summary["ready_to_post"]["total"] == "120.00"


def test_overview_surfaces_documents_changed_after_submission(engine: Engine) -> None:
    engine.submit(make_document(number="B-5", total="1000.00"), requested_by="priya")
    engine.submit(make_document(number="B-5", total="1500.00"), requested_by="priya")
    summary = overview(engine.store, engine.directory)
    assert summary["recent_changes"]
    assert summary["recent_changes"][0]["kind"] == "document_changed"


def test_budget_flags_reach_the_overview(engine: Engine) -> None:
    engine.submit(make_document(total="5500.00", account="429", dated="2026-09-05"),
                  requested_by="priya")
    assert overview(engine.store, engine.directory)["budget_flags"] == ["AR-0001"]
