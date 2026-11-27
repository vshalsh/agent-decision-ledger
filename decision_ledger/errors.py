"""Exception hierarchy for decision_ledger."""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for every error raised by decision_ledger."""


class InvalidEntryError(LedgerError, ValueError):
    """An entry passed to ``Ledger.record`` is not acceptable (bad kind, actor, payload...)."""


class LedgerCorruptError(LedgerError):
    """The ledger on disk cannot be safely appended to (partial or tampered tail line)."""


class LedgerLockedError(LedgerError):
    """Another writer holds the ledger's exclusive lock and the wait timed out."""
