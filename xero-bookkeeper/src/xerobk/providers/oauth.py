"""Xero OAuth 2.0 with PKCE.

Uses the PKCE flow rather than the authorization-code-with-secret flow. A
desktop app cannot keep a client secret secret — anything shipped to a user's
machine is readable by that user and anyone who compromises it — so Xero's
"Mobile or desktop application" app type, which issues no secret and requires
PKCE, is the correct choice here.

The flow:

1. Generate a random ``code_verifier`` and its S256 ``code_challenge``.
2. Open the system browser at Xero's consent screen.
3. Catch the redirect on ``http://localhost:<port>/callback`` with a one-shot
   local HTTP listener.
4. Exchange the code + verifier for tokens.
5. Persist tokens at 0600 and refresh them transparently.

The refresh token rotates on every use. It is written back to disk before the
new access token is returned, so a crash mid-refresh cannot strand the user
holding a token Xero has already invalidated.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

from ..config import (
    XERO_AUTHORIZE_URL,
    XERO_CONNECTIONS_URL,
    XERO_TOKEN_URL,
    ensure_dirs,
    token_path,
)
from . import AuthError

# Refresh this many seconds before nominal expiry, so a slow request cannot
# land on the far side of the boundary with a token that expired in flight.
_EXPIRY_SKEW = 120


@dataclass
class TokenSet:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    tenant_id: str = ""
    tenant_name: str = ""
    scopes: list[str] = field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        return time.time() >= (self.expires_at - _EXPIRY_SKEW)

    @property
    def is_usable(self) -> bool:
        return bool(self.access_token and self.tenant_id and not self.is_expired)

    def save(self, path: Path | None = None) -> Path:
        ensure_dirs()
        target = path or token_path()
        payload = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
            "scopes": self.scopes,
        }
        # Write-then-rename so an interrupted save cannot truncate a good token
        # file into an unusable one.
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with suppress(OSError):
            tmp.chmod(0o600)
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> TokenSet:
        target = path or token_path()
        if not target.exists():
            return cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls(
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            expires_at=float(raw.get("expires_at") or 0),
            tenant_id=str(raw.get("tenant_id") or ""),
            tenant_name=str(raw.get("tenant_name") or ""),
            scopes=list(raw.get("scopes") or []),
        )


def _pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for the S256 method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler that captures the ``?code=`` redirect."""

    # Deliberately class-level: the handler is instantiated per request by
    # HTTPServer, so the captured code has to outlive the instance.
    result: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/callback", "/"):
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        type(self).result = {k: v[0] for k, v in params.items()}
        ok = "code" in type(self).result
        body = _CALLBACK_OK if ok else _CALLBACK_FAIL.format(
            error=type(self).result.get("error_description")
            or type(self).result.get("error")
            or "no authorization code was returned"
        )
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log."""
        return


_CALLBACK_OK = """<!doctype html><meta charset="utf-8"><title>Connected</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:4rem auto;max-width:32rem;text-align:center;color:#0f172a}
h1{font-size:1.4rem}.tick{font-size:3rem}</style>
<div class="tick">&#10003;</div><h1>Connected to Xero</h1>
<p>You can close this tab and return to the terminal.</p>"""

_CALLBACK_FAIL = """<!doctype html><meta charset="utf-8"><title>Connection failed</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;margin:4rem auto;max-width:32rem;text-align:center;color:#0f172a}}
h1{{font-size:1.4rem}}.x{{font-size:3rem;color:#b91c1c}}code{{background:#f1f5f9;padding:.15rem .35rem;border-radius:4px}}</style>
<div class="x">&#10007;</div><h1>Connection failed</h1><p><code>{error}</code></p>"""


def _post_form(url: str, fields: dict[str, str], timeout: int = 30) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise AuthError(f"Xero rejected the token request ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise AuthError(f"could not reach Xero: {exc.reason}") from exc


def _get_json(url: str, token: str, timeout: int = 30) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise AuthError(f"Xero request failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise AuthError(f"could not reach Xero: {exc.reason}") from exc


def authorize(
    client_id: str,
    scopes: tuple[str, ...],
    port: int = 8720,
    open_browser: bool = True,
    timeout: int = 300,
) -> TokenSet:
    """Run the interactive PKCE consent flow and return a saved TokenSet."""
    if not client_id:
        raise AuthError(
            "XERO_CLIENT_ID is not set. Create a 'Mobile or desktop application' at "
            "https://developer.xero.com/app/manage and export its client id."
        )

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://localhost:{port}/callback"

    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    consent_url = f"{XERO_AUTHORIZE_URL}?{query}"

    _CallbackHandler.result = {}
    try:
        server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as exc:
        raise AuthError(
            f"cannot listen on port {port} for the OAuth redirect: {exc}. "
            "Close whatever is using it, or pass a different --port (and add the "
            "matching redirect URI in your Xero app)."
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"Opening Xero consent screen...\n  {consent_url}\n")
    if open_browser:
        webbrowser.open(consent_url)

    deadline = time.time() + timeout
    try:
        while not _CallbackHandler.result and time.time() < deadline:
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise AuthError(f"timed out after {timeout}s waiting for the Xero redirect")
    if "error" in result:
        raise AuthError(f"Xero returned an error: {result.get('error_description') or result['error']}")
    # Reject a mismatched state: this is the CSRF guard for the callback.
    if result.get("state") != state:
        raise AuthError("OAuth state mismatch — the redirect did not come from this request")

    payload = _post_form(
        XERO_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )

    tokens = TokenSet(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_at=time.time() + float(payload.get("expires_in") or 1800),
        scopes=str(payload.get("scope") or "").split(),
    )

    connections = _get_json(XERO_CONNECTIONS_URL, tokens.access_token)
    if not connections:
        raise AuthError("consent succeeded but no Xero organisation was connected")
    first = connections[0]
    tokens.tenant_id = str(first.get("tenantId") or "")
    tokens.tenant_name = str(first.get("tenantName") or "")
    tokens.save()
    return tokens


def refresh(client_id: str, tokens: TokenSet) -> TokenSet:
    """Exchange a refresh token for a new access token, persisting the result."""
    if not tokens.refresh_token:
        raise AuthError("no refresh token stored — run `xerobk connect` again")
    payload = _post_form(
        XERO_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens.refresh_token,
        },
    )
    tokens.access_token = str(payload.get("access_token") or "")
    # Xero rotates the refresh token on every use; keeping the old one would
    # make the next refresh fail.
    tokens.refresh_token = str(payload.get("refresh_token") or tokens.refresh_token)
    tokens.expires_at = time.time() + float(payload.get("expires_in") or 1800)
    tokens.save()
    return tokens
