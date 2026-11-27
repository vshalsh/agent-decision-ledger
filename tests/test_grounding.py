"""Grounding verifiers from PROJECT_BRIEF.md (test seat, L-002).

Both verifiers are deterministic and use an INDEPENDENT implementation: nothing here imports
decision_ledger.chain or decision_ledger.verify. Hashing is recomputed from raw bytes with
hashlib/json only; the report is parsed with html.parser.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import subprocess
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest

from decision_ledger import Ledger
from decision_ledger.report import write_report

KINDS = ["decision", "tool_call", "tool_result", "observation", "human_approval"]
GENESIS = "0" * 64
N_ENTRIES = 240


def build_ledger(path: Path, n: int = N_ENTRIES, seed: int = 7) -> None:
    rng = random.Random(seed)
    actors = ["agent:alpha", "agent:beta", "tool:search", "human:ops", "tool:é-unicode"]
    with Ledger.open(path, fsync=False) as led:
        for i in range(n):
            kind = KINDS[i % 5] if i < 10 else rng.choice(KINDS)
            led.record(
                kind,
                rng.choice(actors),
                {"i": i, "note": f"entry {i} ☃ <b>&", "n": {"deep": [1, 2.5, None, True]}},
                grounding=[{"source": f"doc/{i}.md", "locator": "s1", "quote": "q"}] if i % 3 == 0 else [],
            )


def independent_recompute(raw: bytes):
    """Return (ok, first_bad_seq, entries). Independent of the library implementation."""
    prev = GENESIS
    entries = []
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    else:
        return False, len(lines), entries
    for n, line in enumerate(lines, start=1):
        try:
            e = json.loads(line.decode("ascii"))
            body = {k: v for k, v in e.items() if k != "hash"}
            canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            full = json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            good = (
                full.encode("ascii") == line
                and e["seq"] == n
                and e["prev_hash"] == prev
                and hashlib.sha256((prev + canon).encode("utf-8")).hexdigest() == e["hash"]
            )
        except Exception:
            good = False
        if not good:
            return False, n, entries
        entries.append(e)
        prev = e["hash"]
    return True, None, entries


def adl_verify_json(path: Path):
    p = subprocess.run([sys.executable, "-m", "decision_ledger", "verify", str(path), "--format", "json"],
                       capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)


@pytest.fixture(scope="module")
def ledger_path(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("grounding") / "ledger.jsonl"
    build_ledger(p)
    return p


def test_chain_recompute(ledger_path, tmp_path):
    raw = ledger_path.read_bytes()
    ok, bad, entries = independent_recompute(raw)
    assert len(entries) >= 200
    assert ok and bad is None, f"independent recompute failed at {bad}"
    # 100% match: every stored hash equals the independent recomputation (checked in loop above);
    # also the library's own verifier must agree
    code, res = adl_verify_json(ledger_path)
    assert code == 0 and res["ok"] is True and res["entries_verified"] == len(entries)
    assert res["head"] == entries[-1]["hash"]

    # >= 20 random single-byte mutations (here 60), each caught at the line containing the byte
    rng = random.Random(20261127)
    line_starts, off = [], 0
    for ln in raw.split(b"\n")[:-1]:
        line_starts.append((off, off + len(ln) + 1))
        off += len(ln) + 1
    sample = 60
    for _ in range(sample):
        pos = rng.randrange(len(raw))
        mask = rng.choice([0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0xFF, 0x55])
        expect_seq = next(i for i, (a, b) in enumerate(line_starts, start=1) if a <= pos < b)
        mutated = bytearray(raw)
        mutated[pos] ^= mask
        # independent verifier must flag the exact line
        ok2, bad2, _ = independent_recompute(bytes(mutated))
        assert not ok2 and bad2 == expect_seq, (pos, mask, bad2, expect_seq)
        # library CLI must too
        m = tmp_path / "m.jsonl"
        m.write_bytes(bytes(mutated))
        code, res = adl_verify_json(m)
        assert code == 1 and res["ok"] is False
        assert res["first_break"]["seq"] == expect_seq, (pos, mask, res["first_break"], expect_seq)


class _Parse(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entries, self.summary, self._in_sum, self.kind_counts, self.actor_counts = [], None, False, {}, {}
        self.total = None
        self.links = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link":
            self.links += 1
        if tag == "article" and "data-seq" in a:
            self.entries.append((int(a["data-seq"]), a["data-kind"], a["data-actor"], a["data-ts"]))
        if tag == "script" and a.get("type") == "application/json" and a.get("id") == "adl-summary":
            self._in_sum, self.summary = True, ""
        if "data-kind-count" in a:
            self._cur = ("k", a["data-kind-count"])
        elif "data-actor-count" in a:
            self._cur = ("a", a["data-actor-count"])
        elif "data-total-entries" in a:
            self._cur = ("t", None)
        else:
            self._cur = None

    def handle_data(self, data):
        if self._in_sum:
            self.summary += data
        elif getattr(self, "_cur", None):
            t, key = self._cur
            v = int(data.strip())
            if t == "k":
                self.kind_counts[key] = v
            elif t == "a":
                self.actor_counts[key] = v
            else:
                self.total = v
            self._cur = None

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_sum = False


def test_report_counts_match_ledger(ledger_path, tmp_path):
    out = tmp_path / "r.html"
    write_report(ledger_path, out)
    p = _Parse()
    p.feed(out.read_text(encoding="utf-8"))
    _, _, truth = independent_recompute(ledger_path.read_bytes())
    # timeline rows: exact seq/kind/actor/ts, in order
    assert p.entries == [(e["seq"], e["kind"], e["actor"], e["ts"]) for e in truth]
    # visible counts
    assert p.total == len(truth)
    assert p.kind_counts == {k: Counter(e["kind"] for e in truth).get(k, 0) for k in KINDS}
    assert p.actor_counts == dict(Counter(e["actor"] for e in truth))
    # embedded JSON summary
    s = json.loads(p.summary)
    assert s["entries"] == len(truth)
    assert s["by_kind"] == {k: v for k, v in Counter(e["kind"] for e in truth).items()} or s["by_kind"] == p.kind_counts
    assert {k: v for k, v in s["by_kind"].items() if v} == dict(Counter(e["kind"] for e in truth))
    assert s["by_actor"] == dict(Counter(e["actor"] for e in truth))
    assert s["first_ts"] == truth[0]["ts"] and s["last_ts"] == truth[-1]["ts"]
    assert s["chain"]["ok"] is True
    # citation count shown == citations in ledger
    html_text = out.read_text(encoding="utf-8")
    assert html_text.count('class="grounding"') == sum(1 for e in truth if e["grounding"])
