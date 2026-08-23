"""Finding Teams meetings and pulling them into minutely.

The path from a calendar entry to a set of minutes is four Graph calls:

1. ``/me/calendarView`` — the meetings in a date range, with their join URLs.
2. ``/me/onlineMeetings?$filter=JoinWebUrl eq '…'`` — the join URL is the only
   handle a calendar event gives you; the transcript APIs need the online
   meeting id behind it.
3. ``/me/onlineMeetings/{id}/transcripts`` — what Teams captured, if anything.
4. ``…/transcripts/{id}/content?$format=text/vtt`` — WebVTT, with the speaker
   names this app cannot otherwise know.

From there it is the ordinary pipeline: the VTT is imported like any other
transcript and minuted by whichever engine is configured.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import pipeline
from ..audio import duration as audio_duration
from ..config import Settings, ensure_dirs, recordings_dir
from ..engines import EngineError
from ..models import Meeting, Minutes
from ..store import Store
from . import TeamsError
from .calendar import CalendarEvent, list_events
from .graph import GraphClient

SOURCE = "teams"


def list_meetings(
    graph: GraphClient,
    *,
    days_back: int = 7,
    days_forward: int = 1,
    limit: int = 50,
    now: datetime | None = None,
) -> list[CalendarEvent]:
    """The Teams meetings on the calendar — the ones that might have transcripts."""
    return list_events(
        graph,
        days_back=days_back,
        days_forward=days_forward,
        limit=limit,
        teams_only=True,
        now=now,
    )


@dataclass
class PullResult:
    """What happened to one meeting."""

    teams: CalendarEvent
    status: str  # imported | updated | skipped | no-transcript | error
    detail: str = ""
    meeting_id: str = ""
    minutes: Minutes | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"imported", "updated"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "meeting_id": self.meeting_id,
            "subject": self.teams.subject,
            "start": self.teams.start.isoformat(),
            "actions": len(self.minutes.actions) if self.minutes else 0,
        }


# --------------------------------------------------------------------------
# Reading Teams
# --------------------------------------------------------------------------


def online_meeting_id(graph: GraphClient, join_url: str) -> str:
    """Resolve a calendar join URL to the online meeting behind it."""
    if not join_url:
        return ""
    # OData string literals escape a single quote by doubling it.
    literal = join_url.replace("'", "''")
    payload = graph.get_json("/me/onlineMeetings", {"$filter": f"JoinWebUrl eq '{literal}'"})
    for row in payload.get("value", []):
        if isinstance(row, dict) and row.get("id"):
            return str(row["id"])
    return ""


def list_transcripts(graph: GraphClient, meeting_id: str) -> list[dict[str, Any]]:
    payload = graph.get_json(f"/me/onlineMeetings/{meeting_id}/transcripts")
    rows = [row for row in payload.get("value", []) if isinstance(row, dict)]
    # Newest last is the useful order: a meeting restarted mid-session has more
    # than one, and the later transcript is the one people mean.
    return sorted(rows, key=lambda row: str(row.get("createdDateTime", "")))


def transcript_content(graph: GraphClient, meeting_id: str, transcript_id: str) -> tuple[str, bool]:
    """Fetch a transcript, preferring the speaker-attributed WebVTT form.

    Returns the text and whether speakers came with it. A tenant can forbid
    speaker attribution, in which case Graph refuses the VTT and the plain-text
    form is the only thing on offer — worth having, but the minutes from it
    cannot attribute an action to anybody.
    """
    path = f"/me/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content"
    try:
        body, _ = graph.get_bytes(path, {"$format": "text/vtt"}, accept="text/vtt")
        return body.decode("utf-8-sig", errors="replace"), True
    except TeamsError as exc:
        if "speaker" not in str(exc).lower():
            raise
    body, _ = graph.get_bytes(
        path, accept="application/vnd.microsoft.graph.transcript+text"
    )
    return body.decode("utf-8-sig", errors="replace"), False


def list_recordings(graph: GraphClient, meeting_id: str) -> list[dict[str, Any]]:
    payload = graph.get_json(f"/me/onlineMeetings/{meeting_id}/recordings")
    rows = [row for row in payload.get("value", []) if isinstance(row, dict)]
    return sorted(rows, key=lambda row: str(row.get("createdDateTime", "")))


# --------------------------------------------------------------------------
# Pulling into minutely
# --------------------------------------------------------------------------


def pull_meeting(
    store: Store,
    graph: GraphClient,
    teams_meeting: CalendarEvent,
    settings: Settings | None = None,
    *,
    with_recording: bool = False,
    engine: str | None = None,
    force: bool = False,
) -> PullResult:
    """Import one Teams meeting's transcript and minute it."""
    settings = settings or Settings.load()
    existing = store.find_external(SOURCE, teams_meeting.event_id)
    if existing is not None and not force and store.get_transcript(existing.meeting_id) is not None:
        return PullResult(
            teams_meeting, "skipped", "already imported", meeting_id=existing.meeting_id
        )

    try:
        meeting_id = online_meeting_id(graph, teams_meeting.join_url)
        if not meeting_id:
            return PullResult(teams_meeting, "no-transcript", "no Teams meeting behind that invite")

        transcripts_available = list_transcripts(graph, meeting_id)
        if not transcripts_available:
            return PullResult(
                teams_meeting,
                "no-transcript",
                "Teams captured no transcript for this meeting",
            )

        text, attributed = transcript_content(
            graph, meeting_id, str(transcripts_available[-1]["id"])
        )
    except TeamsError as exc:
        return PullResult(teams_meeting, "error", str(exc))

    if not text.strip():
        return PullResult(teams_meeting, "no-transcript", "the transcript Teams returned was empty")

    local = existing or pipeline.create_meeting(
        store, title=teams_meeting.title, held_on=teams_meeting.held_on
    )
    local.title = local.title or teams_meeting.title
    local.source = SOURCE
    local.external_id = teams_meeting.event_id
    local.held_on = teams_meeting.held_on.isoformat()

    suffix = ".vtt" if attributed else ".txt"
    with tempfile.TemporaryDirectory(prefix="minutely-teams-") as tmp:
        staged = Path(tmp) / f"{local.meeting_id}{suffix}"
        staged.write_text(text, encoding="utf-8")
        try:
            local, _transcript = pipeline.import_file(store, staged, meeting=local)
        except pipeline.PipelineError as exc:
            return PullResult(teams_meeting, "error", f"could not read the transcript: {exc}")

    if not local.participants:
        local.participants = teams_meeting.attendees
    local.emails = local.emails or teams_meeting.emails
    if teams_meeting.end is not None and not local.duration:
        # The transcript's own span is the better measure and import_file has
        # already used it when the cues carry timings. The calendar block is
        # the fallback: it says how long the meeting was booked for, which is
        # not the same as how long it ran.
        local.duration = (teams_meeting.end - teams_meeting.start).total_seconds()

    detail = "" if attributed else "no speaker attribution (tenant policy) — actions are unassigned"
    if with_recording:
        try:
            detail = _attach_recording(graph, meeting_id, local) or detail
        except TeamsError as exc:
            detail = f"transcript imported; recording failed: {exc}"
    store.upsert_meeting(local)

    try:
        minutes = pipeline.make_minutes(store, local, settings, engine=engine)
    except (EngineError, pipeline.PipelineError) as exc:
        return PullResult(
            teams_meeting, "error", f"transcript imported but minutes failed: {exc}",
            meeting_id=local.meeting_id,
        )

    return PullResult(
        teams_meeting,
        "updated" if existing is not None else "imported",
        detail,
        meeting_id=local.meeting_id,
        minutes=minutes,
    )


def pull_recent(
    store: Store,
    graph: GraphClient,
    settings: Settings | None = None,
    *,
    days_back: int = 7,
    limit: int = 20,
    with_recording: bool = False,
    engine: str | None = None,
    force: bool = False,
    match: str = "",
    on_progress: Callable[[CalendarEvent], None] | None = None,
) -> list[PullResult]:
    """Pull every Teams meeting in the window that has a transcript."""
    results: list[PullResult] = []
    needle = match.strip().lower()
    for teams_meeting in list_meetings(graph, days_back=days_back, days_forward=0, limit=limit):
        # Filter before pulling, not after: a subject filter that still
        # imported everything would be worse than no filter at all.
        if needle and needle not in teams_meeting.subject.lower():
            continue
        if on_progress is not None:
            on_progress(teams_meeting)
        results.append(
            pull_meeting(
                store,
                graph,
                teams_meeting,
                settings,
                with_recording=with_recording,
                engine=engine,
                force=force,
            )
        )
    return results


def _attach_recording(graph: GraphClient, meeting_id: str, local: Meeting) -> str:
    recordings = list_recordings(graph, meeting_id)
    if not recordings:
        return "transcript imported; Teams has no recording for this meeting"
    ensure_dirs()
    destination = recordings_dir() / f"{local.meeting_id}.mp4"
    written = graph.download(
        f"/me/onlineMeetings/{meeting_id}/recordings/{recordings[-1]['id']}/content",
        destination,
    )
    local.audio_path = str(destination)
    local.duration = local.duration or audio_duration(destination)
    return f"recording saved ({written // (1024 * 1024)} MB)"
