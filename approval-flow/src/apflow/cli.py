"""Command line interface.

Uses ``argparse`` rather than a CLI framework to keep the package free of
runtime dependencies. Every command accepts ``--json`` so the tool can be
driven from a scheduled job as easily as from a terminal — `apflow pull` on a
timer is the normal way to run this.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import __version__
from .budget import Budget, commitments_from_store
from .config import Settings, data_dir, governance_path
from .matrix import ApprovalMatrix, MatrixError
from .models import Decision, Directory, DocumentKind, User
from .money import ZERO, fmt, to_money
from .notify import Notifier
from .posting import PostingError, push_approved
from .providers import ProviderError
from .providers.factory import get_provider
from .reports import build_queue, overview, spend_under_approval, turnaround
from .store import Store
from .workflow import Engine, WorkflowError

_STATE_MARK = {"pending": "..", "approved": "ok", "rejected": "XX", "cancelled": "--"}


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _rule(width: int = 78) -> None:
    print("-" * width)


class Context:
    """Everything a command needs, assembled once.

    The governance files resolve against the snapshot directory first when one
    is in use. That is what lets the bundled demo run with a single flag while
    a real deployment keeps its matrix wherever it keeps its other reviewed
    configuration.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.settings = Settings.load()
        if getattr(args, "allow_writes", False):
            self.settings.allow_writes = True
        self.provider = get_provider(snapshot=getattr(args, "snapshot", None), settings=self.settings)
        self.store = Store(getattr(args, "db", None))
        self.directory = Directory.load(self._path("users"))
        self.budget = Budget.load(self._path("budget"))
        self._matrix: ApprovalMatrix | None = None
        self._base_currency: str | None = None
        self.notifier = Notifier(self.store, self.directory, self.settings)

    def _path(self, kind: str) -> Path:
        explicit = getattr(self.args, kind, None)
        if explicit:
            return Path(explicit).expanduser()
        snapshot = getattr(self.args, "snapshot", None)
        if snapshot:
            candidate = Path(snapshot) / f"{kind}.json"
            if candidate.exists():
                return candidate
        return governance_path(self.settings, kind)

    @property
    def base_currency(self) -> str:
        """The organisation's own currency — what limits and budgets are in."""
        if self._base_currency is None:
            try:
                self._base_currency = self.provider.organisation().base_currency
            except ProviderError:
                self._base_currency = "AUD"
        return self._base_currency

    @property
    def matrix(self) -> ApprovalMatrix:
        if self._matrix is None:
            self._matrix = ApprovalMatrix.load(self._path("matrix"))
        return self._matrix

    @property
    def engine(self) -> Engine:
        return Engine(
            store=self.store,
            matrix=self.matrix,
            directory=self.directory,
            settings=self.settings,
            budget=self.budget,
            notifier=self.notifier,
        )

    def close(self) -> None:
        self.store.close()

    def require_user(self, user_id: str | None) -> User:
        if not user_id:
            raise WorkflowError("this command needs --as <user-id>")
        return self.directory.require(user_id)

    def resolve(self, handle: str) -> int:
        request_id = self.store.resolve_reference(handle)
        if request_id is None:
            raise WorkflowError(f"no approval request matches {handle!r}")
        return request_id


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        summary = overview(ctx.store, ctx.directory)
        if args.json:
            _emit({"source": ctx.provider.name, **summary})
            return 0

        pending = summary["pending"]
        ready = summary["ready_to_post"]
        print(f"\napflow · {ctx.provider.name} ({'read/write' if ctx.provider.can_write else 'read-only'})")
        print(f"{len(ctx.directory)} user(s) · {len(ctx.matrix.rules)} matrix rule(s)"
              f" · {len(ctx.budget.lines)} budget line(s)")
        _rule()
        print(f"  Awaiting approval   {pending['count']:>5}   {fmt(to_money(pending['total'])):>16}")
        print(f"  Past SLA            {pending['breached']:>5}   oldest {pending['oldest_days']} day(s)")
        print(f"  Approved, unposted  {ready['count']:>5}   {fmt(to_money(ready['total'])):>16}")
        _rule()

        if summary["by_approver"]:
            print("\nOn whose desk")
            for label, bucket in list(summary["by_approver"].items())[:8]:
                flag = f"  {bucket['breached']} past SLA" if bucket["breached"] else ""
                print(
                    f"  {label[:28]:<28}{bucket['count']:>3} item(s)"
                    f"{fmt(to_money(bucket['total'])):>16}   oldest {bucket['oldest_days']}d{flag}"
                )
        if summary["budget_flags"]:
            print(f"\nBudget flags: {', '.join(summary['budget_flags'])}")
        if summary["recent_changes"]:
            print("\nDocuments changed after submission")
            for change in summary["recent_changes"][:5]:
                print(f"  {change['reference'] or '—':<10}{change['detail'][:60]}")
        print()
        return 0
    finally:
        ctx.close()


def cmd_pull(args: argparse.Namespace) -> int:
    """Fetch documents from the ledger and route anything new."""
    ctx = Context(args)
    try:
        kinds = (
            [DocumentKind.parse(args.kind)]
            if args.kind
            else [DocumentKind.BILL, DocumentKind.INVOICE, DocumentKind.PURCHASE_ORDER]
        )
        requested_by = args.requested_by or "system"
        results: list[dict[str, Any]] = []
        engine = ctx.engine

        for kind in kinds:
            for document in ctx.provider.documents(kind, status=args.status):
                if args.dry_run:
                    try:
                        resolution = ctx.matrix.resolve(document, ctx.directory)
                        detail = (
                            "auto-approve"
                            if resolution.auto_approve
                            else " -> ".join(s.name for s in resolution.steps)
                        )
                        outcome = "would_route"
                    except MatrixError as exc:
                        detail, outcome = str(exc), "unroutable"
                    results.append(
                        {
                            "outcome": outcome,
                            "kind": str(kind),
                            "number": document.number,
                            "contact": document.contact.name,
                            "amount": str(document.amount_total),
                            "message": detail,
                        }
                    )
                    continue
                try:
                    result = engine.submit(document, requested_by=requested_by)
                except MatrixError as exc:
                    results.append(
                        {
                            "outcome": "unroutable",
                            "kind": str(kind),
                            "number": document.number,
                            "contact": document.contact.name,
                            "amount": str(document.amount_total),
                            "message": str(exc),
                        }
                    )
                    continue
                results.append(
                    {
                        **result.as_dict(),
                        "kind": str(kind),
                        "number": document.number,
                        "contact": document.contact.name,
                        "amount": str(document.amount_total),
                    }
                )

        if args.json:
            _emit({"source": ctx.provider.name, "results": results})
            return 0

        # `unchanged` and `already_*` are the steady state of a scheduled pull.
        # Printing them would bury the handful of rows that need a human.
        interesting = [
            r
            for r in results
            if r["outcome"] != "unchanged" and not str(r["outcome"]).startswith("already_")
        ]
        print(f"\nPulled from {ctx.provider.name}: {len(results)} document(s)")
        _rule(96)
        if not interesting:
            print("  nothing new — every document is already routed or already decided\n")
            return 0
        print(f"  {'outcome':<18}{'ref':<10}{'document':<22}{'amount':>14}  detail")
        _rule(96)
        for row in interesting:
            print(
                f"  {row['outcome']:<18}{row.get('reference') or '—'!s:<10}"
                f"{(row['contact'][:14] + ' ' + str(row['number']))[:21]:<22}"
                f"{fmt(to_money(row['amount'])):>14}  {str(row.get('message') or '')[:36]}"
            )
        _rule(96)
        unroutable = [r for r in results if r["outcome"] == "unroutable"]
        if unroutable:
            print(f"\n{len(unroutable)} document(s) matched no rule and were NOT routed.")
            print("Run `apflow matrix --check` — a catch-all rule stops this happening.")
        print()
        return 1 if unroutable else 0
    finally:
        ctx.close()


def cmd_queue(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        queue = build_queue(ctx.store, ctx.directory)
        if args.json:
            _emit(queue.as_dict())
            return 0
        if not queue.entries:
            print("\nNothing is waiting for approval.\n")
            return 0
        base = ctx.base_currency
        print(f"\n{len(queue.entries)} request(s) awaiting approval · {fmt(queue.total, base)}")
        _rule(104)
        print(f"  {'ref':<9}{'document':<30}{'amount':>13}  {'step':<22}{'waiting on':<18}age")
        _rule(104)
        for entry in queue.entries:
            flag = "!" if entry.breached else " "
            budget = "$" if entry.budget_status in ("warn", "breach") else " "
            print(
                f" {flag}{budget}{entry.reference:<9}"
                f"{(entry.contact[:18] + ' ' + entry.number)[:29]:<30}"
                f"{fmt(entry.amount, base) + ('*' if entry.is_foreign else ' '):>13}  "
                f"{(str(entry.step_position) + '. ' + entry.step_name)[:21]:<22}"
                f"{', '.join(entry.waiting_on)[:17]:<18}{entry.age_days:.1f}d"
            )
        _rule(104)
        if queue.breached:
            print(f"  ! {len(queue.breached)} past SLA")
        if any(e.is_foreign for e in queue.entries):
            print(f"  * raised in another currency, shown here in {base}")
        if any(e.budget_status in ("warn", "breach") for e in queue.entries):
            print("  $ budget warning or breach")
        print()
        return 0
    finally:
        ctx.close()


def cmd_inbox(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        user = ctx.require_user(args.user)
        rows = ctx.store.pending_steps_for_user(user.user_id)
        if args.json:
            _emit({"user": user.user_id, "items": rows})
            return 0
        if not rows:
            print(f"\nNothing is waiting on {user.label}.\n")
            return 0
        print(f"\n{len(rows)} request(s) waiting on {user.label}")
        limit = "unlimited" if user.approval_limit is None else fmt(user.approval_limit)
        print(f"approval limit: {limit}")
        _rule(98)
        base = ctx.base_currency
        for row in rows:
            amount = to_money(row["base_amount"])
            over = "" if user.within_limit(amount) else "  OVER YOUR LIMIT"
            delegated = (
                f"  (covering for {row['delegated_from']})" if row["delegated_from"] else ""
            )
            foreign = (
                f"  [{row['currency']} {to_money(row['amount']):,.2f}]"
                if str(row["currency"]) != base
                else ""
            )
            print(
                f"  {row['reference']:<9}{(str(row['contact_name'])[:20] + ' ' + str(row['number']))[:29]:<30}"
                f"{fmt(amount, base):>13}  "
                f"{str(row['name'])[:20]:<22}{over}{delegated}{foreign}"
            )
        _rule(98)
        print("\n  apflow show <ref>                      full detail and audit trail")
        print(f"  apflow approve <ref> --as {user.user_id}")
        print(f"  apflow reject  <ref> --as {user.user_id} --comment \"...\"\n")
        return 0
    finally:
        ctx.close()


def cmd_show(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        view = ctx.engine.request_view(ctx.resolve(args.reference))
        if args.json:
            _emit(view)
            return 0

        currency = str(view["currency"])
        print(f"\n{view['reference']}  ·  {str(view['kind']).replace('_', ' ')} {view['number']}"
              f"  ·  {str(view['state']).upper()}")
        _rule(88)
        print(f"  Counterparty   {view['contact']}")
        base = ctx.base_currency
        amount_line = f"{currency} {fmt(to_money(view['amount']), currency)}"
        if currency != base:
            amount_line += f"   ({base} {fmt(to_money(view['base_amount']), base)} at the booked rate)"
        print(f"  Amount         {amount_line}")
        print(f"  Dated          {view['document_date'] or '—'}      Due {view['due_date'] or '—'}")
        print(f"  Routed by      {view['rule']}")
        print(f"  Raised by      {view['requested_by']} at {view['created_at']}")
        print(f"  Fingerprint    {view['fingerprint']}")
        if view["pushed_at"]:
            print(f"  Posted         {view['pushed_at']}  ({view['push_ref']})")

        budget = view.get("budget") or {}
        if budget.get("impacts") or budget.get("message"):
            print(f"\n  Budget: {str(view['budget_status']).upper()} — {budget.get('message') or 'within budget'}")
            for impact in budget.get("impacts", []):
                print(
                    f"    {impact['label'][:38]:<40}"
                    f"{fmt(to_money(impact['remaining_before'])):>13} left ->"
                    f"{fmt(to_money(impact['remaining_after'])):>13}"
                    f"   ({impact['utilisation']}% used)"
                )
            for note in budget.get("unbudgeted", []):
                print(f"    unbudgeted: {note}")

        document = view.get("document") or {}
        if document.get("line_items"):
            print("\n  Lines")
            for line in document["line_items"]:
                print(
                    f"    {str(line['description'])[:42]:<44}"
                    f"{line['account_code'] or '—'!s:<8}"
                    f"{fmt(to_money(line['line_amount']), currency):>13}"
                )

        print("\n  Workflow")
        for step in view["steps"]:
            mark = {"approved": "ok", "pending": "..", "rejected": "XX",
                    "skipped": "--", "cancelled": "--"}.get(step["state"], "??")
            approvers = ", ".join(
                a["label"] + (f" (for {a['delegated_from']})" if a["delegated_from"] else "")
                for a in step["approvers"]
            )
            quorum = " · all must agree" if step["quorum"] == "all" else ""
            print(f"   {mark}  {step['position']}. {step['name']}: {approvers}{quorum}")
            for decision in step["decisions"]:
                stale = "" if decision["current"] else "   [superseded version]"
                acting = f" for {decision['acting_for']}" if decision["acting_for"] else ""
                print(
                    f"        {decision['decision']:<8}{decision['user_id']}{acting}"
                    f"  {decision['created_at'][:19]}{stale}"
                )
                if decision["comment"]:
                    print(f"          “{decision['comment']}”")
            if step["note"]:
                print(f"        note: {step['note']}")

        print("\n  Audit trail")
        for event in view["events"]:
            actor = f" [{event['actor']}]" if event["actor"] else ""
            print(f"    {event['created_at'][:19]}  {event['kind']:<20}{actor} {event['detail'][:60]}")
        print()
        return 0
    finally:
        ctx.close()


def _decide(args: argparse.Namespace, decision: Decision) -> int:
    ctx = Context(args)
    try:
        user = ctx.require_user(args.user)
        result = ctx.engine.decide(
            request_id=ctx.resolve(args.reference),
            user_id=user.user_id,
            decision=decision,
            comment=args.comment or "",
        )
        if args.json:
            _emit(result.as_dict())
            return 0
        print(f"\n{result.message}")
        if result.next_step:
            print(f"  next: {result.next_step}")
        print()
        return 0
    finally:
        ctx.close()


def cmd_approve(args: argparse.Namespace) -> int:
    return _decide(args, Decision.APPROVE)


def cmd_reject(args: argparse.Namespace) -> int:
    return _decide(args, Decision.REJECT)


def cmd_cancel(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        user = ctx.require_user(args.user)
        request_id = ctx.resolve(args.reference)
        ctx.engine.cancel(request_id, user.user_id, args.reason or "")
        reference = ctx.store.request_row(request_id)
        print(f"\n{reference['reference'] if reference else request_id} withdrawn\n")
        return 0
    finally:
        ctx.close()


def cmd_matrix(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        path = ctx._path("matrix")
        if args.init:
            if path.exists() and not args.force:
                print(f"error: {path} already exists (use --force to overwrite)", file=sys.stderr)
                return 2
            ApprovalMatrix.starter().save(path)
            print(f"wrote a starter approval matrix to {path}")
            print("Edit it, then run `apflow matrix --check` to validate it against your users.")
            return 0

        matrix = ctx.matrix
        problems = matrix.validate(ctx.directory)
        if args.json:
            _emit({"path": str(path), "matrix": matrix.as_dict(), "problems": problems})
            return 1 if problems else 0

        print(f"\nApproval matrix · {path}")
        _rule(88)
        for index, rule in enumerate(matrix.rules, start=1):
            print(f"  {index}. {rule.name}")
            print(f"       when: {rule.condition.describe()}")
            if rule.auto_approve and not rule.steps:
                print("       -> auto-approve, no human step")
            for step in rule.steps:
                quorum = "all of" if str(step.quorum) == "all" else "any of"
                condition = (
                    "" if step.condition.is_empty else f"   [only when {step.condition.describe()}]"
                )
                sla = f"   SLA {step.sla_days}d" if step.sla_days else ""
                print(f"       -> {step.name}: {quorum} {', '.join(step.approvers)}{condition}{sla}")
            if rule.note:
                print(f"       note: {rule.note}")
        _rule(88)
        if problems:
            print(f"\n{len(problems)} problem(s):")
            for problem in problems:
                print(f"  ! {problem}")
            print()
            return 1
        print("\nNo problems found.\n")
        return 0
    finally:
        ctx.close()


def cmd_users(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        path = ctx._path("users")
        if args.json:
            _emit({"path": str(path), "users": ctx.directory.as_list()})
            return 0
        if not len(ctx.directory):
            print(f"\nNo users configured. Expected a JSON file at {path}:\n")
            print(json.dumps(
                {"users": [{"id": "cfo", "name": "Your Name", "email": "you@example.com",
                            "role": "CFO", "approval_limit": None}]},
                indent=2,
            ))
            print()
            return 1
        today = date.today()
        print(f"\n{len(ctx.directory)} user(s) · {path}")
        _rule(92)
        print(f"  {'id':<12}{'name':<24}{'role':<16}{'limit':>14}  status")
        _rule(92)
        for user in ctx.directory:
            limit = "unlimited" if user.approval_limit is None else fmt(user.approval_limit)
            status = "inactive" if not user.active else (
                f"away until {user.away_until} -> {user.delegate_to or 'nobody'}"
                if user.is_away(today) else "active"
            )
            print(f"  {user.user_id:<12}{user.name[:23]:<24}{user.role[:15]:<16}{limit:>14}  {status}")
        _rule(92)
        print()
        return 0
    finally:
        ctx.close()


def cmd_budget(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        path = ctx._path("budget")
        if not ctx.budget:
            print(f"\nNo budget configured (looked in {path}).")
            print("Budget checking is off until one exists.\n")
            return 0
        commitments = commitments_from_store(ctx.budget, ctx.store)
        rows: list[dict[str, Any]] = []
        for line in ctx.budget.lines:
            if args.period and line.period != args.period:
                continue
            committed = commitments.get(line.key, ZERO)
            remaining = line.amount - committed
            share = (
                (committed / line.amount * Decimal("100")).quantize(Decimal("1"))
                if line.amount
                else ZERO
            )
            rows.append(
                {
                    "label": line.label,
                    "period": line.period,
                    "account": line.account,
                    "tracking": line.tracking,
                    "budget": str(line.amount),
                    "committed": str(committed),
                    "remaining": str(remaining),
                    "used_pct": str(share),
                }
            )
        if args.json:
            _emit({"path": str(path), "granularity": ctx.budget.granularity, "lines": rows})
            return 0

        print(f"\nBudget · {ctx.budget.granularity} · {path}")
        print("committed = every request that is pending or approved, posted or not")
        _rule(92)
        print(f"  {'line':<34}{'budget':>13}{'committed':>13}{'remaining':>13}  used")
        _rule(92)
        for row in rows:
            flag = " !" if to_money(row["remaining"]) < 0 else ""
            print(
                f"  {row['label'][:33]:<34}{fmt(to_money(row['budget'])):>13}"
                f"{fmt(to_money(row['committed'])):>13}{fmt(to_money(row['remaining'])):>13}"
                f"  {row['used_pct']}%{flag}"
            )
        _rule(92)
        print()
        return 0
    finally:
        ctx.close()


def cmd_push(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        summary = push_approved(ctx.engine, ctx.provider, dry_run=args.dry_run, limit=args.limit)
        if args.json:
            _emit(summary.as_dict())
            return 0
        if not summary.outcomes:
            print("\nNothing approved and waiting to post.\n")
            return 0
        heading = "Would post" if args.dry_run else "Posting to " + ctx.provider.name
        print(f"\n{heading}")
        _rule(96)
        for outcome in summary.outcomes:
            print(
                f"  {outcome.action:<12}{outcome.reference:<9}"
                f"{(outcome.contact[:16] + ' ' + outcome.number)[:25]:<26}"
                f"{fmt(outcome.amount, outcome.currency):>13}  {outcome.detail[:30]}"
            )
        _rule(96)
        if summary.count("changed"):
            print(
                f"\n{summary.count('changed')} document(s) were edited after approval and were "
                "NOT posted.\nTheir approvals have been reset and the approvers notified."
            )
        if summary.count("pushed"):
            print(f"\nPosted {summary.count('pushed')} document(s), {fmt(summary.value_pushed)}.")
        print()
        return 1 if summary.count("failed") else 0
    finally:
        ctx.close()


def cmd_remind(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        messages = ctx.notifier.reminders()
        if not messages:
            if args.json:
                _emit({"reminders": 0})
            else:
                print("\nNothing is overdue.\n")
            return 0
        if args.send:
            sent, errors = ctx.notifier.send(messages)
            if args.json:
                _emit({"reminders": len(messages), "sent": sent, "errors": errors})
                return 1 if errors else 0
            print(f"\nSent {sent} of {len(messages)} reminder(s).")
            for error in errors:
                print(f"  ! {error}")
            print()
            return 1 if errors else 0

        for message in messages:
            ctx.notifier._emit(message)
        if args.json:
            _emit({"reminders": len(messages),
                   "messages": [{"to": m.to_user, "subject": m.subject, "kind": m.kind}
                                for m in messages]})
            return 0
        print(f"\n{len(messages)} reminder(s) written to the outbox ({data_dir() / 'outbox'})")
        _rule(88)
        for message in messages:
            print(f"  {message.kind:<12}{message.to_user:<12}{message.subject[:56]}")
        _rule(88)
        print("\nConfigure smtp_host and smtp_from in config.json, then use --send to deliver.\n")
        return 0
    finally:
        ctx.close()


def cmd_audit(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        events = ctx.store.recent_events(args.limit)
        writes = ctx.store.audit_tail(args.limit)
        if args.json:
            _emit({"events": events, "writes": writes})
            return 0
        print(f"\nLast {len(events)} workflow event(s)")
        _rule(104)
        for event in events:
            actor = f"[{event['actor']}]" if event["actor"] else ""
            print(
                f"  {event['created_at'][:19]:<21}{event.get('reference') or '—'!s:<9}"
                f"{event['kind']:<22}{actor:<12}{event['detail'][:36]}"
            )
        _rule(104)
        if writes:
            print(f"\nLast {len(writes)} write(s) to the ledger")
            _rule(104)
            for write in writes:
                print(
                    f"  {write['created_at'][:19]:<21}{write['action'][:24]:<26}"
                    f"{str(write['target'])[:20]:<22}{write['outcome']:<10}{str(write['detail'])[:24]}"
                )
            _rule(104)
            pending = [w for w in writes if w["outcome"] == "pending"]
            if pending:
                print(
                    f"\n{len(pending)} write(s) still marked pending. Each was sent but its "
                    "outcome was never recorded — check the ledger before retrying."
                )
        print()
        return 0
    finally:
        ctx.close()


def cmd_report(args: argparse.Namespace) -> int:
    ctx = Context(args)
    try:
        if args.what == "turnaround":
            data = turnaround(ctx.store, ctx.directory)
            if args.json:
                _emit(data)
                return 0
            print(f"\nDecision turnaround · {data['decisions']} decision(s) · "
                  f"median {data['median_hours']}h")
            _rule(88)
            print(f"  {'approver':<24}{'decisions':>10}{'median':>10}{'slowest':>10}{'rejected':>10}")
            _rule(88)
            for approver in data["approvers"]:
                print(
                    f"  {approver['label'][:23]:<24}{approver['decisions']:>10}"
                    f"{approver['median_hours']:>9}h{approver['slowest_hours']:>9}h"
                    f"{approver['rejection_rate']:>9}%"
                )
            _rule(88)
            print()
            return 0

        data = spend_under_approval(ctx.store)
        if args.json:
            _emit(data)
            return 0
        print(f"\nSpend under approval · {fmt(to_money(data['total']))}")
        _rule(60)
        print("  By counterparty")
        for name, amount in list(data["by_contact"].items())[:12]:
            print(f"    {name[:36]:<38}{fmt(to_money(amount)):>16}")
        print("\n  By account code")
        for code, amount in list(data["by_account"].items())[:12]:
            print(f"    {code[:36]:<38}{fmt(to_money(amount)):>16}")
        print()
        return 0
    finally:
        ctx.close()


def cmd_connect(args: argparse.Namespace) -> int:
    from .providers.oauth import authorize

    settings = Settings.load()
    settings.allow_writes = bool(args.allow_writes)
    tokens = authorize(settings.client_id, settings.scopes(), port=args.port)
    settings.save()
    print(f"\nConnected to {tokens.tenant_name or tokens.tenant_id}")
    print(f"  scopes: {' '.join(tokens.scopes)}")
    print(f"  mode:   {'read/write' if settings.allow_writes else 'read-only'}\n")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    settings = Settings.load()
    if args.set:
        fields = settings.__dataclass_fields__
        for assignment in args.set:
            if "=" not in assignment:
                print(f"error: expected key=value, got {assignment!r}", file=sys.stderr)
                return 2
            key, _, value = assignment.partition("=")
            key = key.strip()
            if key not in fields:
                print(f"error: unknown setting {key!r}. Known: {', '.join(sorted(fields))}",
                      file=sys.stderr)
                return 2
            current = getattr(settings, key)
            if isinstance(current, bool):
                setattr(settings, key, value.strip().lower() in ("1", "true", "yes", "on"))
            elif isinstance(current, int):
                setattr(settings, key, int(value))
            else:
                setattr(settings, key, value.strip())
        path = settings.save()
        print(f"saved {path}")
    payload = {k: getattr(settings, k) for k in settings.__dataclass_fields__}
    if args.json:
        _emit(payload)
        return 0
    print(f"\nSettings · {data_dir() / 'config.json'}")
    _rule(60)
    for key, value in payload.items():
        print(f"  {key:<28}{value}")
    _rule(60)
    print()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .server import serve

    ctx = Context(args)
    try:
        serve(
            engine=ctx.engine,
            provider=ctx.provider,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return 0
    finally:
        ctx.close()


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apflow",
        description="Approval workflows and spend control for a Xero ledger.",
        epilog="Runs against the bundled demo org until you connect a real one with `apflow connect`.",
    )
    parser.add_argument("--version", action="version", version=f"apflow {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--snapshot", metavar="DIR", help="read from a snapshot directory instead of Xero")
    common.add_argument("--matrix", metavar="FILE", help="path to the approval matrix JSON")
    common.add_argument("--users", metavar="FILE", help="path to the user directory JSON")
    common.add_argument("--budget", metavar="FILE", help="path to the budget JSON")
    common.add_argument("--db", metavar="FILE", help="path to the local database")
    common.add_argument("--json", action="store_true", help="emit JSON instead of formatted text")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", parents=[common], help="queue overview")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pull", parents=[common], help="fetch documents and route anything new")
    p.add_argument("--kind", choices=[str(k) for k in DocumentKind], help="one document kind only")
    p.add_argument("--status", default="DRAFT",
                   help="ledger status to pull (default DRAFT; use '' for all)")
    p.add_argument("--requested-by", metavar="USER", help="who is submitting (default: system)")
    p.add_argument("--dry-run", action="store_true", help="show the routing without creating requests")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("queue", parents=[common], help="everything awaiting approval")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("inbox", parents=[common], help="what is waiting on one person")
    p.add_argument("--as", dest="user", metavar="USER", required=True, help="the approver")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("show", parents=[common], help="full detail and audit trail for one request")
    p.add_argument("reference", help="AR-0007, a document number, or a document id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", parents=[common], help="approve a request")
    p.add_argument("reference")
    p.add_argument("--as", dest="user", metavar="USER", required=True)
    p.add_argument("--comment", default="", help="optional note, kept in the audit trail")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", parents=[common], help="reject a request (a reason is required)")
    p.add_argument("reference")
    p.add_argument("--as", dest="user", metavar="USER", required=True)
    p.add_argument("--comment", required=True, help="why — the requester needs to know what to fix")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("cancel", parents=[common], help="withdraw a request")
    p.add_argument("reference")
    p.add_argument("--as", dest="user", metavar="USER", required=True)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("matrix", parents=[common], help="show, validate, or initialise the matrix")
    p.add_argument("--init", action="store_true", help="write a starter matrix")
    p.add_argument("--force", action="store_true", help="overwrite an existing matrix")
    p.add_argument("--check", action="store_true", help="validate only (the default output anyway)")
    p.set_defaults(func=cmd_matrix)

    p = sub.add_parser("users", parents=[common], help="the approver directory")
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("budget", parents=[common], help="budget lines, commitments, and headroom")
    p.add_argument("--period", help="one period only, e.g. 2026-09")
    p.set_defaults(func=cmd_budget)

    p = sub.add_parser("push", parents=[common],
                       help="post approved documents to the ledger (this changes it)")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent")
    p.add_argument("--limit", type=int, help="post at most this many")
    p.add_argument("--allow-writes", action="store_true",
                   help="use the connection's write scopes for this run")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("remind", parents=[common], help="chase overdue approvals")
    p.add_argument("--send", action="store_true", help="deliver over SMTP instead of the outbox")
    p.set_defaults(func=cmd_remind)

    p = sub.add_parser("audit", parents=[common], help="workflow events and ledger writes")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("report", parents=[common], help="turnaround or spend analysis")
    p.add_argument("what", choices=["turnaround", "spend"])
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("connect", help="authorise this machine against a Xero organisation")
    p.add_argument("--allow-writes", action="store_true", help="also request write scopes")
    p.add_argument("--port", type=int, default=8721, help="local port for the OAuth redirect")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("config", parents=[common], help="show or change settings")
    p.add_argument("--set", action="append", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("ui", parents=[common], help="open the approver dashboard in a browser")
    p.add_argument("--port", type=int, default=None, help="port to listen on (default: pick a free one)")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    p.set_defaults(func=cmd_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (WorkflowError, MatrixError, PostingError, KeyError) as exc:
        message = exc.args[0] if exc.args else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # Piping into `head` closes the stream early. Exiting quietly is the
        # expected behaviour for a CLI; a traceback here is just noise.
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
