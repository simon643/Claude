"""Shared fixtures.

Every test runs against a throwaway ``MINUTELY_HOME`` so nothing touches the
developer's real recordings, and so the config file cannot leak between tests.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from minutely.store import Store  # noqa: E402

DEMO = SRC / "minutely" / "data" / "demo.vtt"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "minutely-home"
    monkeypatch.setenv("MINUTELY_HOME", str(base))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return base


@pytest.fixture
def store(home: Path) -> Iterator[Store]:
    instance = Store()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def demo_path() -> Path:
    return DEMO
