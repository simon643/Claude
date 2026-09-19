"""Regenerate the synthetic demo organisation.

The data is invented and deliberately seeded with the situations the workflow
is supposed to handle: a bill below the auto-approval band, a foreign-currency
bill whose base-currency value crosses a limit its face value does not, a
travel bill that breaches its budget line, a contractor account sitting at 92%
committed, and a supplier whose email matches one of the approvers.

    python data/demo/generate.py
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
TODAY = "2026-09-19"


def gst(total: str) -> str:
    """The GST inside a GST-inclusive amount, at the Australian 10% rate."""
    return str((Decimal(total) / Decimal("11")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def line(description: str, total: str, account: str, tracking: dict[str, str] | None = None) -> dict:
    return {
        "description": description,
        "quantity": "1.0000",
        "unit_amount": total,
        "line_amount_gross": total,
        "line_tax_amount": gst(total),
        "account_code": account,
        "tracking": tracking or {},
    }


def document(
    doc_id: str,
    number: str,
    contact: tuple[str, str],
    total: str,
    account: str,
    dated: str,
    due: str,
    kind: str = "ACCPAY",
    currency: str = "AUD",
    rate: str = "1.00",
    reference: str = "",
    description: str = "",
    tracking: dict[str, str] | None = None,
) -> dict:
    name, email = contact
    return {
        "invoice_id": doc_id,
        "invoice_number": number,
        "type": kind,
        "status": "DRAFT",
        "contact": {"contact_id": f"c-{name.lower().replace(' ', '-')}", "name": name,
                    "email": email, "is_supplier": kind == "ACCPAY", "is_customer": kind == "ACCREC"},
        "invoice_date": dated,
        "due_date": due,
        "amount_total": total,
        "amount_tax": gst(total),
        "amount_net": str(Decimal(total) - Decimal(gst(total))),
        "amount_due": total,
        "currency_code": currency,
        "currency_rate": rate,
        "reference": reference,
        "line_items": [line(description or number, total, account, tracking)],
    }


SUPPLIERS = {
    "officeworks": ("Officeworks", "ap@officeworks.example"),
    "northwind": ("Northwind Media", "billing@northwind.example"),
    "harbourline": ("Harbourline Consulting", "accounts@harbourline.example"),
    "cloudstack": ("Cloudstack Pty Ltd", "billing@cloudstack.example"),
    "ferngully": ("Ferngully Travel", "invoices@ferngully.example"),
    "meridian": ("Meridian Legal LLP", "ar@meridianlegal.example"),
    "apex": ("Apex Facilities", "accounts@apexfacilities.example"),
    # Deliberate: this supplier's email is also an approver's. The engine must
    # refuse to let Ben approve spend going to his own address.
    "lightfoot": ("Lightfoot Design", "ben.attwood@harbourview.example"),
}

CUSTOMERS = {
    "brightside": ("Brightside Group", "ap@brightside.example"),
    "cove": ("Cove & Co", "finance@coveandco.example"),
}


def build() -> dict[str, dict]:
    bills = [
        document("bill-2201", "B-2201", SUPPLIERS["officeworks"], "189.20", "461",
                 "2026-09-02", "2026-09-30", description="Stationery and printer toner"),
        document("bill-2202", "B-2202", SUPPLIERS["northwind"], "4620.00", "420",
                 "2026-09-04", "2026-10-04", description="Spring campaign — media buy",
                 tracking={"Department": "Marketing"}),
        document("bill-2203", "B-2203", SUPPLIERS["harbourline"], "38500.00", "300",
                 "2026-09-05", "2026-10-05", reference="SOW-14",
                 description="Platform migration — September"),
        document("bill-2204", "B-2204", SUPPLIERS["cloudstack"], "1320.00", "433",
                 "2026-09-08", "2026-09-22", description="Hosting and backups"),
        document("bill-2205", "B-2205", SUPPLIERS["ferngully"], "6930.00", "429",
                 "2026-09-09", "2026-10-09", description="Q3 conference travel — 4 staff"),
        # USD 9,000 is under a AUD 10,000 limit at face value and over it at the
        # booked rate. Limits are checked in base currency for this reason.
        document("bill-2206", "B-2206", SUPPLIERS["meridian"], "9000.00", "462",
                 "2026-09-11", "2026-10-11", currency="USD", rate="1.52",
                 description="Contract review — US subsidiary"),
        document("bill-2207", "B-2207", SUPPLIERS["northwind"], "132.00", "420",
                 "2026-09-12", "2026-10-12", description="Stock imagery licence"),
        document("bill-2208", "B-2208", SUPPLIERS["apex"], "2750.00", "461",
                 "2026-09-15", "2026-10-15", description="Quarterly building services"),
        document("bill-2209", "B-2209", SUPPLIERS["lightfoot"], "3300.00", "420",
                 "2026-09-16", "2026-10-16", description="Brand refresh — design retainer",
                 tracking={"Department": "Marketing"}),
    ]

    invoices = [
        document("inv-4101", "INV-4101", CUSTOMERS["brightside"], "17600.00", "200",
                 "2026-09-10", "2026-10-10", kind="ACCREC",
                 description="Implementation — milestone 2"),
        document("inv-4102", "INV-4102", CUSTOMERS["cove"], "3410.00", "200",
                 "2026-09-14", "2026-10-14", kind="ACCREC", description="Support retainer"),
    ]

    purchase_orders = [
        {
            **document("po-0031", "PO-0031", SUPPLIERS["harbourline"], "22000.00", "300",
                       "2026-09-17", "2026-10-17", description="Phase 2 statement of work"),
            "purchase_order_id": "po-0031",
            "purchase_order_number": "PO-0031",
        }
    ]

    accounts = [
        {"code": "200", "name": "Sales", "type": "REVENUE", "tax_type": "OUTPUT"},
        {"code": "300", "name": "Contractors", "type": "DIRECTCOSTS", "tax_type": "INPUT"},
        {"code": "420", "name": "Advertising & marketing", "type": "EXPENSE", "tax_type": "INPUT"},
        {"code": "429", "name": "Travel", "type": "EXPENSE", "tax_type": "INPUT"},
        {"code": "433", "name": "IT & software", "type": "EXPENSE", "tax_type": "INPUT"},
        {"code": "461", "name": "Office expenses", "type": "EXPENSE", "tax_type": "INPUT"},
        {"code": "462", "name": "Legal & professional", "type": "EXPENSE", "tax_type": "INPUT"},
        {"code": "090", "name": "Business account", "type": "BANK", "tax_type": "NONE"},
    ]

    contacts = [
        {"contact_id": f"c-{name.lower().replace(' ', '-')}", "name": name, "email": email,
         "is_supplier": True, "is_customer": False}
        for name, email in SUPPLIERS.values()
    ] + [
        {"contact_id": f"c-{name.lower().replace(' ', '-')}", "name": name, "email": email,
         "is_supplier": False, "is_customer": True}
        for name, email in CUSTOMERS.values()
    ]

    users = {
        "users": [
            {"id": "priya", "name": "Priya Raman", "email": "priya@harbourview.example",
             "role": "Accounts payable", "approval_limit": "2500.00", "escalates_to": "jo"},
            {"id": "dana", "name": "Dana Whitfield", "email": "dana@harbourview.example",
             "role": "Operations manager", "approval_limit": "25000.00", "escalates_to": "cfo"},
            {"id": "ben", "name": "Ben Attwood", "email": "ben.attwood@harbourview.example",
             "role": "Marketing lead", "approval_limit": "10000.00", "escalates_to": "cfo"},
            # Sam is on leave; everything routed to Sam goes to Jo, and the
            # substitution is recorded on the decision rather than hidden.
            {"id": "sam", "name": "Sam Okafor", "email": "sam@harbourview.example",
             "role": "Financial controller", "approval_limit": "50000.00",
             "away_until": "2026-09-26", "delegate_to": "jo", "escalates_to": "cfo"},
            {"id": "jo", "name": "Jo Lindqvist", "email": "jo@harbourview.example",
             "role": "Finance manager", "approval_limit": "50000.00", "escalates_to": "cfo"},
            {"id": "cfo", "name": "Alex Mercer", "email": "alex@harbourview.example",
             "role": "CFO", "approval_limit": None},
        ]
    }

    # Every band is bounded by the authority of the people staffing it. An
    # unbounded rule needs someone with unlimited authority on EVERY step, or
    # the workflow stalls the first time a document exceeds the step-1
    # approver's limit — which is what `apflow matrix --check` exists to catch.
    matrix = {
        "version": 1,
        "rules": [
            {
                "name": "Low-value bills — auto",
                "when": {"kinds": ["bill"], "amount_max": "250.00"},
                "auto_approve": True,
                "note": "Below the cost of reviewing it. Still logged and still audited.",
                "steps": [],
            },
            {
                "name": "Marketing spend",
                "when": {"kinds": ["bill", "purchase_order"], "accounts": ["420"],
                         "amount_max": "10000.00"},
                "steps": [
                    {"name": "Marketing lead", "approvers": ["ben"], "sla_days": 2},
                    {"name": "Finance", "approvers": ["jo", "sam"], "quorum": "any",
                     "when": {"amount_min": "2500.00"}, "sla_days": 2},
                ],
            },
            {
                "name": "Supplier bills — standard",
                "when": {"kinds": ["bill", "purchase_order"], "amount_max": "25000.00"},
                "steps": [
                    {"name": "Budget holder", "approvers": ["dana"], "sla_days": 2},
                    {"name": "Finance", "approvers": ["sam"], "when": {"amount_min": "2500.00"},
                     "sla_days": 2},
                ],
            },
            {
                "name": "Supplier bills — major",
                "when": {"kinds": ["bill", "purchase_order"]},
                "note": "No ceiling, so the CFO stands behind every step.",
                "steps": [
                    {"name": "Budget holder", "approvers": ["dana", "cfo"], "quorum": "any",
                     "sla_days": 2},
                    {"name": "Finance", "approvers": ["sam", "cfo"], "quorum": "any",
                     "sla_days": 2},
                    {"name": "CFO", "approvers": ["cfo"], "sla_days": 3},
                ],
            },
            {
                "name": "Customer invoices",
                "when": {"kinds": ["invoice"], "amount_max": "15000.00"},
                "steps": [{"name": "Finance review", "approvers": ["jo"], "sla_days": 1}],
            },
            {
                "name": "Customer invoices — major",
                "when": {"kinds": ["invoice"]},
                "steps": [
                    {"name": "Finance review", "approvers": ["jo", "cfo"], "quorum": "any",
                     "sla_days": 1},
                    {"name": "CFO", "approvers": ["cfo"], "sla_days": 2},
                ],
            },
            {
                "name": "Everything else",
                "note": "Catch-all. Nothing reaches the ledger unrouted.",
                "steps": [{"name": "Finance review", "approvers": ["jo", "cfo"], "quorum": "any",
                           "sla_days": 2}],
            },
        ],
    }

    budget = {
        "version": 1,
        "granularity": "monthly",
        "fy_start_month": 7,
        "lines": [
            {"period": "2026-09", "account": "420", "amount": "12000.00", "name": "Marketing"},
            {"period": "2026-09", "account": "429", "amount": "4000.00", "name": "Travel"},
            {"period": "2026-09", "account": "433", "amount": "8000.00", "name": "IT & software"},
            {"period": "2026-09", "account": "300", "amount": "60000.00", "name": "Contractors"},
            {"period": "2026-09", "account": "461", "amount": "5000.00", "name": "Office"},
            {"period": "2026-10", "account": "420", "amount": "12000.00", "name": "Marketing"},
            {"period": "2026-10", "account": "300", "amount": "60000.00", "name": "Contractors"},
        ],
    }

    return {
        "organisation.json": {
            "name": "Harbourview Systems Pty Ltd",
            "legal_name": "Harbourview Systems Pty Ltd",
            "country": "AU",
            "base_currency": "AUD",
            "timezone": {"tz_name": "Australia/Sydney"},
            "as_of": TODAY,
        },
        "contacts.json": {"contacts": contacts},
        "accounts.json": {"accounts": accounts},
        "bills.json": {"invoices": bills},
        "invoices.json": {"invoices": invoices},
        "purchase_orders.json": {"purchase_orders": purchase_orders},
        "users.json": users,
        "matrix.json": matrix,
        "budget.json": budget,
    }


def main() -> None:
    for filename, payload in build().items():
        target = HERE / filename
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {target.relative_to(HERE.parents[1])}")


if __name__ == "__main__":
    main()
