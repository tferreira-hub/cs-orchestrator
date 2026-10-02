#!/usr/bin/env python3
"""Scaled Tech-Touch inbound engine (Use Case 1, Tech Touch V3).

Deterministic, side-effect-free triage and allocation for pooled inbound items. This is
the core of the Scaled model: unify multi-channel inbound, classify intent, and
round-robin to available pooled CSMs under a 24-hour SLA. It intentionally does NOT call
any external system — a "technical -> Zendesk" result is described as a *handoff*, never
executed here (same honesty rule the agent harness enforces). Channel adapters (the actual
Zendesk-misroute / Slack / mailbox / form intake) are a separate, later increment; this
module operates on already-normalised inbound items so it is fully unit-testable.

An inbound item is a plain dict:
    {
      "id": str,                       # stable id (channel message id)
      "channel": str,                  # zendesk_misroute | slack_call | mailbox |
                                       #   campaign_reply | high_intent_form
      "from": str,                     # sender email / handle (used for dedupe)
      "subject": str, "body": str,     # free text scanned for intent
      "account_ref": str | None,       # matched account id/name if known
      "received_at": epoch seconds,    # when it arrived (for SLA + dedupe window)
    }

triage_inbound(items, roster, now, ...) -> {"tickets": [...], "merged": [...], "summary": {...}}
where each ticket carries its classified intent, routed destination, assigned owner
(round-robin, availability-aware), SLA due time, and whether it breached/needs reassign.
"""

from __future__ import annotations

import re
from typing import Any

# SLA + dedupe windows from the spec.
SLA_HOURS = 24          # first-response SLA
REASSIGN_AFTER_HOURS = 20  # OOO / stalled -> auto-reassign to next available CSM
DEDUPE_WINDOW_S = 2 * 3600  # merge duplicate messages from the same sender within 2h

# Intent keyword sets (lowercased, word-boundary matched). Order of precedence:
# expansion > technical > billing > general, so a "licence upgrade" that also says
# "error" is still treated as an expansion opportunity (highest commercial value).
INTENT_KEYWORDS = {
    "expansion": [
        "licence", "license", "upgrade", "add seats", "add seat", "additional user",
        "additional users", "more seats", "expand", "capacity", "add-on", "add on",
        "purchase", "buy more", "upsell", "increase",
    ],
    "technical": [
        "bug", "crash", "outage", "error", "broken", "not working", "down", "500",
        "exception", "defect", "fault", "glitch", "cannot log in", "can't log in",
        "login issue", "integration failing",
    ],
    "billing": [
        "invoice", "rate change", "subscription", "payment", "billing", "charge",
        "refund", "receipt", "price", "renewal cost", "overcharged", "credit note",
    ],
}

# Where each intent routes, per the spec.
INTENT_ROUTE = {
    "expansion": {"destination": "expansion_queue", "priority": 2, "csql": True,
                  "note": "High-intent expansion. Routed to the account owner / expansion queue as a CSQL."},
    "technical": {"destination": "zendesk_handoff", "priority": 3, "csql": False,
                  "note": "Technical intent. Handoff to Zendesk for the technical support desk; closed in the CS queue."},
    "billing": {"destination": "cs_pooled_queue", "priority": 3, "csql": False,
                "note": "Account/billing intent. Standard CS service ticket in the pooled queue."},
    "general": {"destination": "cs_pooled_queue", "priority": 4, "csql": False,
                "note": "General enquiry. Standard CS service ticket in the pooled queue."},
}


def classify_intent(text: str) -> str:
    """Classify inbound text into expansion | technical | billing | general.
    Expansion wins ties (highest commercial value), then technical, then billing."""
    t = (text or "").lower()

    def _hit(words: list[str]) -> bool:
        for w in words:
            # Word-boundary match for single tokens; substring for multi-word phrases.
            if " " in w:
                if w in t:
                    return True
            elif re.search(r"\b" + re.escape(w) + r"\b", t):
                return True
        return False

    if _hit(INTENT_KEYWORDS["expansion"]):
        return "expansion"
    if _hit(INTENT_KEYWORDS["technical"]):
        return "technical"
    if _hit(INTENT_KEYWORDS["billing"]):
        return "billing"
    return "general"


def _available(roster: list[dict]) -> list[dict]:
    """Pooled CSMs currently marked available, in a stable order for deterministic
    round-robin. A roster entry is {"name": str, "available": bool}."""
    avail = [c for c in roster if c.get("available", True)]
    return sorted(avail, key=lambda c: str(c.get("name", "")))


def _dedupe(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge items from the same sender within DEDUPE_WINDOW_S. Returns
    (primary_items, merged_away). The earliest message in a cluster is primary and
    carries a merged_ids list of the later duplicates."""
    # Group by (channel-agnostic) sender; sort each group by received_at.
    by_sender: dict[str, list[dict]] = {}
    for it in items:
        key = str(it.get("from", "")).strip().lower() or ("_id:" + str(it.get("id")))
        by_sender.setdefault(key, []).append(it)
    primaries: list[dict] = []
    merged_away: list[dict] = []
    for key, group in by_sender.items():
        group = sorted(group, key=lambda x: x.get("received_at", 0))
        current = None
        for it in group:
            if current is None:
                current = dict(it)
                current["merged_ids"] = []
                continue
            if it.get("received_at", 0) - current.get("received_at", 0) <= DEDUPE_WINDOW_S:
                current["merged_ids"].append(it.get("id"))
                merged_away.append(it)
            else:
                primaries.append(current)
                current = dict(it)
                current["merged_ids"] = []
        if current is not None:
            primaries.append(current)
    # Preserve overall arrival order of primaries.
    primaries.sort(key=lambda x: x.get("received_at", 0))
    return primaries, merged_away


def triage_inbound(items: list[dict], roster: list[dict] | None = None,
                   now: int | None = None) -> dict[str, Any]:
    """Triage + round-robin a batch of normalised inbound items.

    - Dedupe within the 2h window (per sender).
    - Classify intent and route (technical -> Zendesk handoff, billing/general -> pooled
      queue, expansion -> CSQL / owner).
    - Round-robin assign the items that stay in the CS pooled queue (billing/general/
      expansion) across AVAILABLE pooled CSMs; technical handoffs are not assigned (they
      leave the CS queue).
    - Attach a 24h SLA due time; flag items already within REASSIGN_AFTER_HOURS of breach.

    Pure and deterministic: given the same inputs it returns the same output, and it
    performs no I/O. `roster` entries: {"name": str, "available": bool}."""
    import time as _t
    now = int(now if now is not None else _t.time())
    roster = roster or []
    primaries, merged = _dedupe(items or [])
    available = _available(roster)

    tickets: list[dict] = []
    rr_index = 0  # round-robin cursor across available CSMs
    for it in primaries:
        intent = classify_intent((it.get("subject", "") + " " + it.get("body", "")).strip())
        route = INTENT_ROUTE[intent]
        received = int(it.get("received_at", now))
        sla_due = received + SLA_HOURS * 3600
        age_h = max(0, (now - received) / 3600.0)
        # Assign an owner only for items that remain in the CS pooled/expansion queue.
        owner = None
        if route["destination"] != "zendesk_handoff" and available:
            owner = available[rr_index % len(available)]["name"]
            rr_index += 1
        # SLA state: breached if past due; "reassign" if an assigned item is close to
        # breach (>= REASSIGN_AFTER_HOURS old) or its owner is now unavailable.
        breached = now > sla_due
        owner_unavailable = bool(owner) and owner not in {c["name"] for c in available}
        needs_reassign = (owner is not None) and (not breached) and \
            (age_h >= REASSIGN_AFTER_HOURS or owner_unavailable)
        tickets.append({
            "id": it.get("id"),
            "channel": it.get("channel"),
            "from": it.get("from"),
            "account_ref": it.get("account_ref"),
            "subject": it.get("subject", ""),
            "intent": intent,
            "destination": route["destination"],
            "priority": route["priority"],
            "csql": route["csql"],
            "routing_note": route["note"],
            "assigned_to": owner,
            "received_at": received,
            "sla_due": sla_due,
            "sla_breached": breached,
            "needs_reassign": needs_reassign,
            "merged_ids": it.get("merged_ids", []),
        })

    # Summary counts for the UI / governance.
    by_intent = {k: 0 for k in ("expansion", "technical", "billing", "general")}
    for t in tickets:
        by_intent[t["intent"]] = by_intent.get(t["intent"], 0) + 1
    assigned = [t for t in tickets if t["assigned_to"]]
    load: dict[str, int] = {}
    for t in assigned:
        load[t["assigned_to"]] = load.get(t["assigned_to"], 0) + 1
    summary = {
        "received": len(items or []),
        "merged": len(merged),
        "tickets": len(tickets),
        "by_intent": by_intent,
        "zendesk_handoffs": by_intent["technical"],
        "csqls": by_intent["expansion"],
        "available_csms": [c["name"] for c in available],
        "load_per_csm": load,
        "breached": sum(1 for t in tickets if t["sla_breached"]),
        "needs_reassign": sum(1 for t in tickets if t["needs_reassign"]),
    }
    return {"tickets": tickets, "merged": merged, "summary": summary}
