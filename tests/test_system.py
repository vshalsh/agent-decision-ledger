"""System / adversarial tests for L-001 (test seat, L-002)."""

from __future__ import annotations

import json
import multiprocessing
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

from decision_ledger import InvalidEntryError, Ledger, LedgerCorruptError, LedgerLockedError, verify_file
from decision_ledger.report import render_report, write_report

KINDS = ["decision", "tool_call", "tool_result", "observation", "human_approval"]


def adl(*args):
    p = subprocess.run([sys.executable, "-m", "decision_ledger", *map(str, args)], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


# ---------- WCAG contrast ----------

def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def lum(h):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tokens_from_report(tmp_path):
    p = tmp_path / "l.jsonl"
    with Ledger.open(p, fsync=False) as led:
        led.record("decision", "a", {})
    html = render_report(p)
    m = re.search(r":root\{([^}]*)\}", html)
    return html, dict((kv.split(":")[0].lstrip("-"), kv.split(":")[1]) for kv in m.group(1).split(";"))


def css_rules(html):
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    css = css.split("@media print")[0]
    return dict((sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css))


def test_wcag_aa_text_pairs(tmp_path, capsys):
    html, tok = _tokens_from_report(tmp_path)
    t = {k: v for k, v in tok.items()}
    # text-colour / background pairs actually used by the stylesheet (see report CSS)
    pairs = [
        ("text", "surface"), ("text", "surface-alt"),
        ("text-soft", "surface"), ("text-soft", "surface-alt"),
        ("accent", "surface"), ("accent", "surface-alt"),
        ("ok", "ok-tint"), ("warn", "warn-tint"), ("danger", "danger-tint"),
        ("surface", "accent"),    # .k-decision badge
        ("surface", "text"),      # .k-human_approval badge
    ]
    report = []
    for fg, bg in pairs:
        r = ratio(t[fg], t[bg])
        report.append(f"{fg} {t[fg]} on {bg} {t[bg]}: {r:.2f}")
        assert r >= 4.5, f"{fg} on {bg} = {r:.2f} < 4.5"
    # tokens placed on the page-level surface for badges with var(--surface) background
    for fg in ("accent", "text", "text-soft", "ok", "warn", "danger"):
        assert ratio(t[fg], t["surface"]) >= 4.5
    # print stylesheet pairs
    pr = {"surface": "#E2E8F0", "text": "#0F172A", "text-soft": "#1E293B", "accent": "#0F172A"}
    for fg in ("text", "text-soft", "accent"):
        r = ratio(pr[fg], pr["surface"])
        report.append(f"print {fg} {pr[fg]} on {pr['surface']}: {r:.2f}")
        assert r >= 4.5
    # every hex in the stylesheet is a design token or a print colour
    allowed = {v.upper() for v in t.values()} | {"#E2E8F0", "#0F172A", "#1E293B", "#94A3B8"}
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    assert {h.upper() for h in re.findall(r"#[0-9A-Fa-f]{6}\b", css)} <= allowed
    print("\n".join(report))
    with capsys.disabled():
        print("\nWCAG:\n  " + "\n  ".join(report))


# ---------- ledger helpers ----------

def mk(path, n=5, **kw):
    with Ledger.open(path, fsync=False, **kw) as led:
        for i in range(n):
            led.record(KINDS[i % 5], f"actor{i % 2}", {"i": i})
    return path


# ---------- acceptance 1 ----------

def test_ac1_every_byte_flip_in_mixed_200_ledger_names_exact_seq(tmp_path):
    # exhaustive over a sample stride: every 7th byte across a 200-entry ledger with all kinds
    p = mk(tmp_path / "l.jsonl", 200)
    assert verify_file(p).ok
    raw = p.read_bytes()
    starts, off = [], 0
    for ln in raw.split(b"\n")[:-1]:
        starts.append((off, off + len(ln) + 1))
        off += len(ln) + 1
    m = tmp_path / "m.jsonl"
    checked = 0
    for pos in range(0, len(raw), 7):
        b = bytearray(raw)
        b[pos] ^= 0x01
        m.write_bytes(bytes(b))
        res = verify_file(m)
        seq = next(i for i, (a, z) in enumerate(starts, 1) if a <= pos < z)
        assert not res.ok and res.first_break.seq == seq, (pos, res.first_break)
        checked += 1
    assert checked > 1000


def test_truncation_deletion_duplication_reorder(tmp_path):
    p = mk(tmp_path / "l.jsonl", 10)
    lines = p.read_bytes().splitlines(keepends=True)
    cases = {
        "delete3": (lines[:2] + lines[3:], 3),
        "dup": (lines[:3] + [lines[2]] + lines[3:], 4),
        "swap": (lines[:2] + [lines[3], lines[2]] + lines[4:], 3),
        "trunc_nonl": ([*lines[:4], lines[4].rstrip(b"\n")], 5),
    }
    for name, (ls, seq) in cases.items():
        m = tmp_path / f"{name}.jsonl"
        m.write_bytes(b"".join(ls))
        r = verify_file(m)
        assert not r.ok and r.first_break.seq == seq, name


# ---------- acceptance 2: report ----------

def test_report_all_kinds_self_contained(tmp_path):
    p = mk(tmp_path / "l.jsonl", 10)
    out = tmp_path / "r.html"
    write_report(p, out)
    html = out.read_text(encoding="utf-8")
    assert not re.search(r"https?://", html, re.I)
    assert "<link" not in html.lower()
    assert not re.search(r"\bsrc\s*=", html, re.I)
    assert not re.search(r"@import|url\(", html, re.I)
    for k in KINDS:
        assert f'data-kind="{k}"' in html
        assert f"k-{k}" in html


def test_report_escapes_hostile_content(tmp_path):
    p = tmp_path / "l.jsonl"
    evil = "</script><script>alert(1)</script><img src=x onerror=alert(1)>"
    with Ledger.open(p, fsync=False) as led:
        led.record("decision", evil, {evil: evil, "n": [evil]}, grounding=[{"source": evil, "quote": evil}])
    html = render_report(p)
    assert evil not in html
    assert html.count("<script") == 2  # summary json + filter js; '<' is <-escaped inside the json block (D2)
    assert html.count("</script>") == 2
    class P(HTMLParser):
        def __init__(s):
            super().__init__(); s.tags = []
        def handle_starttag(s, tag, a):
            s.tags.append(tag)
    pp = P(); pp.feed(html)
    assert "img" not in pp.tags


def test_report_broken_chain_flags(tmp_path):
    p = mk(tmp_path / "l.jsonl", 8)
    lines = p.read_bytes().splitlines(keepends=True)
    lines[3] = lines[3].replace(b'"i":3', b'"i":9')
    p.write_bytes(b"".join(lines))
    html = render_report(p)
    assert 'data-chain="tampered"' in html and 'data-broken-seq="4"' in html
    assert html.count("UNVERIFIED") >= 4


# ---------- edge cases ----------

def test_empty_ledger_file(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_bytes(b"")
    assert adl("verify", p)[0] == 0
    code, out, _ = adl("verify", p, "--format", "json")
    assert code == 0 and json.loads(out)["ok"] is True
    code, out, _ = adl("stats", p, "--format", "json")
    assert code == 0 and json.loads(out)["entries"] == 0
    assert adl("stats", p)[0] == 0
    o = tmp_path / "r.html"
    code, _, _ = adl("report", p, "-o", o)
    assert code == 0 and "empty" in o.read_text()


def test_only_malformed_first_line(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_bytes(b"this is not json\n")
    code, out, _ = adl("verify", p, "--format", "json")
    j = json.loads(out)
    assert code == 1 and j["first_break"]["seq"] == 1 and j["first_break"]["reason"] in ("malformed line", "malformed_line")
    assert adl("stats", p)[0] in (0, 1)
    assert adl("stats", p, "--format", "json")[0] in (0, 1)
    o = tmp_path / "r.html"
    code, _, err = adl("report", p, "-o", o)
    assert code == 1 and "Traceback" not in err and "CHAIN TAMPERED" in o.read_text()  # L-004: exit 1 on broken chain
    with pytest.raises(LedgerCorruptError):
        Ledger.open(p)


@pytest.mark.parametrize("blob", [b"\n", b"\n\n", b"{", b"\xff\xfe\n", b"[]\n", b"null\n", b"\x00\n"])
def test_garbage_files_never_crash(tmp_path, blob):
    p = tmp_path / "g.jsonl"
    p.write_bytes(blob)
    for cmd in (["verify"], ["stats"], ["report"]):
        extra = ["-o", str(tmp_path / "r.html")] if cmd == ["report"] else []
        code, _, err = adl(*cmd, p, *extra)
        assert "Traceback" not in err, (blob, cmd, err)
        assert code in (0, 1, 2)
    assert adl("verify", p)[0] == 1


def test_missing_file_exit_2(tmp_path):
    code, _, err = adl("verify", tmp_path / "nope.jsonl")
    assert code == 2 and "Traceback" not in err


def test_huge_payload_1mb(tmp_path):
    p = tmp_path / "h.jsonl"
    big = "x" * (1024 * 1024)
    with Ledger.open(p, fsync=False) as led:
        led.record("observation", "a", {"blob": big})
        led.record("decision", "a", {"after": 1})
    with Ledger.open(p, fsync=False) as led:  # reopen resumes after a >64k tail line
        led.record("decision", "a", {"after": 2})
    assert verify_file(p).ok and verify_file(p).entries == 3
    o = tmp_path / "r.html"
    assert adl("report", p, "-o", o)[0] == 0
    assert adl("stats", p)[0] == 0


def test_unicode_control_chars_round_trip(tmp_path):
    """Everything except the lone surrogate (see test_lone_surrogate_report below)."""
    p = tmp_path / "u.jsonl"
    actor = "agent:é中\U0001F600"
    payload = {"emoji": "\U0001F600\U0001F468‍\U0001F469", "ctrl": "a\x00b\x1f\x7f  \r\n\t",
               "rtl": "‮abc", "kéy": 1, "f": [1.5, -0.0, 1e300, 10**30]}
    with Ledger.open(p, fsync=False) as led:
        e = led.record("tool_call", actor, payload, grounding=[{"source": "sé", "quote": "\U0001F600"}])
        led.record("decision", "b", {"ok": 1})
    assert verify_file(p).ok
    assert p.read_bytes().isascii()
    back = json.loads(p.read_text().splitlines()[0])
    assert back["actor"] == actor and back["payload"]["emoji"] == payload["emoji"]
    assert back["payload"]["ctrl"] == payload["ctrl"]
    o = tmp_path / "r.html"
    assert adl("report", p, "-o", o)[0] == 0
    assert adl("stats", p, "--format", "json")[0] == 0


def test_lone_surrogate_report(tmp_path):
    p = tmp_path / "s.jsonl"
    with Ledger.open(p, fsync=False) as led:
        led.record("tool_call", "a", {"lone": "\ud800"})
    assert verify_file(p).ok
    code, _, err = adl("report", p, "-o", tmp_path / "r.html")
    assert code == 0 and "Traceback" not in err


def test_summary_json_block_has_no_html_comment_open(tmp_path):
    p = tmp_path / "c.jsonl"
    with Ledger.open(p, fsync=False) as led:
        led.record("decision", "<!--<script>", {"k": "v"})
    html = render_report(p)
    block = re.search(r'id="adl-summary">(.*?)</script>', html, re.S).group(1)
    assert "<!--" not in block


def test_nested_grounding_citations(tmp_path):
    p = tmp_path / "g.jsonl"
    cite = {"source": "kb/1", "locator": {"page": 3, "span": [10, 20]}, "extra": {"a": [{"b": None}]},
            "score": 0.97, "quote": "q"}
    with Ledger.open(p, fsync=False) as led:
        led.record("decision", "a", {"x": 1}, grounding=[cite, {"source": "kb/2"}])
    assert verify_file(p).ok
    stored = json.loads(p.read_text())
    assert stored["grounding"][0] == cite
    assert adl("report", p, "-o", tmp_path / "r.html")[0] == 0


@pytest.mark.parametrize("kind", ["Decision", "DECISION", "decision ", "", "log", "tool", None, 1, ["decision"], "human-approval"])
def test_bad_kinds_rejected_and_nothing_written(tmp_path, kind):
    p = tmp_path / "k.jsonl"
    with Ledger.open(p, fsync=False) as led:
        led.record("decision", "a", {})
        before = p.read_bytes()
        with pytest.raises(InvalidEntryError):
            led.record(kind, "a", {})
        assert p.read_bytes() == before
        led.record("observation", "a", {})  # ledger still usable
    assert verify_file(p).ok


@pytest.mark.parametrize("actor,payload,grounding", [
    ("", {}, None), (None, {}, None), (5, {}, None), ("a", [], None), ("a", "x", None),
    ("a", {"s": {1, 2}}, None), ("a", {"f": float("nan")}, None), ("a", {"f": float("inf")}, None),
    ("a", {}, "x"), ("a", {}, [{}]), ("a", {}, [{"source": ""}]), ("a", {}, ["s"]), ("a", {}, [{"source": 3}]),
])
def test_invalid_entries_rejected(tmp_path, actor, payload, grounding):
    p = tmp_path / "i.jsonl"
    with Ledger.open(p, fsync=False) as led:
        led.record("decision", "a", {})
        before = p.read_bytes()
        with pytest.raises(InvalidEntryError):
            led.record("decision", actor, payload, grounding=grounding)
        assert p.read_bytes() == before


def test_clock_injection_deterministic(tmp_path):
    base = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
    ticks = iter(base + timedelta(seconds=i) for i in range(100))
    p1, p2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    for p in (p1, p2):
        t = iter(base + timedelta(seconds=i) for i in range(100))
        with Ledger.open(p, clock=lambda: next(t), fsync=False) as led:
            for i in range(5):
                led.record(KINDS[i], "a", {"i": i})
    assert p1.read_bytes() == p2.read_bytes()  # byte-identical => fully deterministic
    first = json.loads(p1.read_text().splitlines()[0])
    assert first["ts"] == "2026-01-02T03:04:05.123456Z"
    # non-UTC aware datetimes normalise to UTC; naive rejected
    tz = timezone(timedelta(hours=5, minutes=30))
    q = tmp_path / "c.jsonl"
    with Ledger.open(q, clock=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=tz), fsync=False) as led:
        assert led.record("decision", "a", {})["ts"] == "2026-01-01T06:30:00.000000Z"
    with Ledger.open(q, clock=lambda: datetime(2026, 1, 1), fsync=False) as led:
        with pytest.raises(InvalidEntryError):
            led.record("decision", "a", {})
    assert verify_file(q).ok


def test_json_format_valid_green_and_broken(tmp_path):
    p = mk(tmp_path / "l.jsonl", 6)
    for sub in ("verify", "stats"):
        code, out, _ = adl(sub, p, "--format", "json")
        assert code == 0 and isinstance(json.loads(out), dict) and "\x1b" not in out
    lines = p.read_bytes().splitlines(keepends=True)
    lines[2] = lines[2].replace(b'"i":2', b'"i":7')
    p.write_bytes(b"".join(lines))
    code, out, _ = adl("verify", p, "--format", "json")
    j = json.loads(out)
    assert code == 1 and j["ok"] is False and j["first_break"]["seq"] == 3
    code, out, _ = adl("stats", p, "--format", "json")
    assert json.loads(out)["chain"]["ok"] is False


# ---------- acceptance 3: concurrency ----------

def _worker(args):
    path, wid, n = args
    for i in range(n):
        with Ledger.open(path, fsync=False, lock_timeout=60) as led:
            led.record(KINDS[i % 5], f"worker:{wid}", {"w": wid, "i": i})


def _worker_shared(args):
    path, wid, n = args
    with Ledger.open(path, fsync=False, lock_timeout=60) as led:
        for i in range(n):
            led.record(KINDS[i % 5], f"worker:{wid}", {"w": wid, "i": i})


@pytest.mark.parametrize("fn", [_worker, _worker_shared])
def test_ac3_concurrent_8x25_single_verified_chain(tmp_path, fn):
    p = tmp_path / "c.jsonl"
    p.touch()
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(8) as pool:
        pool.map(fn, [(str(p), w, 25) for w in range(8)])
    r = verify_file(p)
    assert r.ok and r.entries == 200
    entries = [json.loads(x) for x in p.read_text().splitlines()]
    assert [e["seq"] for e in entries] == list(range(1, 201))
    assert len({e["hash"] for e in entries}) == 200
    per = {}
    for e in entries:
        per.setdefault(e["actor"], []).append(e["payload"]["i"])
    assert all(v == list(range(25)) for v in per.values()) and len(per) == 8


def _hold(path, ready, release):
    import fcntl
    fd = open(path, "ab")
    fcntl.flock(fd, fcntl.LOCK_EX)
    ready.set()
    release.wait(30)


def test_ac3_lock_held_gives_clear_error_no_write(tmp_path):
    p = mk(tmp_path / "l.jsonl", 3)
    before = p.read_bytes()
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(str(p), ready, release))
    led = Ledger.open(p, lock_timeout=0.3, fsync=False)
    proc.start()
    try:
        assert ready.wait(30)
        with led:
            with pytest.raises(LedgerLockedError) as ei:
                led.record("decision", "a", {})
        assert "lock" in str(ei.value).lower() and "Nothing was written" in str(ei.value)
        assert p.read_bytes() == before
    finally:
        release.set()
        proc.join(10)
    with Ledger.open(p, fsync=False) as led:
        led.record("decision", "a", {})
    assert verify_file(p).ok and verify_file(p).entries == 4


def test_two_handles_same_process_do_not_fork(tmp_path):
    p = tmp_path / "l.jsonl"
    a, b = Ledger.open(p, fsync=False), Ledger.open(p, fsync=False)
    for i in range(10):
        (a if i % 2 else b).record("decision", "x", {"i": i})
    a.close(); b.close()
    assert verify_file(p).ok and verify_file(p).entries == 10


def test_resume_refuses_corrupt_tail(tmp_path):
    p = mk(tmp_path / "l.jsonl", 4)
    p.write_bytes(p.read_bytes()[:-5])  # partial last line
    with pytest.raises(LedgerCorruptError):
        Ledger.open(p, fsync=False).record("decision", "a", {})
