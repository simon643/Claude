"""Provider selection.

Resolution order, most explicit first:

1. ``--snapshot PATH`` / ``snapshot=`` argument.
2. A live connection, if a token is stored and ``XERO_CLIENT_ID`` is set.
3. The bundled demo snapshot, so a fresh checkout runs with no setup at all.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from . import ProviderError, XeroProvider
from .oauth import TokenSet
from .snapshot import SnapshotProvider


def demo_snapshot_path() -> Path:
    """Path to the synthetic demo data shipped with the package."""
    return Path(__file__).resolve().parents[3] / "data" / "demo"


def get_provider(
    snapshot: str | Path | None = None,
    settings: Settings | None = None,
    prefer_live: bool = True,
) -> XeroProvider:
    """Return the best available provider, or raise if none can be built."""
    settings = settings or Settings.load()

    if snapshot is not None:
        return SnapshotProvider(snapshot)

    if prefer_live and settings.has_credentials:
        tokens = TokenSet.load()
        if tokens.access_token or tokens.refresh_token:
            # Imported lazily so the offline path never pays for it.
            from .live import LiveProvider

            return LiveProvider(settings, tokens)

    demo = demo_snapshot_path()
    if demo.is_dir():
        return SnapshotProvider(demo)

    raise ProviderError(
        "No data source available. Either pass --snapshot PATH, or set "
        "XERO_CLIENT_ID and run `xerobk connect`."
    )


def describe_source(provider: XeroProvider) -> str:
    """Human-readable one-liner for the UI header and CLI output."""
    mode = "read/write" if provider.can_write else "read-only"
    return f"{provider.name} ({mode})"
