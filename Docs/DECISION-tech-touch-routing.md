# Decision Record: Tech-Touch Routing Layer — HubSpot Service Hub Pro vs CS Platform

| | |
|---|---|
| **Status** | Open — awaiting CS Leadership / RevOps decision |
| **Date raised** | 2 October 2026 |
| **Owners** | RevOps / CS Engineering |
| **Affects** | Tech Touch for CS V3 (Phase-1, sub-$10k segment), the 50–60k platform budget, and the inbound/round-robin build |

## Summary

The *Tech Touch for CS V3* working paper specifies **HubSpot Service Hub Pro** as the
centralised Help Desk: all five inbound channels flow into HubSpot, tickets are native
HubSpot Service tickets, round-robin uses HubSpot's "Rotate Record Owner", and CSM
availability is read from HubSpot Help Desk status.

The CS Platform as currently built implements the **triage and round-robin logic itself**
(deterministic intent classification, even distribution, 24-hour SLA, 20-hour reassign,
2-hour duplicate merge), treating HubSpot as the system of record (CRM), not the Help Desk.

These two models are **mutually exclusive for the routing layer**. A decision is required
before further investment in the five channel adapters or the monthly digest engine, because
it changes both what is built and whether the Service Hub Pro licence is purchased.

## Options

### Option A — HubSpot Service Hub Pro is the Help Desk (as the paper describes)
- The five channels land in HubSpot; triage, round-robin, availability and SLA tasks are
  configured in HubSpot workflows.
- The CS Platform's role becomes **surface + act**: display the HubSpot ticket queue, apply
  CS context (health, risk, ARR), and let CSMs reply/close from one pane — not re-implement
  routing.
- Outbound lifecycle email (Pillars 1, 2, 5; Channel 4 reply-to) is handled by HubSpot
  Marketing Enterprise, which the business already licenses.
- **Cost:** requires the Service Hub Pro upgrade (a line against the 50–60k budget).
- **Pros:** matches the signed paper; native CSM presence; email "comes free"; less bespoke
  code to maintain. **Cons:** licence cost; routing logic lives in HubSpot config, not the
  platform, so it is less portable and less auditable in the CS Platform's own audit log.

### Option B — The CS Platform is the Help Desk (current build)
- Triage + round-robin already exist in the platform (`inbound.py`); HubSpot stays CRM-only.
- **Cost:** no Service Hub Pro upgrade; but the five channel adapters (Zendesk-misroute macro,
  Slack call-log form, `accountmanagement@` mailbox, campaign replies, high-intent forms) and
  an **outbound email + scheduler** capability must be built into the platform.
- A real-time **CSM availability feed** must be sourced (today the engine treats all pooled
  CSMs as available).
- **Pros:** routing logic + SLA + audit live in one governed platform; no per-seat Help Desk
  licence; consistent with the owner-scoped, two-gated, audited write model. **Cons:** more
  bespoke integration to build and maintain; email/scheduler is net-new.

## Recommendation (for discussion, not a unilateral decision)

A pragmatic middle path is viable and worth tabling:

- Keep the CS Platform as the **single pane of glass and action surface** (it already is).
- For the **routing layer specifically**, prefer **Option A** if the Service Hub Pro licence
  is already within budget appetite — it honours the signed paper, gives native CSM presence,
  and makes outbound email a configuration rather than a build. The platform then reads the
  HubSpot ticket queue and acts on it (the Increment-3 Zendesk writes and the HubSpot writes
  already shipped support this).
- Choose **Option B** only if avoiding the Service Hub Pro cost is a priority that outweighs
  building and operating the five channel adapters, the email/scheduler, and a presence feed
  in-house.

The deterministic triage engine already built is **not wasted** under either option: under A
it can validate/mirror HubSpot's routing for auditability; under B it is the routing layer.

## Decision needed

1. Is the **Service Hub Pro licence** in scope for the 50–60k budget? (Gates Option A.)
2. If not, is building the **five channel adapters + outbound email/scheduler + CSM presence
   feed** into the CS Platform acceptable scope? (Gates Option B.)

Until this is resolved, further channel/digest work is paused to avoid building the wrong
layer. The platform's read, risk, renewal, expansion, payment, reporting, and (gated) write
capabilities are unaffected and continue.
