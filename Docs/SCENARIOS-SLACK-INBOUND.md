# CS Platform — End-to-End Scenarios (Slack inbound)

**Audience:** CS Leadership, RevOps, CS Engineering
**Date:** 2 October 2026
**Purpose:** Show how a message a customer or CSM drops in Slack is handled end-to-end,
entirely inside the CS Platform, so a CSM never logs into another system to act on it.

## Decided routing model: Option A (HubSpot Service Hub is the Help Desk)

Per `DECISION-tech-touch-routing.md`, the team has chosen **Option A**:

- The five inbound channels (including Slack call-log) land in **HubSpot Service Hub**.
  Tickets are native HubSpot Service tickets; round-robin uses HubSpot "Rotate Record
  Owner"; CSM availability is HubSpot Help Desk status.
- The **CS Platform's role is "surface + act"**: it reads the HubSpot ticket queue, layers
  CS context on top (health, risk, ARR, renewal, NDR, churn, expansion), and lets the CSM
  **act from one pane** — reply, close, hand off, raise a CSQL, log notes, send the digest —
  through the platform's already-shipped, owner-scoped, audited writes.
- Outbound lifecycle email (the monthly digest, campaign replies) is sent via **HubSpot**
  (transactional single-send), which the business already licenses.

What this means for the scenarios below: the **Slack → HubSpot ticket** hop is HubSpot
configuration (Service Hub routing), not platform code. Everything the CSM then does —
and all the CS intelligence shown — is **live in the platform today** (noted per step).

### Status legend
- **Live** — shipped, deployed, and verified this programme.
- **Config (Option A)** — HubSpot Service Hub configuration owned by RevOps, not platform code.
- **Owner action** — a one-off provisioning step (credential / template), not code.

---

## Scenario 1 — Technical issue raised in Slack → triaged, handed to the support desk, closed in CS

**Requirements:** 5-channel inbound (#18), automated triage technical→support (#19),
round-robin + 24h SLA (#20), duplicate merge (#23), Zendesk ingestion (#3), reply/close.

1. A customer admin posts in the shared Slack channel:
   *"We can't log in this morning — 500 error on every page."*
2. **[Config A]** HubSpot Service Hub ingests the Slack message as a ticket and rotates it
   to an available pooled CSM under the 24-hour first-response SLA.
3. **[Live]** The CS Platform surfaces that ticket in the Pooled Inbox with full account
   context: health score, ARR, renewal date, open ticket count. The platform's own
   deterministic triage classifies the intent as **technical** (keyword match on
   "can't log in", "500", "error"), confirming/validating the routing for the audit log.
4. **[Live]** From one pane the CSM **replies** to acknowledge and **hands the ticket to the
   technical support desk** (Zendesk), using the platform's live Zendesk write
   (`reply_ticket` + `set_ticket_status`). Verified: the deployed Zendesk token is an
   admin agent, so it is write-capable.
5. **[Live]** The CS ticket is **closed in the CS queue** (honest handoff — the support desk
   owns the fix), keeping the triage-accuracy goal (no technical tickets lingering in CS) clean.

**Outcome:** The CSM never opened Zendesk or HubSpot. One pane: see context, reply, hand off, close.

---

## Scenario 2 — Expansion signal in Slack → CSQL raised without leaving the platform

**Requirements:** inbound (#18), triage expansion→CSQL (#19), 85% licence-utilisation
expansion trigger (#11), expansion deal create (Increment 5), HubSpot write (#1).

1. A champion posts: *"We're hiring fast — can we add 10 more user seats next month?"*
2. **[Config A]** HubSpot ingests it; because it is expansion intent it routes to the
   **account owner** (not the pooled queue).
3. **[Live]** The platform classifies the intent as **expansion** (expansion wins ties by
   design — highest commercial value) and cross-checks the live expansion trigger: Pendo /
   Entitlements **licence utilisation**. If the account is already **≥85% utilised** (#11),
   the account page shows the "Add Seats / Upgrade" CTA, corroborating the Slack ask with data.
4. **[Live]** The owner clicks **Create expansion deal (CSQL)** — the gated, owner-scoped,
   audited `HubSpot.create_csql` write raises the deal in HubSpot. Two-gate (explicit
   apply + global write flag); recorded in the immutable audit log.

**Outcome:** A Slack hint becomes a qualified pipeline deal in HubSpot, raised from the
platform, with the utilisation evidence attached. No HubSpot login.

---

## Scenario 3 — Billing question in Slack → CS ticket with live payment context, "No-Chasing" respected

**Requirements:** inbound (#18), triage billing→CS queue (#19), Stripe read-only (#2),
payment "No Chasing" days 1–14 automated / day-15 strategic (#9), duplicate merge (#23).

1. A finance contact posts twice within an hour: *"Did our invoice go through?"* then
   *"Also — were we overcharged this month?"*
2. **[Config A]** HubSpot ingests both and rotates the ticket to an available pooled CSM.
3. **[Live]** The platform's duplicate-merge logic recognises the same sender within the
   **2-hour window** and treats them as **one** item (so the CSM is not double-tasked, #23).
4. **[Live]** Intent classifies as **billing**; the ticket opens with **live Stripe context**
   (read-only, #2): invoice status, days past due, and the computed `dunning_stage`.
   - If the account is in **days 1–14**, the platform shows it is on the **automated
     "No-Chasing" path** (#9) — the CSM does not manually chase.
   - If **day-15+**, the **Strategic playbook** is surfaced instead.
5. **[Live]** The CSM answers from one pane with the real billing facts in front of them.

**Outcome:** The billing question is answered with authoritative Stripe data, and the
"No-Chasing" policy is honoured automatically. No Stripe or billing-tool login.

---

## Scenario 4 — Renewal/health risk flagged in Slack → risk task, live churn + NDR context, proactive save

**Requirements:** inbound (#18), health scoring (#12), ML churn >70% → defensive P1 + 24h
SLA (#5, #8), renewal cadence T-120/90/60/30 (#10), NDR reporting (#16), executive
governance (#14), contact-role close-gate (#15).

1. A CSM posts on behalf of a worried account: *"Customer sounded unhappy on our call and
   said they're reviewing options before renewal."*
2. **[Config A]** HubSpot captures it as a ticket for the account owner.
3. **[Live]** The platform enriches the account with its full live risk picture, in one view:
   - **Health score** (#12) from usage + CSAT + Jiminny **call sentiment** — the "unhappy
     call" is corroborated by real negative sentiment, not a hunch.
   - **ML churn** (#5) — **now live**: the cross-account warehouse reader was provisioned
     this programme and verified returning **10,278 scored accounts**. If this account is
     **>70%**, the orchestrator raises the **Defensive Risk Task at P1 with a 24-hour SLA** (#8).
   - **NDR** (#16) — **now live**: verified real values flowing from the warehouse
     (`rpt_account_ndr_monthly`), so the retention trajectory shows a real number, not "no data".
   - **Renewal cadence** (#10): if the account is inside T-120/90/60/30, the renewal play is
     already queued.
4. **[Live]** The CSM runs the save from one pane: logs the call note (HubSpot write),
   confirms the **contact roles** are complete (the close-gate, #15, blocks a renewal task
   from closing with missing Sponsor/Champion/Finance roles), and the **executive governance
   KPIs** (#14) reflect the at-risk ARR for leadership.

**Outcome:** A soft Slack signal becomes a governed, data-backed retention play with a
P1 SLA — driven from one pane, with live churn and NDR now behind it.

---

## What is live vs. what remains (honest summary)

**Live and verified today (all four scenarios' CS actions):**
- Triage, routing, round-robin, 24h SLA, 20h reassign, 2h duplicate merge (deterministic engine, tested).
- Health, Jiminny sentiment, renewal cadence, expansion trigger, payment "No-Chasing".
- **ML churn and NDR** — warehouse access provisioned and verified end-to-end this programme.
- Owner-scoped, two-gated, audited writes: HubSpot CS write-back, notes, contact roles,
  **expansion deals (CSQL)**; Zendesk reply/close (token confirmed write-capable).
- Monthly performance digest: compile + gated send + 1st-of-month scheduler (dry-run-safe).

**Config (Option A) — RevOps, HubSpot Service Hub:**
- Route the five channels (incl. Slack call-log) into HubSpot Service tickets; configure
  Rotate Record Owner, availability, and the 24h SLA in HubSpot workflows.
- Confirm the Service Hub Pro licence is within the budget (the gate for Option A).

**Owner actions (one-off, not code):**
- HubSpot transactional email template id → to send the monthly digest for real.
- Security hygiene: rotate the Stripe key to a restricted read-only `rk_` key; rotate the
  Tableau connected-app secret (runbook: `SECURITY-ROTATION-RUNBOOK.md`).

**Phase 2 (deferred per Meeting Notes):**
- Rocket Lane onboarding is connected (live); the deeper onboarding-stagnation alerts and
  unified lifecycle view are Phase-2 scope.

---

## The single decision that makes these fully end-to-end

Option A is chosen. The remaining gate is **confirming the HubSpot Service Hub Pro licence**
and configuring the channel routing in HubSpot. Once that is in place, the Slack → ticket
hop is live and all four scenarios run with no manual stitching: the customer's Slack
message lands as a HubSpot ticket, and the CSM does everything else from the CS Platform.
