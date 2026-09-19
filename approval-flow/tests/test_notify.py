"""Notifications and chasing."""

from __future__ import annotations

from pathlib import Path

from apflow.config import Settings
from apflow.models import Decision, StepState
from apflow.notify import ESCALATION, REMINDER, Notifier
from apflow.store import Store
from apflow.workflow import Engine
from tests.conftest import make_document


def _notifier(engine: Engine, tmp_path: Path) -> Notifier:
    notifier = Notifier(engine.store, engine.directory, Settings(), outbox=tmp_path / "outbox")
    engine.notifier = notifier
    return notifier


def _age_step(store: Store, request_id: int, days: float) -> None:
    """Backdate the open step so reminders have something to find."""
    from datetime import UTC, datetime, timedelta

    when = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    step = next(s for s in store.steps_for(request_id) if s["state"] == str(StepState.PENDING))
    with store._tx() as conn:
        conn.execute("UPDATE steps SET opened_at=? WHERE id=?", (when, int(step["id"])))


def test_raising_a_request_writes_to_the_approver(engine: Engine, tmp_path: Path) -> None:
    notifier = _notifier(engine, tmp_path)
    engine.submit(make_document(total="1000.00"), requested_by="priya")
    messages = engine.store.notifications()
    assert [m["user_id"] for m in messages] == ["dana"]
    assert "Approval needed" in messages[0]["subject"]
    assert Path(messages[0]["path"]).exists()
    del notifier


def test_the_message_says_who_is_being_covered_for(engine: Engine, tmp_path: Path) -> None:
    _notifier(engine, tmp_path)
    result = engine.submit(make_document(total="4000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    body = engine.store.notifications()[0]["body"]
    assert "covering for sam" in body


def test_a_decision_tells_the_requester(engine: Engine, tmp_path: Path) -> None:
    _notifier(engine, tmp_path)
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE, comment="fine")
    to_requester = [
        m for m in engine.store.notifications() if m["user_id"] == "priya" and m["kind"] == "decided"
    ]
    assert to_requester and "approved by Dana" in to_requester[0]["subject"]
    assert "fine" in to_requester[0]["body"]
    closed = [m for m in engine.store.notifications() if m["kind"] == "closed"]
    assert closed and "AR-0001 is approved" in closed[0]["subject"]


def test_a_reset_approval_says_why(engine: Engine, tmp_path: Path) -> None:
    _notifier(engine, tmp_path)
    engine.submit(make_document(number="B-3", total="1000.00"), requested_by="priya")
    engine.submit(make_document(number="B-3", total="1200.00"), requested_by="priya")
    changed = [m for m in engine.store.notifications() if "Re-approval" in m["subject"]]
    assert changed and "edited after approval had started" in changed[0]["body"]


def test_nothing_is_chased_before_its_sla(engine: Engine, tmp_path: Path) -> None:
    notifier = _notifier(engine, tmp_path)
    engine.submit(make_document(total="1000.00"), requested_by="priya")
    assert notifier.reminders() == []


def test_an_overdue_step_is_chased(engine: Engine, tmp_path: Path) -> None:
    notifier = _notifier(engine, tmp_path)
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    _age_step(engine.store, result.request_id, 3)
    messages = notifier.reminders()
    assert [m.kind for m in messages] == [REMINDER]
    assert messages[0].to_user == "dana"


def test_a_long_wait_escalates(engine: Engine, tmp_path: Path) -> None:
    notifier = _notifier(engine, tmp_path)
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    _age_step(engine.store, result.request_id, 9)
    messages = notifier.reminders()
    assert [m.kind for m in messages] == [ESCALATION]


def test_one_chase_per_day_per_step(engine: Engine, tmp_path: Path) -> None:
    """A reminder that arrives hourly gets filtered, and then so do the real ones."""
    notifier = _notifier(engine, tmp_path)
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    _age_step(engine.store, result.request_id, 3)
    for message in notifier.reminders():
        notifier._emit(message)
    assert notifier.reminders() == []


def test_a_decided_request_is_not_chased(engine: Engine, tmp_path: Path) -> None:
    notifier = _notifier(engine, tmp_path)
    result = engine.submit(make_document(total="1000.00"), requested_by="priya")
    _age_step(engine.store, result.request_id, 5)
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    assert notifier.reminders() == []


def test_sending_without_smtp_configured_says_so(engine: Engine, tmp_path: Path) -> None:
    import pytest

    notifier = _notifier(engine, tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        notifier.send([])
    assert "SMTP is not configured" in str(excinfo.value)


def test_messages_render_as_real_email(engine: Engine, tmp_path: Path) -> None:
    notifier = _notifier(engine, tmp_path)
    engine.submit(make_document(total="1000.00"), requested_by="priya")
    eml = Path(engine.store.notifications()[0]["path"]).read_text(encoding="utf-8")
    assert "To: dana@example.com" in eml
    assert "Subject: Approval needed" in eml
    del notifier
