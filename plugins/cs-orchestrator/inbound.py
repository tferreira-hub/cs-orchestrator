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

# Strong-fault subset: these say the product is actually broken / unavailable (not a
# minor glitch). When one of these co-occurs with an expansion keyword, support wins over
# upsell (see classify_intent). Kept narrow on purpose so a casual "error" in an expansion
# email does not misroute a genuine upsell.
STRONG_FAULT_KEYWORDS = [
    "outage", "down", "crash", "broken", "not working", "cannot log in", "can't log in",
    "500", "integration failing", "unavailable", "offline",
]

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

# Per-channel prioritisation weighting (0.0–1.0). The platform IS the help desk — the five
# inbound paths map straight in — and each path carries a weight so a request's SOURCE
# nudges its position in the queue. Higher weight = higher commercial/urgency signal.
# Default order: a self-served high-intent web form (buying signal) > a logged inbound call
# (someone picked up the phone) > a Zendesk misroute (support already triaged it) > a reply
# to a campaign/report > a generic mailbox email. Override with CS_CHANNEL_WEIGHTS, a JSON
# map of {channel: weight}; unknown channels fall back to DEFAULT_CHANNEL_WEIGHT.
DEFAULT_CHANNEL_WEIGHT = 0.3
CHANNEL_WEIGHT = {
    "high_intent_form": 1.0,
    "slack_call": 0.8,
    "zendesk_misroute": 0.6,
    "campaign_reply": 0.4,
    "mailbox": 0.3,
}


def channel_weight(channel: str) -> float:
    """Resolve a channel's prioritisation weight (0.0–1.0). Operator-overridable via the
    CS_CHANNEL_WEIGHTS env JSON; falls back to the built-in defaults then
    DEFAULT_CHANNEL_WEIGHT for an unknown channel. Clamped to [0,1]."""
    import json
    import os
    weights = dict(CHANNEL_WEIGHT)
    raw = (os.environ.get("CS_CHANNEL_WEIGHTS") or "").strip()
    if raw:
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                for k, v in override.items():
                    try:
                        weights[str(k)] = float(v)
                    except (TypeError, ValueError):
                        pass
        except (ValueError, TypeError):
            pass
    w = weights.get(str(channel), DEFAULT_CHANNEL_WEIGHT)
    return max(0.0, min(1.0, float(w)))


def effective_priority_score(intent_priority: int, channel: str) -> float:
    """Blend the intent priority band with the channel weight into a single sortable score
    (LOWER sorts first, i.e. more urgent). Intent sets the band (a real outage/risk still
    beats a web form); the channel weight breaks ties WITHIN/near a band so a high-intent
    form outranks a generic mailbox email at the same intent level, without a low-value
    channel ever leapfrogging a genuine fault (max channel swing is < 1 priority band)."""
    return float(intent_priority) - channel_weight(channel)


def classify_intent(text: str) -> str:
    """Classify inbound text into expansion | technical | billing | general.

    Expansion wins ties (highest commercial value) EXCEPT when a strong technical fault is
    also present: a message like 'my licence portal is down' is a broken-product support
    issue, not an upsell, so an outage/failure signal overrides the expansion keyword.
    After that collision rule the order is expansion > technical > billing > general."""
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

    exp = _hit(INTENT_KEYWORDS["expansion"])
    tech = _hit(INTENT_KEYWORDS["technical"])
    # Collision guard: a STRONG fault signal (the product is broken/unavailable) means
    # 'support now' beats 'upsell', even if an expansion word (licence/upgrade) co-occurs.
    strong_fault = _hit(STRONG_FAULT_KEYWORDS)
    if exp and tech and strong_fault:
        return "technical"
    if exp:
        return "expansion"
    if tech:
        return "technical"
    if _hit(INTENT_KEYWORDS["billing"]):
        return "billing"
    return "general"


def _available(roster: list[dict]) -> list[dict]:
    """Pooled CSMs currently marked available, in a stable order for deterministic
    round-robin. A roster entry is {"name": str, "available": bool}."""
    avail = [c for c in roster if c.get("available", True)]
    return sorted(avail, key=lambda c: str(c.get("name", "")))


def _pick_owner(available: list[dict], load: dict[str, int]) -> str | None:
    """Choose the next pooled owner by LEAST current load, ties broken by name for
    determinism. `load` is the running open-ticket count per CSM (seeded with the live
    queue load by the caller, then incremented as this batch assigns). This replaces the
    naive per-call round-robin cursor, which always started at the alphabetically-first
    CSM and systematically overloaded them across repeated triage batches. Returns None
    when no CSM is available."""
    if not available:
        return None
    return min(available, key=lambda c: (load.get(c["name"], 0), str(c.get("name", ""))))["name"]


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
                   now: int | None = None, current_load: dict[str, int] | None = None) -> dict[str, Any]:
    """Triage + load-balanced allocation of a batch of normalised inbound items.

    - Dedupe within the 2h window (per sender).
    - Classify intent and route (technical -> Zendesk handoff, billing/general -> pooled
      queue, expansion -> CSQL / owner).
    - Assign the items that stay in the CS pooled queue (billing/general/expansion) to the
      LEAST-LOADED available pooled CSM. `current_load` seeds each CSM's existing open-
      ticket count (from the live queue) so allocation balances the real workload, not
      just this batch, and never systematically overloads the first CSM.
    - Attach a 24h SLA due time; flag items already within REASSIGN_AFTER_HOURS of breach.

    Pure and deterministic: given the same inputs it returns the same output, and it
    performs no I/O. `roster` entries: {"name": str, "available": bool}."""
    import time as _t
    now = int(now if now is not None else _t.time())
    roster = roster or []
    primaries, merged = _dedupe(items or [])
    available = _available(roster)
    # Running load per CSM, seeded with their existing open-ticket count so the batch
    # balances against the real queue rather than resetting to zero each call.
    load_running: dict[str, int] = dict(current_load or {})

    tickets: list[dict] = []
    for it in primaries:
        intent = classify_intent((it.get("subject", "") + " " + it.get("body", "")).strip())
        route = INTENT_ROUTE[intent]
        # received_at may be absent or explicitly None (a channel adapter that could not
        # parse a timestamp): both mean "arrived now" so SLA ageing still works.
        _rcv = it.get("received_at")
        received = int(_rcv) if isinstance(_rcv, (int, float)) else now
        sla_due = received + SLA_HOURS * 3600
        age_h = max(0, (now - received) / 3600.0)
        # Assign an owner only for items that remain in the CS pooled/expansion queue,
        # to the least-loaded available CSM.
        owner = None
        if route["destination"] != "zendesk_handoff" and available:
            owner = _pick_owner(available, load_running)
            if owner:
                load_running[owner] = load_running.get(owner, 0) + 1
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
            "account_name": it.get("account_name"),
            "subject": it.get("subject", ""),
            "intent": intent,
            "destination": route["destination"],
            "priority": route["priority"],
            # Per-channel prioritisation weighting (blend model): the channel's weight and
            # the blended effective score (lower = more urgent). Intent sets the band;
            # the channel weight orders within it so the SOURCE influences prioritisation.
            "channel_weight": channel_weight(it.get("channel")),
            "priority_score": round(effective_priority_score(route["priority"], it.get("channel")), 3),
            "csql": route["csql"],
            "routing_note": route["note"],
            "assigned_to": owner,
            "received_at": received,
            "sla_due": sla_due,
            "sla_breached": breached,
            "needs_reassign": needs_reassign,
            "merged_ids": it.get("merged_ids", []),
        })

    # Order the queue by the blended priority (lower score = more urgent), then put any
    # SLA-breached items first within a tie, then oldest-first. This is the per-channel
    # weighting made visible: at the same intent level a high-intent form sits above a
    # generic mailbox email, while a genuine fault still outranks any low-value channel.
    tickets.sort(key=lambda t: (t["priority_score"], not t["sla_breached"], t["received_at"]))

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


# ---------------------------------------------------------------------------
# Channel adapters (Tech Touch V3, UC1 "5-channel ingestion").
#
# Each adapter normalises ONE external system's native payload into the inbound-item
# shape that triage_inbound() consumes:
#   {id, channel, from, subject, body, account_ref, account_name, received_at}
#
# They are pure and side-effect-free (no I/O, no account resolution) so they are fully
# unit-testable; the server routes call them, resolve account_ref against the live book,
# then run triage + persist. An adapter returns a LIST of items (usually one) and silently
# drops a payload it cannot parse into text (honest: no fabricated subject/body).
#
# `received_at` is coerced to epoch seconds; a missing/garbage timestamp becomes None and
# triage_inbound defaults it to "now", so SLA ageing still works.
# ---------------------------------------------------------------------------

def _epoch(value: Any) -> int | None:
    """Coerce an ISO-8601 string, epoch seconds, or epoch millis into epoch SECONDS.
    Returns None when it cannot be parsed (triage then treats the item as arriving now)."""
    if value in (None, ""):
        return None
    # Numeric epoch (seconds or millis).
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v / 1000) if v > 1e11 else int(v)  # > ~2001 in millis => it's millis
    s = str(value).strip()
    if s.isdigit():
        v = float(s)
        return int(v / 1000) if v > 1e11 else int(v)
    # ISO-8601 (tolerate a trailing Z).
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _clean(*vals: Any) -> str:
    """First non-empty stripped string among vals, else ''."""
    for v in vals:
        if v not in (None, ""):
            s = str(v).strip()
            if s:
                return s
    return ""


def from_zendesk_misroute(payload: dict) -> list[dict]:
    """Channel 1: a Zendesk ticket a support agent macro-tagged as account management
    (Ticket_Type = Account_Management). Zendesk webhook posts the ticket object (or a
    {"ticket": {...}} envelope). We take its subject/description and requester email.
    The ticket's custom tag is the trigger upstream; here we only normalise it."""
    t = payload.get("ticket") if isinstance(payload.get("ticket"), dict) else payload
    if not isinstance(t, dict):
        return []
    requester = t.get("requester") if isinstance(t.get("requester"), dict) else {}
    frm = _clean(t.get("requester_email"), requester.get("email"), t.get("from"), t.get("email"))
    subject = _clean(t.get("subject"), t.get("title"))
    body = _clean(t.get("description"), t.get("body"), t.get("comment"), t.get("latest_comment"))
    if not (subject or body):
        return []
    tid = _clean(t.get("id"), t.get("ticket_id"), t.get("external_id")) or ("zd-" + str(abs(hash(subject + frm)) % 10**10))
    return [{
        "id": "zd-" + tid,
        "channel": "zendesk_misroute",
        "from": frm,
        "subject": subject,
        "body": body,
        "account_ref": t.get("account_ref") or t.get("organization_id") or None,
        "account_name": _clean(t.get("organization_name"), t.get("account_name")) or None,
        "received_at": _epoch(t.get("created_at") or t.get("updated_at") or payload.get("received_at")),
    }]


def from_slack_call(payload: dict) -> list[dict]:
    """Channel 2: an inbound phone call logged by non-CSM staff via a Slack workflow form.
    The form enforces caller email, request type, and a brief. Slack posts the form field
    values (shapes vary); we accept common keys plus a generic {"fields": {...}} map."""
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
    if not isinstance(fields, dict):
        return []
    frm = _clean(fields.get("email"), fields.get("caller_email"), fields.get("customer_email"), fields.get("from"))
    req_type = _clean(fields.get("request_type"), fields.get("type"), fields.get("category"))
    brief = _clean(fields.get("brief"), fields.get("details"), fields.get("summary"),
                   fields.get("message"), fields.get("body"))
    company = _clean(fields.get("company"), fields.get("account_name"), fields.get("organisation"))
    if not (brief or req_type):
        return []
    subject = _clean(fields.get("subject")) or ("Inbound call" + (": " + req_type if req_type else ""))
    # Keep the request type in the scanned text so the intent classifier can use it.
    body = (req_type + ". " + brief).strip(". ").strip() if req_type else brief
    cid = _clean(fields.get("id"), payload.get("event_id")) or ("call-" + str(abs(hash(frm + brief)) % 10**10))
    return [{
        "id": "slack-" + cid,
        "channel": "slack_call",
        "from": frm,
        "subject": subject,
        "body": body,
        "account_ref": fields.get("account_ref") or None,
        "account_name": company or None,
        "received_at": _epoch(fields.get("logged_at") or payload.get("received_at")),
    }]


def from_mailbox(payload: dict) -> list[dict]:
    """Channel 3: a direct email to the generic account-management mailbox. An email
    connector posts {from, subject, body/text/html, message_id, date}."""
    m = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    if not isinstance(m, dict):
        return []
    frm = _clean(m.get("from"), m.get("sender"), m.get("from_email"))
    subject = _clean(m.get("subject"))
    body = _clean(m.get("text"), m.get("body"), m.get("plain"), m.get("snippet"), m.get("html"))
    if not (subject or body):
        return []
    mid = _clean(m.get("message_id"), m.get("id")) or ("mail-" + str(abs(hash(subject + frm)) % 10**10))
    return [{
        "id": "mail-" + mid,
        "channel": "mailbox",
        "from": frm,
        "subject": subject,
        "body": body,
        "account_ref": m.get("account_ref") or None,
        "account_name": _clean(m.get("account_name")) or None,
        "received_at": _epoch(m.get("date") or m.get("received_at")),
    }]


def from_campaign_reply(payload: dict) -> list[dict]:
    """Channel 4: a reply to an automated renewal / monthly-report email. Same email shape
    as the mailbox, but we tag the channel so these thread as campaign replies (and an
    expansion-intent reply becomes a CSQL). A thread/account ref is usually carried."""
    m = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    if not isinstance(m, dict):
        return []
    frm = _clean(m.get("from"), m.get("sender"), m.get("from_email"))
    subject = _clean(m.get("subject"))
    body = _clean(m.get("text"), m.get("body"), m.get("plain"), m.get("snippet"), m.get("html"))
    if not (subject or body):
        return []
    mid = _clean(m.get("message_id"), m.get("id")) or ("camp-" + str(abs(hash(subject + frm)) % 10**10))
    return [{
        "id": "camp-" + mid,
        "channel": "campaign_reply",
        "from": frm,
        "subject": subject,
        "body": body,
        # Campaign replies usually carry the account/thread they replied to.
        "account_ref": m.get("account_ref") or m.get("account_id") or m.get("thread_account_ref") or None,
        "account_name": _clean(m.get("account_name")) or None,
        "received_at": _epoch(m.get("date") or m.get("received_at")),
    }]


def from_high_intent_form(payload: dict) -> list[dict]:
    """Channel 5: a website high-intent form (add licences / upgrade). These bypass the
    general queue and should classify as expansion; we prepend an explicit expansion
    phrase to the scanned text so the classifier routes it as a CSQL even if the free-text
    is terse. HubSpot/Marketo form posts {email, company, fields...}."""
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
    if not isinstance(fields, dict):
        return []
    frm = _clean(fields.get("email"), fields.get("work_email"), fields.get("from"))
    company = _clean(fields.get("company"), fields.get("company_name"), fields.get("account_name"))
    want = _clean(fields.get("request"), fields.get("message"), fields.get("comments"),
                  fields.get("details"), fields.get("interest"))
    form_name = _clean(fields.get("form_name"), fields.get("form")) or "Licence/upgrade request"
    # Explicit expansion lexicon so classify_intent() routes this to the expansion/CSQL
    # lane; the form itself IS the high-intent signal.
    subject = form_name
    body = ("licence upgrade add seats expansion request. " + want).strip()
    fid = _clean(fields.get("id"), fields.get("submission_id")) or ("form-" + str(abs(hash(frm + want)) % 10**10))
    return [{
        "id": "form-" + fid,
        "channel": "high_intent_form",
        "from": frm,
        "subject": subject,
        "body": body,
        "account_ref": fields.get("account_ref") or None,
        "account_name": company or None,
        "received_at": _epoch(fields.get("submitted_at") or payload.get("received_at")),
    }]


# Dispatch table so a single generic route can normalise any channel by name.
CHANNEL_ADAPTERS = {
    "zendesk_misroute": from_zendesk_misroute,
    "slack_call": from_slack_call,
    "mailbox": from_mailbox,
    "campaign_reply": from_campaign_reply,
    "high_intent_form": from_high_intent_form,
}


def normalise_channel(channel: str, payload: dict) -> list[dict]:
    """Normalise a native payload for a named channel into inbound items. Returns [] for an
    unknown channel or an unparseable payload (honest no-op, never fabricates)."""
    fn = CHANNEL_ADAPTERS.get(str(channel or "").strip())
    if not fn or not isinstance(payload, dict):
        return []
    try:
        return fn(payload)
    except Exception:  # noqa: BLE001 - a malformed payload must not raise into the request
        return []
