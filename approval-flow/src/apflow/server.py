"""Local approver dashboard.

Runs an HTTP server bound to the loopback interface and opens the system
browser at it. There is no deployment, no account, and nothing listening on a
public interface.

Three defences, because "it's only localhost" is not a security model — every
process and every user on the machine can reach 127.0.0.1, and any website the
browser visits can make requests to it:

* **Loopback bind.** The socket is bound to 127.0.0.1, never 0.0.0.0.
* **Session token.** A random token is minted per run and required on every
  request. A page the user happens to be browsing cannot guess it.
* **Host header check.** Requests whose Host is not a loopback literal are
  rejected, which blocks DNS rebinding.

**The "acting as" selector is a convenience, not authentication.** Anyone who
can reach this process can act as anyone in the directory, exactly as they can
from the command line. That is the correct threat model for a tool that runs on
one finance person's machine, and the wrong one for a shared deployment. Every
decision is recorded against the selected user, so the audit trail is only as
strong as the trust placed in whoever holds the keyboard. The UI says so.
"""

from __future__ import annotations

import http.server
import json
import secrets
import socket
import threading
import urllib.parse
import webbrowser
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

from .budget import commitments_from_store
from .matrix import MatrixError
from .models import Decision
from .money import ZERO
from .providers import LedgerProvider, ProviderError
from .reports import build_queue, overview, spend_under_approval, turnaround
from .workflow import Engine, WorkflowError

UI_DIR = Path(__file__).resolve().parent / "ui"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MAX_BODY = 64 * 1024


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


class AppState:
    """Shared state for the request handlers."""

    def __init__(self, engine: Engine, provider: LedgerProvider, token: str) -> None:
        self.engine = engine
        self.provider = provider
        self.token = token
        # The engine writes through one SQLite connection; serialise the
        # handler threads that share it.
        self.lock = threading.Lock()
        try:
            self.base_currency = provider.organisation().base_currency
        except ProviderError:
            self.base_currency = "AUD"


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "apflow"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, state: AppState, **kwargs: Any) -> None:
        self.state = state
        super().__init__(*args, **kwargs)

    # -- helpers ----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This app renders only its own data; no external resources at all.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _authorised(self, query: dict[str, list[str]]) -> bool:
        supplied = self.headers.get("X-Session-Token") or (query.get("token") or [""])[0]
        # Constant-time compare so the token cannot be recovered by timing.
        return secrets.compare_digest(supplied or "", self.state.token)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in _LOOPBACK_HOSTS

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        if not self._host_ok():
            self._error(403, "requests must be addressed to localhost")
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            self._serve_ui()
            return
        if not parsed.path.startswith("/api/"):
            self._error(404, "not found")
            return
        if not self._authorised(query):
            self._error(403, "missing or invalid session token")
            return

        try:
            with self.state.lock:
                payload = self._handle_get(parsed.path, query)
        except (WorkflowError, MatrixError) as exc:
            self._error(400, str(exc))
            return
        except ProviderError as exc:
            self._error(502, str(exc))
            return
        if payload is None:
            self._error(404, "not found")
            return
        self._json(payload)

    def do_POST(self) -> None:
        if not self._host_ok():
            self._error(403, "requests must be addressed to localhost")
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorised(query):
            self._error(403, "missing or invalid session token")
            return
        body = self._body()
        try:
            with self.state.lock:
                payload = self._handle_post(parsed.path, body)
        except (WorkflowError, MatrixError) as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        except ProviderError as exc:
            self._json({"ok": False, "error": str(exc)}, 502)
            return
        if payload is None:
            self._error(404, "not found")
            return
        self._json(payload)

    # -- handlers ---------------------------------------------------------

    def _handle_get(self, path: str, query: dict[str, list[str]]) -> Any:
        engine = self.state.engine
        if path == "/api/overview":
            queue = build_queue(engine.store, engine.directory)
            return {
                "source": self.state.provider.name,
                "can_write": self.state.provider.can_write,
                "base_currency": self.state.base_currency,
                "summary": overview(engine.store, engine.directory),
                "queue": queue.as_dict(),
                "users": engine.directory.as_list(),
                "rules": [r.name for r in engine.matrix.rules],
            }
        if path == "/api/inbox":
            user_id = (query.get("user") or [""])[0]
            if not user_id:
                return {"user": "", "items": []}
            user = engine.directory.get(user_id)
            if user is None:
                raise WorkflowError(f"unknown user {user_id!r}")
            return {
                "user": user.as_dict(),
                "items": engine.store.pending_steps_for_user(user.user_id),
            }
        if path == "/api/request":
            handle = (query.get("id") or [""])[0]
            request_id = engine.store.resolve_reference(handle)
            if request_id is None:
                return None
            return engine.request_view(request_id)
        if path == "/api/budget":
            commitments = commitments_from_store(engine.budget, engine.store)
            return {
                "granularity": engine.budget.granularity,
                "lines": [
                    {
                        "label": line.label,
                        "period": line.period,
                        "account": line.account,
                        "budget": str(line.amount),
                        "committed": str(commitments.get(line.key, ZERO)),
                        "remaining": str(line.amount - commitments.get(line.key, ZERO)),
                    }
                    for line in engine.budget.lines
                ],
            }
        if path == "/api/reports":
            return {
                "turnaround": turnaround(engine.store, engine.directory),
                "spend": spend_under_approval(engine.store),
            }
        return None

    def _handle_post(self, path: str, body: dict[str, Any]) -> Any:
        engine = self.state.engine
        if path == "/api/decide":
            request_id = engine.store.resolve_reference(str(body.get("reference") or ""))
            if request_id is None:
                raise WorkflowError("no such request")
            result = engine.decide(
                request_id=request_id,
                user_id=str(body.get("user") or ""),
                decision=Decision(str(body.get("decision") or "approve")),
                comment=str(body.get("comment") or ""),
            )
            return {"ok": True, **result.as_dict()}
        if path == "/api/cancel":
            request_id = engine.store.resolve_reference(str(body.get("reference") or ""))
            if request_id is None:
                raise WorkflowError("no such request")
            engine.cancel(request_id, str(body.get("user") or ""), str(body.get("reason") or ""))
            return {"ok": True}
        if path == "/api/pull":
            # Re-reads the ledger and routes anything new. Deliberately a POST:
            # it creates requests and sends notifications.
            from .models import DocumentKind

            requested_by = str(body.get("user") or "system")
            results = []
            for kind in (DocumentKind.BILL, DocumentKind.INVOICE, DocumentKind.PURCHASE_ORDER):
                for document in self.state.provider.documents(kind, status="DRAFT"):
                    try:
                        results.append(engine.submit(document, requested_by=requested_by).as_dict())
                    except MatrixError as exc:
                        results.append({"outcome": "unroutable", "message": str(exc)})
            return {"ok": True, "results": results}
        return None

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

    def log_message(self, *args: Any) -> None:
        """Suppress the default access log; it would echo query strings."""
        return


def _free_port(preferred: int = 0) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def serve(
    engine: Engine,
    provider: LedgerProvider,
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    """Start the dashboard and block until interrupted."""
    token = secrets.token_urlsafe(32)
    chosen = _free_port(port if port is not None else engine.settings.ui_port)
    state = AppState(engine, provider, token)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", chosen), partial(Handler, state=state))
    url = f"http://127.0.0.1:{chosen}/"

    print(f"apflow running at {url}")
    print(f"  data source: {provider.name} ({'read/write' if provider.can_write else 'read-only'})")
    print(f"  {len(engine.directory)} user(s) in the directory")
    print("  bound to loopback only — press Ctrl+C to stop")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        server.server_close()


__all__ = ["AppState", "Handler", "serve"]
