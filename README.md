# agent-decision-ledger

A tamper-evident audit trail for AI agents, as a drop-in Python library and a CLI (`adl`).

Your agent records each decision, tool call, tool result, observation and human approval
into an append-only JSONL file. Every entry is hash-chained to the one before it and can carry
grounding citations (what the agent relied on). You can check the ledger offline at any time:
if a single byte has been changed, `adl verify` tells you which entry is broken and why.
`adl report` turns the ledger into a self-contained HTML audit timeline.

- Pure Python standard library. No dependencies, no network calls, no LLM calls.
- Python 3.10+ on POSIX (Linux, macOS). Appends are protected by an `fcntl` file lock.

## Why

Agent-governance platforms (kill switches, policy engines, governed agent runtimes) all
assume an audit trail already exists. Usually that trail is a log file anyone can quietly
edit. This library is a small, open primitive for that missing piece: an evidence log that
records what the agent did and what it based each step on, and that shows when it has been
altered.

## Install

```bash
pip install .            # from a checkout
pip install -e '.[test]' # for development (adds pytest)
```

## Library quickstart

```python
from decision_ledger import Ledger

with Ledger.open("ledger.jsonl") as ledger:
    ledger.record("observation", "agent:support", {"ticket": "T-1042", "summary": "refund request"})
    ledger.record("decision", "agent:support", {"action": "refund_full", "amount": 54.5},
                  grounding=[{"source": "policy/refunds.md", "locator": "section 2.1",
                              "quote": "Items that arrive damaged are eligible for a full refund."}])
```

`record()` returns the stored entry, including its `seq` and `hash`.

| API | Notes |
|---|---|
| `Ledger.open(path, *, lock_timeout=10.0, clock=None, fsync=True)` | Creates the file if it doesn't exist. Reopening an existing ledger resumes its chain from the last line. If that last line is partial or fails its own hash check, it raises `LedgerCorruptError` rather than append to a broken chain. |
| `ledger.record(kind, actor, payload, grounding=None)` | `kind` must be one of `decision`, `tool_call`, `tool_result`, `observation`, `human_approval`. Any other kind raises `InvalidEntryError`. `actor` is a non-empty string, `payload` is a JSON-serialisable dict, and `grounding` is a list of citation dicts, each with a non-empty `source`. |
| `ledger.seq`, `ledger.head` | The sequence number and hash of the last entry written. |
| `verify_file(path)` | Returns a `VerifyResult` (`ok`, `entries`, `head`, `first_break`). |

**Concurrency.** Each `record()` takes an exclusive `fcntl.flock` on the ledger file and
re-reads the current last entry while holding the lock, then chains onto that entry. Several
threads or processes writing to the same file therefore end up in a single linear chain. If
the lock can't be acquired within `lock_timeout` seconds (`0` fails immediately, `None` waits
forever), `record()` raises `LedgerLockedError` and writes nothing:

```
LedgerLockedError: could not acquire the exclusive append lock on ledger.jsonl within 10s:
another writer is holding it. Nothing was written.
```

## CLI

```
adl verify LEDGER [--format text|json]     recompute the whole chain
adl report LEDGER -o REPORT.html [--title]  write a self-contained HTML audit report
adl stats  LEDGER [--format text|json]     counts by kind and actor, time span, chain status
```

Exit codes: `0` means success or a verified chain, `1` means the chain is broken (`verify`
only), and `2` means a usage or I/O error such as a missing file. Output is coloured only on
a TTY, and colour is turned off whenever `NO_COLOR` is set. The status word (`PASS`, `FAIL`,
`WARN`, `INFO`) is always printed, so the colour is never the only signal. `--format json`
output is never coloured.

Try it with the bundled scripted agent. It is a fake refund-support agent with no LLM, and
it is deterministic:

```console
$ python examples/demo_agent.py ledger.jsonl
wrote 264 entries to ledger.jsonl

$ adl verify ledger.jsonl
PASS ledger.jsonl: chain verified, 264 entries, head 14d61bd13f4b0b41

$ adl stats ledger.jsonl
Ledger   ledger.jsonl
Chain    PASS verified (264 entries)
Entries  264
Span     2026-11-27T08:55:05.705000Z -> 2026-11-27T10:26:33.806000Z (1h 31m 28s)

By kind
  decision            33
  tool_call           95
  tool_result         95
  observation         35
  human_approval       6

By actor
  agent:refund-assistant       161
  tool:orders-api               32
  tool:policy-search            32
  tool:payments                 25
  human:support-supervisor       6
  tool:tickets                   6
  system:scheduler               2

$ adl report ledger.jsonl -o report.html
PASS ledger.jsonl: chain verified, 264 entries, head 14d61bd13f4b0b41
INFO report written to report.html
```

Then flip one bit inside entry 113:

```console
$ adl verify tampered.jsonl
FAIL tampered.jsonl: chain broken at seq 113 (line 113): hash mismatch: stored b1aabc27040e... != recomputed 5c0922d28219...
  112 entries before seq 113 verified; everything from that seq on is untrusted.
$ echo $?
1
```

The HTML report is a single file. It has no external stylesheets, scripts, fonts or images
and needs no CDN. It contains:

- a chain-status banner at the top: green `CHAIN VERIFIED` with the head hash, or red
  `CHAIN TAMPERED: broken at seq N (line N): <reason>`;
- summary cards and per-kind and per-actor tables;
- a timeline grouped by UTC hour, with each entry's kind badge, actor, timestamp, payload
  fields, grounding citations and short hashes;
- entries after a break flagged `UNVERIFIED`, and malformed lines shown with their raw bytes;
- kind filters that use a small inline script. Without the script, everything is shown.

Its colours come from `DESIGN_TOKENS.md` and no others are used. A print stylesheet swaps to
a light background.

## Ledger format

A ledger is UTF-8/ASCII text with **one JSON object per line**. Every line, including the
last, ends with `\n`. Each entry has exactly these eight fields:

| Field | Type | Meaning |
|---|---|---|
| `ts` | string | Time the entry was recorded, in UTC ISO-8601 with microseconds and a `Z` suffix, e.g. `2026-11-27T08:55:05.705000Z`. |
| `seq` | integer | Position in the chain. The first entry is `1` and each later entry adds exactly 1, so `seq` N is always on line N. |
| `kind` | string | One of `decision`, `tool_call`, `tool_result`, `observation`, `human_approval`. |
| `actor` | string | Who produced the entry, e.g. `agent:refund-assistant`, `tool:payments`, `human:support-supervisor`. |
| `payload` | object | Free-form JSON content of the step. |
| `grounding` | array of objects | Citations the step relied on. Each one has `source` (required), and optionally `locator` and `quote` plus any other keys. Empty list if there are none. |
| `prev_hash` | string | `hash` of the previous entry. For `seq` 1 it is the genesis value, 64 zeros. |
| `hash` | string | 64 lowercase hex chars, computed as described below. |

### Hashing recipe

1. **Body.** Take the entry without its `hash` key. That leaves the other seven fields,
   `prev_hash` included.
2. **Canonical JSON.** Serialise the body as JSON with object keys sorted recursively (by
   code point), no insignificant whitespace (separators `,` and `:`), and every non-ASCII
   character escaped as `\uXXXX` with lowercase hex (surrogate pairs for characters outside
   the BMP). `NaN` and `Infinity` are not allowed. Numbers are written in Python's shortest
   round-trip form. In Python this is exactly
   `json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)`.
3. **Hash.** `hash = lowercase_hex( sha256( utf8( prev_hash + canonical_json(body) ) ) )`.
   This is plain string concatenation: the 64-char `prev_hash` followed directly by the JSON
   text.
4. **Line.** The stored line is the canonical JSON (same rules) of the full entry *with*
   `hash`, followed by `\n`. A line that parses but is not byte-identical to its canonical
   form counts as tampered. So a hand-edited file with reordered keys or added spaces fails,
   as does a disguise such as `é` written instead of `é`.

A minimal independent verifier:

```python
import hashlib, json
prev = "0" * 64
for n, line in enumerate(open("ledger.jsonl", "rb").read().splitlines(), start=1):
    e = json.loads(line)
    body = {k: v for k, v in e.items() if k != "hash"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert e["seq"] == n and e["prev_hash"] == prev
    assert hashlib.sha256((prev + canon).encode()).hexdigest() == e["hash"], n
    prev = e["hash"]
```

(`adl verify` also runs the full schema and canonical-bytes checks.)

### What `adl verify` checks, per line, in order

| Reason | Triggered by |
|---|---|
| `malformed line` | Non-ASCII bytes, invalid JSON, not an object, missing or extra fields, wrong field types, unknown `kind`, non-canonical bytes, an empty line, or a final line with no newline (truncation). |
| `sequence gap` | `seq` is not the previous seq + 1. This covers deleted, duplicated or reordered entries. |
| `hash mismatch` | `prev_hash` doesn't equal the previous entry's `hash`, or the stored `hash` doesn't equal the recomputed one. |

Verification stops at the first failure. The reported `seq` is the sequence number expected at
that line position, so it is exact even when the damaged byte is inside the `seq` field itself
or breaks the JSON entirely.

## Grounding & traceability

The tool contains no AI, but it is used to vouch for AI-produced output, so its claims are
checked by two deterministic verifiers. Neither involves sampling or judgment.

**1. Chain-recompute verifier.** `adl verify` (or `verify_file`) recomputes every hash
from the raw ledger bytes.

- *Threshold: 100%.* Every entry must recompute to its stored hash and link to its
  predecessor. One failure means the ledger is not verified, and nothing after it is trusted.
- *Tamper detection:* changing any single byte must be detected, and the error must name the
  seq of the line that contains that byte. The smoke tests flip 25 random single bits in the
  264-entry demo ledger. During development, every byte of a 26-entry ledger was mutated
  with four XOR masks (48,936 mutations), and all of them were caught at the correct seq. The
  test seat's `tests/test_grounding.py::test_chain_recompute` covers the ≥20-mutation sample
  from the brief.

**2. Report-fidelity verifier.** The HTML report must match the JSONL file it was rendered
from.

- *Threshold: exact.* The number of entries, the actors and their counts, the timestamps
  (`data-ts`), the sequence numbers (`data-seq`), the per-kind counts and the number of
  citations shown in the HTML must all equal the values recomputed from the JSONL. Each
  timeline entry carries `data-seq`, `data-kind`, `data-actor` and `data-ts` attributes, and
  the document embeds a JSON summary in `<script type="application/json" id="adl-summary">`,
  so this can be checked mechanically (`tests/test_grounding.py::test_report_counts_match_ledger`,
  owned by the test seat).

**What a user sees on tamper.** On the terminal (exit code 1):

```
FAIL <path>: chain broken at seq <N> (line <N>): <hash mismatch|sequence gap|malformed line>: <detail>
  <N-1> entries before seq <N> verified; everything from that seq on is untrusted.
```

The detail text depends on the reason:

- `stored <12 hex>... != recomputed <12 hex>...`
- `prev_hash <12 hex>... does not link to seq <N-1> (<12 hex>...)`
- `expected seq <N>, found <M>`
- for malformed lines, a parser or schema message such as
  `invalid JSON (Expecting ',' delimiter at column 87)` or
  `bytes are not the canonical encoding (line was edited)`.

In the HTML report, a red banner reads `CHAIN TAMPERED: broken at seq N (line N): <reason>`,
followed by the detail and "Entries 1–(N-1) verified; seq N and everything after it cannot be
trusted." The broken entry has a red `CHAIN BROKEN` badge, and every later entry has a yellow
`UNVERIFIED (after break)` badge.

**Scope of the guarantee.** A hash chain is *tamper-evident*, not tamper-proof. Someone who
can rewrite the whole file can also recompute every hash after the point they changed. To
detect that, store the head hash (`adl verify --format json` returns it as `head`) somewhere
the writer can't modify, and check later that the ledger still reaches the same head.
External anchoring of this kind is planned for a future version.

## Not in scope (v0.1)

- Distributed or Merkle anchoring, blockchains, signing keys
- Real-time streaming and OpenTelemetry export (candidates for v2)
- Windows support (needs a non-`fcntl` lock)

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest
```

## License

MIT. See [LICENSE](LICENSE).
