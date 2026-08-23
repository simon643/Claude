"""A very small Microsoft Graph client.

Only what this app needs: authenticated GETs, `@odata.nextLink` paging, and
error messages a person can act on. Graph is unusually good at explaining
itself in the body of a 403 — most of the failures here are a tenant setting or
a missing admin consent rather than a bug — so those bodies are read and
translated rather than swallowed.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ..config import GRAPH_BASE
from . import TeamsAuthError, TeamsError
from .auth import TeamsAuth
from .transport import Downloader, Response, Transport, urllib_download, urllib_transport

# Graph throttles, and says for how long. Retry a bounded number of times.
MAX_RETRIES = 3
MAX_BACKOFF = 60.0

# Failures whose real cause is a tenant policy, not the request.
_EXPLAINED = {
    "graphaccesstotranscriptsdisabled": (
        "your Microsoft 365 administrator has turned off Graph API access to Teams "
        "transcripts. The meeting has a transcript — this app is not allowed to read it. "
        "An administrator can re-enable it in the Teams admin centre."
    ),
    "speakerattributionnotallowed": (
        "the tenant does not allow speaker-attributed transcripts"
    ),
    "authorization_requestdenied": (
        "the sign-in is missing consent for this permission. Some Teams scopes need an "
        "administrator to approve them once for the tenant."
    ),
}


class GraphClient:
    """Authenticated read access to Microsoft Graph."""

    def __init__(
        self,
        auth: TeamsAuth,
        *,
        transport: Transport = urllib_transport,
        downloader: Downloader = urllib_download,
        base: str = GRAPH_BASE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.auth = auth
        self._transport = transport
        self._downloader = downloader
        self.base = base.rstrip("/")
        self._sleep = sleep

    # -- requests ---------------------------------------------------------

    def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        accept: str = "application/json",
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            headers = {
                "Authorization": f"Bearer {self.auth.access_token()}",
                "Accept": accept,
            }
            headers.update(extra_headers or {})
            response = self._transport("GET", url, headers, None)

            if response.ok:
                return response
            # One unprompted 401 is normal: a token can be revoked or rotated
            # between the expiry check and the request landing.
            if response.status == 401 and not refreshed:
                refreshed = True
                self.auth.refresh()
                continue
            if response.status in {429, 503, 504} and attempt < MAX_RETRIES:
                self._sleep(_retry_after(response, attempt))
                continue
            raise _error_for(response, url)

        raise TeamsError(f"gave up on {url} after {MAX_RETRIES} retries")

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self.get(path, params).json()

    def get_bytes(
        self, path: str, params: dict[str, str] | None = None, *, accept: str = "*/*"
    ) -> tuple[bytes, str]:
        response = self.get(path, params, accept=accept)
        return response.body, response.content_type

    def download(self, path: str, destination: Path) -> int:
        """Stream a large resource straight to disk. Returns bytes written."""
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self.auth.access_token()}"}
        return self._downloader(url, headers, destination)

    def paged(
        self, path: str, params: dict[str, str] | None = None, *, limit: int = 500
    ) -> Iterator[dict[str, Any]]:
        """Walk a collection, following `@odata.nextLink`."""
        seen = 0
        payload = self.get_json(path, params)
        while True:
            for item in payload.get("value", []):
                if isinstance(item, dict):
                    yield item
                    seen += 1
                    if seen >= limit:
                        return
            following = payload.get("@odata.nextLink")
            if not isinstance(following, str) or not following:
                return
            payload = self.get_json(following)


class GraphError(TeamsError):
    """A Graph request that failed, with whatever Graph said about it."""

    def __init__(self, message: str, status: int, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _error_for(response: Response, url: str) -> TeamsError:
    code, message = _graph_error(response)
    explained = _EXPLAINED.get(code.lower())
    where = urllib.parse.urlsplit(url).path

    if explained:
        return GraphError(explained, response.status, code)
    if response.status == 401:
        return TeamsAuthError("Microsoft rejected the sign-in — run: minutely teams login")
    if response.status == 403:
        return GraphError(
            f"access denied by Microsoft for {where}: {message or code or 'no reason given'}",
            403,
            code,
        )
    if response.status == 404:
        return GraphError(f"Microsoft has no such resource: {where}", 404, code)
    return GraphError(
        f"Microsoft Graph returned {response.status} for {where}"
        + (f": {message}" if message else ""),
        response.status,
        code,
    )


def _graph_error(response: Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except TeamsError:
        return "", response.text[:200]
    error = payload.get("error")
    if not isinstance(error, dict):
        return "", ""
    inner = error.get("innerError")
    code = str(error.get("code", ""))
    if isinstance(inner, dict) and inner.get("code"):
        # The inner code is the specific one — the outer is usually just
        # "Forbidden", which explains nothing.
        code = str(inner["code"])
    return code, str(error.get("message", ""))


def _retry_after(response: Response, attempt: int) -> float:
    raw = response.headers.get("retry-after", "")
    try:
        return min(float(raw), MAX_BACKOFF)
    except ValueError:
        return min(2.0**attempt, MAX_BACKOFF)
