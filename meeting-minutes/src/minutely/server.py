"""The local recorder and review UI.

Recording happens in the browser because that is where the microphone
permission lives and where every platform already has a working audio stack —
no PortAudio, no device enumeration, no third-party wheel between the user and
their meeting. MediaRecorder streams chunks to this server, which appends them
to one file on disk.

The server is bound to loopback and defended three ways, because "it's only
localhost" is not a security model — every process and every user on the
machine can reach 127.0.0.1, and any website the user visits can make requests
to it:

* **Loopback bind.** The socket is bound to 127.0.0.1, never 0.0.0.0.
* **Session token.** A random token is minted per run, injected into the page,
  and required on every API request. A page the user happens to be browsing
  cannot guess it.
* **Host header check.** Requests whose Host is not a loopback literal are
  rejected, which blocks DNS rebinding.

Transcription and summarisation can take minutes, so they run on a worker
thread and the page polls for the result.
"""

from __future__ import annotations

import http.server
import json
import secrets
import socket
import threading
import urllib.parse
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

from . import pipeline, render
from .audio import duration as audio_duration
from .audio import suffix_for
from .config import Settings, ensure_dirs, recordings_dir
from .engines import EngineError
from .engines.factory import ENGINES
from .models import Meeting
from .share import ShareError, compose, send
from .share import available as share_transports
from .store import Store
from .teams import TeamsError
from .teams.auth import TeamsAuth
from .teams.calendar import upcoming
from .teams.factory import build_auth, build_client
from .teams.graph import GraphClient
from .teams.sync import pull_recent
from .templates import NAMES as TEMPLATE_NAMES
from .templates import catalogue
from .transcribers import TranscriptionError
from .transcribers.factory import get_transcriber

UI_DIR = Path(__file__).resolve().parent / "ui"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# One MediaRecorder chunk. The page sends every few seconds, so this is roomy.
MAX_CHUNK_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
# Playback is served in slices so a long meeting does not have to be read into
# memory to be scrubbed through.
STREAM_CHUNK = 256 * 1024
_AUDIO_TYPES = {
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


class AppState:
    """Shared state for the request handlers."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        token: str,
        *,
        teams_auth: Callable[[], TeamsAuth] | None = None,
        teams_client: Callable[[], GraphClient] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.token = token
        self.lock = threading.Lock()
        # meeting_id -> {"state": running|done|error, "message": str, "step": str}
        # Two reserved keys, "teams-login" and "teams-pull", track the Teams
        # jobs, which are per-server rather than per-meeting.
        self.jobs: dict[str, dict[str, str]] = {}
        # Injectable so the tests can drive the whole Teams path without a
        # tenant, a browser, or a network.
        self.teams_auth = teams_auth or (lambda: build_auth(self.settings))
        self.teams_client = teams_client or (lambda: build_client(self.settings))

    def set_job(self, meeting_id: str, state: str, message: str = "", step: str = "") -> None:
        with self.lock:
            self.jobs[meeting_id] = {"state": state, "message": message, "step": step}

    def job(self, meeting_id: str) -> dict[str, str]:
        with self.lock:
            return dict(self.jobs.get(meeting_id, {"state": "idle", "message": "", "step": ""}))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "minutely"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, state: AppState, **kwargs: Any) -> None:
        self.state = state
        super().__init__(*args, **kwargs)

    # -- plumbing ---------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page renders only its own data and loads nothing from the network.
        # blob: is needed so the user can play back what was just recorded.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src data:; media-src 'self' blob:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _authorised(self, query: dict[str, list[str]]) -> bool:
        supplied = self.headers.get("X-Session-Token") or (query.get("token") or [""])[0]
        # Constant-time compare so the token cannot be recovered by timing.
        return secrets.compare_digest(supplied or "", self.state.token)

    def _host_ok(self) -> bool:
        raw = self.headers.get("Host") or ""
        host = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
        return host.strip("[]") in _LOOPBACK_HOSTS

    def _meeting(self, query: dict[str, list[str]]) -> Meeting | None:
        meeting_id = (query.get("id") or query.get("meeting") or [""])[0]
        if not meeting_id:
            return None
        return self.state.store.get_meeting(meeting_id)

    # -- GET --------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._host_ok():
            self._error(403, "requests must be addressed to localhost")
            return

        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._serve_ui()
            return
        if not path.startswith("/api/"):
            self._error(404, "not found")
            return
        if not self._authorised(query):
            self._error(401, "invalid or missing session token")
            return

        try:
            self._route_get(path, query)
        except (EngineError, TranscriptionError, pipeline.PipelineError, TeamsError, ShareError) as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        store = self.state.store

        if path == "/api/state":
            transcriber = get_transcriber(self.state.settings.transcriber, self.state.settings)
            self._json(
                {
                    "engine": self.state.settings.engine,
                    "engines": list(ENGINES),
                    "templates": catalogue(),
                    "transcriber": self.state.settings.transcriber,
                    "transcriber_ready": transcriber.available(),
                    "auto_process": self.state.settings.auto_process,
                    "stats": store.stats(),
                }
            )

        elif path == "/api/meetings":
            self._json({"meetings": [m.to_dict() for m in store.list_meetings()]})

        elif path == "/api/meeting":
            meeting = self._meeting(query)
            if meeting is None:
                self._error(404, "unknown meeting")
                return
            minutes = store.get_minutes(meeting.meeting_id)
            transcript = store.get_transcript(meeting.meeting_id)
            self._json(
                {
                    "meeting": meeting.to_dict(),
                    "minutes": minutes.to_dict() if minutes else None,
                    "markdown": render.to_markdown(minutes, transcript, meeting.duration)
                    if minutes
                    else "",
                    "transcript": transcript.to_dict() if transcript else None,
                    "job": self.state.job(meeting.meeting_id),
                }
            )

        elif path == "/api/actions":
            status = (query.get("status") or ["open"])[0]
            owner = (query.get("owner") or [""])[0]
            self._json({"actions": store.action_rows(status=status, owner=owner or None)})

        elif path == "/api/calendar/upcoming":
            self._json(self._upcoming(query))

        elif path == "/api/audio":
            meeting = self._meeting(query)
            if meeting is None:
                self._error(404, "unknown meeting")
                return
            self._serve_audio(meeting)

        elif path == "/api/teams/state":
            self._json(self._teams_state())

        elif path == "/api/job":
            meeting_id = (query.get("meeting") or [""])[0]
            self._json(self.state.job(meeting_id))

        elif path == "/api/export":
            meeting = self._meeting(query)
            if meeting is None:
                self._error(404, "unknown meeting")
                return
            minutes = store.get_minutes(meeting.meeting_id)
            if minutes is None:
                self._error(404, "this meeting has no minutes yet")
                return
            fmt = (query.get("fmt") or ["md"])[0]
            transcript = store.get_transcript(meeting.meeting_id)
            body = render.render(minutes, fmt, transcript, meeting.duration)
            kind = "text/html; charset=utf-8" if fmt == "html" else "text/plain; charset=utf-8"
            self._send(200, body.encode("utf-8"), kind)

        else:
            self._error(404, "unknown endpoint")

    # -- POST -------------------------------------------------------------

    def do_POST(self) -> None:
        if not self._host_ok():
            self._error(403, "requests must be addressed to localhost")
            return

        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorised(query):
            self._error(401, "invalid or missing session token")
            return

        path = parsed.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        limit = MAX_CHUNK_BYTES if path == "/api/record/chunk" else MAX_JSON_BYTES
        if length > limit:
            self._error(413, "payload too large")
            return
        raw = self.rfile.read(length) if length else b""

        try:
            if path == "/api/record/chunk":
                self._json(self._handle_chunk(query, raw))
                return
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                self._error(400, "expected a JSON object")
                return

            if path == "/api/record/start":
                self._json(self._handle_start(payload))
            elif path == "/api/record/stop":
                self._json(self._handle_stop(payload))
            elif path == "/api/process":
                self._json(self._handle_process(payload))
            elif path == "/api/action":
                self._json(self._handle_action(payload))
            elif path == "/api/meeting/update":
                self._json(self._handle_update(payload))
            elif path == "/api/notes":
                self._json(self._handle_notes(payload))
            elif path == "/api/share":
                self._json(self._handle_share(payload))
            elif path == "/api/teams/login":
                self._json(self._handle_teams_login())
            elif path == "/api/teams/pull":
                self._json(self._handle_teams_pull(payload))
            else:
                self._error(404, "unknown endpoint")
        except json.JSONDecodeError:
            self._error(400, "invalid JSON body")
        except (EngineError, TranscriptionError, pipeline.PipelineError, TeamsError, ShareError) as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _handle_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        ensure_dirs()
        title = str(payload.get("title", "")).strip()
        suffix = suffix_for(str(payload.get("mime", "")))
        meeting = pipeline.create_meeting(
            self.state.store, title=title, held_on=date.today(), status="recording"
        )
        # Started from a calendar entry: carry the invite across, and record
        # the event id so a later `teams pull` recognises the same meeting
        # instead of importing a second copy of it.
        event_id = str(payload.get("event_id", "")).strip()
        if event_id:
            meeting.source = "teams"
            meeting.external_id = event_id
        meeting.participants = [
            str(name).strip() for name in payload.get("attendees", []) if str(name).strip()
        ]
        meeting.emails = [
            str(address).strip() for address in payload.get("emails", []) if str(address).strip()
        ]
        template = str(payload.get("template", "")).strip()
        if template in TEMPLATE_NAMES:
            meeting.template = template
        meeting.audio_path = str(recordings_dir() / f"{meeting.meeting_id}{suffix}")
        # Create the file now so an append never races the first chunk.
        Path(meeting.audio_path).touch()
        self.state.store.upsert_meeting(meeting)
        self.state.set_job(meeting.meeting_id, "recording", "recording in progress")
        return {"meeting": meeting.to_dict()}

    def _handle_chunk(self, query: dict[str, list[str]], raw: bytes) -> dict[str, Any]:
        meeting = self._meeting(query)
        if meeting is None:
            raise pipeline.PipelineError("unknown meeting")
        if not meeting.audio_path:
            raise pipeline.PipelineError("this meeting is not recording")
        # The path comes from the database, never from the request, so a
        # crafted meeting id cannot steer the write anywhere else.
        target = Path(meeting.audio_path)
        with open(target, "ab") as handle:
            handle.write(raw)
        return {"ok": True, "bytes": target.stat().st_size}

    def _handle_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = str(payload.get("meeting_id", ""))
        meeting = self.state.store.get_meeting(meeting_id)
        if meeting is None:
            raise pipeline.PipelineError("unknown meeting")

        title = str(payload.get("title", "")).strip()
        if title:
            meeting.title = title
        participants = [str(p).strip() for p in payload.get("participants", []) if str(p).strip()]
        if participants:
            meeting.participants = participants

        path = Path(meeting.audio_path) if meeting.audio_path else None
        size = path.stat().st_size if path and path.exists() else 0
        if size == 0:
            meeting.status = "new"
            self.state.store.upsert_meeting(meeting)
            self.state.set_job(meeting.meeting_id, "error", "no audio was captured")
            raise pipeline.PipelineError(
                "no audio was captured — check that the tab has microphone permission"
            )

        reported = payload.get("duration")
        meeting.duration = (
            float(reported) if isinstance(reported, (int, float)) and reported > 0 else None
        ) or (audio_duration(path) if path else None)
        meeting.status = "new"
        self.state.store.upsert_meeting(meeting)
        self.state.set_job(meeting.meeting_id, "idle", "recording saved")

        auto = payload.get("process", self.state.settings.auto_process)
        if auto:
            self._start_job(meeting, str(payload.get("engine", "")) or None)
        return {"meeting": meeting.to_dict(), "bytes": size, "processing": bool(auto)}

    def _handle_process(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting = self.state.store.get_meeting(str(payload.get("meeting_id", "")))
        if meeting is None:
            raise pipeline.PipelineError("unknown meeting")
        engine = str(payload.get("engine", "")) or None
        template = str(payload.get("template", "")) or None
        if template is not None and template not in TEMPLATE_NAMES:
            raise pipeline.PipelineError(f"unknown template {template!r}")
        self._start_job(meeting, engine, template)
        return {"ok": True, "job": self.state.job(meeting.meeting_id)}

    def _handle_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            action_id = int(payload.get("id", 0))
        except (TypeError, ValueError):
            raise pipeline.PipelineError("action id must be a number")
        status = str(payload.get("status", "open"))
        if not self.state.store.set_action_status(action_id, status):
            raise pipeline.PipelineError(f"no action with id {action_id}")
        return {"ok": True, "id": action_id, "status": status}

    def _handle_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting = self.state.store.get_meeting(str(payload.get("meeting_id", "")))
        if meeting is None:
            raise pipeline.PipelineError("unknown meeting")
        title = str(payload.get("title", "")).strip()
        if title:
            meeting.title = title
        template = str(payload.get("template", "")).strip()
        if template:
            if template not in TEMPLATE_NAMES:
                raise pipeline.PipelineError(f"unknown template {template!r}")
            meeting.template = template
        return {"meeting": self.state.store.upsert_meeting(meeting).to_dict()}

    def _handle_notes(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save the notes as they are typed.

        Called on a debounce while a meeting is running, so it has to be cheap
        and it has to never lose anything: the notes are the one artefact here
        that cannot be regenerated.
        """
        meeting_id = str(payload.get("meeting_id", ""))
        if self.state.store.get_meeting(meeting_id) is None:
            raise pipeline.PipelineError("unknown meeting")
        notes = str(payload.get("notes", ""))
        self.state.store.set_notes(meeting_id, notes)
        return {"ok": True, "characters": len(notes)}

    # -- Teams ------------------------------------------------------------

    def _teams_state(self) -> dict[str, Any]:
        auth = self.state.teams_auth()
        tokens = auth.tokens
        return {
            "configured": bool(self.state.settings.client_id),
            "signed_in": auth.signed_in,
            "account": tokens.account if tokens else "",
            "recordings": bool(tokens and tokens.can_read_recordings),
            "can_send_mail": auth.can_send_mail,
            "mail": share_transports(self.state.settings, auth.signed_in and auth.can_send_mail),
            "login": self.state.job("teams-login"),
            "pull": self.state.job("teams-pull"),
        }

    def _upcoming(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """The next few hours of calendar, annotated with what we already have."""
        auth = self.state.teams_auth()
        if not auth.signed_in:
            return {"signed_in": False, "events": []}
        hours = _positive_int((query.get("hours") or [""])[0], self.state.settings.calendar_horizon_hours)
        events = upcoming(self.state.teams_client(), hours=hours)
        rows = []
        for event in events:
            existing = self.state.store.find_external("teams", event.event_id)
            rows.append(
                {
                    **event.to_dict(),
                    "starts_in": event.starts_in(),
                    "recorded_as": existing.meeting_id if existing else "",
                }
            )
        return {"signed_in": True, "events": rows}

    def _serve_audio(self, meeting: Meeting) -> None:
        """Stream the recording, honouring Range so the player can seek."""
        if not meeting.audio_path:
            self._error(404, "this meeting has no recording")
            return
        path = Path(meeting.audio_path)
        if not path.is_file():
            self._error(404, "the recording file is missing from disk")
            return

        size = path.stat().st_size
        content_type = _AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
        start, end = _parse_range(self.headers.get("Range"), size)
        partial = start is not None
        first = start or 0
        last = end if end is not None else size - 1
        if first >= size or last < first:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = last - first + 1

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return

        remaining = length
        with path.open("rb") as handle:
            handle.seek(first)
            while remaining > 0:
                chunk = handle.read(min(STREAM_CHUNK, remaining))
                if not chunk:
                    break
                # A player that seeks away mid-stream drops the connection;
                # that is normal, not an error worth a traceback.
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(chunk)
                remaining -= len(chunk)

    def _handle_share(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting = self.state.store.get_meeting(str(payload.get("meeting_id", "")))
        if meeting is None:
            raise pipeline.PipelineError("unknown meeting")
        minutes = self.state.store.get_minutes(meeting.meeting_id)
        if minutes is None:
            raise pipeline.PipelineError("this meeting has no minutes to send yet")

        recipients = payload.get("to")
        message = compose(
            minutes,
            meeting,
            self.state.store.get_transcript(meeting.meeting_id),
            recipients=recipients if isinstance(recipients, list) else None,
            note=str(payload.get("note", "")),
            with_transcript=bool(payload.get("with_transcript")),
        )
        auth = self.state.teams_auth()
        graph = self.state.teams_client() if auth.signed_in and auth.can_send_mail else None
        result = send(
            message, self.state.settings, graph=graph, via=str(payload.get("via", ""))
        )
        return result.to_dict()

    def _handle_teams_login(self) -> dict[str, Any]:
        """Start a device-code sign-in and poll for it on a worker thread.

        The page shows the code; the user types it into microsoft.com in
        whatever browser they are already signed in to. Polling can take a
        minute or two, which is exactly why it does not happen in the request.
        """
        if self.state.job("teams-login").get("state") == "running":
            raise TeamsError("a sign-in is already in progress")
        auth = self.state.teams_auth()
        code = auth.begin()
        self.state.set_job(
            "teams-login", "running", f"enter {code.user_code} at {code.verification_uri}"
        )

        def work() -> None:
            try:
                tokens = auth.poll(code)
                self.state.set_job(
                    "teams-login", "done", f"signed in as {tokens.account or 'Microsoft 365'}"
                )
            except TeamsError as exc:
                self.state.set_job("teams-login", "error", str(exc))

        threading.Thread(target=work, daemon=True, name="minutely-teams-login").start()
        return {
            "user_code": code.user_code,
            "verification_uri": code.verification_uri,
            "message": code.message,
        }

    def _handle_teams_pull(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.state.job("teams-pull").get("state") == "running":
            return {"ok": True, "job": self.state.job("teams-pull")}
        days = int(payload.get("days", 7) or 7)
        self.state.set_job("teams-pull", "running", "asking Microsoft for your meetings")

        def work() -> None:
            store = Store(self.state.store.path)
            try:
                results = pull_recent(
                    store,
                    self.state.teams_client(),
                    self.state.settings,
                    days_back=days,
                    on_progress=lambda meeting: self.state.set_job(
                        "teams-pull", "running", f"checking {meeting.title}"
                    ),
                )
                imported = [r for r in results if r.ok]
                failed = [r for r in results if r.status == "error"]
                if failed and not imported:
                    self.state.set_job("teams-pull", "error", failed[0].detail)
                else:
                    self.state.set_job(
                        "teams-pull",
                        "done",
                        f"{len(imported)} of {len(results)} meeting(s) minuted"
                        if results
                        else "no Teams meetings found in that window",
                    )
            except TeamsError as exc:
                self.state.set_job("teams-pull", "error", str(exc))
            except Exception as exc:
                self.state.set_job("teams-pull", "error", f"{type(exc).__name__}: {exc}")
            finally:
                store.close()

        threading.Thread(target=work, daemon=True, name="minutely-teams-pull").start()
        return {"ok": True, "job": self.state.job("teams-pull")}

    # -- background work --------------------------------------------------

    def _start_job(self, meeting: Meeting, engine: str | None, template: str | None = None) -> None:
        current = self.state.job(meeting.meeting_id)
        if current.get("state") == "running":
            return
        self.state.set_job(meeting.meeting_id, "running", "starting", "transcribe")

        def work() -> None:
            # A worker thread needs its own connection; Store hands out one per
            # thread, so this must not reuse the request handler's cursor.
            store = Store(self.state.store.path)
            try:
                fresh = store.get_meeting(meeting.meeting_id)
                if fresh is None:
                    self.state.set_job(meeting.meeting_id, "error", "meeting disappeared")
                    return
                if store.get_transcript(fresh.meeting_id) is None:
                    self.state.set_job(
                        fresh.meeting_id, "running", "transcribing audio", "transcribe"
                    )
                    pipeline.transcribe(store, fresh, self.state.settings)
                    refreshed = store.get_meeting(fresh.meeting_id)
                    if refreshed is not None:
                        fresh = refreshed
                self.state.set_job(fresh.meeting_id, "running", "writing minutes", "minutes")
                pipeline.make_minutes(
                    store, fresh, self.state.settings, engine=engine, template=template
                )
                self.state.set_job(fresh.meeting_id, "done", "minutes ready")
            except (EngineError, TranscriptionError, pipeline.PipelineError) as exc:
                self.state.set_job(meeting.meeting_id, "error", str(exc))
            except Exception as exc:
                self.state.set_job(meeting.meeting_id, "error", f"{type(exc).__name__}: {exc}")
            finally:
                store.close()

        threading.Thread(target=work, daemon=True, name=f"minutely-{meeting.meeting_id}").start()

    # -- UI ---------------------------------------------------------------

    def _serve_ui(self) -> None:
        page = UI_DIR / "app.html"
        if not page.exists():
            self._error(500, "UI assets are missing from the installation")
            return
        html = page.read_text(encoding="utf-8")
        # The token is injected into the page rather than put in the URL, so it
        # never lands in browser history or a Referer header.
        html = html.replace("__SESSION_TOKEN__", self.state.token)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def do_HEAD(self) -> None:
        """Players probe with HEAD before streaming; answer with the headers."""
        self.do_GET()

    def log_message(self, *args: Any) -> None:
        """Suppress the default access log; it would echo query strings."""
        return


def _positive_int(raw: str, fallback: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _parse_range(header: str | None, size: int) -> tuple[int | None, int | None]:
    """Parse a single-range ``bytes=`` header. Anything odd means "send it all"."""
    if not header or not header.strip().lower().startswith("bytes="):
        return None, None
    spec = header.split("=", 1)[1].strip()
    if "," in spec:  # multi-range is legal and not worth supporting here
        return None, None
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            # "bytes=-500" means the last 500 bytes.
            length = int(end_text)
            return max(0, size - length), size - 1
        start = int(start_text)
        return start, int(end_text) if end_text else size - 1
    except ValueError:
        return None, None


def _free_port(preferred: int = 0) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_server(
    store: Store,
    settings: Settings,
    port: int | None = None,
    *,
    teams_auth: Callable[[], TeamsAuth] | None = None,
    teams_client: Callable[[], GraphClient] | None = None,
) -> tuple[http.server.ThreadingHTTPServer, str]:
    """Create the server and return it with its URL. Exposed for testing."""
    token = secrets.token_urlsafe(32)
    chosen = _free_port(port if port is not None else settings.ui_port)
    state = AppState(store, settings, token, teams_auth=teams_auth, teams_client=teams_client)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", chosen), partial(Handler, state=state))
    return server, f"http://127.0.0.1:{chosen}/?token={token}"


def serve(
    store: Store | None = None,
    settings: Settings | None = None,
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    """Start the recorder UI and block until interrupted."""
    settings = settings or Settings.load()
    store = store or Store()
    ensure_dirs()
    server, url = build_server(store, settings, port)

    transcriber = get_transcriber(settings.transcriber, settings)
    print(f"minutely running at {url.split('?')[0]}")
    print(f"  engine      : {settings.engine}")
    print(
        f"  transcriber : {settings.transcriber}"
        f"{'' if transcriber.available() else ' (not found — transcripts must be imported)'}"
    )
    print("  bound to loopback only — press Ctrl+C to stop")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    else:
        print(f"  open: {url}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        server.server_close()
        store.close()
