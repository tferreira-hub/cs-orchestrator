# Inbound design — the CS Platform is the help desk (5 native paths + per-channel weighting)

**For:** Dan Hill (CS Leadership) · **From:** RevOps / CS Engineering · **Updated:** 9 October 2026

> **Decision (Oct 2026, CS Leadership): we are NOT pursuing HubSpot Service Hub Pro.** The
> CS Platform IS the help desk. The five inbound paths map **directly into the platform**,
> which does the triage, round-robin, 24h SLA, auto-reassign, dedupe and technical→Zendesk
> handoff itself. Each path also carries a **prioritisation weight** so the SOURCE of a
> request influences where it sits in the queue. No Service Hub Pro licence is required.

## The five inbound paths (all native)

Each path has a thin webhook/forwarder that posts the raw payload to the platform, which
normalises and triages it. There is **no HubSpot Service Hub dependency**.

| # | Path | How it reaches the platform | Endpoint |
|---|------|-----------------------------|----------|
| 1 | **Zendesk misroute** | Support macro tags `Ticket_Type = Account_Management`; a Zendesk webhook posts the ticket | `POST /api/inbound/channel/zendesk_misroute` |
| 2 | **Inbound call (Slack)** | Non-CSM staff log the call via a Slack workflow form; Slack posts the form fields | `POST /api/inbound/channel/slack_call` |
| 3 | **Generic mailbox** | `accountmanagement@` forwards/posts incoming email | `POST /api/inbound/channel/mailbox` |
| 4 | **Campaign / report reply** | Replies to renewal/monthly-report emails thread in | `POST /api/inbound/channel/campaign_reply` |
| 5 | **High-intent web form** | "Add licences / upgrade" form submissions | `POST /api/inbound/channel/high_intent_form` |

The platform also offers a **pull** path (`POST /api/inbound/ingest-hubspot`) that reads
tickets via the existing HubSpot token, so the inbox can be populated with zero new wiring
while the five webhooks are stood up.

## Triage (unchanged, native)

Every item is intent-classified and routed:
- **Technical** (bug/crash/outage) → **Zendesk handoff** (never clogs the CS queue).
- **Account/billing** → **pooled CS queue**, round-robined to an available CSM under a 24h
  SLA, 2h duplicate-merge, 20h auto-reassign.
- **Expansion** (licence/upgrade/seats) → **high-priority CSQL** to the account owner /
  expansion queue.

## Per-channel prioritisation weighting (NEW)

Intent sets the **band**; the channel weight orders **within/near** the band, so the queue
reflects both *what* the request is and *where it came from*.

| Channel | Default weight |
|---|---|
| High-intent form | 1.0 |
| Slack call-log | 0.8 |
| Zendesk misroute | 0.6 |
| Campaign reply | 0.4 |
| Mailbox | 0.3 |

Each triaged ticket gets a blended `priority_score = intent_priority − channel_weight`
(**lower = more urgent**), and the inbox is ordered by it (SLA-breached first within a tie).
Because the channel swing is always `< 1`, a **genuine fault can never be leapfrogged** by a
low-value channel — but at the *same* intent level a high-intent form outranks a generic
mailbox email. Weights are operator-configurable via the **`CS_CHANNEL_WEIGHTS`** env var
(a JSON map, e.g. `{"mailbox": 0.5}`), clamped to `[0, 1]`.

## Worked scenarios

**1 — Technical outage** (*"500 error on every page"*): classified **technical** → Zendesk
handoff; the CS queue stays clean; the account's live context is on the account 360.

**2 — Billing question** (*"were we overcharged?"*): classified **billing** → pooled queue,
round-robined under the 24h SLA; the CSM answers from the live Stripe context (invoice /
dunning) in one pane. Channel weight orders it against other same-band items.

**3 — High-intent upgrade** (*"add 10 seats next month"*): arrives via the high-intent form
(weight 1.0) → classified **expansion** → top of the queue as a CSQL to the owner,
corroborated by the live 85%-utilisation trigger.

## What RevOps needs to do (internal, no licence)

Point each of the five sources at its `POST /api/inbound/channel/*` endpoint (webhook or
forwarder). That's it — triage, routing, round-robin, SLA, dedupe and weighting are already
live and tested in the platform. Optionally tune `CS_CHANNEL_WEIGHTS` if the default
ranking should change.
