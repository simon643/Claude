"""Bank statement CSV import.

Australian banks each export a different shape, and none of them agree on
column names, column order, sign conventions, or whether there is a header row
at all. This module sniffs the format rather than requiring the user to
pre-massage the file.

Handled variations:

* Header row present, absent, or preceded by junk lines.
* A single signed ``Amount`` column, or separate ``Debit``/``Credit`` columns.
* ``DD/MM/YYYY`` (the Australian default), ``YYYY-MM-DD``, and ``DD-Mon-YYYY``.
* Amounts written as ``$1,234.56``, ``(1234.56)``, or ``1234.56 CR``.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ..models import BankLine
from ..money import ZERO, to_money

# Candidate header names, lowercased, in priority order.
_DATE_KEYS = ("date", "transaction date", "processed date", "value date", "*date")
_AMOUNT_KEYS = ("amount", "transaction amount", "*amount", "value")
_DEBIT_KEYS = ("debit", "debit amount", "withdrawal", "withdrawals", "money out", "paid out")
_CREDIT_KEYS = ("credit", "credit amount", "deposit", "deposits", "money in", "paid in")
_DESC_KEYS = (
    "description",
    "narrative",
    "details",
    "transaction details",
    "particulars",
    "payee",
    "merchant",
    "*description",
)
_REF_KEYS = ("reference", "ref", "code", "transaction id", "cheque number", "*reference")
_BALANCE_KEYS = ("balance", "running balance", "closing balance")

# AU is day-first. Trying month-first patterns before day-first would silently
# mis-date every transaction on the 1st through the 12th of each month.
_DATE_FORMATS = (
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%d %B %Y",
    "%Y/%m/%d",
)


def parse_bank_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _find(
    header: Sequence[str], keys: Iterable[str], exclude: Iterable[int | None] = ()
) -> int | None:
    """Index of the first column matching any key, exact match before prefix.

    ``exclude`` skips columns already claimed by another field. This matters
    for the "Debit Amount"/"Credit Amount" layout: a loose search for "amount"
    happily matches "Debit Amount", which would then be read as a signed total
    and turn every withdrawal into a deposit.
    """
    skip = {idx for idx in exclude if idx is not None}
    lowered = [h.strip().lower().lstrip("*") for h in header]
    for key in keys:
        target = key.lstrip("*")
        for idx, name in enumerate(lowered):
            if idx not in skip and name == target:
                return idx
    for key in keys:
        target = key.lstrip("*")
        for idx, name in enumerate(lowered):
            if idx not in skip and (name.startswith(target) or target in name):
                return idx
    return None


def _looks_like_header(row: Sequence[str]) -> bool:
    """A header row has no parseable date and no parseable amount."""
    if not row:
        return False
    joined = " ".join(row).lower()
    if any(key in joined for key in ("date", "amount", "description", "narrative", "debit")):
        # Confirm it is not a data row that merely mentions one of these words.
        return parse_bank_date(row[0]) is None
    return False


def _cell(row: Sequence[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def read_bank_csv(path: str | Path, account_id: str = "") -> list[BankLine]:
    """Parse a bank statement CSV into :class:`BankLine` records."""
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return parse_bank_csv(raw, account_id=account_id)


def parse_bank_csv(text: str, account_id: str = "") -> list[BankLine]:
    """Parse bank statement CSV content. See module docstring for supported shapes."""
    if not text.strip():
        return []

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)]
    if not rows:
        return []

    # Skip preamble junk (account name, export date) that sits above the header.
    start = 0
    header: list[str] | None = None
    for idx, row in enumerate(rows[:10]):
        if _looks_like_header(row):
            header = [c.strip() for c in row]
            start = idx + 1
            break

    if header is not None:
        date_idx = _find(header, _DATE_KEYS)
        balance_idx = _find(header, _BALANCE_KEYS)
        # Resolve debit/credit first so the amount search cannot steal one of
        # them, then keep the amount search away from every claimed column.
        debit_idx = _find(header, _DEBIT_KEYS, exclude=(date_idx, balance_idx))
        credit_idx = _find(header, _CREDIT_KEYS, exclude=(date_idx, balance_idx, debit_idx))
        amount_idx = _find(
            header, _AMOUNT_KEYS, exclude=(date_idx, balance_idx, debit_idx, credit_idx)
        )
        desc_idx = _find(header, _DESC_KEYS, exclude=(date_idx, balance_idx, amount_idx))
        ref_idx = _find(
            header, _REF_KEYS, exclude=(date_idx, balance_idx, amount_idx, desc_idx)
        )
    else:
        # Headerless: infer by position. The near-universal AU layout is
        # date, amount, description[, balance].
        date_idx, amount_idx, desc_idx = 0, 1, 2
        debit_idx = credit_idx = ref_idx = None
        balance_idx = 3 if len(rows[0]) > 3 else None

    # A file with a Debit/Credit pair and no signed Amount column needs the two
    # combined; debits are money out and must come through negative.
    use_split = amount_idx is None and (debit_idx is not None or credit_idx is not None)
    if date_idx is None:
        date_idx = 0
    if not use_split and amount_idx is None:
        amount_idx = 1

    lines: list[BankLine] = []
    for position, row in enumerate(rows[start:]):
        when = parse_bank_date(_cell(row, date_idx))
        if when is None:
            # Trailing totals row, or a wrapped line; skip rather than fail the
            # whole import over one bad row.
            continue

        if use_split:
            debit = to_money(_cell(row, debit_idx)) if debit_idx is not None else ZERO
            credit = to_money(_cell(row, credit_idx)) if credit_idx is not None else ZERO
            # Some exports write debits already-negative, others positive.
            amount = credit - abs(debit) if debit else credit
        else:
            amount = to_money(_cell(row, amount_idx))

        description = _cell(row, desc_idx)
        if not description and desc_idx is not None:
            # Fall back to the widest remaining text cell.
            candidates = [
                c.strip()
                for i, c in enumerate(row)
                if i not in {date_idx, amount_idx, debit_idx, credit_idx, balance_idx} and c.strip()
            ]
            description = max(candidates, key=len) if candidates else ""

        balance: Decimal | None = None
        if balance_idx is not None:
            cell = _cell(row, balance_idx)
            balance = to_money(cell) if cell else None

        reference = _cell(row, ref_idx)

        # Stable synthetic id: the same statement re-imported yields the same
        # ids, so previously-coded lines can be recognised instead of duplicated.
        digest = hashlib.sha256(
            f"{when.isoformat()}|{amount}|{description}|{reference}|{position}".encode()
        ).hexdigest()[:16]

        lines.append(
            BankLine(
                line_id=digest,
                date=when,
                description=description,
                amount=amount,
                balance=balance,
                reference=reference,
                account_id=account_id,
            )
        )

    return lines
