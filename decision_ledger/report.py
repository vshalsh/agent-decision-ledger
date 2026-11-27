"""Self-contained HTML audit report (no external assets; palette from DESIGN_TOKENS.md)."""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from . import __version__
from .chain import KINDS
from .stats import LedgerStats, compute_stats, format_span, parse_ts
from .verify import VerifyResult, verify_file

# Palette tokens - DESIGN_TOKENS.md "HTML report palette". Do not add colours here.
TOKENS = {
    "surface": "#0F172A",
    "surface-alt": "#1E293B",
    "text": "#E2E8F0",
    "text-soft": "#94A3B8",
    "accent": "#38BDF8",
    "ok": "#4ADE80",
    "ok-tint": "#052E16",
    "warn": "#FACC15",
    "warn-tint": "#422006",
    "danger": "#F87171",
    "danger-tint": "#450A0A",
}

KIND_LABELS = {
    "decision": "Decision",
    "tool_call": "Tool call",
    "tool_result": "Tool result",
    "observation": "Observation",
    "human_approval": "Human approval",
}

_CSS = """
:root{%(vars)s}
*{box-sizing:border-box}
html{background:var(--surface)}
body{margin:0;background:var(--surface);color:var(--text);
 font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.5rem;margin:0 0 4px}
h2{font-size:1.15rem;margin:32px 0 12px}
h3{font-size:.95rem;margin:28px 0 10px;color:var(--text-soft);font-weight:600;
 border-bottom:1px solid var(--surface-alt);padding-bottom:6px}
code,pre,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;font-size:.85em}
.meta{color:var(--text-soft);margin:0}
.banner{margin:20px 0;padding:14px 18px;border-radius:8px;border:2px solid;font-weight:600}
.banner p{margin:4px 0 0;font-weight:400}
.banner.ok{color:var(--ok);background:var(--ok-tint);border-color:var(--ok)}
.banner.danger{color:var(--danger);background:var(--danger-tint);border-color:var(--danger)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{background:var(--surface-alt);border-radius:8px;padding:14px 16px}
.card .big{font-size:1.6rem;font-weight:700;display:block}
table{border-collapse:collapse;width:100%%}
th,td{text-align:left;padding:4px 8px 4px 0;vertical-align:top}
th{color:var(--text-soft);font-weight:600}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid;font-size:.78rem;
 font-weight:600;letter-spacing:.02em;white-space:nowrap;background:var(--surface)}
.k-decision{background:var(--accent);color:var(--surface);border-color:var(--accent)}
.k-human_approval{background:var(--text);color:var(--surface);border-color:var(--text)}
.k-tool_call{color:var(--accent);border-color:var(--accent)}
.k-tool_result{color:var(--text);border-color:var(--text-soft)}
.k-observation{color:var(--text-soft);border-color:var(--text-soft)}
.st-danger{color:var(--danger);background:var(--danger-tint);border-color:var(--danger)}
.st-warn{color:var(--warn);background:var(--warn-tint);border-color:var(--warn)}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 8px}
.filters label{display:inline-flex;align-items:center;gap:6px;cursor:pointer}
.entry{background:var(--surface-alt);border-radius:8px;padding:12px 16px;margin:0 0 10px;
 border-left:4px solid var(--surface-alt)}
.entry.broken{border-left-color:var(--danger)}
.entry.unverified{border-left-color:var(--warn)}
.entry header{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.seq{font-weight:700}
.actor{font-weight:600}
.ts{color:var(--text-soft)}
dl.payload{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:10px 0 0}
dl.payload dt{color:var(--text-soft)}
dl.payload dd{margin:0;overflow-wrap:anywhere}
.grounding{margin:10px 0 0;padding:8px 12px;border-left:2px solid var(--accent);background:var(--surface)}
.grounding h4{margin:0 0 4px;font-size:.8rem;color:var(--accent);text-transform:uppercase;letter-spacing:.06em}
.grounding ol{margin:0;padding-left:20px}
.grounding li{margin:4px 0}
.grounding q{display:block;color:var(--text-soft);font-style:italic;margin-top:2px}
.grounding q::before,.grounding q::after{content:""}
.hashes{margin:8px 0 0;color:var(--text-soft);font-size:.8rem;overflow-wrap:anywhere}
.raw{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text-soft);margin:8px 0 0}
a{color:var(--accent)}
footer{margin-top:40px;color:var(--text-soft);font-size:.85rem}
.hidden{display:none}
@media print{
 :root{--surface:#E2E8F0;--surface-alt:#E2E8F0;--text:#0F172A;--text-soft:#1E293B;--accent:#0F172A}
 *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
 .filters{display:none}
 .entry{break-inside:avoid;border:1px solid #94A3B8}
}
"""

_JS = """
(function(){
 var boxes=document.querySelectorAll('.filters input[type=checkbox]');
 function apply(){
  var on={};boxes.forEach(function(b){on[b.value]=b.checked;});
  document.querySelectorAll('article.entry').forEach(function(a){
   a.classList.toggle('hidden',on[a.dataset.kind]===false);});
 }
 boxes.forEach(function(b){b.addEventListener('change',apply);});
})();
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _value_html(value: Any) -> str:
    if isinstance(value, str):
        return _e(value)
    return f"<code>{_e(json.dumps(value, ensure_ascii=False, sort_keys=True))}</code>"


def _citation_html(cite: dict) -> str:
    parts = [f"<code>{_e(cite.get('source', ''))}</code>"]
    if cite.get("locator") not in (None, ""):
        parts.append(f" <span class=\"ts\">{_e(cite['locator'])}</span>")
    extra = {k: v for k, v in cite.items() if k not in ("source", "locator", "quote")}
    for key in sorted(extra):
        parts.append(f" <span class=\"ts\">{_e(key)}=</span>{_value_html(extra[key])}")
    if cite.get("quote") not in (None, ""):
        parts.append(f"<q>{_e(cite['quote'])}</q>")
    return "<li class=\"citation\">" + "".join(parts) + "</li>"


def _entry_html(entry: dict, status: Optional[str], break_label: str) -> str:
    kind = entry["kind"]
    classes = "entry" + {"broken": " broken", "unverified": " unverified"}.get(status or "", "")
    head = [
        f"<span class=\"seq mono\">#{_e(entry['seq'])}</span>",
        f"<span class=\"badge k-{_e(kind)}\">{_e(KIND_LABELS.get(kind, kind))}</span>",
        f"<span class=\"actor\">{_e(entry['actor'])}</span>",
        f"<time class=\"ts mono\" datetime=\"{_e(entry['ts'])}\">{_e(entry['ts'])}</time>",
    ]
    if status == "broken":
        head.append(f"<span class=\"badge st-danger\">CHAIN BROKEN: {_e(break_label)}</span>")
    elif status == "unverified":
        head.append("<span class=\"badge st-warn\">UNVERIFIED (after break)</span>")
    out = [
        f"<article class=\"{classes}\" data-seq=\"{_e(entry['seq'])}\" data-kind=\"{_e(kind)}\" "
        f"data-actor=\"{_e(entry['actor'])}\" data-ts=\"{_e(entry['ts'])}\">",
        "<header>" + "".join(head) + "</header>",
    ]
    payload = entry["payload"]
    if payload:
        rows = "".join(f"<dt>{_e(k)}</dt><dd>{_value_html(payload[k])}</dd>" for k in sorted(payload))
        out.append(f"<dl class=\"payload\">{rows}</dl>")
    grounding = entry["grounding"]
    if grounding:
        items = "".join(_citation_html(c) for c in grounding)
        out.append(
            f"<section class=\"grounding\"><h4>Grounding ({len(grounding)})</h4><ol>{items}</ol></section>"
        )
    out.append(
        f"<p class=\"hashes mono\">prev <span title=\"{_e(entry['prev_hash'])}\">{_e(entry['prev_hash'][:16])}</span>"
        f" &rarr; hash <span title=\"{_e(entry['hash'])}\">{_e(entry['hash'][:16])}</span></p>"
    )
    out.append("</article>")
    return "".join(out)


def _bucket(ts: str) -> str:
    parsed = parse_ts(ts)
    if parsed is None:
        return "Unparseable timestamp"
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:00 UTC")


def render_report(
    path: str | os.PathLike,
    *,
    title: str = "Decision ledger audit report",
    result: Optional[VerifyResult] = None,
    generated_at: Optional[datetime] = None,
) -> str:
    """Return the full HTML document for the ledger at ``path``."""
    path = os.fspath(path)
    if result is None or not result.parsed:
        result = verify_file(path, collect=True)
    stats: LedgerStats = compute_stats(path, result)
    brk = result.first_break
    generated = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

    # --- banner
    if result.ok:
        banner = (
            "<div class=\"banner ok\" role=\"status\" data-chain=\"verified\">"
            f"CHAIN VERIFIED: all {result.entries} entries recompute to head "
            f"<span class=\"mono\" title=\"{_e(result.head)}\">{_e(result.head[:16])}</span>"
            "<p>Every hash was recomputed from the raw ledger bytes; sequence is contiguous "
            "and every entry links to its predecessor.</p></div>"
        )
    else:
        banner = (
            f"<div class=\"banner danger\" role=\"alert\" data-chain=\"tampered\" data-broken-seq=\"{brk.seq}\">"
            f"CHAIN TAMPERED: broken at seq {brk.seq} (line {brk.line}): {_e(brk.label)}"
            f"<p class=\"mono\">{_e(brk.detail)}</p>"
            + (
                f"<p>Entries 1&ndash;{brk.seq - 1} verified; seq {brk.seq} and everything after it "
                "cannot be trusted.</p></div>"
                if brk.seq > 1
                else "<p>No entry could be verified; the whole ledger is untrusted.</p></div>"
            )
        )

    # --- summary
    kind_rows = "".join(
        f"<tr><td><span class=\"badge k-{k}\">{KIND_LABELS[k]}</span></td>"
        f"<td class=\"n\" data-kind-count=\"{k}\">{stats.by_kind[k]}</td></tr>"
        for k in KINDS
    )
    actor_rows = "".join(
        f"<tr><td>{_e(a)}</td><td class=\"n\" data-actor-count=\"{_e(a)}\">{n}</td></tr>"
        for a, n in stats.by_actor.items()
    )
    summary = (
        "<div class=\"grid\">"
        f"<div class=\"card\"><span class=\"big\" data-total-entries=\"{stats.entries}\">{stats.entries}</span>entries</div>"
        f"<div class=\"card\"><span class=\"big\">{len(stats.by_actor)}</span>actors</div>"
        f"<div class=\"card\"><span class=\"big\">{_e(format_span(stats.span_seconds))}</span>"
        f"<span class=\"mono ts\">{_e(stats.first_ts or 'n/a')}<br>&rarr; {_e(stats.last_ts or 'n/a')}</span></div>"
        "</div>"
        "<div class=\"grid\" style=\"margin-top:12px\">"
        f"<div class=\"card\"><table><thead><tr><th>Kind</th><th class=\"n\">Entries</th></tr></thead>"
        f"<tbody>{kind_rows}</tbody></table></div>"
        f"<div class=\"card\"><table><thead><tr><th>Actor</th><th class=\"n\">Entries</th></tr></thead>"
        f"<tbody>{actor_rows}</tbody></table></div>"
        "</div>"
    )

    # --- timeline
    def status_of(seq: int) -> Optional[str]:
        if brk is None:
            return None
        if seq == brk.seq:
            return "broken"
        return "unverified" if seq > brk.seq else None

    entries = [p.entry for p in result.parsed if p.entry is not None]
    bad_lines = [p for p in result.parsed if p.entry is None]
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    ordered = sorted(entries, key=lambda e: (parse_ts(e["ts"]) or far_future, e["seq"]))
    timeline: list[str] = []
    current = None
    for entry in ordered:
        bucket = _bucket(entry["ts"])
        if bucket != current:
            timeline.append(f"<h3>{_e(bucket)}</h3>")
            current = bucket
        timeline.append(_entry_html(entry, status_of(entry["seq"]), brk.label if brk else ""))

    if bad_lines:
        raw_by_line = _raw_lines(path, {p.line for p in bad_lines})
        timeline.append("<h3>Malformed lines</h3>")
        for p in bad_lines:
            raw = raw_by_line.get(p.line, "")
            snippet = raw if len(raw) <= 400 else raw[:400] + "..."
            label = "CHAIN BROKEN" if brk and p.line == brk.line else "UNVERIFIED"
            badge = "st-danger" if label == "CHAIN BROKEN" else "st-warn"
            cls = "broken" if label == "CHAIN BROKEN" else "unverified"
            timeline.append(
                f"<article class=\"entry {cls} malformed\" data-line=\"{p.line}\"><header>"
                f"<span class=\"seq mono\">line {p.line}</span>"
                f"<span class=\"badge {badge}\">{label}: malformed line</span></header>"
                f"<p>{_e(p.error)}</p><pre class=\"raw\">{_e(snippet)}</pre></article>"
            )

    filters = "".join(
        f"<label><input type=\"checkbox\" value=\"{k}\" checked> "
        f"<span class=\"badge k-{k}\">{KIND_LABELS[k]}</span></label>"
        for k in KINDS
    )

    summary_json = json.dumps(
        {
            "entries": stats.entries,
            "by_kind": stats.by_kind,
            "by_actor": stats.by_actor,
            "first_ts": stats.first_ts,
            "last_ts": stats.last_ts,
            "chain": result.to_dict(),
        },
        sort_keys=True,
    ).replace("</", "<\\/")

    css_vars = ";".join(f"--{k}:{v}" for k, v in TOKENS.items())
    doc = [
        "<!DOCTYPE html>",
        "<html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>{_e(title)}</title>",
        f"<style>{_CSS % {'vars': css_vars}}</style>",
        "</head><body><main>",
        f"<h1>{_e(title)}</h1>",
        f"<p class=\"meta\">Ledger <code>{_e(os.path.basename(path))}</code> &middot; generated "
        f"{_e(generated.strftime('%Y-%m-%dT%H:%M:%SZ'))} &middot; adl {_e(__version__)}</p>",
        banner,
        "<h2>Summary</h2>",
        summary,
        "<h2>Timeline</h2>",
        f"<div class=\"filters\" aria-label=\"Filter by kind\">{filters}</div>",
        "".join(timeline) or "<p class=\"meta\">The ledger is empty.</p>",
        "<footer>Self-contained report: no external assets. Hashes: sha256 over prev_hash + canonical JSON "
        "of each entry without its hash field.</footer>",
        f"<script type=\"application/json\" id=\"adl-summary\">{summary_json}</script>",
        f"<script>{_JS}</script>",
        "</main></body></html>",
    ]
    return "\n".join(doc) + "\n"


def _raw_lines(path: str, wanted: set) -> dict:
    out = {}
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, start=1):
            if n in wanted:
                out[n] = raw.rstrip(b"\n").decode("ascii", errors="replace")
    return out


def write_report(path: str | os.PathLike, output: str | os.PathLike, **kwargs: Any) -> VerifyResult:
    """Render the report for ``path`` into ``output``; return the chain verification result."""
    result = verify_file(path, collect=True)
    document = render_report(path, result=result, **kwargs)
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(document)
    return result
