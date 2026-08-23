"""The three verbs, in one place.

``record`` / ``import`` produce a meeting, ``transcribe`` gives it words, and
``minutes`` turns those words into the document. The CLI and the browser UI are
both thin callers of the functions here, so the two can never drift into
behaving differently.
"""

from __future__ import annotations

import secrets
import shutil
from datetime import date, datetime
from pathlib import Path

from . import transcripts
from .audio import duration as audio_duration
from .config import (
    AUDIO_SUFFIXES,
    TRANSCRIPT_SUFFIXES,
    Settings,
    ensure_dirs,
    recordings_dir,
    transcripts_dir,
)
from .engines import EngineError
from .engines.factory import get_engine
from .models import Meeting, Minutes, Transcript
from .store import Store
from .templates import get as get_template
from .transcribers import TranscriptionError
from .transcribers.factory import get_transcriber


class PipelineError(RuntimeError):
    """Raised when a stage cannot proceed."""


def new_meeting_id(when: datetime | None = None) -> str:
    """A sortable, human-readable id: ``2026-08-23-1432-7f3a``."""
    stamp = (when or datetime.now()).strftime("%Y-%m-%d-%H%M")
    return f"{stamp}-{secrets.token_hex(2)}"


def create_meeting(
    store: Store,
    *,
    title: str = "",
    held_on: date | None = None,
    meeting_id: str = "",
    status: str = "new",
) -> Meeting:
    ensure_dirs()
    when = held_on or date.today()
    meeting = Meeting(
        meeting_id=meeting_id or new_meeting_id(),
        title=title,
        held_on=when.isoformat(),
        status=status,
    )
    return store.upsert_meeting(meeting)


def import_file(
    store: Store,
    path: str | Path,
    *,
    title: str = "",
    held_on: date | None = None,
    meeting: Meeting | None = None,
) -> tuple[Meeting, Transcript | None]:
    """Bring an existing audio file or transcript under management.

    The file is copied into the data directory rather than referenced in place,
    so a meeting does not break when someone tidies their Downloads folder.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise PipelineError(f"no such file: {source}")

    suffix = source.suffix.lower()
    if suffix not in AUDIO_SUFFIXES and suffix not in TRANSCRIPT_SUFFIXES:
        raise PipelineError(
            f"unsupported file type {suffix or '(none)'} — "
            f"audio: {', '.join(sorted(AUDIO_SUFFIXES))}; "
            f"transcript: {', '.join(sorted(TRANSCRIPT_SUFFIXES))}"
        )

    ensure_dirs()
    held = held_on or _file_date(source)
    target_meeting = meeting or create_meeting(
        store, title=title or source.stem.replace("-", " ").replace("_", " "), held_on=held
    )

    if suffix in AUDIO_SUFFIXES:
        destination = recordings_dir() / f"{target_meeting.meeting_id}{suffix}"
        shutil.copy2(source, destination)
        target_meeting.audio_path = str(destination)
        target_meeting.duration = audio_duration(destination)
        target_meeting.status = "new"
        return store.upsert_meeting(target_meeting), None

    transcript = transcripts.load(source)
    destination = transcripts_dir() / f"{target_meeting.meeting_id}{suffix}"
    shutil.copy2(source, destination)
    transcript.source = str(destination)
    store.save_transcript(target_meeting.meeting_id, transcript)
    target_meeting.transcript_path = str(destination)
    target_meeting.participants = transcript.speakers()
    target_meeting.duration = target_meeting.duration or transcript.duration
    target_meeting.status = "transcribed"
    return store.upsert_meeting(target_meeting), transcript


def transcribe(
    store: Store,
    meeting: Meeting,
    settings: Settings | None = None,
    *,
    force: bool = False,
) -> Transcript:
    """Run speech recognition over the meeting's audio."""
    settings = settings or Settings.load()
    existing = store.get_transcript(meeting.meeting_id)
    if existing is not None and not force:
        return existing
    if not meeting.audio_path:
        raise PipelineError(
            f"meeting {meeting.meeting_id} has no audio — "
            "record one, or import a transcript file directly"
        )

    transcriber = get_transcriber(settings.transcriber, settings)
    try:
        transcript = transcriber.transcribe(Path(meeting.audio_path), language=settings.language)
    except TranscriptionError:
        raise
    except OSError as exc:
        raise TranscriptionError(f"transcription failed: {exc}")

    destination = transcripts_dir() / f"{meeting.meeting_id}.vtt"
    ensure_dirs()
    destination.write_text(transcripts.to_vtt(transcript), encoding="utf-8")

    store.save_transcript(meeting.meeting_id, transcript)
    meeting.transcript_path = str(destination)
    meeting.participants = transcript.speakers()
    meeting.duration = meeting.duration or transcript.duration
    meeting.status = "transcribed"
    store.upsert_meeting(meeting)
    return transcript


def make_minutes(
    store: Store,
    meeting: Meeting,
    settings: Settings | None = None,
    *,
    engine: str | None = None,
    template: str | None = None,
) -> Minutes:
    """Generate and persist minutes for a meeting that already has a transcript."""
    settings = settings or Settings.load()
    if template is not None:
        meeting.template = template
    transcript = store.get_transcript(meeting.meeting_id)
    if transcript is None:
        raise PipelineError(
            f"meeting {meeting.meeting_id} has no transcript yet — run: "
            f"minutely transcribe {meeting.meeting_id}"
        )
    if not transcript.segments:
        raise EngineError("the transcript is empty; nothing to summarise")

    chosen = get_engine(engine, settings)
    minutes = chosen.summarise(
        transcript,
        title=meeting.title,
        held_on=_as_date(meeting.held_on),
        meeting_id=meeting.meeting_id,
        notes=meeting.notes,
        template=get_template(meeting.template),
    )
    saved = store.save_minutes(minutes)

    if not meeting.title and minutes.title:
        meeting.title = minutes.title
    meeting.participants = minutes.attendees or meeting.participants
    meeting.status = "minuted"
    store.upsert_meeting(meeting)
    return saved


def process(
    store: Store,
    meeting: Meeting,
    settings: Settings | None = None,
    *,
    engine: str | None = None,
    template: str | None = None,
) -> Minutes:
    """Transcribe if needed, then minute. What the UI calls when recording stops."""
    settings = settings or Settings.load()
    if store.get_transcript(meeting.meeting_id) is None:
        transcribe(store, meeting, settings)
        refreshed = store.get_meeting(meeting.meeting_id)
        if refreshed is not None:
            meeting = refreshed
    return make_minutes(store, meeting, settings, engine=engine, template=template)


def _as_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _file_date(path: Path) -> date:
    try:
        return date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return date.today()
