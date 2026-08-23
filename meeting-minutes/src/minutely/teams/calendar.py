"""Reading the signed-in user's calendar.

Everything here is Microsoft Graph's ``/me/calendarView``, which is the right
endpoint for this job because it expands recurring series server-side: your
weekly standup appears as an occurrence with a real date, not as a rule this
app would have to interpret.

Two callers want this: the Teams sync (which meetings might have transcripts to
pull) and the recorder UI (what is coming up, so a click can start recording
the right meeting with the right title and the right attendees).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .graph import GraphClient

# Teams says which product produced the meeting; a Skype or third-party entry
# has no transcript for us to read.
TEAMS_PROVIDER = "teamsforbusiness"

_EVENT_FIELDS = (
    "id,subject,start,end,organizer,attendees,isOnlineMeeting,onlineMeetingProvider,"
    "onlineMeeting,isCancelled,location,webLink"
)
# Graph returns up to seven fractional-second digits; datetime.fromisoformat
# accepts three or six.
_FRACTION = re.compile(r"\.(\d{1,7})")


@dataclass
class CalendarEvent:
    """One meeting on the signed-in user's calendar."""

    event_id: str
    subject: str
    start: datetime
    end: datetime | None = None
    organizer: str = ""
    attendees: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    join_url: str = ""
    location: str = ""
    online: bool = False
    is_teams: bool = False
    cancelled: bool = False

    @property
    def held_on(self) -> date:
        return self.start.date()

    @property
    def title(self) -> str:
        return self.subject or "Meeting"

    @property
    def minutes_long(self) -> int:
        if self.end is None:
            return 0
        return int((self.end - self.start).total_seconds() // 60)

    def starts_in(self, now: datetime | None = None) -> int:
        """Minutes until it starts; negative once it has."""
        anchor = now or datetime.now(UTC)
        return int((self.start - anchor).total_seconds() // 60)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "subject": self.subject,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "organizer": self.organizer,
            "attendees": self.attendees,
            "emails": self.emails,
            "join_url": self.join_url,
            "location": self.location,
            "online": self.online,
            "is_teams": self.is_teams,
            "minutes_long": self.minutes_long,
        }


def list_events(
    graph: GraphClient,
    *,
    days_back: int = 7,
    days_forward: int = 1,
    limit: int = 50,
    teams_only: bool = False,
    include_cancelled: bool = False,
    now: datetime | None = None,
) -> list[CalendarEvent]:
    """Calendar entries in a window around now, newest first."""
    anchor = now or datetime.now(UTC)
    start = anchor - timedelta(days=max(0, days_back))
    end = anchor + timedelta(days=max(0, days_forward))
    return _fetch(
        graph,
        start,
        end,
        limit=limit,
        teams_only=teams_only,
        include_cancelled=include_cancelled,
        order="desc",
    )


def upcoming(
    graph: GraphClient,
    *,
    hours: int = 12,
    minutes_back: int = 30,
    limit: int = 12,
    now: datetime | None = None,
) -> list[CalendarEvent]:
    """What is about to happen, soonest first.

    The window starts slightly in the past on purpose: the moment you actually
    reach for a recorder is usually a few minutes after the meeting began.
    """
    anchor = now or datetime.now(UTC)
    return _fetch(
        graph,
        anchor - timedelta(minutes=max(0, minutes_back)),
        anchor + timedelta(hours=max(1, hours)),
        limit=limit,
        teams_only=False,
        include_cancelled=False,
        order="asc",
    )


def _fetch(
    graph: GraphClient,
    start: datetime,
    end: datetime,
    *,
    limit: int,
    teams_only: bool,
    include_cancelled: bool,
    order: str,
) -> list[CalendarEvent]:
    rows = graph.paged(
        "/me/calendarView",
        {
            "startDateTime": _graph_time(start),
            "endDateTime": _graph_time(end),
            "$select": _EVENT_FIELDS,
            "$orderby": f"start/dateTime {order}",
            "$top": "50",
        },
        limit=limit * 4,  # room to discard the entries we filter out
    )

    events: list[CalendarEvent] = []
    for row in rows:
        event = _to_event(row)
        if event is None:
            continue
        if event.cancelled and not include_cancelled:
            continue
        if teams_only and not event.is_teams:
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return events


def _to_event(row: dict[str, Any]) -> CalendarEvent | None:
    start = _parse_time(row.get("start"))
    if start is None:
        return None

    provider = str(row.get("onlineMeetingProvider", "")).lower()
    online = bool(row.get("isOnlineMeeting"))
    meeting = row.get("onlineMeeting")
    join_url = str(meeting.get("joinUrl", "")) if isinstance(meeting, dict) else ""
    location = row.get("location")
    people = [a for a in row.get("attendees", []) if isinstance(a, dict)]

    return CalendarEvent(
        event_id=str(row.get("id", "")),
        subject=str(row.get("subject", "")).strip(),
        start=start,
        end=_parse_time(row.get("end")),
        organizer=_name(row.get("organizer")),
        attendees=[name for name in (_name(a) for a in people) if name],
        emails=[address for address in (_email(a) for a in people) if address],
        join_url=join_url,
        location=str(location.get("displayName", "")).strip() if isinstance(location, dict) else "",
        online=online,
        is_teams=online and (not provider or provider == TEAMS_PROVIDER),
        cancelled=bool(row.get("isCancelled")),
    )


def _person(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    address = raw.get("emailAddress")
    return address if isinstance(address, dict) else {}


def _name(raw: Any) -> str:
    address = _person(raw)
    return str(address.get("name") or address.get("address") or "").strip()


def _email(raw: Any) -> str:
    return str(_person(raw).get("address") or "").strip()


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("dateTime", ""))
    if not value:
        return None
    # Trim Graph's seven-digit fraction to something fromisoformat accepts.
    value = _FRACTION.sub(lambda m: "." + m.group(1)[:6], value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # calendarView answers in UTC unless asked otherwise; Graph labels it
        # in the sibling "timeZone" field rather than in the string.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _graph_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
