"""Summary statistics over a ledger file."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .chain import KINDS
from .verify import VerifyResult, verify_file


@dataclass
class LedgerStats:
    path: str
    entries: int  # well-formed entries (including any after a chain break)
    malformed_lines: int
    by_kind: dict
    by_actor: dict
    first_ts: Optional[str]
    last_ts: Optional[str]
    span_seconds: Optional[float]
    verify: VerifyResult

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "entries": self.entries,
            "malformed_lines": self.malformed_lines,
            "by_kind": self.by_kind,
            "by_actor": self.by_actor,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "span_seconds": self.span_seconds,
            "chain": self.verify.to_dict(),
        }


def parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_span(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    total = int(round(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = [f"{days}d"] if days else []
    if days or hours:
        parts.append(f"{hours}h")
    if days or hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def compute_stats(path: str | os.PathLike, result: Optional[VerifyResult] = None) -> LedgerStats:
    path = os.fspath(path)
    if result is None or not result.parsed:
        result = verify_file(path, collect=True)
    entries = [p.entry for p in result.parsed if p.entry is not None]
    kinds = Counter(e["kind"] for e in entries)
    actors = Counter(e["actor"] for e in entries)
    stamps = [t for t in (parse_ts(e["ts"]) for e in entries) if t is not None]
    first = min(stamps) if stamps else None
    last = max(stamps) if stamps else None
    first_ts = last_ts = None
    if first is not None:
        first_ts = next(e["ts"] for e in entries if parse_ts(e["ts"]) == first)
        last_ts = next(e["ts"] for e in reversed(entries) if parse_ts(e["ts"]) == last)
    return LedgerStats(
        path=path,
        entries=len(entries),
        malformed_lines=sum(1 for p in result.parsed if p.entry is None),
        by_kind={k: kinds.get(k, 0) for k in KINDS},
        by_actor=dict(sorted(actors.items(), key=lambda kv: (-kv[1], kv[0]))),
        first_ts=first_ts,
        last_ts=last_ts,
        span_seconds=(last - first).total_seconds() if first is not None else None,
        verify=result,
    )
