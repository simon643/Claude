"""The UI, driven in a real browser.

Nine hundred lines of JavaScript are not covered by testing the JSON endpoints
underneath them. This drives the actual page in Chromium: the meeting rail, the
notepad and its autosave, the template picker, the transcript search, and the
error a share attempt produces when no mail transport is configured.

Skipped unless Playwright and a Chromium build are both present, so the ordinary
`pytest` run stays dependency-free:

    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from minutely import pipeline
from minutely.config import Settings
from minutely.server import build_server
from minutely.store import Store

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")


def _chromium_path() -> str | None:
    """Find a browser: the pre-provisioned one, else whatever Playwright has."""
    for candidate in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")):
        return str(candidate)
    return None


@pytest.fixture
def page(store: Store, demo_path: Path) -> Iterator[Any]:
    meeting, _ = pipeline.import_file(
        store, demo_path, title="Weekly product sync", held_on=date.today()
    )
    store.set_notes(meeting.meeting_id, "Pricing\n- 49 -> 65\nTODO: Sam: circulate mechanics")
    refreshed = store.get_meeting(meeting.meeting_id)
    assert refreshed is not None
    pipeline.make_minutes(store, refreshed, engine="rules")
    pipeline.create_meeting(store, title="Design review", held_on=date(2026, 1, 5))

    server, url = build_server(store, Settings(auto_process=False), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with playwright_api.sync_playwright() as pw:
        launch: dict[str, Any] = {}
        executable = _chromium_path()
        if executable:
            launch["executable_path"] = executable
        try:
            browser = pw.chromium.launch(**launch)
        except Exception as exc:  # pragma: no cover - depends on the machine
            server.shutdown()
            pytest.skip(f"no usable chromium: {exc}")
        tab = browser.new_page(viewport={"width": 1280, "height": 1000})
        errors: list[str] = []
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        tab.goto(url)
        tab.wait_for_timeout(700)
        tab.meeting_id = meeting.meeting_id  # type: ignore[attr-defined]
        tab.js_errors = errors  # type: ignore[attr-defined]
        try:
            yield tab
        finally:
            browser.close()
            server.shutdown()
            server.server_close()


def test_the_rail_groups_meetings_by_day(page: Any) -> None:
    # inner_text reflects CSS, and these headings are upper-cased in the sheet.
    rail = page.inner_text("aside").lower()
    assert "today" in rail
    assert "weekly product sync" in rail
    assert "design review" in rail


def test_opening_a_meeting_shows_the_whole_document(page: Any) -> None:
    page.click("text=Weekly product sync")
    page.wait_for_timeout(500)
    doc = page.inner_text("#doc-body").lower()
    for section in ("your notes", "summary", "discussion", "action points", "send by email"):
        assert section in doc, section
    assert page.input_value("#notepad").startswith("Pricing")
    assert page.js_errors == []


def test_typing_notes_saves_them_without_a_button(page: Any, store: Store) -> None:
    page.click("text=Weekly product sync")
    page.wait_for_timeout(400)
    page.fill("#notepad", "Pricing\n- 49 -> 65\nTODO: Sam: circulate mechanics\nand one more line")
    page.wait_for_timeout(1200)

    assert page.inner_text("#saved-line").strip() == "saved"
    meeting = store.get_meeting(page.meeting_id)
    assert meeting is not None and "and one more line" in meeting.notes


def test_the_template_picker_persists_the_choice(page: Any, store: Store) -> None:
    page.click("text=Weekly product sync")
    page.wait_for_timeout(400)
    page.select_option("select", "standup")
    page.wait_for_timeout(400)
    meeting = store.get_meeting(page.meeting_id)
    assert meeting is not None and meeting.template == "standup"


def test_the_transcript_panel_searches_and_highlights(page: Any) -> None:
    page.click("text=Weekly product sync")
    page.wait_for_timeout(400)
    page.click("details.transcript summary")
    page.wait_for_timeout(300)
    assert page.locator(".tline").count() > 10

    page.fill("details.transcript input", "keycloak")
    page.wait_for_timeout(300)
    hits = page.locator(".tline").count()
    assert 0 < hits <= 5
    assert page.locator("mark").count() > 0


def test_sharing_with_nothing_configured_explains_itself(page: Any) -> None:
    page.click("text=Weekly product sync")
    page.wait_for_timeout(400)
    page.fill("input[placeholder^='someone@']", "sam@example.com")
    page.click("text=Send minutes")
    page.wait_for_timeout(600)
    assert "teams login --with-email" in page.inner_text("#doc-body")


def test_a_meeting_with_no_minutes_offers_to_make_some(page: Any) -> None:
    page.click("text=Design review")
    page.wait_for_timeout(400)
    body = page.inner_text("#doc-body").lower()
    assert "enhance notes" in body
    assert "action points" not in body
