"""Sending minutes by email.

Two senders, because the right one depends on what you already have:

* **Microsoft 365.** If you signed in for the Teams integration, one more
  scope (``Mail.Send``) lets minutely send the minutes from your real mailbox,
  with your signature block absent but your address on the From line. No SMTP
  server to configure and no password to store anywhere.
* **SMTP.** For everyone else. The host, port, and username are settings; the
  password is read from ``MINUTELY_SMTP_PASSWORD`` at send time and is never
  written to disk — the same stance this app takes with API keys.

Recipients default to the attendees on the calendar invite, which is the whole
reason the invite's addresses are kept.

The audio is never attached. A meeting recording is large, and it is a
recording of people who agreed to be minuted, not to be forwarded.
"""

from __future__ import annotations

import base64
import re
import smtplib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from . import render
from .config import Settings
from .models import Meeting, Minutes, Transcript
from .teams import TeamsError
from .teams.graph import GraphClient

# Deliberately loose: this is a typo catcher, not an RFC 5322 parser. Rejecting
# a valid-but-exotic address would be worse than letting the mail server judge.
_ADDRESS = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
# Graph rejects attachments over 3 MB on this endpoint (they need an upload
# session instead), and no mail server thanks you for a huge one either.
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024

SMTPFactory = Callable[[str, int], smtplib.SMTP]


class ShareError(RuntimeError):
    """The minutes could not be sent."""


@dataclass
class Attachment:
    name: str
    content: bytes
    media_type: str = "text/markdown"

    @property
    def maintype(self) -> str:
        return self.media_type.split("/")[0]

    @property
    def subtype(self) -> str:
        return self.media_type.split("/")[-1]


@dataclass
class Email:
    subject: str
    html: str
    text: str
    recipients: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    sender: str = ""


@dataclass
class ShareResult:
    via: str  # graph | smtp
    recipients: list[str]
    subject: str
    attachments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "via": self.via,
            "recipients": self.recipients,
            "subject": self.subject,
            "attachments": self.attachments,
        }


# --------------------------------------------------------------------------
# Composing
# --------------------------------------------------------------------------


def normalise_recipients(raw: Sequence[str]) -> list[str]:
    """Split, trim, de-duplicate, and reject anything that is not an address."""
    seen: dict[str, None] = {}
    for entry in raw:
        for candidate in re.split(r"[,;\s]+", str(entry or "")):
            address = candidate.strip().strip("<>")
            if not address:
                continue
            if not _ADDRESS.match(address):
                raise ShareError(f"that does not look like an email address: {address}")
            seen.setdefault(address, None)
    return list(seen)


def compose(
    minutes: Minutes,
    meeting: Meeting | None = None,
    transcript: Transcript | None = None,
    *,
    recipients: Sequence[str] | None = None,
    note: str = "",
    with_transcript: bool = False,
    sender: str = "",
) -> Email:
    """Build the message: HTML body, plain-text alternative, markdown attached."""
    duration = meeting.duration if meeting else None
    addresses = normalise_recipients(
        recipients if recipients is not None else (meeting.emails if meeting else [])
    )
    if not addresses:
        raise ShareError(
            "no recipients — pass --to, or import the meeting from your calendar "
            "so the attendees come with it"
        )

    markdown = render.to_markdown(minutes, transcript, duration)
    attachments = [
        Attachment(name=_filename(minutes, "md"), content=markdown.encode("utf-8"))
    ]
    if with_transcript and transcript is not None and transcript.segments:
        from . import transcripts as transcript_module

        body = transcript_module.to_vtt(transcript).encode("utf-8")
        if len(body) <= MAX_ATTACHMENT_BYTES:
            attachments.append(
                Attachment(name=_filename(minutes, "vtt"), content=body, media_type="text/vtt")
            )

    oversized = [a.name for a in attachments if len(a.content) > MAX_ATTACHMENT_BYTES]
    if oversized:
        raise ShareError(f"attachment too large to send: {', '.join(oversized)}")

    return Email(
        subject=render.email_subject(minutes),
        html=render.to_email_html(minutes, transcript, duration, note=note),
        text=(f"{note}\n\n" if note else "") + render.to_text(minutes, transcript, duration),
        recipients=addresses,
        attachments=attachments,
        sender=sender,
    )


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


def send(
    message: Email,
    settings: Settings | None = None,
    *,
    graph: GraphClient | None = None,
    via: str = "",
    smtp_factory: SMTPFactory | None = None,
) -> ShareResult:
    """Send with whichever transport is available, or the one named."""
    settings = settings or Settings.load()
    chosen = (via or "").strip().lower()

    if chosen == "graph" or (not chosen and graph is not None):
        if graph is None:
            raise ShareError("not signed in to Microsoft 365 — run: minutely teams login")
        return send_via_graph(message, graph)
    if chosen == "smtp" or (not chosen and settings.smtp_ready):
        return send_via_smtp(message, settings, smtp_factory=smtp_factory)
    if chosen:
        raise ShareError(f"unknown mail transport {chosen!r} (choose from: graph, smtp)")
    raise ShareError(
        "no way to send mail is configured — either sign in to Microsoft 365 "
        "(`minutely teams login --with-email`) or set an SMTP server "
        "(`minutely config --smtp-host smtp.example.com --smtp-from you@example.com`)"
    )


def send_via_graph(message: Email, graph: GraphClient) -> ShareResult:
    """Send as the signed-in Microsoft 365 user."""
    if not graph.auth.can_send_mail:
        raise ShareError(
            "this Microsoft sign-in cannot send mail — run: "
            "minutely teams login --with-email (it asks for the Mail.Send permission)"
        )
    payload = {
        "message": {
            "subject": message.subject,
            "body": {"contentType": "HTML", "content": message.html},
            "toRecipients": [
                {"emailAddress": {"address": address}} for address in message.recipients
            ],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment.name,
                    "contentType": attachment.media_type,
                    "contentBytes": base64.b64encode(attachment.content).decode("ascii"),
                }
                for attachment in message.attachments
            ],
        },
        # It is the user's mailbox; the sent copy belongs in their Sent Items.
        "saveToSentItems": True,
    }
    try:
        graph.post_json("/me/sendMail", payload)
    except TeamsError as exc:
        raise ShareError(f"Microsoft would not send the mail: {exc}")
    return ShareResult(
        "graph",
        list(message.recipients),
        message.subject,
        [a.name for a in message.attachments],
    )


def send_via_smtp(
    message: Email, settings: Settings, *, smtp_factory: SMTPFactory | None = None
) -> ShareResult:
    """Send through a configured SMTP server."""
    if not settings.smtp_host:
        raise ShareError(
            "no SMTP server configured — minutely config --smtp-host smtp.example.com "
            "--smtp-from you@example.com"
        )
    sender = message.sender or settings.smtp_from or settings.smtp_user
    if not sender:
        raise ShareError("no From address — minutely config --smtp-from you@example.com")

    mail = EmailMessage()
    mail["Subject"] = message.subject
    mail["From"] = sender
    mail["To"] = ", ".join(message.recipients)
    mail.set_content(message.text)
    mail.add_alternative(message.html, subtype="html")
    for attachment in message.attachments:
        mail.add_attachment(
            attachment.content,
            maintype=attachment.maintype,
            subtype=attachment.subtype,
            filename=attachment.name,
        )

    factory = smtp_factory or smtplib.SMTP
    try:
        with factory(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_starttls:
                server.starttls()
            if settings.smtp_user:
                password = settings.smtp_password
                if not password:
                    raise ShareError(
                        "MINUTELY_SMTP_PASSWORD is not set — minutely never stores the "
                        "password, so it has to come from the environment"
                    )
                server.login(settings.smtp_user, password)
            server.send_message(mail)
    except ShareError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise ShareError(f"{settings.smtp_host} rejected the username or password")
    except smtplib.SMTPException as exc:
        raise ShareError(f"{settings.smtp_host} refused the message: {exc}")
    except OSError as exc:
        raise ShareError(f"could not reach {settings.smtp_host}:{settings.smtp_port}: {exc}")

    return ShareResult(
        "smtp",
        list(message.recipients),
        message.subject,
        [a.name for a in message.attachments],
    )


def _filename(minutes: Minutes, extension: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", minutes.title or "minutes").strip("-").lower()[:50]
    date_part = (minutes.held_on or "")[:10]
    return "-".join(part for part in (stem or "minutes", date_part) if part) + f".{extension}"


def available(settings: Settings, graph_ready: bool) -> list[str]:
    """Which transports could send right now."""
    options = []
    if graph_ready:
        options.append("graph")
    if settings.smtp_ready:
        options.append("smtp")
    return options
