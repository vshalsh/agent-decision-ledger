"""agent-decision-ledger: a tamper-evident, hash-chained audit trail for AI agents."""

__version__ = "0.1.0"

from .chain import GENESIS_HASH, KINDS, canonical_json, compute_hash  # noqa: E402
from .errors import (  # noqa: E402
    InvalidEntryError,
    LedgerCorruptError,
    LedgerError,
    LedgerLockedError,
)
from .ledger import Ledger  # noqa: E402
from .verify import Break, VerifyResult, verify_file  # noqa: E402

__all__ = [
    "__version__",
    "Ledger",
    "KINDS",
    "GENESIS_HASH",
    "canonical_json",
    "compute_hash",
    "verify_file",
    "VerifyResult",
    "Break",
    "LedgerError",
    "InvalidEntryError",
    "LedgerCorruptError",
    "LedgerLockedError",
]
