"""The append-only, hash-chained ledger writer."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Union

from .chain import GENESIS_HASH, KINDS, canonical_json, compute_hash, encode_line
from .errors import InvalidEntryError, LedgerCorruptError, LedgerError, LedgerLockedError
from .verify import check_shape

try:  # POSIX advisory locking; the only platform-specific dependency.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

Clock = Callable[[], Union[datetime, str]]

_TAIL_CHUNK = 64 * 1024


def format_ts(value: Union[datetime, str]) -> str:
    """Render a timestamp as UTC ISO-8601 with microseconds and a ``Z`` suffix."""
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        raise InvalidEntryError("clock returned a naive datetime; timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Ledger:
    """An append-only JSONL decision ledger.

    Open with :meth:`Ledger.open`; append with :meth:`record`. Every append takes an
    exclusive ``fcntl.flock`` on the ledger file, re-reads the current tail *under the
    lock* and chains onto it, so concurrent writers (threads or processes) serialise
    into one linear chain. If the lock cannot be obtained within ``lock_timeout``
    seconds a :class:`LedgerLockedError` is raised; nothing is written.
    """

    def __init__(
        self,
        path: str,
        fd: int,
        *,
        lock_timeout: Optional[float],
        clock: Clock,
        fsync: bool,
    ) -> None:
        self.path = path
        self._fd: Optional[int] = fd
        self._lock_timeout = lock_timeout
        self._clock = clock
        self._fsync = fsync
        self._thread_lock = threading.Lock()
        self.seq = 0
        self.head = GENESIS_HASH

    # ------------------------------------------------------------------ open/close
    @classmethod
    def open(
        cls,
        path: Union[str, os.PathLike],
        *,
        lock_timeout: Optional[float] = 10.0,
        clock: Optional[Clock] = None,
        fsync: bool = True,
    ) -> "Ledger":
        """Open (creating if needed) the ledger at ``path`` and resume its chain.

        ``lock_timeout``: seconds to wait for the exclusive append lock (``0`` = fail
        immediately, ``None`` = wait forever). ``clock``: zero-argument callable returning
        an aware ``datetime`` (or pre-formatted string), used for ``ts``; defaults to
        the current UTC time. ``fsync``: flush every append to stable storage.

        Raises :class:`LedgerCorruptError` if the existing tail is partial or does not
        hash correctly - appending onto it would extend a broken chain.
        """
        if fcntl is None:  # pragma: no cover
            raise LedgerError("decision_ledger requires POSIX fcntl file locking (Linux/macOS)")
        path = os.fspath(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        ledger = cls(path, fd, lock_timeout=lock_timeout, clock=clock or _utc_now, fsync=fsync)
        try:
            with ledger._locked():
                ledger.seq, ledger.head = ledger._read_tail()
        except BaseException:
            ledger.close()
            raise
        return ledger

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._fd is None

    # ------------------------------------------------------------------ append
    def record(
        self,
        kind: str,
        actor: str,
        payload: dict,
        grounding: Optional[Iterable[dict]] = None,
    ) -> dict:
        """Append one entry and return it (including ``seq`` and ``hash``).

        ``kind`` must be one of :data:`decision_ledger.KINDS`; ``actor`` a non-empty string;
        ``payload`` a JSON-serialisable dict; ``grounding`` an optional list of citation
        dicts, each with at least a non-empty string ``source``.
        """
        body = self._validate(kind, actor, payload, grounding)
        if self._fd is None:
            raise LedgerError("ledger is closed")
        with self._thread_lock, self._locked():
            last_seq, last_hash = self._read_tail()
            entry = {
                "ts": format_ts(self._clock()),
                "seq": last_seq + 1,
                "kind": body["kind"],
                "actor": body["actor"],
                "payload": body["payload"],
                "grounding": body["grounding"],
                "prev_hash": last_hash,
            }
            entry["hash"] = compute_hash(last_hash, entry)
            line = encode_line(entry)
            written = os.write(self._fd, line)
            if written != len(line):  # pragma: no cover - only on a full disk
                raise LedgerCorruptError(f"short write to {self.path}: {written} of {len(line)} bytes")
            if self._fsync:
                os.fsync(self._fd)
            self.seq, self.head = entry["seq"], entry["hash"]
        return entry

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _validate(kind: Any, actor: Any, payload: Any, grounding: Any) -> dict:
        if kind not in KINDS:
            raise InvalidEntryError(f"unknown kind {kind!r}; expected one of: {', '.join(KINDS)}")
        if not isinstance(actor, str) or not actor.strip():
            raise InvalidEntryError("actor must be a non-empty string")
        if not isinstance(payload, dict):
            raise InvalidEntryError(f"payload must be a dict, got {type(payload).__name__}")
        citations = [] if grounding is None else list(grounding)
        for i, cite in enumerate(citations):
            if not isinstance(cite, dict):
                raise InvalidEntryError(f"grounding[{i}] must be a dict, got {type(cite).__name__}")
            source = cite.get("source")
            if not isinstance(source, str) or not source.strip():
                raise InvalidEntryError(f"grounding[{i}] needs a non-empty string 'source'")
        try:
            # Round-trip so the stored values are exactly what will be hashed later.
            payload = json.loads(canonical_json(payload))
            citations = json.loads(canonical_json(citations))
        except (TypeError, ValueError) as exc:
            raise InvalidEntryError(f"payload/grounding is not JSON-serialisable: {exc}") from None
        return {"kind": kind, "actor": actor, "payload": payload, "grounding": citations}

    def _locked(self) -> "_FileLock":
        return _FileLock(self._fd, self.path, self._lock_timeout)

    def _read_tail(self) -> tuple[int, str]:
        """Return (seq, hash) of the last entry on disk, validating it. Caller holds the lock."""
        size = os.fstat(self._fd).st_size
        if size == 0:
            return 0, GENESIS_HASH
        last = os.pread(self._fd, 1, size - 1)
        if last != b"\n":
            raise LedgerCorruptError(
                f"{self.path} ends with a partial line (no trailing newline); "
                f"refusing to append. Run 'adl verify {self.path}'."
            )
        # Walk backwards from the final newline to the one that precedes the last line.
        end = size - 1  # offset of the trailing newline
        pos, start = end, 0
        while pos > 0:
            step = min(_TAIL_CHUNK, pos)
            pos -= step
            nl = os.pread(self._fd, step, pos).rfind(b"\n")
            if nl != -1:
                start = pos + nl + 1
                break
        raw = os.pread(self._fd, end - start, start)
        entry, error = check_shape(raw)
        if error is not None:
            raise LedgerCorruptError(
                f"last line of {self.path} is malformed ({error}); refusing to append. "
                f"Run 'adl verify {self.path}'."
            )
        if compute_hash(entry["prev_hash"], entry) != entry["hash"]:
            raise LedgerCorruptError(
                f"last entry of {self.path} (seq {entry['seq']}) fails its hash check; "
                f"refusing to append. Run 'adl verify {self.path}'."
            )
        return entry["seq"], entry["hash"]


class _FileLock:
    """Exclusive ``flock`` on an open fd with a bounded wait."""

    def __init__(self, fd: Optional[int], path: str, timeout: Optional[float]) -> None:
        if fd is None:
            raise LedgerError("ledger is closed")
        self.fd, self.path, self.timeout = fd, path, timeout

    def __enter__(self) -> "_FileLock":
        if self.timeout is None:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self
        deadline = time.monotonic() + self.timeout
        delay = 0.002
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LedgerLockedError(
                        f"could not acquire the exclusive append lock on {self.path} within "
                        f"{self.timeout:g}s: another writer is holding it. Nothing was written."
                    ) from None
                time.sleep(delay)
                delay = min(delay * 2, 0.05)

    def __exit__(self, *exc: Any) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)
