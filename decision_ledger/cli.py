"""The ``adl`` command-line interface: verify, report, stats.

Exit codes (all commands): 0 chain verified (and, for ``verify``, every --expect-head /
--min-entries expectation met), 1 chain broken or expectation failed, 2 usage or I/O error.
``report`` still writes the HTML when the chain is broken, then exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from . import __version__
from ._term import Style, use_color
from .stats import compute_stats, format_span
from .chain import is_hex64
from .verify import VerifyResult, verify_file

EXIT_OK, EXIT_BROKEN, EXIT_ERROR = 0, 1, 2

EMPTY_HEAD = "(empty)"


def _head(result: VerifyResult, n: int = 16) -> str:
    return result.head[:n] if result.entries else EMPTY_HEAD


def _status_line(result: VerifyResult, style: Style) -> str:
    name = result.path
    if result.ok:
        if result.entries == 0:
            return f"{style.ok('PASS')} {name}: chain verified (empty ledger, 0 entries), head {EMPTY_HEAD}"
        return f"{style.ok('PASS')} {name}: chain verified, {result.entries} entries, head {_head(result)}"
    return f"{style.fail('FAIL')} {name}: {result.first_break.message()}"


def _expectations(args: argparse.Namespace, result: VerifyResult) -> list[dict]:
    """Checks against externally recorded state; these catch line-boundary truncation and wipes."""
    checks = []
    if args.expect_head is not None:
        found = result.head if result.entries else None
        checks.append({
            "check": "expect_head",
            "ok": result.ok and result.head == args.expect_head,
            "expected": args.expect_head,
            "found": found,
            "message": (
                f"head mismatch: expected {args.expect_head[:16]}..., found "
                f"{found[:16] + '...' if found else EMPTY_HEAD} after {result.entries} verified entries "
                "(ledger truncated, rolled back or replaced)"
            ),
        })
    if args.min_entries is not None:
        checks.append({
            "check": "min_entries",
            "ok": result.ok and result.entries >= args.min_entries,
            "expected": args.min_entries,
            "found": result.entries,
            "message": (
                f"too few entries: {result.entries} verified, expected at least {args.min_entries} "
                "(ledger truncated or wiped)"
            ),
        })
    return checks


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.expect_head is not None:
        args.expect_head = args.expect_head.strip().lower()
        if not is_hex64(args.expect_head):
            print("ERROR --expect-head must be a full 64-character hex sha256 head hash", file=sys.stderr)
            return EXIT_ERROR
    if args.min_entries is not None and args.min_entries < 0:
        print("ERROR --min-entries must be >= 0", file=sys.stderr)
        return EXIT_ERROR
    result = verify_file(args.ledger)
    checks = _expectations(args, result)
    failed = [c for c in checks if not c["ok"]]
    ok = result.ok and not failed
    if args.format == "json":
        doc = result.to_dict()
        if checks:
            doc["checks"] = [{k: v for k, v in c.items() if k != "message"} for c in checks]
            doc["ok"] = ok
            if result.ok and failed:
                doc["status"] = "expectation_failed"
        print(json.dumps(doc, sort_keys=True))
        return EXIT_OK if ok else EXIT_BROKEN

    style = Style(use_color(sys.stdout))
    if not result.ok:
        print(_status_line(result, style))
        print(style.dim(f"  {result.entries} entries before seq {result.first_break.seq} verified; "
                        "everything from that seq on is untrusted."))
    elif failed:
        for c in failed:
            print(f"{style.fail('FAIL')} {result.path}: {c['message']}")
        print(style.dim(f"  the chain itself recomputes cleanly ({result.entries} entries, head "
                        f"{_head(result)}) but does not match the externally recorded expectation."))
    else:
        print(_status_line(result, style))
        for c in checks:
            what = "head matches" if c["check"] == "expect_head" else f"at least {c['expected']} entries"
            print(style.dim(f"  expectation met: {what}"))
    return EXIT_OK if ok else EXIT_BROKEN


def _cmd_report(args: argparse.Namespace) -> int:
    from .report import write_report  # local import keeps 'verify' startup minimal

    result = write_report(args.ledger, args.output, title=args.title)
    style = Style(use_color(sys.stdout))
    print(_status_line(result, style))
    print(f"{style.ok('INFO')} report written to {args.output}")
    return EXIT_OK if result.ok else EXIT_BROKEN


def _cmd_stats(args: argparse.Namespace) -> int:
    stats = compute_stats(args.ledger)
    if args.format == "json":
        print(json.dumps(stats.to_dict(), sort_keys=True))
        return EXIT_OK if stats.verify.ok else EXIT_BROKEN
    style = Style(use_color(sys.stdout))
    v = stats.verify
    if v.ok:
        chain = (f"{style.ok('PASS')} verified ({v.entries} entries)" if v.entries
                 else f"{style.ok('PASS')} verified (empty ledger, 0 entries)")
    else:
        chain = f"{style.fail('FAIL')} broken at seq {v.first_break.seq}: {v.first_break.label}"
    lines = [
        f"Ledger   {stats.path}",
        f"Chain    {chain}",
        f"Entries  {stats.entries}" + (f"  {style.warn('WARN')} {stats.malformed_lines} malformed line(s)"
                                       if stats.malformed_lines else ""),
        f"Span     {stats.first_ts or 'n/a'} -> {stats.last_ts or 'n/a'} ({format_span(stats.span_seconds)})",
        "",
        "By kind",
    ]
    lines += [f"  {k:<16}{n:>6}" for k, n in stats.by_kind.items()]
    lines += ["", "By actor"]
    width = max([16] + [len(a) + 2 for a in stats.by_actor])
    lines += [f"  {a:<{width}}{n:>6}" for a, n in stats.by_actor.items()] or [style.dim("  (none)")]
    print("\n".join(lines))
    return EXIT_OK if v.ok else EXIT_BROKEN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adl",
        description="agent-decision-ledger: verify, report on and summarise hash-chained agent ledgers.",
    )
    parser.add_argument("--version", action="version", version=f"adl {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("verify", help="recompute the whole hash chain; exit 1 on the first break")
    p.add_argument("ledger", help="path to a ledger .jsonl file")
    p.add_argument("--expect-head", metavar="HASH",
                   help="fail unless the verified chain ends at this externally recorded head hash")
    p.add_argument("--min-entries", metavar="N", type=int,
                   help="fail unless at least N entries verify")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("report", help="write a self-contained HTML audit report")
    p.add_argument("ledger", help="path to a ledger .jsonl file")
    p.add_argument("-o", "--output", required=True, help="output HTML path")
    p.add_argument("--title", default="Decision ledger audit report")
    p.set_defaults(func=_cmd_report)

    p = sub.add_parser("stats", help="counts by kind and actor, time span, chain status")
    p.add_argument("ledger", help="path to a ledger .jsonl file")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.set_defaults(func=_cmd_stats)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if os.path.isdir(args.ledger):
        print(f"ERROR {args.ledger}: is a directory, not a ledger file", file=sys.stderr)
        return EXIT_ERROR
    if not os.path.isfile(args.ledger):
        print(f"ERROR {args.ledger}: no such ledger file", file=sys.stderr)
        return EXIT_ERROR
    try:
        return args.func(args)
    except BrokenPipeError:  # pragma: no cover - e.g. `adl stats x | head`
        return EXIT_OK
    except OSError as exc:
        print(f"ERROR {exc.filename or args.ledger}: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
