"""Posting approved documents back to the ledger."""

from __future__ import annotations

import pytest

from apflow.models import Decision, DocumentKind
from apflow.posting import PostingError, push_approved
from apflow.workflow import Engine
from tests.conftest import RecordingProvider, make_document


def _approve(engine: Engine, document) -> int:
    result = engine.submit(document, requested_by="priya")
    engine.decide(result.request_id, "dana", Decision.APPROVE)
    return result.request_id


def test_an_approved_bill_is_authorised_in_the_ledger(engine: Engine) -> None:
    document = make_document(total="1000.00")
    _approve(engine, document)
    provider = RecordingProvider([document])

    summary = push_approved(engine, provider)
    assert summary.count("pushed") == 1
    assert provider.calls == [(document.document_id, "AUTHORISED")]


def test_a_pushed_document_is_not_pushed_again(engine: Engine) -> None:
    document = make_document(total="1000.00")
    _approve(engine, document)
    provider = RecordingProvider([document])
    push_approved(engine, provider)
    second = push_approved(engine, provider)
    assert second.outcomes == []
    assert len(provider.calls) == 1


def test_a_document_edited_after_approval_is_not_posted(engine: Engine) -> None:
    """The gap between the last approval and the push is exactly where an
    approved-then-edited bill would otherwise slip through."""
    document = make_document(number="B-7", total="1000.00")
    request_id = _approve(engine, document)
    # Kept inside the same approval band, so the same request reopens rather
    # than being superseded by a re-routed one.
    edited = make_document(number="B-7", total="1500.00")
    provider = RecordingProvider([edited])

    summary = push_approved(engine, provider)
    assert summary.count("pushed") == 0
    assert summary.count("changed") == 1
    assert provider.calls == []

    view = engine.request_view(request_id)
    assert view["state"] == "pending"
    assert any(e["kind"] == "push_blocked" for e in view["events"])
    assert any(e["kind"] == "document_changed" for e in view["events"])


def test_a_dry_run_changes_nothing(engine: Engine) -> None:
    document = make_document(number="B-8", total="1000.00")
    request_id = _approve(engine, document)
    provider = RecordingProvider([make_document(number="B-8", total="9000.00")])

    summary = push_approved(engine, provider, dry_run=True)
    assert summary.count("changed") == 1
    assert provider.calls == []
    # The preview must not have reset the approvals it was only reporting on.
    assert engine.request_view(request_id)["state"] == "approved"


def test_a_missing_document_is_skipped_not_invented(engine: Engine) -> None:
    _approve(engine, make_document(total="1000.00"))
    summary = push_approved(engine, RecordingProvider([]))
    assert summary.count("skipped") == 1


def test_a_failed_write_is_recorded_and_not_marked_pushed(engine: Engine) -> None:
    document = make_document(total="1000.00")
    request_id = _approve(engine, document)
    provider = RecordingProvider([document], fail=True)

    summary = push_approved(engine, provider)
    assert summary.count("failed") == 1
    assert engine.store.audit_tail()[0]["outcome"] == "failed"
    assert engine.store.request_row(request_id)["pushed_at"] is None
    # Still approved and still unposted, so a later run can retry it.
    assert len(engine.store.list_requests(state="approved", pushed=False)) == 1


def test_a_read_only_connection_refuses_to_push(engine: Engine, demo) -> None:
    _approve(engine, make_document(total="1000.00"))
    with pytest.raises(PostingError) as excinfo:
        push_approved(engine, demo)
    assert "read-only" in str(excinfo.value)


def test_a_read_only_connection_can_still_preview(engine: Engine) -> None:
    document = make_document(total="1000.00")
    _approve(engine, document)

    class ReadOnly(RecordingProvider):
        can_write = False

    summary = push_approved(engine, ReadOnly([document]), dry_run=True)
    assert summary.count("would_push") == 1


def test_purchase_orders_post_too(engine: Engine) -> None:
    document = make_document(number="PO-5", total="1000.00", kind=DocumentKind.PURCHASE_ORDER)
    _approve(engine, document)
    provider = RecordingProvider([document])
    push_approved(engine, provider)
    assert provider.calls == [(document.document_id, "AUTHORISED")]
