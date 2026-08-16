"""Command line interface.

Uses ``argparse`` rather than a CLI framework to keep the package free of
runtime dependencies. Every command accepts ``--json`` so the tool can be
driven from a script or a scheduled job as easily as from a terminal.
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
from .close import CRITICAL, INFO, WARNING, build_context, run_close
from .config import Settings, data_dir
from .money import fmt
from .providers import ProviderError, XeroProvider
from .providers.bankcsv import read_bank_csv
from .providers.factory import get_provider
from .reconcile import suggest_all, summarise
from .reports import aged_detail, build_dashboard
from .rules import RuleSet, learn_rules
from .store import Store

_SEVERITY_MARK = {CRITICAL: "!!", WARNING: " !", INFO: "  "}


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))


def _rule(width: int = 72) -> None:
    print("-" * width)


def _provider(args: argparse.Namespace) -> XeroProvider:
    return get_provider(snapshot=getattr(args, "snapshot", None))


def _rules_path(args: argparse.Namespace) -> Path:
    override = getattr(args, "rules", None)
    return Path(override) if override else data_dir() / "rules.json"


def _load_ruleset(args: argparse.Namespace, store: Store | None = None) -> RuleSet:
    path = _rules_path(args)
    ruleset = RuleSet.load(path) if path.exists() else RuleSet.defaults()
    if store is not None:
        ruleset.rules.extend(learn_rules(store.coding_history()))
    return ruleset


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    provider = _provider(args)
    dashboard = build_dashboard(provider)

    if args.json:
        _emit(dashboard.to_dict(), True)
        return 0

    org = dashboard.organisation
    currency = dashboard.currency
    print(f"\n{org.name}  ({org.country} · {currency})")
    print(f"source: {provider.name} · {'read/write' if provider.can_write else 'read-only'}")
    print(f"as at:  {dashboard.as_of}")
    _rule()
    print(f"  Cash                {fmt(dashboard.cash_balance, currency):>16}")
    print(f"  Owed to you         {fmt(dashboard.receivables, currency):>16}")
    print(f"  You owe             {fmt(dashboard.payables, currency):>16}")
    print(f"  Net position        {fmt(dashboard.net_position, currency):>16}")
    _rule()
    print(f"  Overdue             {fmt(dashboard.overdue_total, currency):>16}  ({dashboard.overdue_share}%)")
    print(f"  Drafts pending      {dashboard.draft_count:>16}")

    if dashboard.top_debtors:
        print("\nTop debtors")
        for debtor in dashboard.top_debtors[:5]:
            age = f"{debtor.oldest_days}d" if debtor.oldest_days > 0 else "current"
            print(
                f"  {debtor.name[:34]:<34} {fmt(debtor.total, currency):>14}"
                f"  {debtor.share:>5}%  oldest {age}"
            )
    print()
    return 0


def cmd_aged(args: argparse.Namespace) -> int:
    provider = _provider(args)
    detail = aged_detail(provider, payables=args.payables)

    if args.json:
        _emit(detail, True)
        return 0

    label = "Payables" if args.payables else "Receivables"
    currency = detail["currency"]
    print(f"\n{label} as at {detail['as_of']}")
    _rule(96)
    print(f"  {'Invoice':<14}{'Customer':<30}{'Due':<12}{'Bucket':<12}{'Amount':>14}")
    _rule(96)
    for row in detail["rows"]:
        contact = (row.get("contact") or {}).get("name") or "—"
        print(
            f"  {str(row.get('document_number') or '—')[:13]:<14}"
            f"{contact[:29]:<30}"
            f"{row.get('due_date') or '—'!s:<12}"
            f"{row.get('bucket_label') or '—'!s:<12}"
            f"{fmt(Decimal(str(row.get('total') or '0')), currency):>14}"
        )
    _rule(96)
    print(f"  {'TOTAL':<56}{'':<12}{fmt(Decimal(detail['total']), currency):>14}")
    print(f"  {'overdue':<56}{'':<12}{fmt(Decimal(detail['overdue']), currency):>14}\n")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    provider = _provider(args)
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"error: no such file: {csv_path}", file=sys.stderr)
        return 2

    lines = read_bank_csv(csv_path)
    if not lines:
        print(f"error: no transactions parsed from {csv_path}", file=sys.stderr)
        return 2

    with Store() as store:
        ruleset = _load_ruleset(args, store)
        suggestions = suggest_all(lines, provider.invoices(), ruleset)
        stats = summarise(suggestions)

        if args.apply:
            for suggestion in suggestions:
                if suggestion.is_confident:
                    store.record_coding(
                        suggestion.line,
                        account_code=suggestion.account_code,
                        tax_type=suggestion.tax_type,
                        contact_name=suggestion.contact_name,
                        invoice_ids=[i.invoice_id for i in suggestion.invoices],
                        decided_by="auto",
                        confidence=suggestion.confidence,
                    )

        if args.json:
            _emit(
                {
                    "stats": {
                        "total": stats.total,
                        "confident": stats.confident,
                        "review": stats.review,
                        "unknown": stats.unknown,
                        "auto_rate": stats.auto_rate,
                        "matched_value": str(stats.matched_value),
                    },
                    "applied": bool(args.apply),
                    "suggestions": [s.to_dict() for s in suggestions],
                },
                True,
            )
            return 0

        currency = provider.organisation().base_currency
        print(f"\n{stats.total} bank line(s) from {csv_path.name}")
        print(
            f"  {stats.confident} confident ({stats.auto_rate}%) · "
            f"{stats.review} to review · {stats.unknown} with no suggestion"
        )
        print(f"  matched value: {fmt(stats.matched_value, currency)}")
        _rule(96)
        for suggestion in suggestions:
            line = suggestion.line
            when = line.date.isoformat() if line.date else "—"
            marker = "OK" if suggestion.is_confident else ("??" if suggestion.ambiguous else "  ")
            print(
                f"{marker} {when}  {line.description[:38]:<38} "
                f"{fmt(line.amount, currency):>13}  {suggestion.confidence:>3}%  {suggestion.summary()}"
            )
            if args.verbose and suggestion.reasons:
                for reason in suggestion.reasons:
                    print(f"        · {reason}")
        _rule(96)
        if args.apply:
            print(f"recorded {stats.confident} confident coding(s) locally\n")
        else:
            print("nothing was written — re-run with --apply to record the confident ones\n")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    provider = _provider(args)
    settings = Settings.load()

    bank_lines = None
    if args.bank_csv:
        bank_lines = read_bank_csv(args.bank_csv)

    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    ctx = build_context(
        provider,
        as_of=as_of,
        frequency=args.frequency or settings.bas_frequency,
        gst_registered=settings.gst_registered,
        escalation_days=settings.aged_debt_escalation_days,
        large_threshold=settings.large_transaction_threshold,
        suspense_codes=tuple(settings.suspense_account_codes),
        bank_lines=bank_lines,
    )
    report = run_close(ctx)
    payload = report.to_dict()

    with Store() as store:
        store.record_close(payload)

    if args.json:
        _emit(payload, True)
        return 1 if report.critical else 0

    period = report.period
    print(f"\nClose checklist — {period.label}  ({period.start} → {period.end})")
    print(f"{report.organisation.name} · as at {report.as_of}")
    if period.due:
        print(f"earliest standard lodgement due {period.due:%d %b %Y}")
    _rule()
    print(
        f"{len(report.critical)} blocker(s) · {len(report.warnings)} warning(s) · "
        f"{len(report.info)} note(s)"
    )
    _rule()

    for finding in report.findings:
        print(f"\n{_SEVERITY_MARK.get(finding.severity, '  ')} {finding.title}")
        print(f"     {finding.detail}")
        if finding.action:
            print(f"     → {finding.action}")
        if finding.items and args.verbose:
            for item in finding.items:
                print(f"       · {item}")
        elif finding.items:
            for item in finding.items[:3]:
                print(f"       · {item}")
            if len(finding.items) > 3:
                print(f"       · … and {len(finding.items) - 3} more (--verbose to list)")

    print()
    if report.is_clean:
        print("No blockers. Review the warnings, then close the period in Xero.\n")
    else:
        print("Resolve the blockers above before closing the period.\n")
    return 1 if report.critical else 0


def cmd_connect(args: argparse.Namespace) -> int:
    from .providers.oauth import authorize

    settings = Settings.load()
    if args.allow_writes:
        settings.allow_writes = True
        settings.save()

    if not settings.client_id:
        print(
            "XERO_CLIENT_ID is not set.\n\n"
            "  1. Go to https://developer.xero.com/app/manage and create an app\n"
            "  2. Choose the 'Mobile or desktop application' type (PKCE, no secret)\n"
            f"  3. Add this redirect URI: http://localhost:{args.port}/callback\n"
            "  4. export XERO_CLIENT_ID=<your client id>\n\n"
            "Then run `xerobk connect` again.",
            file=sys.stderr,
        )
        return 2

    scopes = settings.scopes()
    print(f"requesting scopes: {' '.join(scopes)}")
    try:
        tokens = authorize(settings.client_id, scopes, port=args.port)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nconnected to {tokens.tenant_name or tokens.tenant_id}")
    print(f"token stored at {data_dir() / 'token.json'} (owner-readable only)")
    print(f"mode: {'read/write' if settings.allow_writes else 'read-only'}\n")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Dump the current ledger to a snapshot directory for offline use."""
    provider = _provider(args)
    target = Path(args.output)
    target.mkdir(parents=True, exist_ok=True)

    from .models import InvoiceType

    def dump(filename: str, payload: Any) -> None:
        (target / filename).write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

    org = provider.organisation()
    dump(
        "organisation.json",
        {
            "name": org.name,
            "legal_name": org.legal_name,
            "organisation_type": org.organisation_type,
            "country": org.country,
            "region": org.country,
            "base_currency": org.base_currency,
            "timezone": {"tz_name": org.timezone},
            "line_of_business": org.line_of_business,
            "company_registration_number": org.registration_number,
            "financial_year_start_date": org.financial_year_start,
            "financial_year_end_date": org.financial_year_end,
        },
    )

    dump("contacts.json", {"contacts": [
        {"contact_id": c.contact_id, "name": c.name, "email": c.email}
        for c in provider.contacts()
    ]})

    for filename, wanted in (("invoices.json", InvoiceType.ACCREC), ("bills.json", InvoiceType.ACCPAY)):
        dump(filename, {"invoices": [
            {
                "invoice_id": i.invoice_id,
                "invoice_number": i.invoice_number,
                "reference": i.reference,
                "status": i.status.value,
                "type": i.invoice_type.value,
                "contact": {"contact_id": i.contact.contact_id, "name": i.contact.name, "email": i.contact.email},
                "amount_total": str(i.amount_total),
                "amount_due": str(i.amount_due),
                "amount_paid": str(i.amount_paid),
                "amount_net": str(i.amount_net),
                "amount_tax": str(i.amount_tax),
                "currency_code": i.currency_code,
                "invoice_date": i.invoice_date,
                "due_date": i.due_date,
                "line_items": [
                    {
                        "description": line.description,
                        "quantity": str(line.quantity),
                        "unit_amount": str(line.unit_amount),
                        "line_amount_gross": str(line.line_amount),
                        "line_tax_amount": str(line.tax_amount),
                        "line_tax_treatment": line.tax_treatment.value,
                    }
                    for line in i.line_items
                ],
            }
            for i in provider.invoices(wanted)
        ]})

    receivables = provider.aged_receivables()
    dump("aged_receivables.json", {
        "as_of_date": receivables.as_of,
        "organisation_base_currency": receivables.currency,
        "age_buckets": {k: str(v) for k, v in receivables.buckets.as_dict().items()},
        "aged_receivables": list(receivables.rows),
    })

    cash = provider.cash_position()
    dump("cash_position.json", {
        "organisation_base_currency": cash.currency,
        "snapshot_date": cash.snapshot_date,
        "cash_balance": str(cash.cash_balance),
        "amount_owed": str(cash.amount_owed),
        "amount_due": str(cash.amount_due),
    })

    print(f"snapshot written to {target}")
    print("this directory contains real financial data — do not commit it")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    path = _rules_path(args)

    if args.init:
        if path.exists() and not args.force:
            print(f"error: {path} already exists (use --force to overwrite)", file=sys.stderr)
            return 2
        RuleSet.defaults().save(path)
        print(f"wrote {len(RuleSet.defaults().rules)} starter rules to {path}")
        print("edit the account codes to match your chart of accounts")
        return 0

    with Store() as store:
        ruleset = _load_ruleset(args, store)

    if args.json:
        _emit({"path": str(path), "rules": [r.to_dict() for r in ruleset.ordered()]}, True)
        return 0

    print(f"\n{len(ruleset.rules)} rule(s) — {path}")
    _rule(88)
    for rule in ruleset.ordered():
        condition = ", ".join(rule.contains) or rule.regex or "(amount only)"
        print(
            f"  {rule.priority:>4}  {rule.name[:30]:<30} {condition[:26]:<26} "
            f"→ {rule.account_code:<6} {rule.source}"
        )
    print()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .server import serve

    provider = _provider(args)
    with Store() as store:
        ruleset = _load_ruleset(args, store)
        serve(
            provider,
            settings=Settings.load(),
            store=store,
            ruleset=ruleset,
            port=args.port,
            open_browser=not args.no_browser,
        )
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xerobk",
        description="Local-first bookkeeping companion for Xero.",
        epilog="Runs against the bundled demo data until you connect a real org with `xerobk connect`.",
    )
    parser.add_argument("--version", action="version", version=f"xerobk {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--snapshot", metavar="DIR", help="read from a snapshot directory instead of Xero")
    common.add_argument("--rules", metavar="FILE", help="path to a rules JSON file")
    common.add_argument("--json", action="store_true", help="emit JSON instead of formatted text")
    common.add_argument("-v", "--verbose", action="store_true", help="show full detail")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", parents=[common], help="headline financial position")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("aged", parents=[common], help="aged receivables or payables detail")
    p.add_argument("--payables", action="store_true", help="show payables instead of receivables")
    p.set_defaults(func=cmd_aged)

    p = sub.add_parser("reconcile", parents=[common], help="suggest coding for a bank statement CSV")
    p.add_argument("csv", help="path to the bank statement CSV")
    p.add_argument("--apply", action="store_true", help="record the confident suggestions locally")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("close", parents=[common], help="run the month-end / BAS close checklist")
    p.add_argument("--frequency", choices=["monthly", "quarterly", "annual"], help="reporting period")
    p.add_argument("--as-of", metavar="YYYY-MM-DD", help="run the checklist as at this date")
    p.add_argument("--bank-csv", metavar="FILE", help="include unreconciled lines from this CSV")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("connect", help="authorise this machine against a Xero organisation")
    p.add_argument("--allow-writes", action="store_true", help="also request write scopes")
    p.add_argument("--port", type=int, default=8720, help="local port for the OAuth redirect")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("snapshot", parents=[common], help="dump the ledger to a local snapshot directory")
    p.add_argument("output", help="directory to write the snapshot into")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("rules", parents=[common], help="inspect or initialise coding rules")
    p.add_argument("--init", action="store_true", help="write the starter rule set")
    p.add_argument("--force", action="store_true", help="overwrite an existing rules file")
    p.set_defaults(func=cmd_rules)

    p = sub.add_parser("ui", parents=[common], help="open the local dashboard in a browser")
    p.add_argument("--port", type=int, default=None, help="port to listen on (default: pick a free one)")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    p.set_defaults(func=cmd_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
