"""Reading the calendar: what is coming up, and what shape it is."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from minutely.teams.auth import TeamsAuth
from minutely.teams.calendar import list_events, upcoming
from minutely.teams.graph import GraphClient
from tests.fakes import FakeMicrosoft, calendar_payload, signed_in_token

NOW = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def graph(fake: FakeMicrosoft, tmp_path: Path) -> GraphClient:
    signed_in_token(tmp_path / "teams-token.json")
    auth = TeamsAuth(client_id="c", transport=fake, token_path=tmp_path / "teams-token.json")
    return GraphClient(auth, transport=fake, sleep=lambda _s: None)


def event(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "event-2",
        "subject": "Design review",
        "start": {"dateTime": "2026-08-24T10:30:00.0000000"},
        "end": {"dateTime": "2026-08-24T11:00:00.0000000"},
        "isOnlineMeeting": False,
        "location": {"displayName": "Room 3"},
        "organizer": {"emailAddress": {"name": "Ada Byron", "address": "ada@example.com"}},
        "attendees": [
            {"emailAddress": {"name": "Bob Ross", "address": "bob@example.com"}},
        ],
    }
    row.update(overrides)
    return row


def test_upcoming_includes_meetings_that_are_not_online() -> None:
    # An in-person meeting is exactly the case where you want to record
    # locally, so it must not be filtered out with the non-Teams entries.
    fake = FakeMicrosoft().json_route(r"/me/calendarView", {"value": [event()]})
    events = upcoming(graph(fake, Path("/tmp")), now=NOW)
    assert [e.title for e in events] == ["Design review"]
    assert events[0].online is False
    assert events[0].is_teams is False
    assert events[0].location == "Room 3"


def test_upcoming_carries_the_invite_names_and_addresses() -> None:
    fake = FakeMicrosoft().json_route(r"/me/calendarView", calendar_payload())
    found = upcoming(graph(fake, Path("/tmp")), now=NOW)[0]
    assert found.attendees == ["Priya Raman", "Marcus Bell"]
    assert found.emails == ["priya@example.com", "marcus@example.com"]
    assert found.organizer == "Dana Okafor"
    assert found.is_teams is True


def test_upcoming_asks_for_a_forward_window_starting_slightly_in_the_past() -> None:
    fake = FakeMicrosoft().json_route(r"/me/calendarView", {"value": []})
    upcoming(graph(fake, Path("/tmp")), hours=4, minutes_back=30, now=NOW)
    url = fake.urls("calendarView")[0]
    # 08:30 to 13:00 — a meeting you are already ten minutes into is still one
    # you might want to start recording.
    assert "startDateTime=2026-08-24T08%3A30%3A00Z" in url
    assert "endDateTime=2026-08-24T13%3A00%3A00Z" in url
    assert "start%2FdateTime+asc" in url


def test_cancelled_meetings_are_left_out() -> None:
    fake = FakeMicrosoft().json_route(
        r"/me/calendarView", {"value": [event(isCancelled=True), event(id="live")]}
    )
    assert len(upcoming(graph(fake, Path("/tmp")), now=NOW)) == 1


def test_countdown_and_length_are_derived_from_the_invite() -> None:
    fake = FakeMicrosoft().json_route(r"/me/calendarView", {"value": [event()]})
    found = upcoming(graph(fake, Path("/tmp")), now=NOW)[0]
    assert found.minutes_long == 30
    assert found.starts_in(NOW) == 90
    assert found.starts_in(datetime(2026, 8, 24, 10, 45, tzinfo=UTC)) == -15


def test_list_events_can_narrow_to_teams_only() -> None:
    payload = {"value": [event(), calendar_payload()["value"][0]]}
    fake = FakeMicrosoft().json_route(r"/me/calendarView", payload)
    client = graph(fake, Path("/tmp"))
    assert len(list_events(client, teams_only=False, now=NOW)) == 2
    assert [e.subject for e in list_events(client, teams_only=True, now=NOW)] == [
        "Weekly product sync"
    ]


def test_an_event_with_no_start_is_skipped_rather_than_crashing() -> None:
    fake = FakeMicrosoft().json_route(
        r"/me/calendarView", {"value": [{"id": "broken", "subject": "No when"}, event()]}
    )
    assert [e.subject for e in upcoming(graph(fake, Path("/tmp")), now=NOW)] == ["Design review"]
