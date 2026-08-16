#!/usr/bin/env python3
"""Regenerate the synthetic demo snapshot.

Every name, amount, and identifier here is invented. The data is shaped to
exercise each close check at least once, so `xerobk close --snapshot data/demo`
produces an interesting report rather than an empty one.

Deliberately planted issues:

    * duplicate contact      Harbourline Constructions / ... Pty Ltd
    * duplicate invoices     DEMO-1042 and DEMO-1043, same contact and amount
    * debtor concentration   Harbourline holds most of the debtor book
    * aged debt              several invoices well past 60 days
    * draft invoice          DEMO-1055, unapproved, inside the period
    * GST mismatch           DEMO-1048 records tax that does not recompute
    * missing email          Verity Projects has no address on file
    * missing due date       DEMO-1031 has none

Run with: python data/demo/generate.py
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

OUT = Path(__file__).parent
AS_OF = date(2026, 8, 16)
CENTS = Decimal("0.01")


def q(value: Decimal) -> str:
    return str(value.quantize(CENTS, rounding=ROUND_HALF_UP))


def inclusive(gross: str) -> tuple[str, str, str]:
    """(gross, net, gst) for a GST-inclusive amount at 10%."""
    total = Decimal(gross)
    gst = (total / Decimal("11")).quantize(CENTS, rounding=ROUND_HALF_UP)
    return q(total), q(total - gst), q(gst)


CONTACTS = [
    ("c1000000-0000-4000-8000-000000000001", "Harbourline Constructions Pty Ltd", "accounts@harbourline.example"),
    # Same entity, entered twice — the duplicate-contact check should catch this.
    ("c1000000-0000-4000-8000-000000000002", "Harbourline Constructions", None),
    ("c1000000-0000-4000-8000-000000000003", "Kestrel Build Group Pty Ltd", "ap@kestrelbuild.example"),
    ("c1000000-0000-4000-8000-000000000004", "Verity Projects Pty Ltd", None),
    ("c1000000-0000-4000-8000-000000000005", "Ashgrove Property Trust", "finance@ashgrove.example"),
    ("c2000000-0000-4000-8000-000000000001", "Meridian Surveying Pty Ltd", "billing@meridiansurvey.example"),
    ("c2000000-0000-4000-8000-000000000002", "Coastal Concrete Supplies", "accounts@coastalconcrete.example"),
]

# (number, contact index, reference, issued, due, gross, status)
SALES = [
    ("DEMO-1012", 0, "18 Pelican Rise (Base Stage)", "2025-12-04", "2025-12-18", "148500.00", "AUTHORISED"),
    ("DEMO-1019", 0, "18 Pelican Rise (Frame)", "2026-02-12", "2026-02-26", "121000.00", "AUTHORISED"),
    ("DEMO-1024", 2, "7 Wattle Court (Base Stage)", "2026-03-19", "2026-04-02", "88000.00", "AUTHORISED"),
    ("DEMO-1031", 3, "Feasibility & concept design", "2026-04-08", None, "23650.00", "AUTHORISED"),
    ("DEMO-1036", 0, "18 Pelican Rise (Lockup)", "2026-05-14", "2026-05-28", "165000.00", "AUTHORISED"),
    ("DEMO-1042", 4, "Kennaway Rd subdivision — stage 1", "2026-06-11", "2026-06-25", "44000.00", "AUTHORISED"),
    ("DEMO-1043", 4, "Kennaway Rd subdivision — stage 1", "2026-06-16", "2026-06-30", "44000.00", "AUTHORISED"),
    ("DEMO-1048", 2, "7 Wattle Court (Frame)", "2026-07-09", "2026-07-23", "96800.00", "AUTHORISED"),
    ("DEMO-1051", 0, "22 Ferngully Way (Base Stage)", "2026-07-28", "2026-08-11", "132000.00", "AUTHORISED"),
    ("DEMO-1053", 4, "Kennaway Rd subdivision — stage 2", "2026-08-06", "2026-08-20", "57200.00", "AUTHORISED"),
    ("DEMO-1054", 3, "Site establishment", "2026-08-12", "2026-08-26", "18150.00", "AUTHORISED"),
    ("DEMO-1055", 2, "7 Wattle Court (Lockup)", "2026-08-14", "2026-08-28", "104500.00", "DRAFT"),
    ("DEMO-1009", 4, "Ashgrove staging fee", "2025-10-02", "2025-10-16", "26400.00", "PAID"),
    ("DEMO-1017", 2, "Preliminary works", "2026-01-22", "2026-02-05", "35200.00", "PAID"),
]

BILLS = [
    ("BILL-4471", 5, "Detail survey — Pelican Rise", "2026-06-30", "2026-07-30", "9350.00", "AUTHORISED"),
    ("BILL-4488", 6, "Concrete supply — Wattle Court", "2026-07-14", "2026-08-13", "27500.00", "AUTHORISED"),
    ("BILL-4501", 5, "Contour & feature survey", "2026-08-03", "2026-09-02", "5060.00", "AUTHORISED"),
    ("BILL-4460", 6, "Concrete supply — Pelican Rise", "2026-05-28", "2026-06-27", "31900.00", "PAID"),
]


def build_invoice(row, index, kind, force_tax=None):
    number, contact_index, reference, issued, due, gross, status = row
    contact_id, name, email = CONTACTS[contact_index]
    total, net, gst = inclusive(gross)
    paid = total if status == "PAID" else "0.00"
    amount_due = "0.00" if status == "PAID" else total

    return {
        "invoice_id": f"{'a' if kind == 'ACCREC' else 'b'}0000000-0000-4000-8000-{index:012d}",
        "invoice_number": number,
        "reference": reference,
        "status": status,
        "type": kind,
        "contact": {"contact_id": contact_id, "name": name, "email": email},
        "amount_total": total,
        "amount_due": amount_due,
        "amount_paid": paid,
        "amount_credited": "0.00",
        "amount_net": net,
        # force_tax plants a deliberate GST inconsistency on one invoice.
        "amount_tax": force_tax if force_tax is not None else gst,
        "currency_code": "AUD",
        "currency_rate": "1.0",
        "invoice_date": issued,
        "due_date": due,
        "line_items": [
            {
                "description": reference,
                "quantity": "1.0",
                "unit_amount": total,
                "line_amount_gross": total,
                "line_tax_amount": gst,
                "line_tax_treatment": "Inclusive",
            }
        ],
    }


def main() -> None:
    sales = [
        build_invoice(row, i, "ACCREC", force_tax="0.00" if row[0] == "DEMO-1048" else None)
        for i, row in enumerate(SALES, start=1)
    ]
    bills = [build_invoice(row, i, "ACCPAY") for i, row in enumerate(BILLS, start=1)]

    (OUT / "invoices.json").write_text(json.dumps({"invoices": sales}, indent=2) + "\n")
    (OUT / "bills.json").write_text(json.dumps({"invoices": bills}, indent=2) + "\n")

    (OUT / "contacts.json").write_text(
        json.dumps(
            {"contacts": [
                {"contact_id": cid, "name": name, "email": email,
                 "is_customer": index < 5, "is_supplier": index >= 5}
                for index, (cid, name, email) in enumerate(CONTACTS)
            ]},
            indent=2,
        ) + "\n"
    )

    (OUT / "organisation.json").write_text(
        json.dumps(
            {
                "name": "Wattlebird Developments Unit Trust",
                "legal_name": "Wattlebird Developments Pty Ltd",
                "organisation_type": "TRUST",
                "region": "AU",
                "country": "AU",
                "base_currency": "AUD",
                "timezone": {"tz_name": "Australia/Sydney", "utc_offset": "+10"},
                "line_of_business": "Residential Property Operators",
                "company_registration_number": "00000000000",
                "financial_year_start_date": "2026-07-01",
                "financial_year_end_date": "2027-06-30",
            },
            indent=2,
        ) + "\n"
    )

    outstanding = sum(
        Decimal(inv["amount_due"]) for inv in sales if inv["status"] == "AUTHORISED"
    )
    payable = sum(
        Decimal(inv["amount_due"]) for inv in bills if inv["status"] == "AUTHORISED"
    )

    (OUT / "cash_position.json").write_text(
        json.dumps(
            {
                "organisation_base_currency": "AUD",
                "snapshot_date": AS_OF.isoformat(),
                "cash_balance": "84210.55",
                "amount_owed": q(outstanding),
                "amount_due": q(payable),
            },
            indent=2,
        ) + "\n"
    )

    # Leave the aged reports to be computed from the invoices themselves, so the
    # demo also exercises the aging code rather than reading a canned answer.
    for filename in ("aged_receivables.json", "aged_payables.json"):
        (OUT / filename).write_text(
            json.dumps({"as_of_date": AS_OF.isoformat(), "organisation_base_currency": "AUD",
                        "age_buckets": {}, "aged_receivables": []}, indent=2) + "\n"
        )

    print(f"demo snapshot written to {OUT}")
    print(f"  {len(sales)} sales invoices, {len(bills)} bills, {len(CONTACTS)} contacts")
    print(f"  receivables {q(outstanding)}  payables {q(payable)}")


if __name__ == "__main__":
    main()
