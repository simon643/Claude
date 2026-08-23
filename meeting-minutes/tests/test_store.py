from __future__ import annotations

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
