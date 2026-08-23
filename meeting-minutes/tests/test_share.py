"""Composing and sending the minutes as email."""

from __future__ import annotations

import base64
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest

from minutely.config import Settings
from minutely.models import ActionItem, Decision, Meeting, Minutes, Segment, Transcript
from minutely.share import ShareError, compose, normalise_recipients, send, send_via_graph
from minutely.teams.auth import TeamsAuth
from minutely.teams.graph import GraphClient
from tests.fakes import FakeMicrosoft, json_response, signed_in_token

MAIL_SCOPE = "https://graph.microsoft.com/Mail.Send"


def sample() -> tuple[Minutes, Meeting, Transcript]:
    minutes = Minutes(
        meeting_id="m1",
        title="Weekly product sync",
        held_on="2026-08-24",
        attendees=["Dana", "Sam"],
        summary="They met.",
        decisions=[Decision(text="Bundle analytics.", segment_index=0)],
        actions=[ActionItem(text="Write the one-pager", owner="Sam", due="2026-09-01", segment_index=1)],
        review=[ActionItem(text="Credit the nineteen customers")],
        open_questions=["Do we grandfather recent signups?"],
        engine="rules",
    )
    meeting = Meeting(
        meeting_id="m1",
        title="Weekly product sync",
        held_on="2026-08-24",
        duration=2700,
        emails=["dana@example.com", "sam@example.com"],
    )
    transcript = Transcript(
        segments=[
            Segment(index=0, text="Let's talk pricing.", speaker="Dana", start=61.0, end=64.0),
            Segment(index=1, text="I'll write the one-pager.", speaker="Sam", start=125.0, end=129.0),
        ]
    )
    return minutes, meeting, transcript


class FakeSMTP:
    """Stands in for smtplib.SMTP, recording what a real server would see."""

    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        self.fail_with: Exception | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _clear_smtp() -> None:
    FakeSMTP.instances.clear()


def graph_with_mail(fake: FakeMicrosoft, tmp_path: Path, *, scopes: list[str] | None = None) -> GraphClient:
    signed_in_token(tmp_path / "teams-token.json", scopes=scopes if scopes is not None else [MAIL_SCOPE])
    auth = TeamsAuth(client_id="c", transport=fake, token_path=tmp_path / "teams-token.json")
    return GraphClient(auth, transport=fake, sleep=lambda _s: None)


# -- recipients -------------------------------------------------------------


def test_recipients_are_split_trimmed_and_deduplicated() -> None:
    assert normalise_recipients(["a@x.com, b@x.com", " a@x.com ", "<c@x.com>"]) == [
        "a@x.com",
        "b@x.com",
        "c@x.com",
    ]


def test_an_obvious_typo_is_caught_before_sending() -> None:
    with pytest.raises(ShareError, match="does not look like an email address"):
        normalise_recipients(["dana@example.com", "sam.example.com"])


def test_recipients_default_to_the_calendar_invite() -> None:
    minutes, meeting, transcript = sample()
    message = compose(minutes, meeting, transcript)
    assert message.recipients == ["dana@example.com", "sam@example.com"]


def test_sending_to_nobody_says_what_to_do() -> None:
    minutes, _meeting, _transcript = sample()
    with pytest.raises(ShareError, match="no recipients"):
        compose(minutes, Meeting(meeting_id="m1"))


# -- composing --------------------------------------------------------------


def test_the_email_carries_the_minutes_in_three_forms() -> None:
    minutes, meeting, transcript = sample()
    message = compose(minutes, meeting, transcript, note="Sorry I missed this one.")

    assert message.subject == "Minutes: Weekly product sync — 2026-08-24"
    # HTML for reading, text for clients that will not render it, markdown to keep.
    assert "Write the one-pager" in message.html
    assert "Sorry I missed this one." in message.html
    assert "ACTION POINTS" in message.text
    assert [a.name for a in message.attachments] == ["weekly-product-sync-2026-08-24.md"]
    assert message.attachments[0].content.decode().startswith("# Weekly product sync")


def test_the_email_body_is_mail_client_safe() -> None:
    minutes, meeting, transcript = sample()
    html = compose(minutes, meeting, transcript).html
    # No stylesheet, no media queries, no colour-scheme tricks: mail clients
    # strip or mangle all three.
    assert "<style" not in html
    assert "prefers-color-scheme" not in html
    assert "style=" in html
    assert "45 min" in html


def test_the_transcript_is_attached_only_when_asked_for() -> None:
    minutes, meeting, transcript = sample()
    assert len(compose(minutes, meeting, transcript).attachments) == 1
    with_transcript = compose(minutes, meeting, transcript, with_transcript=True)
    assert [a.name for a in with_transcript.attachments] == [
        "weekly-product-sync-2026-08-24.md",
        "weekly-product-sync-2026-08-24.vtt",
    ]
    assert with_transcript.attachments[1].content.decode().startswith("WEBVTT")


def test_the_recording_is_never_attached() -> None:
    minutes, meeting, transcript = sample()
    meeting.audio_path = "/somewhere/recording.webm"
    message = compose(minutes, meeting, transcript, with_transcript=True)
    assert all(not a.name.endswith(".webm") for a in message.attachments)


# -- sending through Microsoft 365 -----------------------------------------


def test_graph_send_builds_the_payload_microsoft_expects(tmp_path: Path) -> None:
    minutes, meeting, transcript = sample()
    fake = FakeMicrosoft().add(r"/me/sendMail", json_response({}, 202))
    result = send_via_graph(compose(minutes, meeting, transcript), graph_with_mail(fake, tmp_path))

    assert result.via == "graph"
    assert result.recipients == ["dana@example.com", "sam@example.com"]

    import json

    body = json.loads(fake.calls[0].body or b"{}")
    assert body["saveToSentItems"] is True
    message = body["message"]
    assert message["subject"].startswith("Minutes:")
    assert message["body"]["contentType"] == "HTML"
    assert [r["emailAddress"]["address"] for r in message["toRecipients"]] == result.recipients
    attachment = message["attachments"][0]
    assert attachment["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert base64.b64decode(attachment["contentBytes"]).decode().startswith("# Weekly")


def test_a_signin_without_the_mail_permission_says_how_to_fix_it(tmp_path: Path) -> None:
    minutes, meeting, transcript = sample()
    fake = FakeMicrosoft()
    graph = graph_with_mail(fake, tmp_path, scopes=["Calendars.Read"])
    with pytest.raises(ShareError, match="--with-email"):
        send_via_graph(compose(minutes, meeting, transcript), graph)
    assert fake.calls == []


def test_a_microsoft_refusal_is_reported_not_swallowed(tmp_path: Path) -> None:
    minutes, meeting, transcript = sample()
    fake = FakeMicrosoft().add(
        r"/me/sendMail",
        json_response({"error": {"code": "ErrorSendAsDenied", "message": "not allowed"}}, 403),
    )
    with pytest.raises(ShareError, match="Microsoft would not send"):
        send_via_graph(compose(minutes, meeting, transcript), graph_with_mail(fake, tmp_path))


# -- sending through SMTP ---------------------------------------------------


def smtp_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_from": "me@example.com",
    }
    values.update(overrides)
    return Settings(**values)


def test_smtp_sends_a_multipart_message_with_the_markdown_attached() -> None:
    minutes, meeting, transcript = sample()
    result = send(
        compose(minutes, meeting, transcript), smtp_settings(), via="smtp", smtp_factory=FakeSMTP
    )

    assert result.via == "smtp"
    server = FakeSMTP.instances[0]
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.started_tls is True
    assert server.login_args is None  # no username configured, so no login attempt

    sent = server.sent[0]
    assert sent["To"] == "dana@example.com, sam@example.com"
    assert sent["From"] == "me@example.com"
    assert sent["Subject"].startswith("Minutes:")
    parts = [part.get_content_type() for part in sent.walk()]
    assert "text/plain" in parts and "text/html" in parts
    attached = [p.get_filename() for p in sent.iter_attachments() if p.get_filename()]
    assert attached == ["weekly-product-sync-2026-08-24.md"]


def test_smtp_logs_in_with_a_password_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINUTELY_SMTP_PASSWORD", "hunter2")
    minutes, meeting, transcript = sample()
    send(
        compose(minutes, meeting, transcript),
        smtp_settings(smtp_user="me@example.com"),
        via="smtp",
        smtp_factory=FakeSMTP,
    )
    assert FakeSMTP.instances[0].login_args == ("me@example.com", "hunter2")


def test_a_missing_smtp_password_is_explained_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINUTELY_SMTP_PASSWORD", raising=False)
    minutes, meeting, transcript = sample()
    with pytest.raises(ShareError, match="MINUTELY_SMTP_PASSWORD"):
        send(
            compose(minutes, meeting, transcript),
            smtp_settings(smtp_user="me@example.com"),
            via="smtp",
            smtp_factory=FakeSMTP,
        )


def test_starttls_can_be_turned_off() -> None:
    minutes, meeting, transcript = sample()
    send(
        compose(minutes, meeting, transcript),
        smtp_settings(smtp_starttls=False),
        via="smtp",
        smtp_factory=FakeSMTP,
    )
    assert FakeSMTP.instances[0].started_tls is False


def test_a_rejected_message_is_reported_with_the_server_that_rejected_it() -> None:
    minutes, meeting, transcript = sample()

    def factory(host: str, port: int) -> FakeSMTP:
        server = FakeSMTP(host, port)
        server.fail_with = smtplib.SMTPRecipientsRefused({})
        return server

    with pytest.raises(ShareError, match=r"smtp\.example\.com refused"):
        send(compose(minutes, meeting, transcript), smtp_settings(), via="smtp", smtp_factory=factory)


def test_bad_credentials_are_reported_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINUTELY_SMTP_PASSWORD", "wrong")
    minutes, meeting, transcript = sample()

    class Rejecting(FakeSMTP):
        def login(self, user: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"nope")

    with pytest.raises(ShareError, match="rejected the username or password"):
        send(
            compose(minutes, meeting, transcript),
            smtp_settings(smtp_user="me@example.com"),
            via="smtp",
            smtp_factory=Rejecting,
        )


# -- choosing a transport ---------------------------------------------------


def test_microsoft_is_preferred_when_signed_in(tmp_path: Path) -> None:
    minutes, meeting, transcript = sample()
    fake = FakeMicrosoft().add(r"/me/sendMail", json_response({}, 202))
    result = send(
        compose(minutes, meeting, transcript),
        smtp_settings(),
        graph=graph_with_mail(fake, tmp_path),
        smtp_factory=FakeSMTP,
    )
    assert result.via == "graph"
    assert FakeSMTP.instances == []


def test_smtp_is_used_when_there_is_no_microsoft_signin() -> None:
    minutes, meeting, transcript = sample()
    result = send(compose(minutes, meeting, transcript), smtp_settings(), smtp_factory=FakeSMTP)
    assert result.via == "smtp"


def test_with_nothing_configured_the_error_names_both_options() -> None:
    minutes, meeting, transcript = sample()
    with pytest.raises(ShareError, match="teams login --with-email"):
        send(compose(minutes, meeting, transcript), Settings(), smtp_factory=FakeSMTP)
