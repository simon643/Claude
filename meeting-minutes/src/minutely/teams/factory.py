"""Build a signed-in Graph client from settings."""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .auth import TeamsAuth
from .graph import GraphClient
from .transport import Downloader, Transport, urllib_download, urllib_transport


def build_auth(
    settings: Settings | None = None,
    *,
    client_id: str = "",
    tenant: str = "",
    with_recordings: bool = False,
    transport: Transport = urllib_transport,
    token_path: Path | None = None,
) -> TeamsAuth:
    settings = settings or Settings.load()
    return TeamsAuth(
        client_id=client_id or settings.client_id,
        tenant=tenant or settings.teams_tenant,
        with_recordings=with_recordings,
        transport=transport,
        token_path=token_path,
    )


def build_client(
    settings: Settings | None = None,
    *,
    auth: TeamsAuth | None = None,
    transport: Transport = urllib_transport,
    downloader: Downloader = urllib_download,
    token_path: Path | None = None,
) -> GraphClient:
    resolved = auth or build_auth(settings, transport=transport, token_path=token_path)
    return GraphClient(resolved, transport=transport, downloader=downloader)
