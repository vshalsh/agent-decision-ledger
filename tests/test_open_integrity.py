"""L-004 regressions: open() integrity, truncation expectations, exit codes, golden vectors."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from decision_ledger import Ledger, LedgerCorruptError, verify_file

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "golden.jsonl"
GOLDEN_HEAD = "75c83750e330ed366013296cf935f608c517e4d3eedbb040d08dd87dc44df1d6"


def adl(*args):
    p = subprocess.run([sys.executable, "-m", "decision_ledger", *map(str, args)],
                       capture_output=True, text=True, cwd=ROOT)
    return p.returncode, p.stdout, p.stderr


def make(path: Path, n: int = 21) -> Path:
    with Ledger.open(path, fsync=False) as led:
        for i in range(n):
            led.record("tool_call", "agent:a", {"i": i})
    return path


def tamper_seq(path: Path, seq: int) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    lines[seq - 1] = lines[seq - 1].replace(f'"i":{seq - 1}'.encode(), b'"i":999')
    path.write_bytes(b"".join(lines))


def head_of(path: Path) -> str:
    return json.loads(path.read_bytes().splitlines()[-1])["hash"]


# ---------------------------------------------------------------- Sev-1: open() integrity


def test_open_refuses_chain_broken_before_tail(tmp_path):
    p = make(tmp_path / "l.jsonl")
    tamper_seq(p, 6)
    before = p.read_bytes()
    with pytest.raises(LedgerCorruptError, match=r"chain broken at seq 6 \(line 6\): hash mismatch"):
        Ledger.open(p)
    assert p.read_bytes() == before  # nothing appended


def test_open_refuses_sequence_gap_before_tail(tmp_path):
    p = make(tmp_path / "l.jsonl")
    lines = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(b"".join(lines[:9] + lines[10:]))
    with pytest.raises(LedgerCorruptError, match=r"seq 10 .*sequence gap"):
        Ledger.open(p)


def test_open_verify_false_is_unsafe_resume(tmp_path):
    p = make(tmp_path / "l.jsonl")
    tamper_seq(p, 6)
    with Ledger.open(p, verify=False) as led:  # documented opt-out: only the tail is checked
        assert led.seq == 21
        assert led.record("decision", "agent:a", {})["seq"] == 22
    assert verify_file(p).first_break.seq == 6


def test_open_verify_false_still_checks_tail(tmp_path):
    p = make(tmp_path / "l.jsonl")
    tamper_seq(p, 21)
    with pytest.raises(LedgerCorruptError, match="fails its hash check"):
        Ledger.open(p, verify=False)


def test_open_resumes_intact_chain_with_full_verify(tmp_path):
    p = make(tmp_path / "l.jsonl")
    with Ledger.open(p) as led:
        assert (led.seq, led.head) == (21, head_of(p))
        led.record("observation", "agent:a", {})
    assert verify_file(p).ok and verify_file(p).entries == 22


# ---------------------------------------------------------------- Sev-2: truncation / wipe


def test_expect_head_pass_and_fail(tmp_path):
    p = make(tmp_path / "l.jsonl")
    head = head_of(p)
    code, out, _ = adl("verify", p, "--expect-head", head.upper())
    assert code == 0 and out.startswith("PASS ") and "expectation met: head matches" in out

    lines = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(b"".join(lines[:15]))  # clean line-boundary truncation
    assert adl("verify", p)[0] == 0  # the chain alone cannot see it
    code, out, _ = adl("verify", p, "--expect-head", head)
    assert code == 1
    assert out.startswith(f"FAIL {p}: head mismatch: expected {head[:16]}..., found ")
    assert "after 15 verified entries (ledger truncated, rolled back or replaced)" in out

    code, out, _ = adl("verify", p, "--expect-head", head, "--format", "json")
    doc = json.loads(out)
    assert code == 1 and doc["ok"] is False and doc["status"] == "expectation_failed"
    assert doc["checks"] == [{"check": "expect_head", "expected": head, "found": doc["head"], "ok": False}]


def test_min_entries_pass_and_fail(tmp_path):
    p = make(tmp_path / "l.jsonl")
    assert adl("verify", p, "--min-entries", 21)[0] == 0
    code, out, _ = adl("verify", p, "--min-entries", 22)
    assert code == 1
    assert "FAIL" in out and "too few entries: 21 verified, expected at least 22 (ledger truncated or wiped)" in out

    p.write_bytes(b"")  # wiped
    code, out, _ = adl("verify", p, "--min-entries", 1)
    assert code == 1 and "too few entries: 0 verified" in out
    code, out, _ = adl("verify", p, "--expect-head", "a" * 64)
    assert code == 1 and "found (empty)" in out


def test_expectations_fail_on_broken_chain_and_bad_args(tmp_path):
    p = make(tmp_path / "l.jsonl")
    head = head_of(p)
    tamper_seq(p, 21)
    code, out, _ = adl("verify", p, "--expect-head", head, "--min-entries", 1)
    assert code == 1 and "chain broken at seq 21" in out
    code, _, err = adl("verify", p, "--expect-head", "abc")
    assert code == 2 and "64-character hex" in err
    code, _, err = adl("verify", p, "--min-entries", -1)
    assert code == 2


def test_empty_ledger_is_visibly_empty(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_bytes(b"")
    code, out, _ = adl("verify", p)
    assert code == 0
    assert out.strip() == f"PASS {p}: chain verified (empty ledger, 0 entries), head (empty)"
    code, out, _ = adl("stats", p)
    assert code == 0 and "PASS verified (empty ledger, 0 entries)" in out
    o = tmp_path / "r.html"
    code, out, _ = adl("report", p, "-o", o)
    assert code == 0 and "head (empty)" in out
    assert "CHAIN VERIFIED: empty ledger, 0 entries, head (empty)" in o.read_text()


# ---------------------------------------------------------------- Sev-3/4


def test_directory_argument_message(tmp_path):
    for cmd in (("verify",), ("stats",), ("report", "-o", tmp_path / "r.html")):
        code, _, err = adl(cmd[0], tmp_path, *cmd[1:])
        assert code == 2 and "is a directory, not a ledger file" in err


def test_stats_and_report_exit_1_on_broken_chain(tmp_path):
    p = make(tmp_path / "l.jsonl")
    assert adl("stats", p)[0] == 0
    tamper_seq(p, 6)
    code, out, _ = adl("stats", p)
    assert code == 1 and "FAIL broken at seq 6" in out
    code, out, _ = adl("stats", p, "--format", "json")
    assert code == 1 and json.loads(out)["chain"]["ok"] is False
    o = tmp_path / "r.html"
    code, out, _ = adl("report", p, "-o", o)
    assert code == 1 and out.startswith("FAIL ")
    assert 'data-broken-seq="6"' in o.read_text()  # report is still written


# ---------------------------------------------------------------- golden vectors


def test_golden_vector_verifies_to_published_head():
    result = verify_file(GOLDEN)
    assert result.ok and result.entries == 3 and result.head == GOLDEN_HEAD
    assert adl("verify", GOLDEN, "--expect-head", GOLDEN_HEAD, "--min-entries", 3)[0] == 0

    prev = "0" * 64  # independent recompute, straight from the README recipe
    for line in GOLDEN.read_bytes().splitlines():
        e = json.loads(line)
        body = {k: v for k, v in e.items() if k != "hash"}
        canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        assert hashlib.sha256((prev + canon).encode()).hexdigest() == e["hash"]
        prev = e["hash"]
    assert prev == GOLDEN_HEAD


def test_readme_golden_block_matches_fixture_bytes():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    block = re.search(r"<!-- golden-ledger:start -->\n```\n(.*?)```\n<!-- golden-ledger:end -->", readme, re.S)
    assert block, "golden ledger block missing from README"
    assert block.group(1).encode("ascii") == GOLDEN.read_bytes()
    assert GOLDEN_HEAD in readme
    assert len(GOLDEN.read_bytes()) == 1062
