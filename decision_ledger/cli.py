"""The ``adl`` command-line interface: verify, report, stats.

Exit codes: 0 success / chain verified, 1 chain broken (``verify`` only), 2 usage or I/O error.
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
from .verify import VerifyResult, verify_file

EXIT_OK, EXIT_BROKEN, EXIT_ERROR = 0, 1, 2


def _status_line(result: VerifyResult, style: Style) -> str:
    name = result.path
    if result.ok:
        return (
            f"{style.ok('PASS')} {name}: chain verified, {result.entries} entries, "
            f"head {result.head[:16]}"
        )
    return f"{style.fail('FAIL')} {name}: {result.first_break.message()}"


def _cmd_verify(args: argparse.Namespace) -> int:
    result = verify_file(args.ledger)
    if args.format == "json":
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        style = Style(use_color(sys.stdout))
        print(_status_line(result, style))
        if not result.ok:
            print(style.dim(f"  {result.entries} entries before seq {result.first_break.seq} verified; "
                            "everything from that seq on is untrusted."))
    return EXIT_OK if result.ok else EXIT_BROKEN


def _cmd_report(args: argparse.Namespace) -> int:
    from .report import write_report  # local import keeps 'verify' startup minimal

    result = write_report(args.ledger, args.output, title=args.title)
    style = Style(use_color(sys.stdout))
    print(_status_line(result, style))
    print(f"{style.ok('INFO')} report written to {args.output}")
    return EXIT_OK


def _cmd_stats(args: argparse.Namespace) -> int:
    stats = compute_stats(args.ledger)
    if args.format == "json":
        print(json.dumps(stats.to_dict(), sort_keys=True))
        return EXIT_OK
    style = Style(use_color(sys.stdout))
    v = stats.verify
    if v.ok:
        chain = f"{style.ok('PASS')} verified ({v.entries} entries)"
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
    return EXIT_OK


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
