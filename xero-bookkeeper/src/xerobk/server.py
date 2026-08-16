"""Local desktop UI.

Runs an HTTP server bound to the loopback interface and opens the system
browser at it. There is no deployment, no external server, and nothing
listening on a public interface.

Three defences, because "it's only localhost" is not a security model — every
process and every user on the machine can reach 127.0.0.1, and any website the
user visits can make requests to it:

* **Loopback bind.** The socket is bound to 127.0.0.1, never 0.0.0.0, so the
  UI is unreachable from the network.
* **Session token.** A random token is minted per run and required on every
  request. A page the user happens to be browsing cannot guess it.
* **Host header check.** Requests whose Host is not a loopback literal are
  rejected. This blocks DNS rebinding, where an attacker's domain resolves to
  127.0.0.1 so their JavaScript can talk to this server as a same-origin peer.

The API is read-mostly. The only state-changing endpoint records a local coding
decision; nothing is posted to Xero from the browser.
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

from .close import build_context, run_close
from .config import Settings
from .models import BankLine
from .providers import ProviderError, XeroProvider
from .providers.bankcsv import parse_bank_csv
from .reconcile import suggest_all, summarise
from .reports import aged_detail, build_dashboard
from .rules import RuleSet, learn_rules
from .store import Store

UI_DIR = Path(__file__).resolve().parent / "ui"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


class AppState:
    """Shared state for the request handlers."""

    def __init__(
        self,
        provider: XeroProvider,
        settings: Settings,
        store: Store,
        ruleset: RuleSet,
        token: str,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.store = store
        self.ruleset = ruleset
        self.token = token
        self.bank_lines: list[BankLine] = []
        self.lock = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "xerobk"
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
        supplied = (
            self.headers.get("X-Session-Token")
            or (query.get("token") or [""])[0]
        )
        # Constant-time compare so the token cannot be recovered by timing.
        return secrets.compare_digest(supplied or "", self.state.token)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in {h.strip("[]") for h in _LOOPBACK_HOSTS}

    # -- routing ----------------------------------------------------------

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
            self._route_api(path, query)
        except ProviderError as exc:
            self._error(502, str(exc))
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _route_api(self, path: str, query: dict[str, list[str]]) -> None:
        state = self.state

        if path == "/api/dashboard":
            self._json(build_dashboard(state.provider).to_dict())

        elif path == "/api/aged":
            payables = (query.get("type") or ["receivables"])[0] == "payables"
            self._json(aged_detail(state.provider, payables=payables))

        elif path == "/api/close":
            ctx = build_context(
                state.provider,
                frequency=state.settings.bas_frequency,
                gst_registered=state.settings.gst_registered,
                escalation_days=state.settings.aged_debt_escalation_days,
                large_threshold=state.settings.large_transaction_threshold,
                suspense_codes=tuple(state.settings.suspense_account_codes),
                bank_lines=state.bank_lines,
            )
            report = run_close(ctx).to_dict()
            state.store.record_close(report)
            self._json(report)

        elif path == "/api/reconcile":
            self._json(self._reconcile_payload())

        elif path == "/api/rules":
            self._json({"rules": [rule.to_dict() for rule in state.ruleset.ordered()]})

        elif path == "/api/audit":
            self._json({"entries": state.store.audit_tail()})

        else:
            self._error(404, "unknown endpoint")

    def _reconcile_payload(self) -> dict[str, Any]:
        state = self.state
        with state.lock:
            lines = list(state.bank_lines)
        if not lines:
            return {"suggestions": [], "stats": None, "message": "No bank lines loaded. Import a CSV."}

        invoices = state.provider.invoices()
        suggestions = suggest_all(lines, invoices, state.ruleset)
        stats = summarise(suggestions)
        already = state.store.coded_line_ids()
        return {
            "suggestions": [
                {**s.to_dict(), "already_coded": s.line.line_id in already} for s in suggestions
            ],
            "stats": {
                "total": stats.total,
                "confident": stats.confident,
                "review": stats.review,
                "unknown": stats.unknown,
                "auto_rate": stats.auto_rate,
                "matched_value": str(stats.matched_value),
            },
        }

    def do_POST(self) -> None:
        if not self._host_ok():
            self._error(403, "requests must be addressed to localhost")
            return

        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorised(query):
            self._error(401, "invalid or missing session token")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > 20 * 1024 * 1024:
            self._error(413, "payload too large")
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        path = parsed.path.rstrip("/") or "/"

        try:
            if path == "/api/import":
                self._json(self._handle_import(raw))
            elif path == "/api/coding":
                self._json(self._handle_coding(raw))
            else:
                self._error(404, "unknown endpoint")
        except ProviderError as exc:
            self._error(502, str(exc))
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _handle_import(self, raw: str) -> dict[str, Any]:
        """Accept bank statement CSV text and hold it for this session."""
        lines = parse_bank_csv(raw)
        with self.state.lock:
            self.state.bank_lines = lines
        return {"imported": len(lines)}

    def _handle_coding(self, raw: str) -> dict[str, Any]:
        """Record a coding decision locally. Does not post to Xero."""
        payload = json.loads(raw or "{}")
        line_id = str(payload.get("line_id") or "")
        with self.state.lock:
            line = next((entry for entry in self.state.bank_lines if entry.line_id == line_id), None)
        if line is None:
            return {"ok": False, "error": f"unknown bank line {line_id}"}

        self.state.store.record_coding(
            line,
            account_code=str(payload.get("account_code") or ""),
            tax_type=str(payload.get("tax_type") or ""),
            contact_name=str(payload.get("contact_name") or ""),
            invoice_ids=list(payload.get("invoice_ids") or []),
            decided_by=str(payload.get("decided_by") or "user"),
            confidence=int(payload.get("confidence") or 0),
        )

        # Fold the new decision back into the rules so the next statement
        # benefits from it immediately.
        learned = learn_rules(self.state.store.coding_history())
        with self.state.lock:
            explicit = [r for r in self.state.ruleset.rules if r.source != "learned"]
            self.state.ruleset.rules = explicit + learned

        return {"ok": True, "learned_rules": len(learned)}

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
    provider: XeroProvider,
    settings: Settings | None = None,
    store: Store | None = None,
    ruleset: RuleSet | None = None,
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    """Start the UI and block until interrupted."""
    settings = settings or Settings.load()
    store = store or Store()
    if ruleset is None:
        ruleset = RuleSet.defaults()
        ruleset.rules.extend(learn_rules(store.coding_history()))

    token = secrets.token_urlsafe(32)
    chosen = _free_port(port if port is not None else settings.ui_port)
    state = AppState(provider, settings, store, ruleset, token)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", chosen), partial(Handler, state=state))
    url = f"http://127.0.0.1:{chosen}/"

    print(f"xerobk running at {url}")
    print(f"  data source: {provider.name} ({'read/write' if provider.can_write else 'read-only'})")
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
        store.close()
