"""Shared fixtures.

Every test gets its own SQLite file under ``tmp_path`` and an ``APFLOW_HOME``
pointing somewhere disposable. Nothing here may touch the real data directory:
a test run that wrote into a user's approval history would be worse than a test
run that failed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apflow.budget import Budget
from apflow.config import Settings
from apflow.matrix import ApprovalMatrix, Condition, Rule, StepSpec
from apflow.models import Contact, Directory, Document, DocumentKind, LineItem, User
from apflow.providers.snapshot import SnapshotProvider
from apflow.store import Store
from apflow.workflow import Engine

DEMO_DIR = Path(__file__).resolve().parents[1] / "data" / "demo"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("APFLOW_HOME", str(home))
    return home


@pytest.fixture
def demo() -> SnapshotProvider:
    return SnapshotProvider(DEMO_DIR)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with Store(tmp_path / "apflow.sqlite3") as opened:
        yield opened


def make_document(
    number: str = "B-1001",
    total: str = "1100.00",
    kind: DocumentKind = DocumentKind.BILL,
    contact: str = "Acme Pty Ltd",
    email: str = "ap@acme.example",
    account: str = "420",
    dated: str = "2026-09-05",
    due: str = "2026-10-05",
    currency: str = "AUD",
    rate: str = "1.00",
    reference: str = "",
    tracking: dict[str, str] | None = None,
    document_id: str | None = None,
    purchase_order: str = "",
) -> Document:
    """Build a document without going through the API parsing layer."""
    amount = Decimal(total)
    tax = (amount / Decimal("11")).quantize(Decimal("0.01"))
    return Document(
        document_id=document_id or f"doc-{number}",
        kind=kind,
        number=number,
        contact=Contact(contact_id=f"c-{contact}", name=contact, email=email),
        document_date=date.fromisoformat(dated),
        due_date=date.fromisoformat(due),
        amount_total=amount,
        amount_tax=tax,
        amount_net=amount - tax,
        currency_code=currency,
        currency_rate=Decimal(rate),
        reference=reference,
        purchase_order_number=purchase_order or (number if kind is DocumentKind.PURCHASE_ORDER else ""),
        line_items=(
            LineItem(
                description=reference or number,
                quantity=Decimal("1.00"),
                unit_amount=amount,
                line_amount=amount,
                tax_amount=tax,
                account_code=account,
                tracking=dict(tracking or {}),
            ),
        ),
    )


@pytest.fixture
def directory() -> Directory:
    return Directory(
        [
            User(user_id="priya", name="Priya", email="priya@example.com",
                 approval_limit=Decimal("2500.00")),
            User(user_id="dana", name="Dana", email="dana@example.com",
                 approval_limit=Decimal("25000.00")),
            User(user_id="sam", name="Sam", email="sam@example.com",
                 approval_limit=Decimal("50000.00"),
                 away_until=date(2026, 12, 31), delegate_to="jo"),
            User(user_id="jo", name="Jo", email="jo@example.com",
                 approval_limit=Decimal("50000.00")),
            User(user_id="cfo", name="Alex", email="alex@example.com", approval_limit=None),
            User(user_id="gone", name="Gone", email="gone@example.com", active=False),
        ]
    )


@pytest.fixture
def matrix() -> ApprovalMatrix:
    return ApprovalMatrix(
        rules=[
            Rule(
                name="Auto",
                condition=Condition(kinds=["bill"], amount_max=Decimal("250.00")),
                auto_approve=True,
            ),
            Rule(
                name="Spend",
                condition=Condition(kinds=["bill", "purchase_order"]),
                steps=(
                    StepSpec(name="Budget holder", approvers=["dana"], sla_days=2),
                    StepSpec(
                        name="Finance",
                        approvers=["sam"],
                        condition=Condition(amount_min=Decimal("2500.00")),
                        sla_days=2,
                    ),
                    StepSpec(
                        name="CFO",
                        approvers=["cfo"],
                        condition=Condition(amount_min=Decimal("25000.00")),
                    ),
                ),
            ),
            Rule(name="Catch-all", steps=(StepSpec(name="Finance review", approvers=["jo"]),)),
        ]
    )


@pytest.fixture
def budget() -> Budget:
    from apflow.budget import BudgetLine

    return Budget(
        lines=[
            BudgetLine(period="2026-09", account="420", amount=Decimal("10000.00"), name="Marketing"),
            BudgetLine(period="2026-09", account="429", amount=Decimal("1000.00"), name="Travel"),
        ]
    )


@pytest.fixture
def engine(store: Store, matrix: ApprovalMatrix, directory: Directory, budget: Budget) -> Engine:
    return Engine(
        store=store,
        matrix=matrix,
        directory=directory,
        settings=Settings(),
        budget=budget,
    )


class RecordingProvider:
    """A writable provider that records what it was asked to do."""

    can_write = True
    name = "recording"

    def __init__(self, documents: list[Document] | None = None, fail: bool = False) -> None:
        self.documents_by_kind: dict[DocumentKind, list[Document]] = {}
        for document in documents or []:
            self.documents_by_kind.setdefault(document.kind, []).append(document)
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def organisation(self) -> Any:
        from apflow.models import Organisation

        return Organisation(name="Test Org", base_currency="AUD")

    def contacts(self) -> list[Contact]:
        return []

    def accounts(self) -> list[Any]:
        return []

    def documents(self, kind: DocumentKind, status: str | None = None) -> list[Document]:
        return list(self.documents_by_kind.get(kind, []))

    def set_document_status(self, document: Document, status: str) -> dict[str, Any]:
        if self.fail:
            from apflow.providers import ProviderError

            raise ProviderError("the ledger said no")
        self.calls.append((document.document_id, status))
        return {"ok": True}


@pytest.fixture
def recording() -> type[RecordingProvider]:
    return RecordingProvider
