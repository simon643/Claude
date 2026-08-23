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
