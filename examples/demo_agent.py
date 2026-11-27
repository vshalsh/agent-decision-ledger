#!/usr/bin/env python3
"""A scripted, fake customer-support refund agent (no LLM, no network).

It works through a queue of synthetic support tickets and records every step in an
agent-decision-ledger: the incoming ticket (observation), order lookups and policy
searches (tool_call / tool_result), its decision with grounding citations, human
sign-off for large refunds (human_approval) and the payment tool call that follows.

    python examples/demo_agent.py demo.jsonl            # 32 tickets -> 230+ entries
    python examples/demo_agent.py demo.jsonl --tickets 50 --seed 3 --force

The run is fully deterministic for a given --seed (fixed simulated clock), so the
resulting hashes are reproducible.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone

try:
    from decision_ledger import Ledger
except ImportError:  # allow running from a source checkout without installing
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from decision_ledger import Ledger

AGENT = "agent:refund-assistant"
ORDERS = "tool:orders-api"
KB = "tool:policy-search"
PAYMENTS = "tool:payments"
SUPERVISOR = "human:support-supervisor"

APPROVAL_THRESHOLD = 150.00  # refunds above this need a human

POLICY = {
    "damaged": ("policy/refunds.md", "section 2.1",
                "Items that arrive damaged are eligible for a full refund within 30 days of delivery."),
    "late": ("policy/refunds.md", "section 2.4",
             "If delivery is more than 7 days late, the shipping fee is refunded on request."),
    "changed_mind": ("policy/refunds.md", "section 3.2",
                     "Unopened items may be returned within 14 days; a 10% restocking fee applies."),
    "wrong_item": ("policy/refunds.md", "section 2.2",
                   "If we ship the wrong item we refund in full and the customer keeps the item."),
    "out_of_window": ("policy/refunds.md", "section 1.3",
                      "Requests received after the applicable return window are declined."),
}

PRODUCTS = [
    ("Ceramic pour-over set", 48.00), ("Noise-cancelling headphones", 229.00),
    ("Trail running shoes", 135.50), ("Cast-iron skillet 28cm", 62.90),
    ("Mechanical keyboard", 174.00), ("Merino base layer", 89.00),
    ("Espresso grinder", 315.00), ("Kids' rain jacket", 39.95),
]


class SimClock:
    """Deterministic clock: advances a few seconds every time it is read."""

    def __init__(self, start: datetime, rng: random.Random) -> None:
        self.now = start
        self.rng = rng

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=self.rng.randint(400, 9000))
        return self.now

    def idle(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _ticket(rng: random.Random, n: int) -> dict:
    product, price = rng.choice(PRODUCTS)
    reason = rng.choices(list(POLICY), weights=[4, 3, 3, 2, 1])[0]
    return {
        "ticket_id": f"T-{1040 + n}",
        "order_id": f"ORD-{rng.randint(200000, 299999)}",
        "product": product,
        "price": price,
        "reason": reason,
        "days_since_delivery": rng.randint(1, 20) if reason != "out_of_window" else rng.randint(31, 60),
    }


def run_ticket(ledger: Ledger, t: dict, rng: random.Random) -> None:
    ledger.record("observation", AGENT, {
        "event": "ticket_received",
        "ticket_id": t["ticket_id"],
        "channel": rng.choice(["email", "chat", "web_form"]),
        "summary": f"Customer requests refund for '{t['product']}' ({t['reason'].replace('_', ' ')}).",
    }, grounding=[{"source": f"ticket://{t['ticket_id']}", "locator": "message 1"}])

    ledger.record("tool_call", AGENT, {
        "tool": "orders.get", "args": {"order_id": t["order_id"]}, "call_id": f"{t['ticket_id']}-c1",
    })
    shipping = 6.50
    ledger.record("tool_result", ORDERS, {
        "call_id": f"{t['ticket_id']}-c1", "status": "ok", "latency_ms": rng.randint(40, 380),
        "result": {
            "order_id": t["order_id"], "item": t["product"], "item_price": t["price"],
            "shipping_fee": shipping, "days_since_delivery": t["days_since_delivery"],
            "opened": t["reason"] in ("damaged", "wrong_item"),
        },
    })

    ledger.record("tool_call", AGENT, {
        "tool": "policy.search", "args": {"query": f"refund {t['reason'].replace('_', ' ')}", "top_k": 2},
        "call_id": f"{t['ticket_id']}-c2",
    })
    src, loc, quote = POLICY[t["reason"]]
    ledger.record("tool_result", KB, {
        "call_id": f"{t['ticket_id']}-c2", "status": "ok", "latency_ms": rng.randint(15, 120),
        "result": {"hits": [{"doc": src, "section": loc, "score": round(rng.uniform(0.78, 0.97), 3)}]},
    }, grounding=[{"source": src, "locator": loc, "quote": quote}])

    # --- the decision, grounded in the order record and the policy clause
    reason = t["reason"]
    if reason in ("damaged", "wrong_item"):
        action, amount = "refund_full", t["price"] + shipping
    elif reason == "late":
        action, amount = "refund_shipping", shipping
    elif reason == "changed_mind" and t["days_since_delivery"] <= 14:
        action, amount = "refund_partial", round(t["price"] * 0.9, 2)
    else:
        action, amount = "decline", 0.0
    amount = round(amount, 2)
    needs_human = amount > APPROVAL_THRESHOLD
    ledger.record("decision", AGENT, {
        "ticket_id": t["ticket_id"], "action": action, "amount": amount, "currency": "USD",
        "requires_approval": needs_human,
        "rationale": f"{reason.replace('_', ' ').capitalize()} claim, delivered "
                     f"{t['days_since_delivery']} days ago; applied {loc} of the refund policy.",
        "confidence": round(rng.uniform(0.72, 0.99), 2),
    }, grounding=[
        {"source": src, "locator": loc, "quote": quote},
        {"source": f"orders-api://{t['order_id']}", "locator": "item_price, days_since_delivery"},
        {"source": f"ticket://{t['ticket_id']}", "locator": "message 1"},
    ])

    if action == "decline":
        ledger.record("tool_call", AGENT, {
            "tool": "tickets.reply", "call_id": f"{t['ticket_id']}-c3",
            "args": {"ticket_id": t["ticket_id"], "template": "refund_declined_out_of_window"},
        })
        ledger.record("tool_result", "tool:tickets", {"call_id": f"{t['ticket_id']}-c3", "status": "ok"})
        return

    if needs_human:
        approved = rng.random() > 0.15
        ledger.record("human_approval", SUPERVISOR, {
            "ticket_id": t["ticket_id"], "approved": approved, "amount": amount,
            "note": "Checked photos attached to ticket." if approved else "Ask customer for photos first.",
        }, grounding=[{"source": f"ticket://{t['ticket_id']}", "locator": "attachments"}])
        if not approved:
            ledger.record("decision", AGENT, {
                "ticket_id": t["ticket_id"], "action": "request_evidence", "amount": 0.0, "currency": "USD",
                "rationale": "Supervisor withheld approval; requesting photo evidence before refunding.",
                "confidence": 0.95,
            }, grounding=[{"source": f"ledger://approval/{t['ticket_id']}", "locator": "human_approval"}])
            return

    ledger.record("tool_call", AGENT, {
        "tool": "payments.refund", "call_id": f"{t['ticket_id']}-c4",
        "args": {"order_id": t["order_id"], "amount": amount, "currency": "USD"},
        "idempotency_key": f"{t['ticket_id']}-refund",
    })
    ok = rng.random() > 0.05
    ledger.record("tool_result", PAYMENTS, {
        "call_id": f"{t['ticket_id']}-c4", "status": "ok" if ok else "error",
        "latency_ms": rng.randint(200, 1400),
        "result": {"refund_id": f"RF-{rng.randint(10000, 99999)}"} if ok else {"error": "gateway_timeout"},
    })
    if not ok:
        ledger.record("observation", AGENT, {
            "event": "retry_scheduled", "ticket_id": t["ticket_id"], "after_seconds": 300,
            "summary": "Payment gateway timed out; refund will be retried with the same idempotency key.",
        })


def build_demo_ledger(path: str, *, tickets: int = 32, seed: int = 7) -> int:
    """Write a fresh demo ledger to ``path`` and return the number of entries recorded."""
    rng = random.Random(seed)
    clock = SimClock(datetime(2026, 11, 27, 8, 55, 0, tzinfo=timezone.utc), rng)
    with Ledger.open(path, clock=clock, fsync=False) as ledger:
        ledger.record("observation", "system:scheduler", {
            "event": "shift_started", "queue": "refunds", "tickets_pending": tickets,
            "policy_version": "refunds.md@2026-10-01",
        }, grounding=[{"source": "policy/refunds.md", "locator": "version 2026-10-01"}])
        for n in range(tickets):
            run_ticket(ledger, _ticket(rng, n), rng)
            clock.idle(rng.randint(20, 240))
        ledger.record("observation", "system:scheduler", {"event": "shift_ended", "queue": "refunds"})
        return ledger.seq


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", help="ledger file to create (JSONL)")
    parser.add_argument("--tickets", type=int, default=32)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)
    if os.path.exists(args.output):
        if not args.force:
            print(f"ERROR {args.output} already exists (use --force to overwrite)", file=sys.stderr)
            return 2
        os.remove(args.output)
    count = build_demo_ledger(args.output, tickets=args.tickets, seed=args.seed)
    print(f"wrote {count} entries to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
