"""The local dashboard: its guards, and the endpoints the page relies on."""

from __future__ import annotations

import http.server
import json
import secrets
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from functools import partial
from typing import Any

import pytest

from apflow.server import AppState, Handler
from apflow.workflow import Engine
from tests.conftest import RecordingProvider, make_document


@pytest.fixture
def dashboard(engine: Engine) -> Iterator[tuple[str, str]]:
    engine.submit(make_document(number="B-1", total="4000.00"), requested_by="priya")
    token = secrets.token_urlsafe(16)
    state = AppState(engine, RecordingProvider(), token)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, state=state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", token
    finally:
        server.shutdown()
        server.server_close()


def _call(base: str, path: str, token: str | None = None, body: Any = None,
          host: str | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Session-Token"] = token
    if host:
        headers["Host"] = host
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_the_page_carries_the_token_not_the_url(dashboard) -> None:
    base, token = dashboard
    with urllib.request.urlopen(base + "/") as response:
        page = response.read().decode()
    assert "__SESSION_TOKEN__" not in page
    assert token in page


def test_an_unauthenticated_api_call_is_refused(dashboard) -> None:
    base, _ = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _call(base, "/api/overview")
    assert excinfo.value.code == 403


def test_a_wrong_token_is_refused(dashboard) -> None:
    base, _ = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _call(base, "/api/overview", token="not-the-token")
    assert excinfo.value.code == 403


def test_a_non_loopback_host_header_is_refused(dashboard) -> None:
    """The DNS-rebinding guard: an attacker's domain resolving to 127.0.0.1."""
    base, token = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _call(base, "/api/overview", token=token, host="evil.example")
    assert excinfo.value.code == 403


def test_security_headers_are_present(dashboard) -> None:
    base, _ = dashboard
    with urllib.request.urlopen(base + "/") as response:
        headers = response.headers
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"


def test_overview_feeds_the_page(dashboard) -> None:
    base, token = dashboard
    payload = _call(base, "/api/overview", token=token)
    assert payload["summary"]["pending"]["count"] == 1
    assert payload["queue"]["entries"][0]["waiting_on"] == ["Dana"]
    assert any(user["id"] == "dana" for user in payload["users"])


def test_inbox_filters_to_one_person(dashboard) -> None:
    base, token = dashboard
    assert len(_call(base, "/api/inbox?user=dana", token=token)["items"]) == 1
    assert _call(base, "/api/inbox?user=jo", token=token)["items"] == []


def test_deciding_through_the_api(dashboard) -> None:
    base, token = dashboard
    result = _call(base, "/api/decide", token=token,
                   body={"reference": "AR-0001", "user": "dana", "decision": "approve",
                         "comment": "fine"})
    assert result["ok"] and result["step_closed"]
    view = _call(base, "/api/request?id=AR-0001", token=token)
    assert view["steps"][0]["state"] == "approved"


def test_a_refused_decision_comes_back_as_an_error_not_a_crash(dashboard) -> None:
    base, token = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _call(base, "/api/decide", token=token,
              body={"reference": "AR-0001", "user": "jo", "decision": "approve"})
    assert excinfo.value.code == 400
    assert "not their turn" in json.loads(excinfo.value.read())["error"]


def test_an_unknown_path_is_a_clean_404(dashboard) -> None:
    base, token = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _call(base, "/api/nope", token=token)
    assert excinfo.value.code == 404
