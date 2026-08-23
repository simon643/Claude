"""Command line interface.

``argparse`` rather than a CLI framework, to keep the package free of runtime
dependencies. Every command takes ``--json`` so the tool drives a script as
easily as a terminal.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__, render, server
from .config import Settings, data_dir, exports_dir
from .engines import EngineError
from .engines.factory import ENGINES
from .models import Meeting, format_duration
from .pipeline import (
    PipelineError,
    create_meeting,
    import_file,
    make_minutes,
)
from .pipeline import transcribe as run_transcribe
from .store import Store
from .teams import TeamsError
from .teams.auth import DeviceCode
from .teams.factory import build_auth, build_client
from .teams.sync import TeamsMeeting, list_meetings, pull_recent
from .transcribers import TranscriptionError
from .transcribers.factory import TRANSCRIBERS, get_transcriber

DEMO_TRANSCRIPT = Path(__file__).resolve().parent / "data" / "demo.vtt"


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))


def _fail(message: str, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps({"error": message}, indent=2))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 1


def _resolve(store: Store, needle: str, as_json: bool) -> Meeting | None:
    meeting = store.resolve_meeting(needle)
    if meeting is None:
        _fail(f"no meeting matching {needle!r} — run `minutely list` to see what is there", as_json)
    return meeting


def _latest(store: Store) -> Meeting | None:
    meetings = store.list_meetings(limit=1)
    return meetings[0] if meetings else None


def _target(store: Store, args: argparse.Namespace) -> Meeting | None:
    """The meeting a command should act on: named, or the most recent."""
    needle = getattr(args, "meeting", "") or ""
    if needle:
        return _resolve(store, needle, args.json)
    latest = _latest(store)
    if latest is None:
        _fail("no meetings yet — record one with `minutely record`", args.json)
    return latest


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"error: --date must be YYYY-MM-DD, got {value!r}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    settings = Settings.load()
    store = Store()
    try:
        server.serve(store, settings, port=args.port, open_browser=not args.no_browser)
    finally:
        store.close()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    return cmd_record(args)


def cmd_import(args: argparse.Namespace) -> int:
    store = Store()
    try:
        meeting = None
        if args.meeting:
            meeting = _resolve(store, args.meeting, args.json)
            if meeting is None:
                return 1
        meeting, transcript = import_file(
            store,
            args.path,
            title=args.title or "",
            held_on=_parse_date(args.date),
            meeting=meeting,
        )
        if args.process:
            if transcript is None:
                run_transcribe(store, meeting, force=False)
                refreshed = store.get_meeting(meeting.meeting_id)
                meeting = refreshed or meeting
            make_minutes(store, meeting, engine=args.engine)
            refreshed = store.get_meeting(meeting.meeting_id)
            meeting = refreshed or meeting

        _emit(meeting.to_dict(), args.json)
        if not args.json:
            kind = "transcript" if transcript is not None else "audio"
            print(f"imported {kind}: {meeting.meeting_id}")
            print(f"  title  : {meeting.title or '(untitled)'}")
            print(f"  status : {meeting.status}")
            if meeting.status != "minuted":
                nxt = "minutes" if transcript is not None else "transcribe"
                print(f"  next   : minutely {nxt} {meeting.meeting_id}")
        return 0
    except (PipelineError, EngineError, TranscriptionError) as exc:
        return _fail(str(exc), args.json)
    finally:
        store.close()


def cmd_transcribe(args: argparse.Namespace) -> int:
    store = Store()
    try:
        meeting = _target(store, args)
        if meeting is None:
            return 1
        transcript = run_transcribe(store, meeting, force=args.force)
        payload = {
            "meeting_id": meeting.meeting_id,
            "segments": len(transcript.segments),
            "words": transcript.word_count,
            "speakers": transcript.speakers(),
        }
        _emit(payload, args.json)
        if not args.json:
            print(f"transcribed {meeting.meeting_id}: {payload['words']} words in {payload['segments']} segments")
            print(f"  next : minutely minutes {meeting.meeting_id}")
        return 0
    except (PipelineError, TranscriptionError) as exc:
        return _fail(str(exc), args.json)
    finally:
        store.close()


def cmd_minutes(args: argparse.Namespace) -> int:
    store = Store()
    try:
        meeting = _target(store, args)
        if meeting is None:
            return 1
        minutes = make_minutes(store, meeting, engine=args.engine)
        transcript = store.get_transcript(meeting.meeting_id)
        if args.json:
            _emit(minutes.to_dict(), True)
        else:
            print(render.render(minutes, args.format, transcript, meeting.duration))
        return 0
    except (PipelineError, EngineError) as exc:
        return _fail(str(exc), args.json)
    finally:
        store.close()


def cmd_show(args: argparse.Namespace) -> int:
    store = Store()
    try:
        meeting = _target(store, args)
        if meeting is None:
            return 1
        minutes = store.get_minutes(meeting.meeting_id)
        if minutes is None:
            return _fail(
                f"no minutes for {meeting.meeting_id} yet — run: minutely minutes {meeting.meeting_id}",
                args.json,
            )
        transcript = store.get_transcript(meeting.meeting_id)
        if args.json:
            _emit(minutes.to_dict(), True)
        else:
            print(render.render(minutes, args.format, transcript, meeting.duration))
        return 0
    finally:
        store.close()


def cmd_export(args: argparse.Namespace) -> int:
    store = Store()
    try:
        meeting = _target(store, args)
        if meeting is None:
            return 1
        minutes = store.get_minutes(meeting.meeting_id)
        if minutes is None:
            return _fail(f"no minutes for {meeting.meeting_id} yet", args.json)
        transcript = store.get_transcript(meeting.meeting_id)
        body = render.render(minutes, args.format, transcript, meeting.duration)

        destination = (
            Path(args.output).expanduser()
            if args.output
            else exports_dir() / f"{meeting.meeting_id}.{args.format}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(body, encoding="utf-8")
        _emit({"meeting_id": meeting.meeting_id, "path": str(destination)}, args.json)
        if not args.json:
            print(str(destination))
        return 0
    finally:
        store.close()


def cmd_list(args: argparse.Namespace) -> int:
    store = Store()
    try:
        meetings = store.list_meetings(limit=args.limit)
        _emit([m.to_dict() for m in meetings], args.json)
        if args.json:
            return 0
        if not meetings:
            print("no meetings yet — record one with `minutely record`")
            return 0
        print(f"{'ID':<22} {'DATE':<11} {'STATUS':<12} {'LENGTH':<9} TITLE")
        for meeting in meetings:
            print(
                f"{meeting.meeting_id:<22} {meeting.held_on[:10]:<11} {meeting.status:<12} "
                f"{format_duration(meeting.duration):<9} {meeting.title or '(untitled)'}"
            )
        return 0
    finally:
        store.close()


def cmd_actions(args: argparse.Namespace) -> int:
    store = Store()
    try:
        if args.meeting:
            meeting = _resolve(store, args.meeting, args.json)
            if meeting is None:
                return 1
            rows = [
                {**item.to_dict(), "meeting_id": meeting.meeting_id, "meeting_title": meeting.title}
                for item in store.list_actions(
                    meeting_id=meeting.meeting_id, status=args.status, owner=args.owner
                )
            ]
        else:
            rows = store.action_rows(status=args.status, owner=args.owner)

        _emit(rows, args.json)
        if args.json:
            return 0
        if not rows:
            print("nothing to show")
            return 0
        print(f"{'ID':<6} {'DUE':<12} {'OWNER':<14} ACTION")
        for row in rows:
            mark = {"done": "x", "dropped": "~"}.get(str(row.get("status")), " ")
            print(
                f"[{mark}]{row.get('id') or ''!s:<3} {(row.get('due') or '—'):<12} "
                f"{(row.get('owner') or 'unassigned'):<14} {row.get('text', '')}"
            )
        return 0
    finally:
        store.close()


def cmd_action_status(args: argparse.Namespace) -> int:
    store = Store()
    try:
        status = "open" if args.command == "reopen" else args.command  # done | dropped | open
        if not store.set_action_status(args.id, status):
            return _fail(f"no action with id {args.id}", args.json)
        _emit({"id": args.id, "status": status}, args.json)
        if not args.json:
            print(f"action {args.id} marked {status}")
        return 0
    finally:
        store.close()


def cmd_config(args: argparse.Namespace) -> int:
    settings = Settings.load()
    changed = False
    for field in (
        "engine",
        "transcriber",
        "whisper_bin",
        "whisper_model",
        "language",
        "model",
        "teams_client_id",
        "teams_tenant",
    ):
        value = getattr(args, field, None)
        if value:
            setattr(settings, field, value)
            changed = True
    if args.auto_process is not None:
        settings.auto_process = args.auto_process
        changed = True
    if changed:
        settings.save()

    transcriber = get_transcriber(settings.transcriber, settings)
    payload = {
        field: getattr(settings, field) for field in settings.__dataclass_fields__
    }
    payload["non_owners"] = list(settings.non_owners)
    payload["data_dir"] = str(data_dir())
    payload["transcriber_ready"] = transcriber.available()
    payload["anthropic_api_key_set"] = settings.has_api_key

    _emit(payload, args.json)
    if not args.json:
        print(f"data dir    : {payload['data_dir']}")
        print(f"engine      : {settings.engine}" + ("" if settings.engine != "claude" or settings.has_api_key else "  (ANTHROPIC_API_KEY is not set)"))
        print(f"transcriber : {settings.transcriber}" + ("" if payload["transcriber_ready"] else "  (binary not found)"))
        if settings.transcriber == "whisper":
            print(f"  binary    : {settings.whisper_bin}")
            print(f"  model     : {settings.whisper_model or '(not set)'}")
        print(f"language    : {settings.language}")
        print(f"auto process: {settings.auto_process}")
        teams_state = "not configured"
        if settings.client_id:
            teams_state = "signed in" if build_auth(settings).signed_in else "configured, not signed in"
        print(f"teams       : {teams_state}")
        if changed:
            print("saved")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Load the bundled sample meeting so the tool can be tried in one command."""
    store = Store()
    try:
        meeting = create_meeting(store, title="Weekly product sync (demo)", held_on=date.today())
        meeting, _ = import_file(store, DEMO_TRANSCRIPT, meeting=meeting)
        minutes = make_minutes(store, meeting, engine=args.engine)
        transcript = store.get_transcript(meeting.meeting_id)
        if args.json:
            _emit(minutes.to_dict(), True)
        else:
            print(render.to_text(minutes, transcript, meeting.duration))
            print()
            print(f"(demo meeting {meeting.meeting_id} — `minutely actions` to see the register)")
        return 0
    except (PipelineError, EngineError) as exc:
        return _fail(str(exc), args.json)
    finally:
        store.close()


def cmd_delete(args: argparse.Namespace) -> int:
    store = Store()
    try:
        meeting = _resolve(store, args.meeting, args.json)
        if meeting is None:
            return 1
        if not args.yes:
            return _fail(
                f"this deletes {meeting.meeting_id} and its minutes — pass --yes to confirm",
                args.json,
            )
        removed_files = []
        if args.files:
            for path in (meeting.audio_path, meeting.transcript_path):
                if path and Path(path).exists():
                    Path(path).unlink()
                    removed_files.append(path)
        store.delete_meeting(meeting.meeting_id)
        _emit({"deleted": meeting.meeting_id, "files": removed_files}, args.json)
        if not args.json:
            print(f"deleted {meeting.meeting_id}" + (f" and {len(removed_files)} file(s)" if removed_files else ""))
        return 0
    finally:
        store.close()


# --------------------------------------------------------------------------
# Teams
# --------------------------------------------------------------------------


def cmd_teams_login(args: argparse.Namespace) -> int:
    settings = Settings.load()
    auth = build_auth(
        settings,
        client_id=args.client_id or "",
        tenant=args.tenant or "",
        with_recordings=args.with_recordings,
    )

    def announce(code: DeviceCode) -> None:
        if args.json:
            return
        print(code.message or f"Go to {code.verification_uri} and enter the code {code.user_code}")
        print()
        print(f"  code : {code.user_code}")
        print(f"  url  : {code.verification_uri}")
        print()
        print("waiting for you to finish signing in... (Ctrl+C to give up)")

    try:
        tokens = auth.login(announce)
    except TeamsError as exc:
        return _fail(str(exc), args.json)

    # Remember the application id so the next sign-in needs no flags.
    changed = False
    if args.client_id and args.client_id != settings.teams_client_id:
        settings.teams_client_id = args.client_id
        changed = True
    if args.tenant and args.tenant != settings.teams_tenant:
        settings.teams_tenant = args.tenant
        changed = True
    if changed:
        settings.save()

    payload = {
        "account": tokens.account,
        "tenant": tokens.tenant,
        "scopes": tokens.scopes,
        "recordings": tokens.can_read_recordings,
    }
    _emit(payload, args.json)
    if not args.json:
        print(f"signed in as {tokens.account or '(unknown account)'}")
        print("  next : minutely teams list")
    return 0


def cmd_teams_status(args: argparse.Namespace) -> int:
    settings = Settings.load()
    auth = build_auth(settings)
    tokens = auth.tokens
    payload = {
        "signed_in": auth.signed_in,
        "account": tokens.account if tokens else "",
        "tenant": tokens.tenant if tokens else settings.teams_tenant,
        "client_id_set": bool(settings.client_id),
        "scopes": tokens.scopes if tokens else [],
        "recordings": bool(tokens and tokens.can_read_recordings),
    }
    _emit(payload, args.json)
    if not args.json:
        if not settings.client_id:
            print("no Microsoft application id configured")
            print("  set one : minutely config --teams-client-id <id>")
        elif not auth.signed_in:
            print("not signed in to Microsoft")
            print("  sign in : minutely teams login")
        else:
            print(f"signed in as {payload['account'] or '(unknown account)'}")
            print(f"  tenant     : {payload['tenant']}")
            print(f"  recordings : {'yes' if payload['recordings'] else 'no (transcripts only)'}")
    return 0


def cmd_teams_logout(args: argparse.Namespace) -> int:
    removed = build_auth().logout()
    _emit({"signed_out": removed}, args.json)
    if not args.json:
        print("signed out" if removed else "was not signed in")
        print("note: this forgets the local tokens; it does not revoke consent in Microsoft 365")
    return 0


def cmd_teams_list(args: argparse.Namespace) -> int:
    try:
        graph = build_client()
        meetings = list_meetings(graph, days_back=args.days, days_forward=1, limit=args.limit)
    except TeamsError as exc:
        return _fail(str(exc), args.json)

    _emit([m.to_dict() for m in meetings], args.json)
    if args.json:
        return 0
    if not meetings:
        print(f"no Teams meetings on your calendar in the last {args.days} days")
        return 0
    print(f"{'WHEN':<17} {'ORGANISER':<22} SUBJECT")
    for meeting in meetings:
        when = meeting.start.strftime("%Y-%m-%d %H:%M")
        print(f"{when:<17} {(meeting.organizer or '—')[:21]:<22} {meeting.title}")
    print()
    print(f"pull them in with: minutely teams pull --days {args.days}")
    return 0


def cmd_teams_pull(args: argparse.Namespace) -> int:
    store = Store()
    try:
        settings = Settings.load()
        graph = build_client(settings)

        def progress(meeting: TeamsMeeting) -> None:
            if not args.json:
                print(f"  checking {meeting.start.strftime('%Y-%m-%d')} {meeting.title}...")

        results = pull_recent(
            store,
            graph,
            settings,
            days_back=args.days,
            limit=args.limit,
            with_recording=args.with_recording,
            engine=args.engine,
            force=args.force,
            match=args.match or "",
            on_progress=progress,
        )
    except TeamsError as exc:
        return _fail(str(exc), args.json)
    finally:
        store.close()

    _emit([r.to_dict() for r in results], args.json)
    if args.json:
        return 0

    imported = [r for r in results if r.ok]
    print()
    for result in results:
        mark = {"imported": "+", "updated": "~", "skipped": "=", "no-transcript": ".", "error": "!"}
        print(
            f"{mark.get(result.status, '?')} {result.teams.start.strftime('%Y-%m-%d')} "
            f"{result.teams.title}"
            + (f" — {result.detail}" if result.detail else "")
        )
    print()
    if imported:
        print(f"{len(imported)} meeting(s) minuted. Read them with: minutely show")
        print("Open actions across every meeting: minutely actions")
    elif results:
        print("nothing new to import.")
        print("A meeting only has a transcript if someone turned on recording or")
        print("transcription in Teams while it was running.")
    else:
        print(f"no Teams meetings on your calendar in the last {args.days} days")
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minutely",
        description="Record meetings, transcribe them locally, and write the minutes.",
        epilog="Try `minutely demo` first — it needs no audio and no network.",
    )
    parser.add_argument("--version", action="version", version=f"minutely {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", parents=[common], help="open the browser recorder (main entry point)")
    p.add_argument("--port", type=int, default=None, help="port for the local UI")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("ui", parents=[common], help="alias for `record`")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("import", parents=[common], help="import an audio file or an existing transcript")
    p.add_argument("path", help="audio (.webm/.m4a/.mp3/.wav...) or transcript (.vtt/.srt/.txt/.json)")
    p.add_argument("--title", help="meeting title")
    p.add_argument("--date", help="meeting date (YYYY-MM-DD); defaults to the file's date")
    p.add_argument("--meeting", help="attach to an existing meeting instead of creating one")
    p.add_argument("--process", action="store_true", help="transcribe and write minutes immediately")
    p.add_argument("--engine", choices=ENGINES, help="minutes engine to use with --process")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("transcribe", parents=[common], help="run speech recognition over a meeting's audio")
    p.add_argument("meeting", nargs="?", default="", help="meeting id or title (default: most recent)")
    p.add_argument("--force", action="store_true", help="re-transcribe even if a transcript exists")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("minutes", parents=[common], help="write minutes from a meeting's transcript")
    p.add_argument("meeting", nargs="?", default="")
    p.add_argument("--engine", choices=ENGINES, help="rules (offline, default) or claude")
    p.add_argument("--format", choices=render.FORMATS, default="txt")
    p.set_defaults(func=cmd_minutes)

    p = sub.add_parser("show", parents=[common], help="print the minutes already generated")
    p.add_argument("meeting", nargs="?", default="")
    p.add_argument("--format", choices=render.FORMATS, default="txt")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("export", parents=[common], help="write minutes to a file")
    p.add_argument("meeting", nargs="?", default="")
    p.add_argument("--format", choices=render.FORMATS, default="md")
    p.add_argument("-o", "--output", help="destination path")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("list", parents=[common], help="list meetings")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("actions", parents=[common], help="the action register across meetings")
    p.add_argument("--status", choices=["open", "done", "dropped", "all"], default="open")
    p.add_argument("--owner", help="filter by owner")
    p.add_argument("--meeting", help="limit to one meeting")
    p.set_defaults(func=cmd_actions)

    for name, helptext in (
        ("done", "mark an action done"),
        ("reopen", "reopen a closed action"),
        ("dropped", "mark an action as not happening"),
    ):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("id", type=int, help="action id from `minutely actions`")
        p.set_defaults(func=cmd_action_status)

    p = sub.add_parser("config", parents=[common], help="show or change settings")
    p.add_argument("--engine", choices=ENGINES)
    p.add_argument("--transcriber", choices=TRANSCRIBERS)
    p.add_argument("--whisper-bin", dest="whisper_bin", help="path to whisper-cli / whisper")
    p.add_argument("--whisper-model", dest="whisper_model", help="path to a whisper.cpp model file")
    p.add_argument("--language", help="spoken language hint, e.g. en")
    p.add_argument("--model", help="Anthropic model id for the claude engine")
    p.add_argument(
        "--teams-client-id",
        dest="teams_client_id",
        help="Microsoft Entra application (client) id for the Teams integration",
    )
    p.add_argument(
        "--teams-tenant",
        dest="teams_tenant",
        help="Microsoft tenant id, or 'organizations' (default)",
    )
    p.add_argument(
        "--auto-process",
        dest="auto_process",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="transcribe and minute automatically when a recording stops",
    )
    p.set_defaults(func=cmd_config)

    teams = sub.add_parser(
        "teams",
        parents=[common],
        help="pull Teams meeting transcripts and minute them",
        description=(
            "Teams records and transcribes meetings itself. These commands sign in to "
            "Microsoft 365, find your Teams meetings, and pull the transcripts Teams "
            "already produced — including the speaker names local recording cannot give you."
        ),
    )
    teams_sub = teams.add_subparsers(dest="teams_command", required=True)

    t = teams_sub.add_parser("login", parents=[common], help="sign in to Microsoft 365")
    t.add_argument("--client-id", help="Microsoft Entra application (client) id")
    t.add_argument("--tenant", help="tenant id, or 'organizations' (default)")
    t.add_argument(
        "--with-recordings",
        action="store_true",
        help="also request permission to download meeting recordings",
    )
    t.set_defaults(func=cmd_teams_login)

    t = teams_sub.add_parser("status", parents=[common], help="who is signed in, and with what")
    t.set_defaults(func=cmd_teams_status)

    t = teams_sub.add_parser("logout", parents=[common], help="forget the local Microsoft tokens")
    t.set_defaults(func=cmd_teams_logout)

    t = teams_sub.add_parser("list", parents=[common], help="Teams meetings on your calendar")
    t.add_argument("--days", type=int, default=7, help="how far back to look (default: 7)")
    t.add_argument("--limit", type=int, default=25)
    t.set_defaults(func=cmd_teams_list)

    t = teams_sub.add_parser("pull", parents=[common], help="import Teams transcripts and minute them")
    t.add_argument("--days", type=int, default=7, help="how far back to look (default: 7)")
    t.add_argument("--limit", type=int, default=20)
    t.add_argument("--match", help="only meetings whose subject contains this text")
    t.add_argument("--engine", choices=ENGINES, help="minutes engine to use")
    t.add_argument(
        "--with-recording",
        action="store_true",
        help="also download the meeting recording (needs `teams login --with-recordings`)",
    )
    t.add_argument("--force", action="store_true", help="re-import meetings already pulled")
    t.set_defaults(func=cmd_teams_pull)

    p = sub.add_parser("demo", parents=[common], help="minute a bundled sample meeting")
    p.add_argument("--engine", choices=ENGINES, default="rules")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("delete", parents=[common], help="delete a meeting and its minutes")
    p.add_argument("meeting")
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.add_argument("--files", action="store_true", help="also delete the audio and transcript files")
    p.set_defaults(func=cmd_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
        return result
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (PipelineError, EngineError, TranscriptionError, TeamsError) as exc:
        return _fail(str(exc), getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main())
