"""Notifications.

Approval systems fail quietly: the request is raised, nobody is told, and three
weeks later someone asks why the supplier has not been paid. So every routing
event writes a message, addressed to a person, with the decision context in it.

Delivery is deliberately unopinionated. With no SMTP host configured the
messages are written to the outbox as ``.eml`` files and recorded in the
database — complete, addressed, and inspectable, with nothing leaving the
machine. Configure a host and ``apflow remind --send`` will deliver them.

Nothing here is allowed to fail an approval. :meth:`Engine._notify` swallows
exceptions from this module and records them as events, because a mail server
being down must not lose a decision someone has already made.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .config import Settings, outbox_dir
from .models import Directory, StepState
from .money import fmt, to_money
from .store import Store, parse_ts

OPENED = "opened"
REMINDER = "reminder"
ESCALATION = "escalation"
DECIDED = "decided"
CLOSED = "closed"
CHANGED = "changed"


def _hours_open(value: Any) -> float:
    started = parse_ts(value)
    if started is None:
        return 0.0
    return max(0.0, (datetime.now(UTC) - started).total_seconds() / 3600)


@dataclass
class Message:
    to_user: str
    to_email: str
    subject: str
    body: str
    kind: str
    request_id: int | None = None
    step_id: int | None = None

    def as_email(self, sender: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = sender or "apflow@localhost"
        message["To"] = self.to_email or f"{self.to_user}@localhost"
        message["Subject"] = self.subject
        message.set_content(self.body)
        return message


class Notifier:
    """Renders and records the messages an approval workflow generates."""

    def __init__(
        self,
        store: Store,
        directory: Directory,
        settings: Settings | None = None,
        outbox: Path | None = None,
    ) -> None:
        self.store = store
        self.directory = directory
        self.settings = settings or Settings()
        self.outbox = outbox

    # -- engine hooks -----------------------------------------------------

    def on_opened(self, request_id: int) -> None:
        self._notify_current_step(request_id, OPENED)

    def on_changed(self, request_id: int) -> None:
        self._notify_current_step(request_id, CHANGED)

    def on_decided(self, request_id: int, step_id: int | None = None, actor: str = "") -> None:
        """Tell the requester what just happened, and the next step it is theirs."""
        request = self.store.request_row(request_id)
        if request is None:
            return
        actor_user = self.directory.get(actor)
        requester = self.directory.get(str(request["requested_by"]))
        if requester is not None:
            decisions = self.store.decisions_for(request_id, str(request["fingerprint"]))
            last = decisions[-1] if decisions else {}
            verb = "approved" if last.get("decision") == "approve" else "rejected"
            self._emit(
                Message(
                    to_user=requester.user_id,
                    to_email=requester.email,
                    subject=f"{request['reference']} {verb} by "
                    f"{actor_user.label if actor_user else actor}",
                    body=self._decision_body(request, last),
                    kind=DECIDED,
                    request_id=request_id,
                    step_id=step_id,
                )
            )
        if request["state"] == "pending":
            self._notify_current_step(request_id, OPENED)

    def on_closed(self, request_id: int) -> None:
        request = self.store.request_row(request_id)
        if request is None:
            return
        requester = self.directory.get(str(request["requested_by"]))
        if requester is None:
            return
        self._emit(
            Message(
                to_user=requester.user_id,
                to_email=requester.email,
                subject=f"{request['reference']} is {request['state']}",
                body=self._summary_body(request)
                + f"\nFinal state: {str(request['state']).upper()}\n",
                kind=CLOSED,
                request_id=request_id,
            )
        )

    def on_auto_approved(self, request_id: int) -> None:
        request = self.store.request_row(request_id)
        if request is None:
            return
        requester = self.directory.get(str(request["requested_by"]))
        if requester is None:
            return
        self._emit(
            Message(
                to_user=requester.user_id,
                to_email=requester.email,
                subject=f"{request['reference']} auto-approved under {request['rule_name']}",
                body=self._summary_body(request)
                + "\nNo human approval was required under the matrix rule above.\n"
                "The decision is logged and appears in the audit trail like any other.\n",
                kind=CLOSED,
                request_id=request_id,
            )
        )

    # -- reminders --------------------------------------------------------

    def reminders(self, as_of: datetime | None = None) -> list[Message]:
        """Chase every step that has been open too long.

        The threshold is the step's own SLA when it has one, otherwise the
        global setting. Past the escalation threshold the approver's escalation
        contact is copied in by name — naming them in the body rather than
        silently re-routing, because moving someone's authority without telling
        them is how approval systems lose the trust of the people in them.
        """
        del as_of  # timestamps come from the rows themselves
        messages: list[Message] = []
        for request in self.store.list_requests(state="pending"):
            request_id = int(request["id"])
            for step in self.store.steps_for(request_id):
                if step["state"] != str(StepState.PENDING) or not step["opened_at"]:
                    continue
                if self._earlier_step_open(request_id, int(step["position"])):
                    continue
                hours = _hours_open(step["opened_at"])
                days = hours / 24
                sla = int(step["sla_days"]) or self.settings.reminder_after_days
                if days < sla:
                    continue
                escalate = days >= max(sla, self.settings.escalation_after_days)
                kind = ESCALATION if escalate else REMINDER
                last = self.store.last_notification(request_id, int(step["id"]), kind)
                if last is not None and _hours_open(last["created_at"]) < 20:
                    # One chase per day per step. A reminder that arrives hourly
                    # gets filtered, and then the real ones go unread too.
                    continue
                for approver in step["approvers"]:
                    user = self.directory.get(str(approver["user_id"]))
                    if user is None or not user.active:
                        continue
                    if self._has_decided(request_id, int(step["id"]), user.user_id, request):
                        continue
                    messages.append(
                        Message(
                            to_user=user.user_id,
                            to_email=user.email,
                            subject=(
                                f"{'ESCALATION' if escalate else 'Reminder'}: "
                                f"{request['reference']} waiting {days:.0f} day(s)"
                            ),
                            body=self._reminder_body(request, step, user.user_id, days, escalate),
                            kind=kind,
                            request_id=request_id,
                            step_id=int(step["id"]),
                        )
                    )
        return messages

    def _earlier_step_open(self, request_id: int, position: int) -> bool:
        return any(
            int(s["position"]) < position and s["state"] == str(StepState.PENDING)
            for s in self.store.steps_for(request_id)
        )

    def _has_decided(
        self, request_id: int, step_id: int, user_id: str, request: dict[str, Any]
    ) -> bool:
        return any(
            int(d["step_id"]) == step_id and d["user_id"].lower() == user_id.lower()
            for d in self.store.decisions_for(request_id, str(request["fingerprint"]))
        )

    # -- delivery ---------------------------------------------------------

    def _notify_current_step(self, request_id: int, kind: str) -> None:
        request = self.store.request_row(request_id)
        if request is None:
            return
        for step in self.store.steps_for(request_id):
            if step["state"] != str(StepState.PENDING):
                continue
            for approver in step["approvers"]:
                user = self.directory.get(str(approver["user_id"]))
                if user is None or not user.active:
                    continue
                prefix = "Re-approval needed" if kind == CHANGED else "Approval needed"
                self._emit(
                    Message(
                        to_user=user.user_id,
                        to_email=user.email,
                        subject=f"{prefix}: {request['reference']} "
                        f"{request['contact_name']} "
                        f"{fmt(to_money(request['amount']), str(request['currency']))}",
                        body=self._request_body(request, step, approver, kind),
                        kind=kind,
                        request_id=request_id,
                        step_id=int(step["id"]),
                    )
                )
            return  # only the step that is actually open

    def _emit(self, message: Message) -> int:
        path = ""
        if self.outbox is not None or not self.settings.can_send_mail:
            path = str(self._write_eml(message))
        return self.store.add_notification(
            request_id=message.request_id,
            step_id=message.step_id,
            user_id=message.to_user,
            email=message.to_email,
            kind=message.kind,
            subject=message.subject,
            body=message.body,
            path=path,
        )

    def _write_eml(self, message: Message) -> Path:
        directory = self.outbox or outbox_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        target = directory / f"{stamp}-{message.kind}-{message.to_user}.eml"
        target.write_text(
            message.as_email(self.settings.smtp_from or "apflow@localhost").as_string(),
            encoding="utf-8",
        )
        return target

    def send(self, messages: list[Message]) -> tuple[int, list[str]]:
        """Deliver over SMTP. Returns ``(sent, errors)``.

        Messages are recorded before the connection is opened, so a send that
        dies halfway leaves a record of what was attempted rather than a silent
        gap.
        """
        if not self.settings.can_send_mail:
            raise RuntimeError(
                "SMTP is not configured — set smtp_host and smtp_from in config.json "
                "(password via APFLOW_SMTP_PASSWORD). Without it, messages are written "
                "to the outbox instead."
            )
        ids = [self._emit(message) for message in messages]
        sent = 0
        errors: list[str] = []
        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as smtp:
                if self.settings.smtp_starttls:
                    smtp.starttls()
                if self.settings.smtp_user and self.settings.smtp_password:
                    smtp.login(self.settings.smtp_user, self.settings.smtp_password)
                for notification_id, message in zip(ids, messages, strict=True):
                    if not message.to_email:
                        errors.append(f"{message.to_user} has no email address")
                        continue
                    try:
                        smtp.send_message(message.as_email(self.settings.smtp_from))
                    except smtplib.SMTPException as exc:
                        errors.append(f"{message.to_email}: {exc}")
                        continue
                    self.store.mark_notification_sent(notification_id)
                    sent += 1
        except OSError as exc:
            errors.append(f"could not reach {self.settings.smtp_host}: {exc}")
        return sent, errors

    # -- bodies -----------------------------------------------------------

    def _summary_body(self, request: dict[str, Any]) -> str:
        amount = fmt(to_money(request["amount"]), str(request["currency"]))
        lines = [
            f"{request['reference']}  ·  {str(request['kind']).replace('_', ' ')}"
            f" {request['number']}",
            f"Counterparty : {request['contact_name']}",
            f"Amount       : {amount}",
            f"Dated        : {request['document_date'] or '—'}",
            f"Due          : {request['due_date'] or '—'}",
            f"Routed by    : {request['rule_name']}",
            f"Raised by    : {request['requested_by']}",
        ]
        if request["budget_status"] in ("warn", "breach"):
            lines.append(f"Budget       : {str(request['budget_status']).upper()}")
        return "\n".join(lines) + "\n"

    def _request_body(
        self, request: dict[str, Any], step: dict[str, Any], approver: dict[str, Any], kind: str
    ) -> str:
        body = self._summary_body(request)
        body += f"\nStep {step['position']}: {step['name']}"
        if str(step["quorum"]) == "all":
            body += "  (every approver on this step must agree)"
        body += "\n"
        if approver.get("delegated_from"):
            body += (
                f"You are covering for {approver['delegated_from']}, who is marked away.\n"
            )
        if kind == CHANGED:
            body += (
                "\nThis document was edited after approval had started. Earlier approvals "
                "no longer count and the workflow has restarted from the first step.\n"
            )
        body += (
            f"\nTo decide:\n"
            f"    apflow show {request['reference']}\n"
            f"    apflow approve {request['reference']} --as {approver['user_id']}\n"
            f"    apflow reject  {request['reference']} --as {approver['user_id']}"
            f" --comment \"...\"\n"
        )
        return body

    def _decision_body(self, request: dict[str, Any], decision: dict[str, Any]) -> str:
        body = self._summary_body(request)
        if decision:
            body += (
                f"\n{decision.get('user_id')} {decision.get('decision')}d it at "
                f"{decision.get('created_at')}.\n"
            )
            if decision.get("comment"):
                body += f"Comment: {decision['comment']}\n"
        body += f"\nCurrent state: {str(request['state']).upper()}\n"
        return body

    def _reminder_body(
        self,
        request: dict[str, Any],
        step: dict[str, Any],
        user_id: str,
        days: float,
        escalate: bool,
    ) -> str:
        body = self._summary_body(request)
        body += (
            f"\nStep {step['position']} ({step['name']}) has been waiting "
            f"{days:.1f} days for you.\n"
        )
        if escalate:
            user = self.directory.get(user_id)
            if user is not None and user.escalates_to:
                contact = self.directory.get(user.escalates_to)
                body += (
                    f"This is past the escalation threshold. "
                    f"{contact.label if contact else user.escalates_to} has been named "
                    "as the escalation contact for this delay.\n"
                )
        body += (
            f"\n    apflow approve {request['reference']} --as {user_id}\n"
            f"    apflow reject  {request['reference']} --as {user_id} --comment \"...\"\n"
        )
        return body
