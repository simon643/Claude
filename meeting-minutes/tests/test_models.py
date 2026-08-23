from __future__ import annotations

from minutely.models import (
    ActionItem,
    Meeting,
    Minutes,
    Segment,
    Transcript,
    format_clock,
    format_duration,
)


def test_clock_formatting_switches_to_hours_when_needed() -> None:
    assert format_clock(0) == "00:00"
    assert format_clock(65.4) == "01:05"
    assert format_clock(3725) == "1:02:05"
    assert format_clock(-5) == "00:00"


def test_duration_phrasing() -> None:
    assert format_duration(None) == "unknown"
    assert format_duration(42) == "42 s"
    assert format_duration(2520) == "42 min"
    assert format_duration(3900) == "1 h 05 min"


def test_transcript_text_prefixes_speakers() -> None:
    transcript = Transcript(
        segments=[
            Segment(index=0, text="Hello.", speaker="Ada"),
            Segment(index=1, text="No label here."),
        ]
    )
    assert transcript.text == "Ada: Hello.\nNo label here."
    assert transcript.word_count == 4


def test_transcript_duration_is_the_last_end_time() -> None:
    transcript = Transcript(
        segments=[
            Segment(index=0, text="a", start=0.0, end=5.0),
            Segment(index=1, text="b", start=5.0, end=11.5),
        ]
    )
    assert transcript.duration == 11.5


def test_citation_marker_needs_a_timestamp() -> None:
    transcript = Transcript(segments=[Segment(index=0, text="a", start=61.0, end=64.0)])
    assert transcript.cited(0) == "[01:01]"
    assert transcript.cited(None) == ""
    assert transcript.cited(7) == ""


def test_action_dedupe_key_ignores_punctuation_and_case() -> None:
    first = ActionItem(text="Send the deck!", owner="Bob")
    second = ActionItem(text="send the  deck", owner="bob")
    assert first.key() == second.key()
    assert ActionItem(text="Send the deck", owner="Ada").key() != first.key()


def test_round_trip_through_dicts() -> None:
    minutes = Minutes(
        meeting_id="m1",
        title="Sync",
        actions=[ActionItem(text="Do it", owner="Ada", due="2026-01-01", action_id=3)],
    )
    restored = Minutes.from_dict(minutes.to_dict())
    assert restored.actions[0].action_id == 3
    assert restored.actions[0].owner == "Ada"

    meeting = Meeting(meeting_id="m1", title="Sync", duration=61.0, participants=["Ada"])
    assert Meeting.from_dict(meeting.to_dict()) == meeting


def test_bad_numbers_in_stored_data_do_not_crash_loading() -> None:
    item = ActionItem.from_dict({"text": "Do it", "segment_index": "not a number", "id": None})
    assert item.segment_index is None
    assert item.action_id is None
