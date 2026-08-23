from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from minutely import pipeline
from minutely.config import recordings_dir, transcripts_dir
from minutely.store import Store
from minutely.transcribers import TranscriptionError


def test_importing_a_transcript_copies_it_and_records_speakers(store: Store, demo_path: Path) -> None:
    meeting, transcript = pipeline.import_file(store, demo_path, title="Demo")
    assert transcript is not None
    assert meeting.status == "transcribed"
    assert meeting.participants == ["Dana", "Priya", "Marcus", "Sam"]
    # The copy lives under the data directory, not wherever the user had it.
    copied = Path(meeting.transcript_path)
    assert copied.exists()
    assert copied.parent == transcripts_dir()
    assert store.get_transcript(meeting.meeting_id) is not None


def test_importing_audio_leaves_it_untranscribed(store: Store, tmp_path: Path) -> None:
    audio = tmp_path / "chat.webm"
    audio.write_bytes(b"\x1a\x45\xdf\xa3 not really webm")
    meeting, transcript = pipeline.import_file(store, audio)
    assert transcript is None
    assert meeting.status == "new"
    assert Path(meeting.audio_path).parent == recordings_dir()


def test_unsupported_file_types_are_refused(store: Store, tmp_path: Path) -> None:
    junk = tmp_path / "notes.docx"
    junk.write_text("nope")
    with pytest.raises(pipeline.PipelineError, match="unsupported file type"):
        pipeline.import_file(store, junk)


def test_a_missing_file_is_refused(store: Store, tmp_path: Path) -> None:
    with pytest.raises(pipeline.PipelineError, match="no such file"):
        pipeline.import_file(store, tmp_path / "ghost.vtt")


def test_minutes_need_a_transcript_first(store: Store) -> None:
    meeting = pipeline.create_meeting(store, title="Empty")
    with pytest.raises(pipeline.PipelineError, match="no transcript"):
        pipeline.make_minutes(store, meeting)


def test_transcribing_without_audio_explains_the_alternative(store: Store) -> None:
    meeting = pipeline.create_meeting(store, title="Empty")
    with pytest.raises(pipeline.PipelineError, match="import a transcript"):
        pipeline.transcribe(store, meeting)


def test_transcribe_returns_the_existing_transcript_rather_than_rerunning(
    store: Store, demo_path: Path
) -> None:
    meeting, _ = pipeline.import_file(store, demo_path)
    # No audio and no whisper binary: this only works because it short-circuits.
    transcript = pipeline.transcribe(store, meeting)
    assert transcript.segments


def test_a_disabled_transcriber_says_what_to_do_instead(store: Store, tmp_path: Path) -> None:
    from minutely.config import Settings

    audio = tmp_path / "chat.webm"
    audio.write_bytes(b"junk")
    meeting, _ = pipeline.import_file(store, audio)
    settings = Settings(transcriber="none")
    with pytest.raises(TranscriptionError, match="minutely import"):
        pipeline.transcribe(store, meeting, settings)


def test_end_to_end_from_transcript_to_minutes(store: Store, demo_path: Path) -> None:
    meeting, _ = pipeline.import_file(store, demo_path, held_on=date(2026, 8, 24))
    minutes = pipeline.make_minutes(store, meeting, engine="rules")
    assert minutes.actions
    assert minutes.meeting_id == meeting.meeting_id

    refreshed = store.get_meeting(meeting.meeting_id)
    assert refreshed is not None
    assert refreshed.status == "minuted"
    # Actions came back with database ids, so they can be ticked off.
    assert all(a.action_id is not None for a in minutes.actions)


def test_meeting_ids_are_sortable_and_unique() -> None:
    from datetime import datetime

    when = datetime(2026, 8, 24, 14, 32)
    first = pipeline.new_meeting_id(when)
    second = pipeline.new_meeting_id(when)
    assert first.startswith("2026-08-24-1432-")
    assert first != second
