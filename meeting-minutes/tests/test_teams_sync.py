"""Calendar to minutes, without Microsoft."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutely.config import Settings
from minutely.store import Store
from minutely.teams import TeamsError
from minutely.teams.auth import TeamsAuth
from minutely.teams.calendar import CalendarEvent
from minutely.teams.graph import GraphClient
from minutely.teams.sync import (
    list_meetings,
    online_meeting_id,
    pull_meeting,
    pull_recent,
    transcript_content,
)
from tests.fakes import (
    TEAMS_VTT,
    UNATTRIBUTED_TRANSCRIPT,
    FakeMicrosoft,
    calendar_payload,
    graph_error,
    json_response,
    signed_in_token,
    text_response,
)

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
JOIN_URL = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0"


def graph_client(fake: FakeMicrosoft, tmp_path: Path) -> GraphClient:
    signed_in_token(tmp_path / "teams-token.json")
    auth = TeamsAuth(
        client_id="client-id", transport=fake, token_path=tmp_path / "teams-token.json"
    )
    return GraphClient(auth, transport=fake, downloader=fake.download, sleep=lambda _s: None)


def happy_path(vtt: str = TEAMS_VTT) -> FakeMicrosoft:
    return (
        FakeMicrosoft()
        .json_route(r"/me/calendarView", calendar_payload())
        .json_route(r"/me/onlineMeetings\?", {"value": [{"id": "MSo1N2Y5ZGFjYw"}]})
        .json_route(
            r"/transcripts(\?|$)",
            {"value": [{"id": "transcript-1", "createdDateTime": "2026-08-24T09:46:00Z"}]},
        )
        .add(r"/transcripts/[^/]+/content", text_response(vtt))
    )


# -- reading the calendar ---------------------------------------------------


def test_only_teams_meetings_come_back(tmp_path: Path) -> None:
    payload = calendar_payload(
        extra=[
            {
                "id": "not-online",
                "subject": "Coffee",
                "start": {"dateTime": "2026-08-24T11:00:00.0000000"},
                "isOnlineMeeting": False,
            },
            {
                "id": "other-provider",
                "subject": "Zoom call",
                "start": {"dateTime": "2026-08-24T12:00:00.0000000"},
                "isOnlineMeeting": True,
                "onlineMeetingProvider": "skypeForConsumer",
            },
        ]
    )
    fake = FakeMicrosoft().json_route(r"/me/calendarView", payload)
    meetings = list_meetings(graph_client(fake, tmp_path), now=NOW)

    assert [m.subject for m in meetings] == ["Weekly product sync"]
    assert meetings[0].organizer == "Dana Okafor"
    assert meetings[0].attendees == ["Priya Raman", "Marcus Bell"]
    assert meetings[0].join_url == JOIN_URL


def test_the_window_is_sent_to_graph_as_utc(tmp_path: Path) -> None:
    fake = FakeMicrosoft().json_route(r"/me/calendarView", {"value": []})
    list_meetings(graph_client(fake, tmp_path), days_back=3, days_forward=1, now=NOW)
    url = fake.urls("calendarView")[0]
    assert "startDateTime=2026-08-22T09%3A00%3A00Z" in url
    assert "endDateTime=2026-08-26T09%3A00%3A00Z" in url


def test_graphs_seven_digit_timestamps_parse(tmp_path: Path) -> None:
    fake = FakeMicrosoft().json_route(
        r"/me/calendarView", calendar_payload(start="2026-08-24T09:00:00.1234567")
    )
    meeting = list_meetings(graph_client(fake, tmp_path), now=NOW)[0]
    assert meeting.start == datetime(2026, 8, 24, 9, 0, 0, 123456, tzinfo=UTC)
    assert meeting.held_on.isoformat() == "2026-08-24"


def test_a_join_url_is_resolved_to_an_online_meeting(tmp_path: Path) -> None:
    fake = FakeMicrosoft().json_route(r"/me/onlineMeetings\?", {"value": [{"id": "meeting-id"}]})
    assert online_meeting_id(graph_client(fake, tmp_path), JOIN_URL) == "meeting-id"
    assert "JoinWebUrl+eq+" in fake.urls("onlineMeetings")[0]


def test_a_quote_in_a_join_url_is_escaped_for_odata(tmp_path: Path) -> None:
    fake = FakeMicrosoft().json_route(r"/me/onlineMeetings\?", {"value": []})
    online_meeting_id(graph_client(fake, tmp_path), "https://teams.example/it's")
    # OData escapes a single quote by doubling it; percent-encoding turns each
    # into %27, so the pair must survive as %27%27.
    assert "it%27%27s" in fake.urls("onlineMeetings")[0]


# -- transcripts ------------------------------------------------------------


def test_the_speaker_attributed_form_is_asked_for_first(tmp_path: Path) -> None:
    fake = happy_path()
    text, attributed = transcript_content(graph_client(fake, tmp_path), "m", "t")
    assert attributed is True
    assert "Dana Okafor" in text
    assert "%24format=text%2Fvtt" in fake.urls("/content")[0]


def test_a_tenant_that_forbids_speaker_names_falls_back(tmp_path: Path) -> None:
    fake = FakeMicrosoft().add(
        r"/transcripts/[^/]+/content",
        graph_error("SpeakerAttributionNotAllowed"),
        text_response(UNATTRIBUTED_TRANSCRIPT, content_type="text/plain"),
    )
    text, attributed = transcript_content(graph_client(fake, tmp_path), "m", "t")
    assert attributed is False
    assert "Keycloak" in text


def test_a_blocked_tenant_is_not_papered_over(tmp_path: Path) -> None:
    fake = FakeMicrosoft().add(
        r"/transcripts/[^/]+/content", graph_error("GraphAccessToTranscriptsDisabled")
    )
    with pytest.raises(TeamsError, match="administrator"):
        transcript_content(graph_client(fake, tmp_path), "m", "t")


# -- pulling ----------------------------------------------------------------


def teams_meeting() -> CalendarEvent:
    return CalendarEvent(
        event_id="AAMkAGI2event1",
        subject="Weekly product sync",
        start=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 24, 9, 45, tzinfo=UTC),
        organizer="Dana Okafor",
        attendees=["Priya Raman", "Marcus Bell"],
        join_url=JOIN_URL,
    )


def test_pulling_a_meeting_produces_minutes_with_owners(store: Store, tmp_path: Path) -> None:
    fake = happy_path()
    result = pull_meeting(store, graph_client(fake, tmp_path), teams_meeting(), Settings())

    assert result.status == "imported"
    assert result.minutes is not None
    # Teams supplies the speaker labels that local recording cannot, which is
    # the whole reason to pull rather than re-record.
    assert result.minutes.attendees == ["Dana Okafor", "Priya Raman", "Marcus Bell"]
    owners = {action.owner for action in result.minutes.actions}
    assert "Priya Raman" in owners
    assert "Marcus Bell" in owners
    assert result.minutes.decisions

    stored = store.get_meeting(result.meeting_id)
    assert stored is not None
    assert stored.source == "teams"
    assert stored.external_id == "AAMkAGI2event1"
    assert stored.title == "Weekly product sync"
    # The transcript ran to 1:02.719; that beats the 45-minute calendar block.
    assert stored.duration == pytest.approx(62.719)
    assert Path(stored.transcript_path).exists()


def test_pulling_the_same_meeting_twice_does_nothing_the_second_time(
    store: Store, tmp_path: Path
) -> None:
    first = pull_meeting(store, graph_client(happy_path(), tmp_path), teams_meeting(), Settings())
    second = pull_meeting(store, graph_client(happy_path(), tmp_path), teams_meeting(), Settings())

    assert second.status == "skipped"
    assert second.meeting_id == first.meeting_id
    assert len(store.list_meetings()) == 1


def test_force_re_imports_an_already_pulled_meeting(store: Store, tmp_path: Path) -> None:
    first = pull_meeting(store, graph_client(happy_path(), tmp_path), teams_meeting(), Settings())
    again = pull_meeting(
        store, graph_client(happy_path(), tmp_path), teams_meeting(), Settings(), force=True
    )
    assert again.status == "updated"
    assert again.meeting_id == first.meeting_id
    assert len(store.list_meetings()) == 1


def test_a_meeting_nobody_transcribed_is_reported_not_invented(
    store: Store, tmp_path: Path
) -> None:
    fake = (
        FakeMicrosoft()
        .json_route(r"/me/onlineMeetings\?", {"value": [{"id": "meeting-id"}]})
        .json_route(r"/transcripts(\?|$)", {"value": []})
    )
    result = pull_meeting(store, graph_client(fake, tmp_path), teams_meeting(), Settings())
    assert result.status == "no-transcript"
    assert "no transcript" in result.detail.lower()
    assert store.list_meetings() == []


def test_an_invite_with_no_online_meeting_behind_it_is_reported(
    store: Store, tmp_path: Path
) -> None:
    fake = FakeMicrosoft().json_route(r"/me/onlineMeetings\?", {"value": []})
    result = pull_meeting(store, graph_client(fake, tmp_path), teams_meeting(), Settings())
    assert result.status == "no-transcript"
    assert store.list_meetings() == []


def test_a_graph_failure_becomes_an_error_result_not_a_crash(store: Store, tmp_path: Path) -> None:
    fake = (
        FakeMicrosoft()
        .json_route(r"/me/onlineMeetings\?", {"value": [{"id": "meeting-id"}]})
        .add(r"/transcripts(\?|$)", graph_error("GraphAccessToTranscriptsDisabled"))
    )
    result = pull_meeting(store, graph_client(fake, tmp_path), teams_meeting(), Settings())
    assert result.status == "error"
    assert "administrator" in result.detail


def test_an_unattributed_transcript_still_minutes_but_says_so(
    store: Store, tmp_path: Path
) -> None:
    fake = (
        FakeMicrosoft()
        .json_route(r"/me/onlineMeetings\?", {"value": [{"id": "meeting-id"}]})
        .json_route(r"/transcripts(\?|$)", {"value": [{"id": "t1"}]})
        .add(
            r"/transcripts/[^/]+/content",
            graph_error("SpeakerAttributionNotAllowed"),
            text_response(UNATTRIBUTED_TRANSCRIPT, content_type="text/plain"),
        )
    )
    result = pull_meeting(store, graph_client(fake, tmp_path), teams_meeting(), Settings())
    assert result.status == "imported"
    assert "no speaker attribution" in result.detail
    assert result.minutes is not None
    assert result.minutes.attendees == []

    # With no cue timings to measure, the booked length is the best available.
    stored = store.get_meeting(result.meeting_id)
    assert stored is not None and stored.duration == 45 * 60


def test_the_latest_transcript_wins_when_a_meeting_has_several(
    store: Store, tmp_path: Path
) -> None:
    fake = (
        FakeMicrosoft()
        .json_route(r"/me/onlineMeetings\?", {"value": [{"id": "meeting-id"}]})
        .json_route(
            r"/transcripts(\?|$)",
            {
                "value": [
                    {"id": "older", "createdDateTime": "2026-08-24T09:10:00Z"},
                    {"id": "newer", "createdDateTime": "2026-08-24T09:46:00Z"},
                ]
            },
        )
        .add(r"/transcripts/[^/]+/content", text_response(TEAMS_VTT))
    )
    pull_meeting(store, graph_client(fake, tmp_path), teams_meeting(), Settings())
    assert "/transcripts/newer/content" in fake.urls("/content")[0]


def test_recordings_are_downloaded_only_when_asked_for(store: Store, tmp_path: Path) -> None:
    fake = happy_path().json_route(
        r"/recordings(\?|$)", {"value": [{"id": "rec-1", "createdDateTime": "2026-08-24T09:46:00Z"}]}
    )
    graph = graph_client(fake, tmp_path)

    without = pull_meeting(store, graph, teams_meeting(), Settings())
    assert fake.downloads == []

    store.delete_meeting(without.meeting_id)
    result = pull_meeting(store, graph, teams_meeting(), Settings(), with_recording=True)
    assert result.status == "imported"
    assert len(fake.downloads) == 1
    stored = store.get_meeting(result.meeting_id)
    assert stored is not None and stored.audio_path.endswith(".mp4")
    assert Path(stored.audio_path).exists()


def test_a_failed_recording_does_not_lose_the_transcript(store: Store, tmp_path: Path) -> None:
    fake = happy_path().add(r"/recordings(\?|$)", graph_error("Forbidden", status=403))
    result = pull_meeting(
        store, graph_client(fake, tmp_path), teams_meeting(), Settings(), with_recording=True
    )
    assert result.status == "imported"
    assert "recording failed" in result.detail
    assert result.minutes is not None


def test_pull_recent_walks_the_calendar(store: Store, tmp_path: Path) -> None:
    fake = happy_path()
    seen: list[str] = []
    results = pull_recent(
        store,
        graph_client(fake, tmp_path),
        Settings(),
        on_progress=lambda meeting: seen.append(meeting.subject),
    )
    assert seen == ["Weekly product sync"]
    assert [r.status for r in results] == ["imported"]


def test_match_filters_before_anything_is_imported(store: Store, tmp_path: Path) -> None:
    payload = calendar_payload(
        extra=[
            {
                "id": "other-event",
                "subject": "Board prep",
                "start": {"dateTime": "2026-08-24T14:00:00.0000000"},
                "isOnlineMeeting": True,
                "onlineMeetingProvider": "teamsForBusiness",
                "onlineMeeting": {"joinUrl": JOIN_URL},
            }
        ]
    )
    fake = happy_path()
    fake.override(r"/me/calendarView", json_response(payload))
    seen: list[str] = []

    results = pull_recent(
        store,
        graph_client(fake, tmp_path),
        Settings(),
        match="product",
        on_progress=lambda meeting: seen.append(meeting.subject),
    )
    assert seen == ["Weekly product sync"]
    assert len(results) == 1
    assert [m.title for m in store.list_meetings()] == ["Weekly product sync"]
