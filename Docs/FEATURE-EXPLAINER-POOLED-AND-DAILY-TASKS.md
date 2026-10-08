# CS Platform - How two features work, end to end

Prepared for: Dan Hill (CS Leadership)
Scope: a plain-English, code-grounded walkthrough of the two features you asked about:

1. Pooled accounts, round-robin allocation of inbound work, and escalations.
2. The daily to-do list: how tasks are created and how they are prioritised.

Everything below describes what the platform does **today** in the deployed build. Where the
implementation deliberately differs from the written spec, it is called out as a
**[Deviation]** so you can sign it off or ask us to change it. Where a capability depends on
an integration that is not yet connected, it is called out as **[Not yet connected]** - the
platform shows these honestly as "not connected" / "no data" rather than inventing numbers.

---

## Feature 1 - Pooled accounts, round-robin allocation, and escalations

### 1.1 What "pooled" means in the platform
The pooled (Scaled / Tech-Touch) cohort is the long tail of sub-$10k, 1-20-user Agency and
Corporate accounts that are served by a shared team rather than a named CSM.

An account is treated as pooled when either:
- HubSpot says so explicitly (`cs_customer_tier = Pooled`), which is authoritative; or
- it falls in the pooled segment heuristic (Agency 1-2, Agency 3-20, Corporate) when the
  tier is not set.

The "Move to pooled structure" screen lets an owner (or admin) select 1-20 Agency +
Corporate accounts and move them in bulk: it sets `cs_customer_tier = Pooled` in HubSpot,
assigns a chosen **pooled team** (`cs_pooled_team`), and clears individual ownership so the
accounts join the pooled queue. The action is owner-scoped (a CSM can only move accounts
they own), dry-run by default, two-gated (an explicit apply flag plus a platform write
switch), audited, and reversible.

In the pooled tables the **Owner** column shows the real HubSpot owner when an account still
has one; otherwise it shows the assigned pooled team by name, or a plain "Pooled team" label
when no team is recorded yet. It never shows a fabricated owner.

### 1.2 The five inbound channels
Pooled customers reach us through five channels; all of them funnel into one triaged queue:

| # | Channel | Native source |
|---|---------|---------------|
| 1 | Zendesk misroute | A support agent macro-tags a billing/renewal ticket as account management |
| 2 | Inbound call | Non-CSM staff log the call via a Slack workflow form |
| 3 | Mailbox | Direct email to the generic `accountmanagement@` address |
| 4 | Campaign reply | A reply to an automated renewal / monthly-report email |
| 5 | High-intent form | A website "add licences / upgrade" form |

Each channel has an adapter that converts its native payload into one standard shape
(sender, subject, body, account reference, timestamp). A sixth path - a direct **pull from
HubSpot Service Hub** - reads tickets we already have access to and runs them through the
same pipeline, so the queue is populated today without waiting on every channel to be wired.

**[Not yet connected]** The adapters and their endpoints are live and tested, but the
external wiring (the Zendesk macro, the Slack form, the mailbox connector, the campaign
reply-to, the website form) is RevOps configuration that posts to the platform. Until a
given channel is wired, that channel simply contributes nothing - the HubSpot pull is the
live source in the meantime.

### 1.3 Triage - how an item is classified and routed
Every inbound item's text (subject + body) is scanned for intent, in this order of
precedence:

1. **Expansion** (licence, upgrade, add seats, capacity, add-on) - highest commercial value.
2. **Technical** (bug, crash, outage, error, cannot log in).
3. **Billing** (invoice, subscription, payment, refund, rate change).
4. **General** - anything else.

There is one deliberate collision rule: if a message contains an expansion word **and** a
strong "the product is broken / unavailable" signal (outage, down, crash, cannot log in),
it is treated as **technical** - a customer whose licence portal is down needs support now,
not an upsell. A casual "error" in an otherwise clear upsell email does not trigger this.

Routing by intent:
- **Expansion** -> flagged as a CSQL and routed to the account owner / expansion queue.
- **Technical** -> handed off to Zendesk for the technical desk and closed in the CS queue.
  (The platform describes the handoff; it does not silently execute a Zendesk write.)
- **Billing / General** -> a standard CS service ticket in the pooled queue.

### 1.4 Allocation across the pooled team
Items that stay in the CS pooled queue (billing, general, expansion) are assigned to a
pooled CSM who is currently marked **available** (live presence feed). Duplicate messages
from the same sender within a 2-hour window are merged first, so one person emailing three
times does not create three tickets.

**[Deviation]** The spec says "Rotate Record Owner" (even round-robin). The platform instead
assigns to the **least-loaded available CSM** - the one with the fewest open tickets right
now, ties broken by name. We did this on purpose: a naive rotate always restarts at the
first name alphabetically and systematically overloads them across repeated batches,
whereas least-loaded genuinely balances the real workload. The outcome is still an even
spread; the mechanism is smarter. Happy to switch to literal rotate if you prefer.

Every assigned item gets a 24-hour first-response SLA due time.

### 1.5 Escalations - SLA breach, OOO, and auto-reassign
The queue re-evaluates every open ticket each time it is read, against the current time and
live CSM availability (not frozen at intake):

- **24-hour SLA**: once a ticket passes its due time it is flagged **SLA breached** and sorted
  to the top.
- **20-hour / owner-OOO auto-reassign**: if a ticket has been sitting for 20+ hours, or its
  assigned CSM has since gone out-of-office, it is **actually moved** to the least-loaded
  available CSM - not merely flagged. The move records who it came from, who it went to, and
  why ("owner OOO" or "stalled >= 20h"), and that trail is visible in the Inbox.
- **Technical handoffs** are not assigned to a pooled CSM (they belong to Zendesk), so they
  never count against anyone's pooled load.

The Inbox can be filtered by owner (to see one CSM's routed items) and by intent segment
(All / SLA breached / Technical / Billing / Expansion / General), with the counts kept
consistent with the filtered view.

### 1.6 Durability
The queue is persisted to disk (an append-only file on the shared volume), so it survives a
restart, and re-ingesting the same ticket updates it in place rather than duplicating it or
reopening something already resolved.

---

## Feature 2 - The daily to-do list: creation and prioritisation

### 2.1 Where tasks come from
CSMs do not originate tasks; the platform does. A fixed, deterministic rule engine runs over
each account's live signals and emits a task every time a rule fires. "Deterministic" means
no AI is in this path - the same inputs always produce the same tasks, which is what makes
the queue auditable and the behaviour explainable to a customer.

Each task carries a stable rule identifier, the mandate it serves (Protect / Expand / Use),
a priority, the evidence that fired it, a recommended action, and - for the actionable ones -
a draft message. A built-in judge checks each task's priority and routing against the signed
playbook by rule id (not by wording), so editing a task's text can never silently change its
priority.

### 2.2 The rules and their priorities
Priority 1 is most urgent. The SLA attached to each task comes from its priority
(P1/P2 = 24 hours, P3 = 1 week, P4 = 2 weeks, P5 = 1 week).

| Priority | Mandate | Rule (what fires it) |
|---|---|---|
| **P1** | Protect | Predictive Risk Playbook - a genuine ML churn score >= 70%, a High Pendo risk signal, a support-ticket spike combined with a usage drop, an open Sev-1, or no product visit in 180+ days (Strategic) |
| **P2** | Protect | Churned-account recovery; sudden seat/user contraction (>20% drop in 14 days, independent of renewal); scaled exception escalation; Day-15 payment (high-ARR Strategic only); overdue renewal; a committed success-plan goal off-track or past deadline |
| **P3** | Expand / Use | Expansion triggers (licence utilisation >= 85%, API surge, strong live adoption, ROI AI adoption spike); onboarding stagnation (a stalled Rocket Lane project) |
| **P4** | Expand | Proactive renewal cadence (T-120 / T-90 / T-60 / T-30); Executive Sponsor face-to-face cadence for tier-1 strategic accounts |
| **P5** | Use | Adoption / onboarding intervention; contact-role hygiene (missing Executive Sponsor / Primary Champion / Finance Contact) |

Two honesty guards worth knowing:
- A churn score the platform *computes* from live signals (used when no ML model is
  connected) is **not** treated as an ML prediction - it will not fire the Priority-1
  Predictive Risk Playbook on its own. Only a real model score does.
- The seat-contraction, success-plan, F2F, onboarding-stagnation and ROI-AI rules fire
  **only on real data**. If the underlying signal is not connected, no task is invented.

The **payment "No Chasing" rule** is encoded exactly as the WoW framework states it: days
1-14 past due are fully automated with no CSM task; at day 15 a task is raised only for a
high-ARR Strategic account (executive outreach); everything else auto-suspends with no task.

### 2.3 How the daily list is prioritised and sized
If we simply showed every firing rule, a CSM with a rough book could face a hundred-item
list and freeze. The daily focus is therefore **capacity-shaped**, and it never hides risk:

1. **All Protect work (P1 and P2) is always in the focus list.** Churn and revenue
   protection are never deferred - capping them would be unsafe.
2. The **remaining daily capacity** (a configurable number of tasks per CSM, default 20) is
   then filled with the top of the Expand/Use tail, in priority order and then by ARR, so the
   highest-value growth and adoption work comes next.
3. Anything beyond capacity is **deferred, not dropped** - it stays in the full queue and is
   shown as a count ("+N more this week"), broken down by mandate, so the CSM knows the tail
   exists.
4. If Protect work **alone** exceeds the daily capacity, the platform raises an
   over-capacity signal - a genuine, data-backed indicator that this book is overloaded and
   needs rebalancing or headcount, which feeds the leadership capacity view.

The whole list is owner-scoped: a CSM sees only their own focus; a leader can see across the
team. This is the mechanism that turns "CSMs as orchestrators" from a slogan into a concrete
daily queue: the system decides what matters today, in what order, and how much, and the CSM
executes and escalates.

### 2.4 How this maps to the weekly rhythm
The same prioritised queue powers the time-blocked rhythm: Monday portfolio review reads the
whole queue and the week's deferred tail; the daily Priority-1 block is exactly the Protect
set above (ML risk, sudden seat drops, Sev-1); the renewal and expansion blocks are the P3/P4
Expand tasks; the adoption block is the P5 Use tasks.

---

## Open items to confirm with you
1. **Round-robin mechanism** - keep least-loaded (recommended) or switch to literal rotate?
2. **Daily capacity** - 20 tasks/CSM is the default; tell us the right number per segment.
3. **Channel wiring order** - which of the five inbound channels do you want connected first?
4. **F2F cadence window** - currently 90 days for tier-1 strategic; confirm or adjust.

Nothing in this document is aspirational: each behaviour above is implemented and covered by
automated tests in the deployed build. The "not yet connected" notes are the only gaps, and
they are integration/config steps rather than missing platform logic.
