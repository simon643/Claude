"""A fake Microsoft, so the Teams tests need no tenant and no network.

Requests are matched against regexes on the URL and answered from a queue, and
every call is recorded so a test can assert on what was actually asked for —
the OData filter, the ``$format``, the ``Authorization`` header.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minutely.teams.transport import Response


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None

    @property
    def form(self) -> dict[str, str]:
        import urllib.parse

        raw = (self.body or b"").decode("utf-8")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}


class FakeMicrosoft:
    """A transport that answers from a routing table."""

    def __init__(self) -> None:
        self._routes: list[tuple[re.Pattern[str], list[Response]]] = []
        self.calls: list[Call] = []
        self.downloads: list[tuple[str, dict[str, str], Path]] = []
        self.download_bytes = b"fake mp4 payload"

    def add(self, pattern: str, *responses: Response) -> FakeMicrosoft:
        self._routes.append((re.compile(pattern), list(responses)))
        return self

    def json_route(self, pattern: str, payload: Any, status: int = 200) -> FakeMicrosoft:
        return self.add(pattern, json_response(payload, status))

    def override(self, pattern: str, *responses: Response) -> FakeMicrosoft:
        """Add a route that takes precedence over the ones already there."""
        self._routes.insert(0, (re.compile(pattern), list(responses)))
        return self

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> Response:
        self.calls.append(Call(method, url, dict(headers), body))
        for pattern, queued in self._routes:
            if pattern.search(url):
                # The last queued response repeats, so a test only has to
                # enumerate the responses that differ.
                return queued.pop(0) if len(queued) > 1 else queued[0]
        raise AssertionError(f"no fake route for {method} {url}")

    def download(self, url: str, headers: dict[str, str], destination: Path) -> int:
        self.downloads.append((url, dict(headers), destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.download_bytes)
        return len(self.download_bytes)

    def urls(self, needle: str = "") -> list[str]:
        return [call.url for call in self.calls if needle in call.url]


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def text_response(text: str, status: int = 200, content_type: str = "text/vtt") -> Response:
    return Response(
        status=status, body=text.encode("utf-8"), headers={"content-type": content_type}
    )


def graph_error(code: str, message: str = "", status: int = 403) -> Response:
    return json_response(
        {"error": {"code": "Forbidden", "message": message, "innerError": {"code": code}}},
        status,
    )


# A transcript in the exact dialect Teams emits: a GUID cue identifier, no
# zero padding in the timestamps, and <v Name> voice spans.
TEAMS_VTT = """WEBVTT

0d1e8f6a-9f1c-4a4e-9a2f-1b2c3d4e5f60/9-0
0:0:2.16 --> 0:0:11.5
<v Dana Okafor>Morning everyone. Three things today: the pilot rollout, pricing, and the backlog.</v>

0d1e8f6a-9f1c-4a4e-9a2f-1b2c3d4e5f60/10-0
0:0:12.0 --> 0:0:29.44
<v Priya Raman>I'll spin up a Keycloak instance and run the integration suite. Give me until Thursday.</v>

0d1e8f6a-9f1c-4a4e-9a2f-1b2c3d4e5f60/11-0
0:0:30.0 --> 0:0:44.907
<v Dana Okafor>Marcus, can you let the two remaining customers know we're on it?</v>

0d1e8f6a-9f1c-4a4e-9a2f-1b2c3d4e5f60/12-0
0:0:45.2 --> 0:1:2.719
<v Marcus Bell>Yep. Let's go with the sixty-five price and bundle analytics. That's decided.</v>
"""

UNATTRIBUTED_TRANSCRIPT = (
    "Morning everyone. Three things today.\n"
    "I'll spin up a Keycloak instance and run the integration suite.\n"
)


def calendar_payload(
    *,
    event_id: str = "AAMkAGI2event1",
    subject: str = "Weekly product sync",
    start: str = "2026-08-24T09:00:00.0000000",
    end: str = "2026-08-24T09:45:00.0000000",
    join_url: str = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0",
    extra: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = [
        {
            "id": event_id,
            "subject": subject,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "isOnlineMeeting": True,
            "onlineMeetingProvider": "teamsForBusiness",
            "onlineMeeting": {"joinUrl": join_url},
            "organizer": {"emailAddress": {"name": "Dana Okafor", "address": "dana@example.com"}},
            "attendees": [
                {"emailAddress": {"name": "Priya Raman", "address": "priya@example.com"}},
                {"emailAddress": {"name": "Marcus Bell", "address": "marcus@example.com"}},
            ],
        }
    ]
    events.extend(extra or [])
    return {"value": events}


def signed_in_token(path: Path, *, expires_at: float = 4102444800.0, scopes: list[str] | None = None) -> Path:
    """Write a token file that looks like a completed sign-in."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "access_token": "access-token-value",
                "refresh_token": "refresh-token-value",
                "expires_at": expires_at,
                "scopes": scopes or ["Calendars.Read", "OnlineMeetingTranscript.Read.All"],
                "account": "dana@example.com",
                "tenant": "organizations",
                "client_id": "11111111-2222-3333-4444-555555555555",
            }
        ),
        encoding="utf-8",
    )
    return path
