"""Smoke tests for L-001. The full suite (incl. tests/test_grounding.py) is owned by the test seat."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import random
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from decision_ledger import (
    GENESIS_HASH,
    InvalidEntryError,
    Ledger,
    LedgerCorruptError,
    LedgerLockedError,
    compute_hash,
    verify_file,
)
from decision_ledger.cli import main as adl
from decision_ledger.report import render_report

ROOT = Path(__file__).resolve().parents[1]


def _load_demo():
    spec = importlib.util.spec_from_file_location("demo_agent", ROOT / "examples" / "demo_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


demo_agent = _load_demo()


@pytest.fixture(scope="module")
def demo_ledger(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("demo") / "demo.jsonl"
    count = demo_agent.build_demo_ledger(str(path))
    assert count >= 200
    return path


def _entries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def _line_of_byte(data: bytes, index: int) -> int:
    return data.count(b"\n", 0, index) + 1


# ---------------------------------------------------------------- chain


def test_demo_ledger_verifies_green(demo_ledger, capsys):
    result = verify_file(demo_ledger)
    assert result.ok and result.first_break is None
    entries = _entries(demo_ledger)
    assert result.entries == len(entries) >= 200
    assert {e["kind"] for e in entries} == {"decision", "tool_call", "tool_result", "observation", "human_approval"}
    assert any(e["grounding"] for e in entries)
    assert adl(["verify", str(demo_ledger)]) == 0
    assert capsys.readouterr().out.startswith("PASS ")


def test_hash_recipe_is_reimplementable(demo_ledger):
    import hashlib

    prev = "0" * 64
    for n, line in enumerate(demo_ledger.read_bytes().splitlines(), start=1):
        entry = json.loads(line)
        body = {k: v for k, v in entry.items() if k != "hash"}
        canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        assert entry["seq"] == n and entry["prev_hash"] == prev
        assert hashlib.sha256((prev + canon).encode()).hexdigest() == entry["hash"]
        prev = entry["hash"]


@pytest.mark.parametrize("trial", range(25))
def test_single_byte_flip_names_exact_seq(demo_ledger, tmp_path, trial):
    data = demo_ledger.read_bytes()
    rng = random.Random(1000 + trial)
    index = rng.randrange(len(data))
    mutated = bytearray(data)
    mutated[index] ^= 1 << rng.randrange(8)
    bad = tmp_path / "tampered.jsonl"
    bad.write_bytes(bytes(mutated))

    expected = _line_of_byte(data, index)
    result = verify_file(bad)
    assert not result.ok
    assert result.first_break.seq == expected, result.first_break
    assert adl(["verify", str(bad)]) == 1


def test_break_reasons_are_distinguished(demo_ledger, tmp_path):
    lines = demo_ledger.read_bytes().splitlines(keepends=True)

    edited = json.loads(lines[9])
    edited["payload"]["tampered"] = True
    p = tmp_path / "edit.jsonl"
    p.write_bytes(b"".join(lines[:9] + [(json.dumps(edited, sort_keys=True, separators=(",", ":")) + "\n").encode()] + lines[10:]))
    brk = verify_file(p).first_break
    assert (brk.seq, brk.reason) == (10, "hash_mismatch")

    p = tmp_path / "gap.jsonl"
    p.write_bytes(b"".join(lines[:19] + lines[20:]))  # delete seq 20
    brk = verify_file(p).first_break
    assert (brk.seq, brk.reason) == (20, "sequence_gap")
    assert "expected seq 20, found 21" in brk.detail

    p = tmp_path / "junk.jsonl"
    p.write_bytes(b"".join(lines[:29] + [b"{not json\n"] + lines[30:]))
    brk = verify_file(p).first_break
    assert (brk.seq, brk.reason) == (30, "malformed_line")

    p = tmp_path / "trunc.jsonl"
    p.write_bytes(b"".join(lines)[:-5])
    brk = verify_file(p).first_break
    assert (brk.seq, brk.reason) == (len(lines), "malformed_line")


# ---------------------------------------------------------------- library


def test_record_and_resume(tmp_path):
    path = tmp_path / "l.jsonl"
    with Ledger.open(path) as led:
        first = led.record("decision", "agent:a", {"x": 1}, grounding=[{"source": "doc.md", "locator": "p1"}])
        assert first["seq"] == 1 and first["prev_hash"] == GENESIS_HASH
        assert first["hash"] == compute_hash(GENESIS_HASH, first)
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", first["ts"])
    with Ledger.open(path) as led:  # reopening resumes the chain from the last line
        assert (led.seq, led.head) == (1, first["hash"])
        second = led.record("observation", "agent:a", {})
        assert second["seq"] == 2 and second["prev_hash"] == first["hash"]
    assert verify_file(path).ok


def test_invalid_entries_rejected(tmp_path):
    with Ledger.open(tmp_path / "l.jsonl") as led:
        with pytest.raises(InvalidEntryError, match="unknown kind 'thought'"):
            led.record("thought", "agent:a", {})
        with pytest.raises(InvalidEntryError, match="actor"):
            led.record("decision", "", {})
        with pytest.raises(InvalidEntryError, match="payload must be a dict"):
            led.record("decision", "agent:a", ["x"])
        with pytest.raises(InvalidEntryError, match="source"):
            led.record("decision", "agent:a", {}, grounding=[{"quote": "no source"}])
        with pytest.raises(InvalidEntryError, match="JSON"):
            led.record("decision", "agent:a", {"x": object()})
        assert led.seq == 0
    assert (tmp_path / "l.jsonl").read_bytes() == b""


def test_refuses_to_append_to_corrupt_tail(tmp_path):
    path = tmp_path / "l.jsonl"
    with Ledger.open(path) as led:
        led.record("decision", "agent:a", {"n": 1})
    path.write_bytes(path.read_bytes().replace(b'"n":1', b'"n":2'))
    with pytest.raises(LedgerCorruptError, match="fails its hash check"):
        Ledger.open(path)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(LedgerCorruptError, match="partial line"):
        Ledger.open(path)


def _hold_lock(path, ready, release):
    import fcntl

    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        ready.set()
        release.wait(10)


def test_second_writer_gets_clear_lock_error(tmp_path):
    path = tmp_path / "l.jsonl"
    with Ledger.open(path) as led:
        led.record("decision", "agent:a", {})
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold_lock, args=(str(path), ready, release))
        proc.start()
        try:
            assert ready.wait(10)
            led._lock_timeout = 0.2
            with pytest.raises(LedgerLockedError, match="another writer is holding it"):
                led.record("decision", "agent:a", {})
        finally:
            release.set()
            proc.join(10)
        led.record("decision", "agent:a", {"after": "release"})
    result = verify_file(path)
    assert result.ok and result.entries == 2


def _writer(path, n, actor):
    with Ledger.open(path, fsync=False) as led:
        for i in range(n):
            led.record("tool_call", actor, {"i": i})


def test_concurrent_processes_never_fork_the_chain(tmp_path):
    path = str(tmp_path / "l.jsonl")
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_writer, args=(path, 50, f"agent:{k}")) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    result = verify_file(path)
    assert result.ok and result.entries == 200
    assert Counter(e["actor"] for e in _entries(Path(path))) == {f"agent:{k}": 50 for k in range(4)}


# ---------------------------------------------------------------- stats & report


def test_stats_correct(demo_ledger, capsys):
    entries = _entries(demo_ledger)
    assert adl(["stats", str(demo_ledger), "--format", "json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["entries"] == len(entries)
    kinds = Counter(e["kind"] for e in entries)
    assert stats["by_kind"] == {k: kinds[k] for k in ("decision", "tool_call", "tool_result", "observation", "human_approval")}
    assert stats["by_actor"] == dict(Counter(e["actor"] for e in entries))
    assert stats["first_ts"] == entries[0]["ts"] and stats["last_ts"] == entries[-1]["ts"]
    assert stats["chain"]["ok"] is True
    assert adl(["stats", str(demo_ledger)]) == 0
    text = capsys.readouterr().out
    assert "Chain    PASS verified" in text and "\x1b[" not in text  # not a TTY: no colour


def test_report_self_contained_and_faithful(demo_ledger, tmp_path):
    out = tmp_path / "report.html"
    assert adl(["report", str(demo_ledger), "-o", str(out)]) == 0
    doc = out.read_text(encoding="utf-8")
    entries = _entries(demo_ledger)

    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", doc, re.I)
    assert not re.search(r"@import|url\(\s*['\"]?https?:", doc, re.I)
    assert "<link" not in doc.lower()

    assert doc.count('<article class="entry') == len(entries)
    assert re.findall(r'data-seq="(\d+)"', doc) and sorted(map(int, re.findall(r'data-seq="(\d+)"', doc))) == [
        e["seq"] for e in entries
    ]
    assert f'data-total-entries="{len(entries)}"' in doc
    assert Counter(re.findall(r'data-actor="([^"]+)"', doc)) == Counter(e["actor"] for e in entries)
    assert sorted(re.findall(r'data-ts="([^"]+)"', doc)) == sorted(e["ts"] for e in entries)
    assert 'data-chain="verified"' in doc and "CHAIN VERIFIED" in doc
    assert doc.count('class="citation"') == sum(len(e["grounding"]) for e in entries)
    for token in ("#0F172A", "#1E293B", "#E2E8F0", "#94A3B8", "#38BDF8", "#4ADE80", "#052E16"):
        assert token in doc


def test_report_shows_tamper_banner(demo_ledger, tmp_path):
    lines = demo_ledger.read_bytes().splitlines(keepends=True)
    lines[41] = lines[41].replace(b'"seq":42', b'"seq":43')
    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(b"".join(lines))
    doc = render_report(bad)
    assert 'data-chain="tampered"' in doc and 'data-broken-seq="42"' in doc
    assert "CHAIN TAMPERED: broken at seq 42 (line 42): sequence gap" in doc
    assert "#F87171" in doc and "#450A0A" in doc


def test_console_script_and_no_color(demo_ledger, tmp_path):
    env = dict(os.environ, NO_COLOR="1")
    proc = subprocess.run(
        [sys.executable, "-m", "decision_ledger", "verify", str(demo_ledger)],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert proc.returncode == 0 and proc.stdout.startswith("PASS ") and "\x1b[" not in proc.stdout
    missing = subprocess.run(
        [sys.executable, "-m", "decision_ledger", "verify", str(tmp_path / "nope.jsonl")],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert missing.returncode == 2 and "no such ledger file" in missing.stderr
