"""The Graph client: paging, retries, and turning Microsoft's 403s into English."""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from minutely.teams import TeamsAuthError, TeamsError
from minutely.teams.auth import TeamsAuth
from minutely.teams.graph import GraphClient
from minutely.teams.transport import Response, _StripAuthOnRedirect
from tests.fakes import FakeMicrosoft, graph_error, json_response, signed_in_token


def client(fake: FakeMicrosoft, tmp_path: Path) -> GraphClient:
    signed_in_token(tmp_path / "teams-token.json")
    auth = TeamsAuth(
        client_id="client-id",
        transport=fake,
        token_path=tmp_path / "teams-token.json",
        sleep=lambda _s: None,
    )
    return GraphClient(auth, transport=fake, downloader=fake.download, sleep=lambda _s: None)


def test_requests_carry_the_bearer_token(tmp_path: Path) -> None:
    fake = FakeMicrosoft().json_route(r"/me/calendarView", {"value": []})
    client(fake, tmp_path).get_json("/me/calendarView")
    assert fake.calls[0].headers["Authorization"] == "Bearer access-token-value"


def test_paging_follows_the_next_link(tmp_path: Path) -> None:
    fake = FakeMicrosoft().add(
        r"/me/calendarView",
        json_response(
            {
                "value": [{"id": "one"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?page=2",
            }
        ),
        json_response({"value": [{"id": "two"}]}),
    )
    rows = list(client(fake, tmp_path).paged("/me/calendarView"))
    assert [row["id"] for row in rows] == ["one", "two"]


def test_paging_stops_at_the_limit(tmp_path: Path) -> None:
    fake = FakeMicrosoft().json_route(
        r"/me/calendarView",
        {"value": [{"id": str(i)} for i in range(10)], "@odata.nextLink": "https://x/next"},
    )
    assert len(list(client(fake, tmp_path).paged("/me/calendarView", limit=3))) == 3


def test_an_unprompted_401_refreshes_once_and_retries(tmp_path: Path) -> None:
    fake = (
        FakeMicrosoft()
        .add(
            r"/me/calendarView",
            json_response({"error": {"code": "InvalidAuthenticationToken"}}, 401),
            json_response({"value": [{"id": "one"}]}),
        )
        .add(
            r"/oauth2/v2.0/token",
            json_response({"access_token": "second-token", "expires_in": 3600}),
        )
    )
    assert client(fake, tmp_path).get_json("/me/calendarView")["value"] == [{"id": "one"}]
    assert fake.calls[-1].headers["Authorization"] == "Bearer second-token"


def test_a_second_401_is_reported_rather_than_looped_on(tmp_path: Path) -> None:
    fake = (
        FakeMicrosoft()
        .add(r"/me/calendarView", json_response({"error": {"code": "InvalidAuthenticationToken"}}, 401))
        .add(r"/oauth2/v2.0/token", json_response({"access_token": "second", "expires_in": 3600}))
    )
    with pytest.raises(TeamsAuthError, match="minutely teams login"):
        client(fake, tmp_path).get_json("/me/calendarView")


def test_throttling_is_retried_after_the_interval_microsoft_asks_for(tmp_path: Path) -> None:
    waits: list[float] = []
    fake = FakeMicrosoft().add(
        r"/me/calendarView",
        Response(status=429, body=b"{}", headers={"retry-after": "7", "content-type": "application/json"}),
        json_response({"value": []}),
    )
    signed_in_token(tmp_path / "teams-token.json")
    auth = TeamsAuth(client_id="c", transport=fake, token_path=tmp_path / "teams-token.json")
    graph = GraphClient(auth, transport=fake, sleep=waits.append)
    assert graph.get_json("/me/calendarView") == {"value": []}
    assert waits == [7.0]


def test_a_tenant_that_blocks_transcript_access_is_explained(tmp_path: Path) -> None:
    fake = FakeMicrosoft().add(
        r"/transcripts", graph_error("GraphAccessToTranscriptsDisabled", "Forbidden")
    )
    with pytest.raises(TeamsError, match="administrator has turned off"):
        client(fake, tmp_path).get_json("/me/onlineMeetings/abc/transcripts")


def test_missing_consent_is_explained(tmp_path: Path) -> None:
    fake = FakeMicrosoft().add(r"/transcripts", graph_error("Authorization_RequestDenied"))
    with pytest.raises(TeamsError, match="missing consent"):
        client(fake, tmp_path).get_json("/me/onlineMeetings/abc/transcripts")


def test_a_404_names_the_resource(tmp_path: Path) -> None:
    fake = FakeMicrosoft().add(r"/transcripts", json_response({"error": {"code": "NotFound"}}, 404))
    with pytest.raises(TeamsError, match="/me/onlineMeetings/abc/transcripts"):
        client(fake, tmp_path).get_json("/me/onlineMeetings/abc/transcripts")


def test_downloads_stream_through_the_downloader(tmp_path: Path) -> None:
    fake = FakeMicrosoft()
    destination = tmp_path / "recording.mp4"
    written = client(fake, tmp_path).download("/me/onlineMeetings/abc/recordings/1/content", destination)
    assert written == len(fake.download_bytes)
    assert destination.read_bytes() == fake.download_bytes
    url, headers, _ = fake.downloads[0]
    assert url.endswith("/recordings/1/content")
    assert headers["Authorization"] == "Bearer access-token-value"


def test_a_cross_host_redirect_does_not_carry_the_bearer_token() -> None:
    # Graph answers a recording download with a redirect to Azure storage.
    # urllib would otherwise replay every header at the new host.
    handler = _StripAuthOnRedirect()
    request = urllib.request.Request("https://graph.microsoft.com/v1.0/x/content")
    request.add_header("Authorization", "Bearer secret")
    following = handler.redirect_request(
        request, None, 302, "Found", {}, "https://storage.example.net/blob"
    )
    assert following is not None
    assert "Authorization" not in following.headers
    assert "authorization" not in {k.lower() for k in following.headers}


def test_a_same_host_redirect_keeps_the_token() -> None:
    handler = _StripAuthOnRedirect()
    request = urllib.request.Request("https://graph.microsoft.com/v1.0/x/content")
    request.add_header("Authorization", "Bearer secret")
    following = handler.redirect_request(
        request, None, 302, "Found", {}, "https://graph.microsoft.com/v1.0/y"
    )
    assert following is not None
    assert following.headers.get("Authorization") == "Bearer secret"
