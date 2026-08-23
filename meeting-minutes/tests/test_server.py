"""The local server, exercised over real HTTP."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from minutely import pipeline
from minutely.config import Settings
from minutely.server import build_server
from minutely.store import Store
from tests.conftest import DEMO
from tests.fakes import json_response


class Client:
    def __init__(self, base: str, token: str, store: Store) -> None:
        self.base = base
        self.token = token
        self.store = store

    def get(self, path: str, token: str | None = "", host: str | None = None) -> tuple[int, Any]:
        return self._call("GET", path, None, None, token, host)

    def post(
        self,
        path: str,
        payload: Any = None,
        raw: bytes | None = None,
        token: str | None = "",
        host: str | None = None,
    ) -> tuple[int, Any]:
        return self._call("POST", path, payload, raw, token, host)

    def _call(
        self,
        method: str,
        path: str,
        payload: Any,
        raw: bytes | None,
        token: str | None,
        host: str | None,
    ) -> tuple[int, Any]:
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        request = urllib.request.Request(self.base + path, data=body, method=method)
        if raw is None and payload is not None:
            request.add_header("Content-Type", "application/json")
        if token != "":
            if token is not None:
                request.add_header("X-Session-Token", token)
        else:
            request.add_header("X-Session-Token", self.token)
        if host:
            request.add_header("Host", host)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, _decode(response.headers, response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.headers, exc.read())


def _decode(headers: Any, body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace")
    if "json" in (headers.get("Content-Type") or ""):
        return json.loads(text)
    return text


@pytest.fixture
def client(store: Store) -> Iterator[Client]:
    server, url = build_server(store, Settings(auto_process=False), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base, _, query = url.partition("?")
    token = urllib.parse.parse_qs(query)["token"][0]
    try:
        yield Client(base.rstrip("/"), token, store)
    finally:
        server.shutdown()
        server.server_close()


# -- defences ---------------------------------------------------------------


def test_api_requires_the_session_token(client: Client) -> None:
    status, payload = client.get("/api/meetings", token=None)
    assert status == 401
    assert "token" in payload["error"]


def test_a_wrong_token_is_rejected(client: Client) -> None:
    status, _ = client.get("/api/meetings", token="not-the-token")
    assert status == 401


def test_a_non_loopback_host_header_is_rejected(client: Client) -> None:
    # This is what a DNS rebinding attack looks like from the server's side.
    status, payload = client.get("/api/meetings", host="attacker.example")
    assert status == 403
    assert "localhost" in payload["error"]


def test_the_page_itself_needs_no_token_but_carries_one(client: Client) -> None:
    status, body = client.get("/", token=None)
    assert status == 200
    assert "__SESSION_TOKEN__" not in body
    assert client.token in body


def test_unknown_endpoints_are_404(client: Client) -> None:
    assert client.get("/api/nope")[0] == 404
    assert client.post("/api/nope", {})[0] == 404


def test_responses_carry_a_restrictive_policy(client: Client) -> None:
    request = urllib.request.Request(client.base + "/")
    with urllib.request.urlopen(request, timeout=10) as response:
        policy = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy


# -- recording --------------------------------------------------------------


def test_a_recording_round_trip_writes_one_audio_file(client: Client) -> None:
    status, started = client.post("/api/record/start", {"title": "Standup", "mime": "audio/webm"})
    assert status == 200
    meeting_id = started["meeting"]["meeting_id"]

    for chunk in (b"chunk-one", b"chunk-two"):
        status, _ = client.post(
            f"/api/record/chunk?meeting={meeting_id}", raw=chunk
        )
        assert status == 200

    status, stopped = client.post(
        "/api/record/stop", {"meeting_id": meeting_id, "duration": 12.5, "process": False}
    )
    assert status == 200
    assert stopped["bytes"] == len(b"chunk-onechunk-two")

    audio = Path(stopped["meeting"]["audio_path"])
    assert audio.read_bytes() == b"chunk-onechunk-two"
    assert audio.suffix == ".webm"
    assert stopped["meeting"]["duration"] == 12.5
    assert stopped["meeting"]["title"] == "Standup"


def test_stopping_an_empty_recording_reports_the_likely_cause(client: Client) -> None:
    _, started = client.post("/api/record/start", {"mime": "audio/webm"})
    status, payload = client.post(
        "/api/record/stop", {"meeting_id": started["meeting"]["meeting_id"], "process": False}
    )
    assert status == 400
    assert "microphone permission" in payload["error"]


def test_chunks_for_an_unknown_meeting_are_refused(client: Client) -> None:
    status, payload = client.post("/api/record/chunk?meeting=made-up", raw=b"x")
    assert status == 400
    assert "unknown meeting" in payload["error"]


# -- minutes and the register ----------------------------------------------


def test_processing_a_meeting_produces_minutes(client: Client, demo_path: Path) -> None:
    meeting, _ = pipeline.import_file(client.store, demo_path, title="Demo")
    status, _ = client.post("/api/process", {"meeting_id": meeting.meeting_id, "engine": "rules"})
    assert status == 200

    for _ in range(100):
        _, job = client.get(f"/api/job?meeting={meeting.meeting_id}")
        if job["state"] in {"done", "error"}:
            break
        time.sleep(0.1)
    assert job["state"] == "done", job

    status, payload = client.get(f"/api/meeting?id={meeting.meeting_id}")
    assert status == 200
    assert payload["minutes"]["actions"]
    assert "## Action points" in payload["markdown"]


def test_actions_can_be_ticked_off_over_the_api(client: Client, demo_path: Path) -> None:
    meeting, _ = pipeline.import_file(client.store, demo_path)
    pipeline.make_minutes(client.store, meeting, engine="rules")

    _, listed = client.get("/api/actions?status=open")
    action_id = listed["actions"][0]["id"]
    status, _ = client.post("/api/action", {"id": action_id, "status": "done"})
    assert status == 200

    _, still_open = client.get("/api/actions?status=open")
    assert action_id not in [row["id"] for row in still_open["actions"]]
    _, done = client.get("/api/actions?status=done")
    assert action_id in [row["id"] for row in done["actions"]]


def test_ticking_off_an_unknown_action_is_an_error(client: Client) -> None:
    status, payload = client.post("/api/action", {"id": 9999, "status": "done"})
    assert status == 400
    assert "9999" in payload["error"]


def test_export_renders_html(client: Client, demo_path: Path) -> None:
    meeting, _ = pipeline.import_file(client.store, demo_path, title="Demo")
    pipeline.make_minutes(client.store, meeting, engine="rules")
    status, body = client.get(f"/api/export?id={meeting.meeting_id}&fmt=html")
    assert status == 200
    assert body.startswith("<!doctype html>")


def test_export_without_minutes_is_a_404(client: Client, demo_path: Path) -> None:
    meeting, _ = pipeline.import_file(client.store, demo_path)
    status, payload = client.get(f"/api/export?id={meeting.meeting_id}")
    assert status == 404
    assert "minutes" in payload["error"]


def test_state_reports_what_is_available(client: Client) -> None:
    status, payload = client.get("/api/state")
    assert status == 200
    assert payload["engine"] == "rules"
    assert "transcriber_ready" in payload
    assert payload["stats"]["meetings"] == 0


# -- teams ------------------------------------------------------------------


@pytest.fixture
def teams_client(store: Store) -> Iterator[tuple[Client, Any]]:
    """A server whose Teams calls go to a fake Microsoft."""
    from minutely.teams.auth import TeamsAuth
    from minutely.teams.graph import GraphClient
    from tests.fakes import TEAMS_VTT, FakeMicrosoft, calendar_payload, json_response, text_response

    fake = (
        FakeMicrosoft()
        .add(
            r"/devicecode",
            json_response(
                {
                    "device_code": "device-code",
                    "user_code": "H7XK2M9P",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "message": "Enter H7XK2M9P at https://microsoft.com/devicelogin",
                    "interval": 1,
                    "expires_in": 900,
                }
            ),
        )
        .add(
            r"/oauth2/v2.0/token",
            json_response(
                {
                    "access_token": "token",
                    "refresh_token": "r",
                    "expires_in": 3600,
                    "scope": (
                        "Calendars.Read OnlineMeetingTranscript.Read.All "
                        "https://graph.microsoft.com/Mail.Send"
                    ),
                }
            ),
        )
        .json_route(r"/me/calendarView", calendar_payload())
        .json_route(r"/me/onlineMeetings\?", {"value": [{"id": "meeting-id"}]})
        .json_route(r"/transcripts(\?|$)", {"value": [{"id": "t1"}]})
        .add(r"/transcripts/[^/]+/content", text_response(TEAMS_VTT))
    )

    token_path = store.path.parent / "teams-token.json"

    def auth() -> TeamsAuth:
        return TeamsAuth(
            client_id="client-id",
            transport=fake,
            token_path=token_path,
            sleep=lambda _s: None,
        )

    settings = Settings(auto_process=False, teams_client_id="client-id")
    server, url = build_server(
        store,
        settings,
        port=0,
        teams_auth=auth,
        teams_client=lambda: GraphClient(
            auth(), transport=fake, downloader=fake.download, sleep=lambda _s: None
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base, _, query = url.partition("?")
    token = urllib.parse.parse_qs(query)["token"][0]
    try:
        yield Client(base.rstrip("/"), token, store), fake
    finally:
        server.shutdown()
        server.server_close()


def _await_job(client: Client, name: str) -> dict[str, Any]:
    for _ in range(100):
        _, state = client.get("/api/teams/state")
        job = state[name]
        if job["state"] in {"done", "error"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"teams {name} job never finished")


def test_teams_state_reports_configuration(teams_client: tuple[Client, Any]) -> None:
    client, _fake = teams_client
    status, payload = client.get("/api/teams/state")
    assert status == 200
    assert payload["configured"] is True
    assert payload["signed_in"] is False


def test_signing_in_returns_a_code_and_completes_in_the_background(
    teams_client: tuple[Client, Any],
) -> None:
    client, _fake = teams_client
    status, started = client.post("/api/teams/login", {})
    assert status == 200
    assert started["user_code"] == "H7XK2M9P"
    assert started["verification_uri"].startswith("https://microsoft.com")

    assert _await_job(client, "login")["state"] == "done"
    _, payload = client.get("/api/teams/state")
    assert payload["signed_in"] is True


def test_pulling_from_the_ui_imports_and_minutes(teams_client: tuple[Client, Any]) -> None:
    client, _fake = teams_client
    client.post("/api/teams/login", {})
    _await_job(client, "login")

    status, _ = client.post("/api/teams/pull", {"days": 30})
    assert status == 200
    job = _await_job(client, "pull")
    assert job["state"] == "done", job
    assert "1 of 1" in job["message"]

    _, meetings = client.get("/api/meetings")
    assert [m["title"] for m in meetings["meetings"]] == ["Weekly product sync"]
    assert meetings["meetings"][0]["source"] == "teams"

    _, actions = client.get("/api/actions?status=open")
    assert any(row["owner"] == "Priya Raman" for row in actions["actions"])


def test_a_teams_failure_surfaces_as_a_job_error(teams_client: tuple[Client, Any]) -> None:
    from tests.fakes import graph_error

    client, fake = teams_client
    client.post("/api/teams/login", {})
    _await_job(client, "login")
    # Re-route transcripts to a tenant that blocks Graph access.
    fake.override(r"/transcripts(\?|$)", graph_error("GraphAccessToTranscriptsDisabled"))

    client.post("/api/teams/pull", {"days": 30})
    job = _await_job(client, "pull")
    assert job["state"] == "error"
    assert "administrator" in job["message"]


# -- playback ---------------------------------------------------------------


def _recorded(client: Client, body: bytes = b"0123456789abcdef") -> str:
    _, started = client.post("/api/record/start", {"title": "Playback", "mime": "audio/webm"})
    meeting_id = started["meeting"]["meeting_id"]
    client.post(f"/api/record/chunk?meeting={meeting_id}", raw=body)
    client.post("/api/record/stop", {"meeting_id": meeting_id, "duration": 5, "process": False})
    return meeting_id


def _raw_get(client: Client, path: str, headers: dict[str, str] | None = None, method: str = "GET") -> Any:
    request = urllib.request.Request(client.base + path, method=method)
    request.add_header("X-Session-Token", client.token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def test_a_saved_recording_can_be_played_back(client: Client) -> None:
    meeting_id = _recorded(client)
    status, body, headers = _raw_get(client, f"/api/audio?id={meeting_id}")
    assert status == 200
    assert body == b"0123456789abcdef"
    assert headers["Content-Type"] == "audio/webm"
    assert headers["Accept-Ranges"] == "bytes"


def test_playback_supports_seeking_with_range_requests(client: Client) -> None:
    meeting_id = _recorded(client)
    status, body, headers = _raw_get(
        client, f"/api/audio?id={meeting_id}", {"Range": "bytes=4-8"}
    )
    assert status == 206
    assert body == b"45678"
    assert headers["Content-Range"] == "bytes 4-8/16"
    assert headers["Content-Length"] == "5"


def test_an_open_ended_range_runs_to_the_end(client: Client) -> None:
    meeting_id = _recorded(client)
    status, body, _ = _raw_get(client, f"/api/audio?id={meeting_id}", {"Range": "bytes=10-"})
    assert status == 206
    assert body == b"abcdef"


def test_a_suffix_range_returns_the_tail(client: Client) -> None:
    meeting_id = _recorded(client)
    status, body, _ = _raw_get(client, f"/api/audio?id={meeting_id}", {"Range": "bytes=-4"})
    assert status == 206
    assert body == b"cdef"


def test_an_unsatisfiable_range_is_refused(client: Client) -> None:
    meeting_id = _recorded(client)
    status, _body, headers = _raw_get(
        client, f"/api/audio?id={meeting_id}", {"Range": "bytes=999-1200"}
    )
    assert status == 416
    assert headers["Content-Range"] == "bytes */16"


def test_players_can_probe_with_head(client: Client) -> None:
    meeting_id = _recorded(client)
    status, body, headers = _raw_get(client, f"/api/audio?id={meeting_id}", method="HEAD")
    assert status == 200
    assert body == b""
    assert headers["Content-Length"] == "16"


def test_audio_needs_the_session_token(client: Client) -> None:
    meeting_id = _recorded(client)
    request = urllib.request.Request(client.base + f"/api/audio?id={meeting_id}")
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("expected a 401")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401


def test_a_meeting_with_no_recording_says_so(client: Client, demo_path: Path) -> None:
    meeting, _ = pipeline.import_file(client.store, demo_path)
    status, payload = client.get(f"/api/audio?id={meeting.meeting_id}")
    assert status == 404
    assert "no recording" in payload["error"]


def test_a_recording_deleted_from_disk_is_reported(client: Client) -> None:
    meeting_id = _recorded(client)
    meeting = client.store.get_meeting(meeting_id)
    assert meeting is not None
    Path(meeting.audio_path).unlink()
    status, payload = client.get(f"/api/audio?id={meeting_id}")
    assert status == 404
    assert "missing from disk" in payload["error"]


def test_the_policy_allows_playing_our_own_audio(client: Client) -> None:
    request = urllib.request.Request(client.base + "/")
    with urllib.request.urlopen(request, timeout=10) as response:
        policy = response.headers["Content-Security-Policy"]
    assert "media-src 'self' blob:" in policy


# -- recording from the calendar -------------------------------------------


def test_starting_from_a_calendar_entry_carries_the_invite(client: Client) -> None:
    _, started = client.post(
        "/api/record/start",
        {
            "title": "Weekly product sync",
            "mime": "audio/webm",
            "event_id": "AAMkAGI2event1",
            "attendees": ["Priya Raman", "Marcus Bell"],
            "emails": ["priya@example.com", "marcus@example.com"],
        },
    )
    meeting = client.store.get_meeting(started["meeting"]["meeting_id"])
    assert meeting is not None
    assert meeting.source == "teams"
    assert meeting.external_id == "AAMkAGI2event1"
    assert meeting.participants == ["Priya Raman", "Marcus Bell"]
    # The addresses are what makes "share these minutes" a one-click job.
    assert meeting.emails == ["priya@example.com", "marcus@example.com"]


def test_a_locally_recorded_calendar_meeting_is_found_by_its_event_id(client: Client) -> None:
    client.post(
        "/api/record/start",
        {"title": "Sync", "mime": "audio/webm", "event_id": "AAMkAGI2event1"},
    )
    # This is what stops `teams pull` importing a second copy of a meeting you
    # already recorded yourself.
    assert client.store.find_external("teams", "AAMkAGI2event1") is not None


# -- calendar and sharing over HTTP ----------------------------------------


def test_upcoming_is_empty_until_microsoft_is_connected(client: Client) -> None:
    status, payload = client.get("/api/calendar/upcoming")
    assert status == 200
    assert payload == {"signed_in": False, "events": []}


def test_upcoming_lists_the_calendar_and_flags_what_is_already_recorded(
    teams_client: tuple[Client, Any],
) -> None:
    client, _fake = teams_client
    client.post("/api/teams/login", {})
    _await_job(client, "login")

    status, payload = client.get("/api/calendar/upcoming?hours=99999")
    assert status == 200
    assert payload["signed_in"] is True
    event = payload["events"][0]
    assert event["title"] == "Weekly product sync"
    assert event["emails"] == ["priya@example.com", "marcus@example.com"]
    assert event["recorded_as"] == ""

    # Record it, and the same entry now points at the local meeting.
    _, started = client.post(
        "/api/record/start",
        {"title": event["title"], "mime": "audio/webm", "event_id": event["event_id"]},
    )
    _, payload = client.get("/api/calendar/upcoming?hours=99999")
    assert payload["events"][0]["recorded_as"] == started["meeting"]["meeting_id"]


def test_sharing_minutes_sends_them_through_microsoft(teams_client: tuple[Client, Any]) -> None:
    client, fake = teams_client
    fake.add(r"/me/sendMail", json_response({}, 202))
    client.post("/api/teams/login", {})
    _await_job(client, "login")

    meeting, _ = pipeline.import_file(client.store, DEMO, title="Demo")
    meeting.emails = ["dana@example.com"]
    client.store.upsert_meeting(meeting)
    pipeline.make_minutes(client.store, meeting, engine="rules")

    status, payload = client.post(
        "/api/share", {"meeting_id": meeting.meeting_id, "note": "As discussed."}
    )
    assert status == 200
    assert payload["via"] == "graph"
    assert payload["recipients"] == ["dana@example.com"]
    assert fake.urls("sendMail")


def test_sharing_a_meeting_with_no_minutes_is_refused(teams_client: tuple[Client, Any]) -> None:
    client, _fake = teams_client
    meeting, _ = pipeline.import_file(client.store, DEMO)
    status, payload = client.post("/api/share", {"meeting_id": meeting.meeting_id})
    assert status == 400
    assert "no minutes" in payload["error"]


def test_sharing_with_no_recipients_says_so(teams_client: tuple[Client, Any]) -> None:
    client, _fake = teams_client
    meeting, _ = pipeline.import_file(client.store, DEMO)
    pipeline.make_minutes(client.store, meeting, engine="rules")
    status, payload = client.post("/api/share", {"meeting_id": meeting.meeting_id, "to": []})
    assert status == 400
    assert "no recipients" in payload["error"]


def test_sharing_without_any_mail_transport_explains_the_options(client: Client) -> None:
    meeting, _ = pipeline.import_file(client.store, DEMO)
    pipeline.make_minutes(client.store, meeting, engine="rules")
    status, payload = client.post(
        "/api/share", {"meeting_id": meeting.meeting_id, "to": ["someone@example.com"]}
    )
    assert status == 400
    assert "teams login --with-email" in payload["error"]


# -- notes ------------------------------------------------------------------


def test_notes_typed_in_the_browser_are_saved_as_they_go(client: Client) -> None:
    _, started = client.post("/api/record/start", {"title": "Standup", "mime": "audio/webm"})
    meeting_id = started["meeting"]["meeting_id"]

    status, payload = client.post(
        "/api/notes", {"meeting_id": meeting_id, "notes": "Pricing\n- 49 -> 65"}
    )
    assert status == 200
    assert payload["characters"] == len("Pricing\n- 49 -> 65")

    meeting = client.store.get_meeting(meeting_id)
    assert meeting is not None and meeting.notes == "Pricing\n- 49 -> 65"

    # Typing more replaces, and clearing is allowed.
    client.post("/api/notes", {"meeting_id": meeting_id, "notes": ""})
    meeting = client.store.get_meeting(meeting_id)
    assert meeting is not None and meeting.notes == ""


def test_notes_for_an_unknown_meeting_are_refused(client: Client) -> None:
    status, payload = client.post("/api/notes", {"meeting_id": "nope", "notes": "x"})
    assert status == 400
    assert "unknown meeting" in payload["error"]


def test_the_notes_reach_the_minutes(client: Client, demo_path: Path) -> None:
    meeting, _ = pipeline.import_file(client.store, demo_path, title="Demo")
    client.post(
        "/api/notes",
        {"meeting_id": meeting.meeting_id, "notes": "TODO: Priya: rebuild the index"},
    )
    client.post("/api/process", {"meeting_id": meeting.meeting_id, "engine": "rules"})
    for _ in range(100):
        _, job = client.get(f"/api/job?meeting={meeting.meeting_id}")
        if job["state"] in {"done", "error"}:
            break
        time.sleep(0.1)
    assert job["state"] == "done", job

    _, payload = client.get(f"/api/meeting?id={meeting.meeting_id}")
    assert payload["minutes"]["notes"] == "TODO: Priya: rebuild the index"
    assert any(
        a["text"] == "Rebuild the index" and a["owner"] == "Priya"
        for a in payload["minutes"]["actions"]
    )


def test_the_template_can_be_chosen_and_is_remembered(client: Client, demo_path: Path) -> None:
    meeting, _ = pipeline.import_file(client.store, demo_path)
    status, payload = client.post(
        "/api/meeting/update", {"meeting_id": meeting.meeting_id, "template": "standup"}
    )
    assert status == 200
    assert payload["meeting"]["template"] == "standup"


def test_an_unknown_template_is_refused(client: Client, demo_path: Path) -> None:
    meeting, _ = pipeline.import_file(client.store, demo_path)
    status, payload = client.post(
        "/api/meeting/update", {"meeting_id": meeting.meeting_id, "template": "haiku"}
    )
    assert status == 400
    assert "unknown template" in payload["error"]


def test_the_page_is_told_which_templates_exist(client: Client) -> None:
    _, payload = client.get("/api/state")
    names = [row["name"] for row in payload["templates"]]
    assert "default" in names and "standup" in names
    assert all({"name", "label", "description"} <= set(row) for row in payload["templates"])
