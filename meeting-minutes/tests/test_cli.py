from __future__ import annotations

import json
from pathlib import Path

import pytest

from minutely import pipeline
from minutely.cli import main
from minutely.store import Store


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_demo_produces_minutes_with_no_setup(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["demo"], capsys)
    assert code == 0
    assert "ACTION POINTS" in out
    assert "DECISIONS" in out


def test_demo_json_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["demo", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["engine"] == "rules"
    assert payload["actions"]


def test_list_then_show_uses_the_most_recent_meeting(capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    code, out, _ = run(["list"], capsys)
    assert code == 0
    assert "Weekly product sync (demo)" in out

    code, out, _ = run(["show"], capsys)
    assert code == 0
    assert "ACTION POINTS" in out


def test_actions_and_marking_one_done(capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    code, out, _ = run(["actions", "--json"], capsys)
    rows = json.loads(out)
    assert rows
    action_id = rows[0]["id"]

    code, out, _ = run(["done", str(action_id)], capsys)
    assert code == 0 and "marked done" in out

    code, out, _ = run(["actions", "--status", "open", "--json"], capsys)
    assert action_id not in [row["id"] for row in json.loads(out)]

    code, out, _ = run(["reopen", str(action_id)], capsys)
    assert code == 0
    code, out, _ = run(["actions", "--status", "open", "--json"], capsys)
    assert action_id in [row["id"] for row in json.loads(out)]


def test_actions_filter_by_owner(capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    _, out, _ = run(["actions", "--owner", "priya", "--json"], capsys)
    rows = json.loads(out)
    assert rows and all(row["owner"] == "Priya" for row in rows)


def test_export_writes_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    destination = tmp_path / "minutes.md"
    code, _out, _ = run(["export", "--format", "md", "-o", str(destination)], capsys)
    assert code == 0
    assert destination.read_text(encoding="utf-8").startswith("# ")


def test_import_and_minutes(demo_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["import", str(demo_path), "--title", "Imported", "--json"], capsys)
    assert code == 0
    meeting_id = json.loads(out)["meeting_id"]

    code, out, _ = run(["minutes", meeting_id, "--format", "md"], capsys)
    assert code == 0
    assert "# Imported" in out


def test_a_missing_meeting_is_a_clear_error(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["show", "no-such-meeting"], capsys)
    assert code == 1
    assert "no meeting matching" in err


def test_commands_that_need_a_meeting_say_so_when_there_are_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = run(["show"], capsys)
    assert code == 1
    assert "no meetings yet" in err


def test_delete_requires_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    store = Store()
    meeting_id = store.list_meetings()[0].meeting_id
    store.close()

    code, _, err = run(["delete", meeting_id], capsys)
    assert code == 1 and "--yes" in err

    code, out, _ = run(["delete", meeting_id, "--yes"], capsys)
    assert code == 0 and "deleted" in out

    code, out, _ = run(["list"], capsys)
    assert "no meetings yet" in out


def test_delete_can_remove_the_files_too(capsys: pytest.CaptureFixture[str], demo_path: Path) -> None:
    store = Store()
    meeting, _ = pipeline.import_file(store, demo_path)
    copied = Path(meeting.transcript_path)
    store.close()
    assert copied.exists()

    code, _, _ = run(["delete", meeting.meeting_id, "--yes", "--files"], capsys)
    assert code == 0
    assert not copied.exists()


def test_config_shows_the_data_directory_and_can_change_settings(
    capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    code, out, _ = run(["config"], capsys)
    assert code == 0
    assert str(home) in out

    code, out, _ = run(["config", "--language", "fr", "--engine", "rules", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["language"] == "fr"

    # Settings persist between invocations.
    code, out, _ = run(["config", "--json"], capsys)
    assert json.loads(out)["language"] == "fr"


def test_minutes_without_a_transcript_explains_the_next_step(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    audio = tmp_path / "chat.webm"
    audio.write_bytes(b"junk")
    run(["import", str(audio)], capsys)
    code, _, err = run(["minutes"], capsys)
    assert code == 1
    assert "no transcript" in err


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


# -- teams ------------------------------------------------------------------


def _fake_teams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake: object) -> None:
    """Point the CLI's Teams commands at a fake Microsoft."""
    from minutely.teams.auth import TeamsAuth
    from minutely.teams.graph import GraphClient
    from tests.fakes import signed_in_token

    token_path = tmp_path / "teams-token.json"
    signed_in_token(token_path)

    def auth(*_args: object, **kwargs: object) -> TeamsAuth:
        return TeamsAuth(
            client_id=str(kwargs.get("client_id") or "client-id"),
            transport=fake,  # type: ignore[arg-type]
            token_path=token_path,
            sleep=lambda _s: None,
        )

    def client(*_args: object, **_kwargs: object) -> GraphClient:
        return GraphClient(
            auth(),
            transport=fake,  # type: ignore[arg-type]
            downloader=fake.download,  # type: ignore[attr-defined]
            sleep=lambda _s: None,
        )

    monkeypatch.setattr("minutely.cli.build_auth", auth)
    monkeypatch.setattr("minutely.cli.build_client", client)


def test_teams_status_without_configuration_explains_the_next_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(["teams", "status"], capsys)
    assert code == 0
    assert "no Microsoft application id" in out
    assert "--teams-client-id" in out


def test_teams_list_shows_calendar_meetings(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fakes import FakeMicrosoft, calendar_payload

    fake = FakeMicrosoft().json_route(r"/me/calendarView", calendar_payload())
    _fake_teams(monkeypatch, tmp_path, fake)

    code, out, _ = run(["teams", "list"], capsys)
    assert code == 0
    assert "Weekly product sync" in out
    assert "Dana Okafor" in out


def test_teams_pull_imports_and_minutes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fakes import TEAMS_VTT, FakeMicrosoft, calendar_payload, text_response

    fake = (
        FakeMicrosoft()
        .json_route(r"/me/calendarView", calendar_payload())
        .json_route(r"/me/onlineMeetings\?", {"value": [{"id": "meeting-id"}]})
        .json_route(r"/transcripts(\?|$)", {"value": [{"id": "t1"}]})
        .add(r"/transcripts/[^/]+/content", text_response(TEAMS_VTT))
    )
    _fake_teams(monkeypatch, tmp_path, fake)

    code, out, _ = run(["teams", "pull", "--days", "30"], capsys)
    assert code == 0
    assert "minuted" in out

    code, out, _ = run(["list"], capsys)
    assert "Weekly product sync" in out

    code, out, _ = run(["actions", "--json"], capsys)
    rows = json.loads(out)
    assert any(row["owner"] == "Priya Raman" for row in rows)

    # A second pull is a no-op, not a duplicate.
    code, out, _ = run(["teams", "pull", "--days", "30"], capsys)
    assert "nothing new" in out
    code, out, _ = run(["list", "--json"], capsys)
    assert len(json.loads(out)) == 1


def test_teams_pull_reports_a_meeting_with_no_transcript(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fakes import FakeMicrosoft, calendar_payload

    fake = (
        FakeMicrosoft()
        .json_route(r"/me/calendarView", calendar_payload())
        .json_route(r"/me/onlineMeetings\?", {"value": [{"id": "meeting-id"}]})
        .json_route(r"/transcripts(\?|$)", {"value": []})
    )
    _fake_teams(monkeypatch, tmp_path, fake)

    code, out, _ = run(["teams", "pull"], capsys)
    assert code == 0
    assert "captured no transcript" in out
    assert "turned on recording" in out


def test_teams_logout_forgets_the_tokens(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fakes import FakeMicrosoft

    _fake_teams(monkeypatch, tmp_path, FakeMicrosoft())
    code, out, _ = run(["teams", "logout"], capsys)
    assert code == 0
    assert "signed out" in out
    assert not (tmp_path / "teams-token.json").exists()


def test_config_stores_the_teams_application_id(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["config", "--teams-client-id", "abc-123", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["teams_client_id"] == "abc-123"
    code, out, _ = run(["config"], capsys)
    assert "configured, not signed in" in out


# -- calendar and sharing ---------------------------------------------------


def test_upcoming_without_a_signin_says_what_to_do(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = run(["upcoming"], capsys)
    assert code == 1
    assert "minutely teams login" in err


def test_upcoming_lists_the_calendar(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fakes import FakeMicrosoft, calendar_payload

    fake = FakeMicrosoft().json_route(r"/me/calendarView", calendar_payload())
    _fake_teams(monkeypatch, tmp_path, fake)

    code, out, _ = run(["upcoming", "--hours", "99999", "--json"], capsys)
    assert code == 0
    rows = json.loads(out)
    assert rows[0]["title"] == "Weekly product sync"
    assert rows[0]["emails"] == ["priya@example.com", "marcus@example.com"]


def test_share_dry_run_shows_what_would_go_without_sending(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["demo"], capsys)
    code, out, _ = run(
        ["share", "--to", "dana@example.com", "--note", "As discussed.", "--dry-run"], capsys
    )
    assert code == 0
    assert "dana@example.com" in out
    assert "Minutes: Weekly product sync (demo)" in out
    assert "nothing was sent" in out


def test_share_rejects_a_malformed_address(capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    code, _out, err = run(["share", "--to", "not-an-address", "--dry-run"], capsys)
    assert code == 1
    assert "does not look like an email address" in err


def test_share_without_a_transport_names_both_options(capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    code, _out, err = run(["share", "--to", "dana@example.com"], capsys)
    assert code == 1
    assert "teams login --with-email" in err
    assert "smtp-host" in err


def test_share_needs_minutes_first(
    capsys: pytest.CaptureFixture[str], demo_path: Path
) -> None:
    code, out, _ = run(["import", str(demo_path), "--json"], capsys)
    meeting_id = json.loads(out)["meeting_id"]
    code, _out, err = run(["share", meeting_id, "--to", "a@b.com"], capsys)
    assert code == 1
    assert "no minutes" in err


def test_config_stores_smtp_settings(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        [
            "config",
            "--smtp-host", "smtp.example.com",
            "--smtp-from", "me@example.com",
            "--smtp-port", "2525",
            "--no-smtp-starttls",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    saved = json.loads(out)
    assert saved["smtp_host"] == "smtp.example.com"
    assert saved["smtp_port"] == 2525
    assert saved["smtp_starttls"] is False

    code, out, _ = run(["config"], capsys)
    assert "email       : smtp" in out


# -- notes and templates ----------------------------------------------------


def test_notes_can_be_set_shown_and_cleared(capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    code, out, _ = run(["notes", "--set", "Pricing\n- 49 -> 65"], capsys)
    assert code == 0
    assert "49 -> 65" in out

    code, out, _ = run(["notes"], capsys)
    assert "Pricing" in out

    code, out, _ = run(["notes", "--clear"], capsys)
    assert "(no notes)" in out


def test_notes_can_come_from_a_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run(["demo"], capsys)
    scratch = tmp_path / "notes.md"
    scratch.write_text("Support backlog\nTODO: Marcus: draft the customer note", encoding="utf-8")
    code, _out, _ = run(["notes", "--file", str(scratch)], capsys)
    assert code == 0

    code, out, _ = run(["minutes", "--json"], capsys)
    minutes = json.loads(out)
    assert minutes["notes"].startswith("Support backlog")
    assert any(a["owner"] == "Marcus" and "draft" in a["text"].lower() for a in minutes["actions"])


def test_a_missing_notes_file_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    code, _out, err = run(["notes", "--file", "/nowhere/notes.md"], capsys)
    assert code == 1
    assert "no such file" in err


def test_templates_are_listed(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["templates"], capsys)
    assert code == 0
    assert "standup" in out and "Stand-up" in out


def test_a_template_changes_the_document(capsys: pytest.CaptureFixture[str]) -> None:
    run(["demo"], capsys)
    code, out, _ = run(["minutes", "--template", "client", "--format", "md"], capsys)
    assert code == 0
    assert "## Commitments" in out
    assert "## Action points" not in out

    # And it sticks, so `show` renders the same document.
    code, out, _ = run(["show", "--format", "md"], capsys)
    assert "## Commitments" in out
