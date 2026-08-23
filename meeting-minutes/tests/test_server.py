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
