# CS Platform — Four Capability Scenarios (for team agreement)

**Audience:** CS Leadership, RevOps, CS Engineering
**Date:** 2 October 2026
**Purpose:** Demonstrate that a CSM does their work **in one place** — the CS Platform —
without logging into HubSpot, Zendesk, Stripe, or the warehouse. Each scenario starts with
a message in Slack and is handled end-to-end in the platform.

## Routing model: Option A (decided)

Per `DECISION-tech-touch-routing.md`, the team has chosen **Option A**: the five inbound
channels (including Slack) land in **HubSpot Service Hub**; the **CS Platform surfaces the
queue and is where the CSM acts** — reply, close, hand off, raise a CSQL, move a cohort,
send the digest — through owner-scoped, two-gated, audited writes.

**Status legend per step:** **[Live]** shipped + verified · **[Config A]** HubSpot Service
Hub configuration (RevOps) · **[Owner]** one-off provisioning (credential/template).

---

## Scenario 1 — Technical issue → triaged, handed to support, closed in CS

**Slack message**
> **#cs-inbound** — *Daniel Osei (admin, Meridian Recruitment)* 9:02 AM
> We can't log in this morning — getting a 500 error on every page. Whole team is locked out, can someone help urgently?

**End to end**
1. **[Config A]** HubSpot ingests the Slack message as a ticket and rotates it to an available pooled CSM under the 24-hour SLA.
2. **[Live]** The platform surfaces the ticket with full account context (health, ARR, renewal, open tickets) and classifies the intent as **technical**.
3. **[Live]** From one pane the CSM **replies** to acknowledge and **hands off to the support desk** (Zendesk) using the live Zendesk write — verified write-capable (admin-agent token).
4. **[Live]** The CS ticket is **closed in the CS queue** (honest handoff), keeping the "no technical tickets in the CS queue" goal clean.

**CSM logged into:** nothing but the CS Platform.

---

## Scenario 2 — Expansion signal → CSQL raised in HubSpot from the platform

**Slack message**
> **#cs-inbound** — *Hannah Lees (champion, BrightPath Talent)* 11:27 AM
> We're hiring fast this quarter and running out of licences. Can we add 10 more user seats next month? Keen to get it sorted before our next bill.

**End to end**
1. **[Config A]** HubSpot ingests it; expansion intent routes to the **account owner**.
2. **[Live]** The platform classifies **expansion** and corroborates with the live 85% licence-utilisation trigger (Pendo/Entitlements). If the account is already ≥85% utilised, the "Add Seats / Upgrade" CTA is shown next to the ask.
3. **[Live]** The owner clicks **Create expansion deal (CSQL)** — the gated, owner-scoped, audited `HubSpot.create_csql` write raises the deal in HubSpot.

**CSM logged into:** nothing but the CS Platform (the deal appears in HubSpot).

---

## Scenario 3 — Move 1–20 Agency + Corporate to the pooled structure — executed by the platform

This is the structural-change scenario: the platform **performs the move itself**.

**Slack message**
> **#cs-inbound** — *Priya Natarajan (RevOps)* 2:14 PM
> We've decided to move our **1–20 user Agency accounts and all Corporate accounts** onto the **pooled (Scaled) structure** rather than named ownership. Can CS reassign them out of individual books and into the pooled cohort? How many accounts and ARR are we talking?

**End to end — all [Live], all in the platform**
1. The CSM/ops user opens **Scaled Customer Success → The Pool → "Move to pooled structure"**.
2. The platform **previews the exact cohort** it will touch — the 1–20 Agency + Corporate accounts (`Agency 1-2 Users`, `Agency 3-20 Users`, `Corporate`, or any already carrying `cs_customer_tier = Pooled`) — with **count, total ARR, and current owners**. This answers Priya's question directly.
   - *Verified live on the current book: 21 accounts, ~$500k ARR.*
3. The user clicks **Preview (dry-run)** to see exactly what would change — nothing is written.
4. The user clicks **Move N to pooled** and confirms. The platform executes a **gated, owner-scoped, audited** batch write to HubSpot: it sets **`cs_customer_tier = Pooled`** and **clears the named owner** on each account, so they leave individual books.
5. Those accounts are now served by the **pooled round-robin queue**; the Pooled Dashboard and executive capacity view reflect the rebalanced load immediately.

**Why this is safe:** two-gate (explicit `apply=true` + global write flag), **owner-scoped** (a CSM moves only accounts they own; an admin/ops user moves the cohort), **audited** (every move recorded in the immutable log), **reversible** (set the tier back / reassign an owner), and **dry-run by default** (the preview writes nothing). Accounts already pooled are skipped as no-ops.

**CSM logged into:** nothing but the CS Platform — the re-tiering lands in HubSpot automatically.

---

## Scenario 4 — Renewal/health risk → risk task with live churn + NDR, proactive save

**Slack message**
> **#cs-inbound** — *Aisha Bello (CSM, internal)* 3:10 PM
> Flagging a risk: just got off a call with Northgate Group and they sounded unhappy — mentioned they're reviewing other options before their renewal. Want to get ahead of this.

**End to end**
1. **[Config A]** HubSpot captures it as a ticket for the account owner.
2. **[Live]** The platform enriches the account with its full live risk picture in one view:
   - **Health** from usage + CSAT + Jiminny **call sentiment** (the "unhappy call" corroborated by real sentiment).
   - **ML churn** — **live** (warehouse access provisioned this programme; **10,278 accounts scored**). If >70%, the orchestrator raises a **Defensive Risk Task at P1 with a 24h SLA**.
   - **NDR** — **live** (verified real values from `rpt_account_ndr_monthly`), so the retention trajectory shows a real number.
   - **Renewal cadence** (T-120/90/60/30): the renewal play is already queued.
3. **[Live]** The CSM runs the save from one pane: logs the call note (HubSpot write), confirms the **contact roles** are complete (the close-gate blocks a renewal task from closing with missing Sponsor/Champion/Finance roles), and executive governance KPIs reflect the at-risk ARR.

**CSM logged into:** nothing but the CS Platform.

---

## What this proves

Across all four scenarios the CSM **never leaves the CS Platform**. The platform reads
every live source and **acts on each system** on the CSM's behalf — reply/close a Zendesk
ticket, raise a HubSpot CSQL, **re-tier a whole cohort to pooled in HubSpot**, log notes,
and surface live churn + NDR — all gated, owner-scoped, and audited.

**Live and verified today:** triage/routing/SLA/dedupe, health, sentiment, renewal cadence,
expansion trigger, payment No-Chasing, ML churn + NDR (warehouse provisioned this
programme), HubSpot writes (CS write-back, notes, roles, CSQL, **move-to-pooled**), Zendesk
reply/close, monthly digest (compile + gated send + scheduler).

**Remaining to make Slack intake itself end-to-end:** confirm the HubSpot Service Hub Pro
licence and configure channel routing (Option A). **One-off owner actions:** HubSpot
transactional-email template id (digest send), Stripe `rk_` + Tableau secret rotations.
