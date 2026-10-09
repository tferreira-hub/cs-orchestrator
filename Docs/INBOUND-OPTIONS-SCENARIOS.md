# Inbound scenarios — Service Hub Pro (Option A) vs platform-routed (Option B)

**For:** Dan Hill (CS Leadership) · **From:** RevOps / CS Engineering · **Updated:** 9 October 2026

> **Decision (2 Oct 2026, CS Leadership / RevOps): Option A.** HubSpot Service Hub is the
> Help Desk; the CS Platform is the surface + act layer. Remaining gate: confirm the
> Service Hub Pro licence fits the 50–60k budget, then configure channel routing in HubSpot
> and point the channels at `POST /api/inbound/hubspot`. The deterministic
> triage/round-robin/SLA engine is already live and tested regardless of option. Both
> options are kept below for the record; the CSM experience is identical either way.

**Context:** The Tech-Touch V3 paper proposes HubSpot Service Hub Pro as the central Help
Desk. We don't have that licence yet. The platform now does the triage, round-robin, 24h
SLA, auto-reassign and technical→Zendesk handoff itself (live + tested). So the choice is:

- **Option A — buy Service Hub Pro:** channels land in HubSpot Service; tickets live in
  HubSpot; the platform surfaces + acts. Matches the signed paper; licence cost against the
  50–60k budget.
- **Option B — route into the platform:** channels post straight into the platform; it is
  the central inbox and does the routing. Same outcome, no extra licence (aligns with the
  cost-offset goal). Trade-off: account-management tickets live in the platform, not HubSpot.

Below, three real inbound requests, traced end-to-end under each option. The **CSM
experience and the outcome are identical** — the only difference is where the ticket is born
and where it lives.

---

## Scenario 1 — Technical issue (customer reports an outage)

> *"We can't log in this morning — 500 error on every page."*

**Option A (Service Hub Pro)**
1. Email hits HubSpot Service inbox → native HubSpot ticket created.
2. HubSpot workflow keyword-scans → detects technical intent → pushes to Zendesk via the
   native integration; the HubSpot ticket closes.
3. The platform surfaces the account context; the CSM confirms the handoff.

**Option B (platform-routed)**
1. The channel posts the message to the platform (`/api/inbound/hubspot`).
2. The platform classifies **technical** → routes to the **Zendesk handoff**, leaves the CS
   queue clean.
3. Same account context; same CSM confirmation.

**Outcome (both):** technical issue lands with the Zendesk support desk, nothing lingers in
the CS queue. *Difference: in A the ticket briefly exists in HubSpot Service; in B it's
handled in the platform queue.*

---

## Scenario 2 — Billing question (needs a CSM)

> *"Did our invoice go through? And were we overcharged this month?"*

**Option A**
1. Mailbox connected to HubSpot Service → native ticket.
2. HubSpot workflow: account/billing intent → assigns to the CSM pool via **Rotate Record
   Owner**, 24h SLA task created.
3. The platform shows the CSM the live Stripe context (invoice status, dunning stage) so
   they answer with facts.

**Option B**
1. Mailbox forwards to the platform intake.
2. The platform classifies **billing** → **pooled queue**, round-robins to an available CSM,
   stamps the **24h SLA**, merges any duplicate within 2h.
3. Same live Stripe context in the Pooled Inbox.

**Outcome (both):** a pooled CSM answers within SLA with real billing data. *Difference:
A's round-robin is HubSpot's "Rotate Record Owner"; B's is the platform's (same even
distribution + SLA + dedupe, plus the availability feed).*

---

## Scenario 3 — High-intent upgrade (expansion)

> *"We're hiring fast — can we add 10 more user seats next month?"*

**Option A**
1. HubSpot form submission → high-priority HubSpot workflow → routes to the account owner /
   expansion pipeline, bypassing the standard queue.
2. The platform shows the 85%-utilisation expansion signal; the owner raises the CSQL.

**Option B**
1. The web form POSTs to the platform intake.
2. The platform classifies **expansion** → routes to the **expansion queue / account owner**
   (bypasses the pooled queue), corroborated by the live 85% utilisation trigger.
3. The owner raises the CSQL (gated HubSpot write) from one pane.

**Outcome (both):** the upgrade becomes a qualified expansion deal with the owner, fast-
tracked past the service queue. *No material difference in CSM experience.*

---

## Side-by-side summary

| Dimension | Option A (Service Hub Pro) | Option B (platform-routed) |
|---|---|---|
| 5-channel intake | HubSpot Service inbox | Platform intake endpoint |
| Where tickets live | HubSpot Service tickets | Platform inbound queue |
| Triage (intent scan) | HubSpot workflows | Platform (built, live) |
| Round-robin + 24h SLA + reassign | HubSpot Rotate Record Owner + Help Desk status | Platform (built, live) + availability feed |
| Technical → Zendesk | Native HubSpot→Zendesk | Platform handoff (built) |
| CSM presence (Available/OOO) | Native HubSpot Help Desk | In-platform toggle (built) |
| Licence cost | **Service Hub Pro upgrade** (vs 50–60k budget) | **None** |
| Channel wiring effort | HubSpot config (native connectors) | RevOps wires 5 light feeds to the endpoint |
| Fit to signed Tech-Touch V3 paper | Matches it literally | Meets the goals; departs from the named architecture |
| Sales visibility of CS tickets in HubSpot | Yes (native) | No (tickets in the platform) |

## The decision in one line
Both options deliver the paper's inbound **outcome** (unified intake, automated triage,
technical→Zendesk, account→pooled round-robin under SLA). **Option A** buys that outcome as
HubSpot-native tickets at a licence cost; **Option B** reuses the routing already built in
the platform for no licence, trading HubSpot-native ticket storage for platform-native.

**Question for Dan:** do we hold to the signed Service Hub Pro architecture (A), or route
into the platform and save the licence (B)? Everything behind either choice is built; this
is a go-direction call, not a build request.
