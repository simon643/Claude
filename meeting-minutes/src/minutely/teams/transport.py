"""The one place this package touches the network.

Every request goes through a ``Transport`` callable. The real one is built on
``urllib``; tests pass a function that returns canned responses, which is why
the test suite can cover the whole Teams path without a network, a tenant, or a
Microsoft account.

HTTP error statuses come back as ordinary responses rather than exceptions.
Graph says a great deal in the body of a 403, and a transport that raised would
throw that away before anyone could read it.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import TeamsError

USER_AGENT = "minutely/0.1 (+https://github.com/simon643/Claude)"
DEFAULT_TIMEOUT = 60


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TeamsError(f"expected JSON from Microsoft, got {self.body[:120]!r}: {exc}")
        return payload if isinstance(payload, dict) else {"value": payload}


Transport = Callable[[str, str, dict[str, str], bytes | None], Response]
# Downloads are separate from Transport so that a meeting recording — which can
# be hundreds of megabytes — streams to disk instead of being assembled in
# memory. Tests substitute their own.
Downloader = Callable[[str, dict[str, str], Path], int]


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drop the bearer token when a redirect leaves the original host.

    Graph answers a recording download with a redirect to Azure storage, and
    urllib would otherwise replay every header — including
    ``Authorization`` — at whatever host it was pointed to. A Graph token sent
    to a storage endpoint is a token disclosed to a third party.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        following = super().redirect_request(req, fp, code, msg, headers, newurl)
        if following is None:
            return None
        if urllib.parse.urlsplit(newurl).netloc.lower() != urllib.parse.urlsplit(
            req.full_url
        ).netloc.lower():
            for header in ("Authorization", "authorization"):
                following.headers.pop(header, None)
                following.unredirected_hdrs.pop(header, None)
        return following


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None = None
) -> Response:
    """Perform one HTTP request with the standard library."""
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            return Response(
                status=response.status,
                body=response.read(),
                headers=_lower(dict(response.headers)),
            )
    except urllib.error.HTTPError as exc:
        return Response(status=exc.code, body=exc.read(), headers=_lower(dict(exc.headers or {})))
    except urllib.error.URLError as exc:
        raise TeamsError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}")
    except TimeoutError:
        raise TeamsError(f"timed out talking to {urllib.parse.urlsplit(url).netloc}")


def urllib_download(url: str, headers: dict[str, str], destination: Path) -> int:
    """Stream a URL to a file, returning the number of bytes written."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        request.add_header(key, value)
    opener = urllib.request.build_opener(_StripAuthOnRedirect)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with opener.open(request, timeout=DEFAULT_TIMEOUT) as response, destination.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", errors="replace")
        raise TeamsError(f"download failed with HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise TeamsError(f"download failed: {exc.reason}")
    return destination.stat().st_size


def form(fields: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(fields).encode("utf-8")


def _lower(headers: dict[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}
