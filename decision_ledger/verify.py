"""Offline chain verification.

Every line is checked, in order, for:

1. ``malformed_line``  - not ASCII/JSON, not an object, wrong/missing/extra fields,
   wrong field types, unknown kind, or bytes that are not the canonical encoding.
2. ``sequence_gap``    - ``seq`` is not exactly previous seq + 1 (first entry: 1).
3. ``hash_mismatch``   - ``prev_hash`` does not equal the previous entry's ``hash``
   (genesis: 64 zeros), or the stored ``hash`` differs from the recomputed one.

Verification stops at the first failure and names that entry's sequence number.
The sequence number reported is the *expected* one for that line position, so it
is exact even when the ``seq`` field itself (or the whole line) was damaged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Iterator, Optional

from .chain import FIELDS, GENESIS_HASH, KINDS, canonical_json, compute_hash, is_hex64

MALFORMED = "malformed_line"
SEQUENCE_GAP = "sequence_gap"
HASH_MISMATCH = "hash_mismatch"

REASON_LABELS = {
    MALFORMED: "malformed line",
    SEQUENCE_GAP: "sequence gap",
    HASH_MISMATCH: "hash mismatch",
}


@dataclass
class Break:
    seq: int  # expected sequence number at the broken position
    line: int  # 1-based physical line number
    reason: str  # one of MALFORMED / SEQUENCE_GAP / HASH_MISMATCH
    detail: str

    @property
    def label(self) -> str:
        return REASON_LABELS[self.reason]

    def message(self) -> str:
        return f"chain broken at seq {self.seq} (line {self.line}): {self.label}: {self.detail}"

    def to_dict(self) -> dict:
        return {"seq": self.seq, "line": self.line, "reason": self.reason, "detail": self.detail}


@dataclass
class ParsedLine:
    line: int
    entry: Optional[dict]  # None when the line could not be parsed into a valid entry shape
    error: Optional[str]  # structural problem (malformed), if any


@dataclass
class VerifyResult:
    path: str
    ok: bool
    entries: int  # entries verified before the first break (all of them when ok)
    head: str  # hash of the last verified entry (GENESIS_HASH for an empty ledger)
    first_break: Optional[Break] = None
    parsed: list = field(default_factory=list, repr=False)  # every ParsedLine, for report/stats

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "status": "verified" if self.ok else "tampered",
            "entries_verified": self.entries,
            "head": self.head,
            "first_break": self.first_break.to_dict() if self.first_break else None,
        }


def check_shape(raw: bytes) -> tuple[Optional[dict], Optional[str]]:
    """Parse one line (without its trailing newline). Return (entry, None) or (None/entry, error)."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        return None, f"non-ASCII byte at column {exc.start + 1}"
    if not text:
        return None, "empty line"
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON ({exc.msg} at column {exc.colno})"
    if not isinstance(obj, dict):
        return None, "line is not a JSON object"
    keys = set(obj)
    missing = sorted(set(FIELDS) - keys)
    extra = sorted(keys - set(FIELDS))
    if missing:
        return None, f"missing field(s): {', '.join(missing)}"
    if extra:
        return None, f"unexpected field(s): {', '.join(extra)}"
    seq = obj["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None, "field 'seq' is not an integer"
    for name in ("ts", "actor", "kind"):
        if not isinstance(obj[name], str) or not obj[name]:
            return None, f"field '{name}' is not a non-empty string"
    if obj["kind"] not in KINDS:
        return None, f"unknown kind {obj['kind']!r}"
    if not isinstance(obj["payload"], dict):
        return None, "field 'payload' is not an object"
    if not isinstance(obj["grounding"], list) or not all(isinstance(g, dict) for g in obj["grounding"]):
        return None, "field 'grounding' is not a list of objects"
    for name in ("prev_hash", "hash"):
        if not is_hex64(obj[name]):
            return None, f"field '{name}' is not 64 lowercase hex characters"
    try:
        canonical = canonical_json(obj)
    except ValueError:
        return None, "contains non-finite numbers"
    if canonical != text:
        return obj, "bytes are not the canonical encoding (line was edited)"
    return obj, None


def iter_lines(path: str) -> Iterator[tuple[int, bytes, bool]]:
    """Yield (line_no, raw_without_newline, terminated) for every physical line."""
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, start=1):
            terminated = raw.endswith(b"\n")
            yield n, raw[:-1] if terminated else raw, terminated


def verify_file(path: str | os.PathLike, *, collect: bool = False) -> VerifyResult:
    """Recompute the whole chain of the ledger at ``path``.

    With ``collect=True`` every line (also those after a break) is parsed and kept in
    ``result.parsed`` so reports and stats can show the full, partially trusted picture.
    """
    path = os.fspath(path)
    prev_hash = GENESIS_HASH
    verified = 0
    first_break: Optional[Break] = None
    parsed: list[ParsedLine] = []

    for line_no, raw, terminated in iter_lines(path):
        expected_seq = line_no
        entry, error = check_shape(raw)
        if error is None and not terminated:
            error = "final line is not newline-terminated (truncated or edited)"
        if collect:
            parsed.append(ParsedLine(line_no, entry if error is None else None, error))
        if first_break is not None:
            if not collect:
                break
            continue

        if error is not None:
            first_break = Break(expected_seq, line_no, MALFORMED, error)
        elif entry["seq"] != expected_seq:
            first_break = Break(
                expected_seq, line_no, SEQUENCE_GAP, f"expected seq {expected_seq}, found {entry['seq']}"
            )
        elif entry["prev_hash"] != prev_hash:
            first_break = Break(
                expected_seq,
                line_no,
                HASH_MISMATCH,
                f"prev_hash {entry['prev_hash'][:12]}... does not link to "
                + ("genesis" if expected_seq == 1 else f"seq {expected_seq - 1}")
                + f" ({prev_hash[:12]}...)",
            )
        else:
            recomputed = compute_hash(prev_hash, entry)
            if recomputed != entry["hash"]:
                first_break = Break(
                    expected_seq,
                    line_no,
                    HASH_MISMATCH,
                    f"stored {entry['hash'][:12]}... != recomputed {recomputed[:12]}...",
                )
            else:
                prev_hash = entry["hash"]
                verified += 1

    return VerifyResult(
        path=path,
        ok=first_break is None,
        entries=verified,
        head=prev_hash,
        first_break=first_break,
        parsed=parsed,
    )
