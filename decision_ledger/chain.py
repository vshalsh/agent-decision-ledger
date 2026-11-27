"""Canonical encoding and hashing: the single source of truth for the chain recipe.

Recipe (see README "Ledger format"):

    body      = entry without its "hash" key
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False)
    hash      = sha256((prev_hash + canonical).encode("utf-8")).hexdigest()
    line      = json.dumps(entry_with_hash, <same options>) + "\\n"
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS_HASH = "0" * 64

KINDS = ("decision", "tool_call", "tool_result", "observation", "human_approval")

FIELDS = ("actor", "grounding", "hash", "kind", "payload", "prev_hash", "seq", "ts")


def canonical_json(obj: Any) -> str:
    """Return the canonical JSON text of ``obj`` (sorted keys, no whitespace, ASCII only)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def compute_hash(prev_hash: str, body: dict) -> str:
    """Hash of an entry: sha256 over ``prev_hash`` + canonical JSON of the entry without ``hash``."""
    if "hash" in body:
        body = {k: v for k, v in body.items() if k != "hash"}
    material = prev_hash + canonical_json(body)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def encode_line(entry: dict) -> bytes:
    """Serialise a complete entry (including ``hash``) to its on-disk line, newline included."""
    return (canonical_json(entry) + "\n").encode("ascii")


def is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )
