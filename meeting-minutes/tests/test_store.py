from __future__ import annotations

from pathlib import Path

from minutely.models import ActionItem, Meeting, Minutes
from minutely.store import Store


def make_meeting(store: Store, meeting_id: str = "m1", title: str = "Sync") -> Meeting:
    return store.upsert_meeting(Meeting(meeting_id=meeting_id, title=title, held_on="2026-08-24"))


def test_upsert_is_idempotent_and_preserves_paths(store: Store) -> None:
    make_meeting(store)
    first = store.upsert_meeting(
        Meeting(meeting_id="m1", title="Sync", held_on="2026-08-24", audio_path="/tmp/a.webm")
    )
    assert first.audio_path == "/tmp/a.webm"
    # A later write that knows nothing about the audio must not erase it.
    second = store.upsert_meeting(Meeting(meeting_id="m1", title="Sync v2", held_on="2026-08-24"))
    assert second.audio_path == "/tmp/a.webm"
    assert second.title == "Sync v2"
    assert len(store.list_meetings()) == 1


def test_resolve_by_prefix_and_title(store: Store) -> None:
    make_meeting(store, "2026-08-24-0900-abcd", "Weekly product sync")
    assert store.resolve_meeting("2026-08-24-0900-abcd") is not None
    assert store.resolve_meeting("2026-08-24-09") is not None
    assert store.resolve_meeting("product") is not None
    assert store.resolve_meeting("nothing here") is None


def test_saving_minutes_populates_the_action_register(store: Store) -> None:
    make_meeting(store)
    minutes = Minutes(
        meeting_id="m1",
        actions=[ActionItem(text="Send the deck", owner="Bob", due="2026-08-28")],
    )
    saved = store.save_minutes(minutes)
    assert saved.actions[0].action_id is not None
    assert store.list_actions(meeting_id="m1")[0].owner == "Bob"


def test_regenerating_minutes_keeps_a_completed_action_completed(store: Store) -> None:
    make_meeting(store)
    item = ActionItem(text="Send the deck", owner="Bob")
    saved = store.save_minutes(Minutes(meeting_id="m1", actions=[item]))
    action_id = saved.actions[0].action_id
    assert action_id is not None
    store.set_action_status(action_id, "done")

    # Re-running the engine produces the same action again.
    store.save_minutes(Minutes(meeting_id="m1", actions=[ActionItem(text="Send the deck", owner="Bob")]))
    refreshed = store.list_actions(meeting_id="m1")
    assert len(refreshed) == 1
    assert refreshed[0].status == "done"


def test_actions_filter_by_status_and_owner(store: Store) -> None:
    make_meeting(store)
    store.save_minutes(
        Minutes(
            meeting_id="m1",
            actions=[
                ActionItem(text="Send the deck", owner="Bob"),
                ActionItem(text="Book the room", owner="Ada"),
            ],
        )
    )
    assert len(store.list_actions(status="open")) == 2
    assert len(store.list_actions(owner="ada")) == 1
    assert store.action_rows(status="open")[0]["meeting_title"] == "Sync"


def test_actions_sort_dated_work_first(store: Store) -> None:
    make_meeting(store)
    store.save_minutes(
        Minutes(
            meeting_id="m1",
            actions=[
                ActionItem(text="Someday thing", owner="Ada"),
                ActionItem(text="Friday thing", owner="Bob", due="2026-08-28"),
            ],
        )
    )
    assert [a.text for a in store.list_actions()] == ["Friday thing", "Someday thing"]


def test_get_minutes_returns_the_latest_version(store: Store) -> None:
    make_meeting(store)
    store.save_minutes(Minutes(meeting_id="m1", summary="first", engine="rules"))
    store.save_minutes(Minutes(meeting_id="m1", summary="second", engine="claude"))
    current = store.get_minutes("m1")
    assert current is not None and current.summary == "second"
    assert len(store.minutes_history("m1")) == 2


def test_delete_removes_everything_for_that_meeting(store: Store) -> None:
    make_meeting(store)
    store.save_minutes(Minutes(meeting_id="m1", actions=[ActionItem(text="Send the deck")]))
    assert store.delete_meeting("m1") is True
    assert store.get_meeting("m1") is None
    assert store.list_actions() == []
    assert store.get_minutes("m1") is None


def test_unknown_action_status_is_rejected(store: Store) -> None:
    make_meeting(store)
    store.save_minutes(Minutes(meeting_id="m1", actions=[ActionItem(text="Send the deck")]))
    action_id = store.list_actions()[0].action_id
    assert action_id is not None
    try:
        store.set_action_status(action_id, "maybe")
    except ValueError as exc:
        assert "maybe" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected ValueError")


def test_stats_counts_open_and_done(store: Store) -> None:
    make_meeting(store)
    store.save_minutes(
        Minutes(
            meeting_id="m1",
            actions=[ActionItem(text="A thing"), ActionItem(text="Another thing")],
        )
    )
    first = store.list_actions()[0].action_id
    assert first is not None
    store.set_action_status(first, "done")
    assert store.stats() == {"meetings": 1, "open_actions": 1, "done_actions": 1}


def test_typed_notes_survive_a_write_that_knows_nothing_about_them(store: Store) -> None:
    make_meeting(store)
    store.set_notes("m1", "Pricing\n- 49 -> 65")
    # Most writes — a transcript landing, a title change — carry no notes at
    # all. None of them may erase the one artefact a person made by hand.
    store.upsert_meeting(Meeting(meeting_id="m1", title="Sync v2", held_on="2026-08-24"))
    found = store.get_meeting("m1")
    assert found is not None and found.notes == "Pricing\n- 49 -> 65"


def test_notes_can_be_cleared_but_only_deliberately(store: Store) -> None:
    make_meeting(store)
    store.set_notes("m1", "something")
    assert store.set_notes("m1", "") is True
    found = store.get_meeting("m1")
    assert found is not None and found.notes == ""


def test_setting_notes_on_a_meeting_that_is_not_there(store: Store) -> None:
    assert store.set_notes("nope", "text") is False


def test_the_template_choice_is_remembered(store: Store) -> None:
    meeting = make_meeting(store)
    meeting.template = "standup"
    store.upsert_meeting(meeting)
    found = store.get_meeting("m1")
    assert found is not None and found.template == "standup"


def test_an_older_database_is_migrated_in_place(tmp_path: Path) -> None:
    """A database written before notes existed must open, not explode."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (1);
            CREATE TABLE meetings (
                meeting_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                held_on TEXT NOT NULL DEFAULT '', duration REAL,
                audio_path TEXT NOT NULL DEFAULT '', transcript_path TEXT NOT NULL DEFAULT '',
                participants TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO meetings (meeting_id, title, created_at, updated_at)
            VALUES ('old-1', 'Ancient sync', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """
        )

    store = Store(path)
    try:
        found = store.get_meeting("old-1")
        assert found is not None
        assert found.title == "Ancient sync"
        # The columns added since are present and empty, not missing.
        assert (found.notes, found.template, found.emails, found.external_id) == ("", "", [], "")
        store.set_notes("old-1", "still works")
        refreshed = store.get_meeting("old-1")
        assert refreshed is not None and refreshed.notes == "still works"
    finally:
        store.close()
