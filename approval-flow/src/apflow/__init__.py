"""apflow — approval workflows and spend control on top of Xero.

Documents are pulled out of the ledger as drafts, routed through an approval
matrix, decided by the right people in the right order, and only then written
back. Nothing reaches the ledger without a complete, tamper-evident record of
who approved it and what they were looking at when they did.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
