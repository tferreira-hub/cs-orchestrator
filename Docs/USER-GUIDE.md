# CS Platform — User Guide

**The CIA · Customer Intelligence Agent** · `https://csplatform.jobadder.tools`

This guide walks each of the three Customer Success roles through the platform **page by
page**: what to open, when to look, what to click, and what each panel means. It is
written against the live build.

> **One idea to hold onto:** the platform originates the work. You don't go hunting through
> HubSpot, Zendesk, Stripe and the warehouse — the platform synthesises them into **one
> prioritised queue** and drafts the action. Your job is to orchestrate, approve, and build
> the relationship. (Ways of Working §1: *CSMs are Orchestrators, not Originators*.)

---

## Core concepts (read once)

- **The Three Mandates.** Every task the platform raises is tagged with one of three
  mandates, and the queue is ordered by them: **Must Protect** (churn/risk), **Must Expand**
  (renewals/upgrades), **Must Use** (adoption/onboarding). When you look at any task, the
  mandate tells you *why* it exists.
- **Two motions: Strategic vs Scaled.** **Strategic (High-Touch)** = named, top-ARR accounts
  with a 1:1 CSM and a scheduled cadence (Use Case 2). **Scaled (Tech-Touch)** = the pooled
  sub-$10k long tail, worked exception-only from a shared queue (Use Case 1). The platform
  routes and prioritises differently for each — you'll see the right motion for your role
  automatically, and leaders can toggle between them.
- **Health bands.** Every scorable account is **Healthy (green) / Passive (amber) /
  At-Risk (red)**, computed from live signals (churn, CSAT, usage, seat movement, NDR, call
  sentiment, ROI AI). An account with **no live signal is shown as "not scored"**, never a
  fake green.
- **The WoW judge.** Before any queue is shown, a deterministic judge checks it against the
  signed playbook. The top-bar **"WoW judge: PASS"** badge means the priorities, evidence
  and routing all obey the rules.
- **Everything is evidence-grounded.** Every score, forecast and draft cites the live signal
  behind it. If the data isn't there, you see "no data" — never a guess.

---

## Getting in & finding your way

1. **Sign in** at `csplatform.jobadder.tools` with your JobAdder SSO (Okta → Identity
   Center → Cognito). You land on the **Command Center**.
2. **Your role is automatic.** Admins/CS Leadership see the whole book; a CSM sees only
   their own accounts. You never pick a role — it comes from your SSO group.
3. **The left sidebar** is grouped into sections. Click a section header (e.g. *CS
   Leadership*) to expand it. The sections are:
   - **CS Day to Day** — Dashboard, Command Center, Operating Rhythm
   - **CS Leadership** — Leading/Lagging Indicators, Portfolio Reviews, Team Leaderboard,
     Exec F2F Cadence
   - **Onboarding** — Implementation Governance
   - **Pooled CS** — Overview, Inbox, Process, NRR
   - **Risk** — Risk & Escalation
   - **Expansions** · **Objectives & Adoption** · **Reports** · **Management**
4. **The top bar** always shows: which live sources are connected, the **WoW judge**
   verdict (PASS = the queue obeys the signed playbook), today's date, your name, and the
   **✦ Ask Jane** button.
5. **The global filter bar** (Cohort · Segment · Owner · Health · Lifecycle) + search
   narrows every list on every page at once. The count ("4,312 companies") reflects the
   whole active book.
6. **Click any company row anywhere** to open its **Account 360** drawer.

### Ask Jane (available on every page)
Click **✦ Ask Jane** (top bar) or the launcher (bottom-left). Jane is your CS analyst: she
answers in plain language, **grounded only in live evidence and the same prioritised queue
you see** — never invented. She's scoped like you are (a CSM only gets answers about their
own book). Ask things like *"What are my top 3 actions today?"*, *"Why is Northwind a
risk?"*, *"Which renewals need me this week?"*. Every answer is logged to the Audit Trail.

---

## Use Case 1 — Scaled CSM (Pooled / Tech-Touch)

**You cover a share of ~2,500 sub-$10k accounts across a 4–5 person pool.** You don't own
accounts individually — you work a shared queue. Your mandates are **Protect** and **Use**.

### Start your day → Pooled CS → Inbox
Sidebar → **Pooled CS → Inbox**.
- This is the unified inbound queue. Items from all channels (Zendesk misroutes, logged
  calls, the `accountmanagement@` mailbox, campaign replies, high-intent web forms) land
  here already **triaged**:
  - **Technical** ("login crash") → handed off to Zendesk — it never clogs your queue.
  - **Billing/Account** ("invoice question") → a pooled ticket, round-robin assigned.
  - **Expansion** ("add 10 seats") → flagged a **high-priority CSQL**.
- Each ticket shows its **intent**, **who it's routed to**, and a **24-hour SLA** clock.
  Rows breaching SLA sort to the top.
- **Set yourself Available / OOO** with the presence toggle. Round-robin only assigns to
  *available* CSMs; if a ticket sits 20 hours unanswered (or its owner goes OOO) it
  **auto-reassigns** to the next available person. Duplicate messages from the same sender
  within 2 hours are merged automatically.

**When to look:** first thing each morning and after lunch. The SLA clock is your cue —
anything approaching 24h needs a response.

### Work a ticket
Click the ticket → it opens the **Account 360** for that customer. You get, on one screen:
ARR tier, health score, payment status, recent usage, Rocket Lane onboarding milestone,
monthly-report engagement, and the contact architecture. Act, then mark the ticket resolved
(or hand off). No need to open HubSpot or Zendesk.

### Your risk queue → Command Center
Sidebar → **Command Center**. The task queue here is **risk-first**: Priority-1 ML churn
(>70%) and **sudden seat/usage drops (>20% in 14 days)** sit at the top, then CSQLs, then
SLA-bound inbound. Each P1 risk comes with a **drafted outreach** you can review and send.
Multi-instance noise (test/sandbox) is already suppressed, so you're not chasing ghosts.

**Also surfaced here automatically:** an **onboarding/Rocket Lane stagnation** alert when a
pooled account stalls in implementation, so a stuck onboarding becomes a visible task
instead of a silent churn risk.

### Edge cases the platform handles for you
- **Missing Primary Admin on the 1st** → the monthly digest can't be addressed, so the
  platform raises a **data-cleanup task** in the pooled queue (tag the admin, don't send a
  report into the void).
- **Owner goes OOO / 20h no response** → the ticket auto-reassigns to the next available
  CSM.
- **Same customer messages twice within 2 hours** → merged into one ticket.

**Triage accuracy (a KPI you'll feel):** technical issues are handed to Zendesk at
classification, so the target of **zero technical tickets sitting in the CS queue** is
enforced structurally — your pooled queue only ever holds account/billing/expansion work.

### Prove the team is keeping pace → Team Leaderboard
Sidebar → **CS Leadership → Team Leaderboard** (visible to the pool). See your outreach vs
target, completion rate, and the **weekly 90% target-compliance** figure. It keeps the pool
honest and surfaces who needs backup.

### Monthly value reporting (automatic)
On the **1st of each month**, the platform compiles a performance digest for each account's
Primary Admin (seat utilisation, ROI AI adoption, tickets resolved, CSAT) with an **"Add
Seats / Upgrade" CTA when utilisation > 85%**. You don't build these.
> *Live-send status:* the digest **compiles and schedules** today; the actual email send
> switches on once the HubSpot transactional template is configured (RevOps task). Until
> then it shows "prepared".

### What Jane does for you here
*"What's breaching SLA right now?"* · *"Which pooled accounts are a churn risk this week?"*
· *"Draft a reply for this billing ticket."* Jane reads the same queue and drafts grounded
answers.

---

## Use Case 2 — Strategic CSM (High-Touch, named accounts)

**You own a small book of top-ARR named accounts.** Mandates: **Protect, Expand, Use.** Your
week is structured for you.

### Monday → Operating Rhythm
Sidebar → **CS Day to Day → Operating Rhythm**. The platform time-blocks your week:
- **Monday Analytics & Portfolio Review** — portfolio health shifts, open risk tasks,
  priorities for the week.
- **Daily Priority-1 (Defensive Risk)** — ML churn (>70%), sudden seat contraction, and
  Sev-1 incidents, surfaced automatically.
- **Weekly Renewals & Expansion** and **Adoption & QBR** blocks.

**When to look:** Monday morning to plan; the P1 block every day before anything else.

### Your accounts → Command Center → My Companies / My Renewals
Sidebar → **Command Center**, then the **My Companies** and **My Renewals** tabs.
- **My Renewals** shows every dated account with a countdown and an **AI Forecast**
  (Renewal / Churn Risk / Expansion) — each with a one-line, evidence-grounded rationale
  (e.g. *"Churn Risk, Redshift churn status: Churned (-40)"*). Overdue renewals sort first.
- The **renewal cadence** fires tasks automatically: **T-120** internal risk check →
  **T-90** value/exec outreach → **T-60** commercial proposal → **T-30** contract close.
- **If an account hits T-90 with no Executive Sponsor tagged**, you get an **urgent
  (P2) data-gap task** — you can't run an executive renewal with no economic buyer named.

### Open an account → Account 360
Click any account row. The **Strategic Account 360** shows:
- Contract **renewal countdown** and AI forecast.
- **Rocket Lane implementation board** (onboarding status/milestones).
- **Executive Sponsor F2F log** — record a meeting here; it writes **back to HubSpot**
  (`cs_last_exec_f2f_date` + outcome) so Sales sees it too.
- **Health trendline** combining product usage, ROI AI, CSAT and Jiminny call sentiment,
  plus call summaries and the **contact hierarchy** (Exec Sponsor / Champion / Finance).

### Before you close a renewal or onboarding task
The platform **enforces contact hygiene**: if the account is missing an Executive Sponsor,
Primary Champion/Admin, or Finance Contact, trying to mark the task complete is **blocked**
until you tag the roles. Tag them from the Account 360 contacts panel.

### Executive touchpoints → Exec F2F Cadence
Sidebar → **CS Leadership → Exec F2F Cadence**. See which tier-1 strategic accounts are due
an executive face-to-face and which are overdue, against the 100% cadence target. Log the
meeting outcome from the account's F2F panel.

### Expansion → Expansions
Sidebar → **Expansions**. Accounts that crossed a signed expansion trigger appear as
**CSQLs** with the ARR headroom. Triggers include **85% licence utilisation**, an **API
surge**, a **premium-feature / module usage spike**, and a **ROI AI adoption spike**. Work
them in your weekly Expansion block.

### Edge cases the platform handles for you
- **Multi-instance false positives:** alerts are tied strictly to the account's **primary
  revenue-generating instance** — activity on test/sandbox/secondary instances never drives
  a risk flag on a strategic account, so you don't get paged for a sandbox spike.
- **Unreviewed monthly draft by the 31st:** the platform sends a safe **baseline** report on
  the 1st rather than nothing (see Portfolio Reviews below).
- **T-90 with no Executive Sponsor:** an urgent data-gap task (above).

### Payment risk (you only act at Day 15)
You never chase early. Days 1–14 past-due are fully automated. **Only at Day 15** on a
high-ARR account does a **Day-15 Payment Risk** task appear (executive-outreach playbook).
Scaled accounts auto-suspend with no task. Find these on the **Command Center** queue or
**Reports → Payment Risk Report**.

### Month-end → Portfolio Reviews
Sidebar → **CS Leadership → Portfolio Reviews**. From the **28th**, draft monthly reports
are pre-populated for your named accounts. Between the 28th–31st: **add your executive
comments and approve** each for dispatch. On the **1st**, approved reports go to the Primary
Admin + Exec Sponsor (and log to the HubSpot timeline). If you don't review one by the 31st,
the platform sends a safe **baseline** version automatically.

### What Jane does for you here
*"Prep me for my QBR with Globex."* · *"What changed on my at-risk accounts this week?"* ·
*"Which of my renewals is missing an Exec Sponsor?"*

---

## Use Case 3 — CS Leader (Head of CS / Director)

**You run governance, capacity, and revenue outcomes across both teams.**

### Your home → Command Center (whole book)
Sidebar → **Command Center**. The **KPI strip** is your headline:
- **Total ARR**, **Portfolio GRR %** (target 92), **Portfolio NDR %** (target 100),
  **ARR at Risk ($)**, **Expansion Pipeline ($)**, upcoming QBRs.
- These exclude internal/test instances, so the numbers are the real book.
- Use the **Segment / Cohort filter** to flip between Strategic (High-Touch) and
  Scaled/Pooled views instantly.
- **Company Distribution by Health**, **Portfolio Growth by segment**, and **Renewals by
  Month** give you the shape of the book at a glance.

**When to look:** daily for at-risk ARR; weekly for the retention and pipeline trend.

### Churn & risk → Risk & Escalation
Sidebar → **Risk → Risk & Escalation**. The **ML Churn Risk Matrix** groups the ≥70%-churn
cohort (and sudden-seat-drop accounts) by **primary risk driver and ARR impact**, so you can
see where the dollars are and why. Escalations for high-ARR Day-15 payment and
Exec-Sponsor-intervention accounts surface here too. You also get **24h adherence**: the
platform tracks whether a defensive playbook was *initiated within 24 hours* of each risk or
sudden-drop alert, so you can see the WoW SLA being met (or not) in real time.

### Portfolio health shape → Leading & Lagging Indicators
Sidebar → **CS Leadership → Leading Indicators** (forward-looking signals: usage, adoption,
risk building) and **Lagging Indicators** (realised GRR/NDR and revenue outcomes). Together
with the **Company Distribution by Health** panel on the Command Center, this is the
**Healthy / Passive / At-Risk** breakdown of the whole book — the aggregate view of where
the portfolio stands and where it's heading.

### Team execution → Team Leaderboard & SLA
Sidebar → **CS Leadership → Team Leaderboard**. Live per-CSM completion rates, outreach vs
target, overdue counts, capacity utilisation %, and the **weekly 90% compliance** figure.
The platform also computes **first-response SLA compliance (vs 95%)** and **report-delivery
(vs 98%)**, and the pooled queue analytics show **average response and resolution times** —
your governance evidence that the WoW is being followed.

### Onboarding handoffs → Implementation Governance
Sidebar → **Onboarding → Implementation Governance**. Active Rocket Lane projects, time in
onboarding, **stalled-before-handoff** alerts, and the **on-time handoff rate (vs 90%)** so
you catch a stuck implementation before it becomes a churn risk.

### Data hygiene → Data Gaps & Admin Coverage
Sidebar → **Management → Data Gaps**. The book-wide list of accounts missing a Primary
Admin, Executive Sponsor, or Finance Contact — the cleanup worklist that keeps every
automation accurate. (The admin-coverage roll-up shows ARR at risk per missing role.)

### Reporting coverage → monthly reporting audit
The platform tracks **monthly report delivery coverage** (target 100% of active accounts
reaching their Primary Admin) and surfaces **CSQL hand-raisers** — accounts that replied to
a digest or campaign with expansion intent. *Open/click-through rates per digest become
available once live email send is switched on (RevOps task); until then you see delivery
readiness, not opens.*

### Capacity & revenue
Capacity per CSM (account load, open playbooks, ARR, utilisation %) supports headcount and
rebalancing decisions — on the Leaderboard and `/api/kpis`. Revenue impact (GRR/NDR +
expansion ARR from triggers) is on the Command Center KPI strip and **Reports**.

### Reports → Tableau & Payment Risk
Sidebar → **Reports**. Embedded Tableau dashboards (SSO, no second login) and the live
**Payment Risk Report**.

### Health of the data itself
The top bar shows which sources are **live**. If a core source (HubSpot/Stripe/Zendesk/
Pendo/Churn) or a configured secondary source (ROI AI / Rocket Lane / billing sync) stops
delivering, a **degraded banner** appears — so you know a number is stale *before* you act
on it. **Management → Integrations** shows the full connector status; **Audit Trail** is the
immutable log of every action and Jane answer.

### What Jane does for you here
*"Where is my at-risk ARR concentrated?"* · *"Which CSMs are below their weekly target?"* ·
*"Summarise this month's churn drivers."*

---

## Other pages you'll use

- **Dashboard** (CS Day to Day → Dashboard): your personal landing view — a lighter,
  single-screen summary of your book before you dive into the Command Center.
- **Objectives & Adoption** (sidebar → Objectives & Adoption): customer success-plan
  objectives and product-adoption signals; the **Must Use** mandate lives here. An
  off-track or overdue success-plan goal raises a Protect task.
- **Playbook Controls** (Management → Playbook Controls): the Ways-of-Working rules the
  engine enforces. Admins can propose a change here; changes require a second admin to
  review and approve (segregation of duties) — so the signed playbook can evolve safely.
- **Audit Trail** (Management → Audit Trail): the immutable, hash-chained log of every
  action taken and every Jane answer — who, what, which sources were read, and when. This
  is your compliance and "why did the system do that?" record.
- **Integrations** (Management → Integrations): the live status of every connector
  (direction, what it supplies, connected/configuration-required/access-pending).
- **Reports** (Reports → Tableau Reports / Payment Risk Report): embedded Tableau
  dashboards via SSO, plus the live Stripe-sourced Payment Risk Report.

---

## Quick reference — where everything lives

| I want to… | Go to |
|---|---|
| See my prioritised actions | **Command Center** (task queue, risk-first) |
| Work inbound tickets (pooled) | **Pooled CS → Inbox** |
| Plan my week (strategic) | **Operating Rhythm** |
| See renewals & forecasts | **Command Center → My Renewals** |
| Open one account's full picture | Click any **company row** → Account 360 |
| Log an executive meeting | Account 360 → **F2F panel** (or **Exec F2F Cadence**) |
| Review & approve monthly reports | **Portfolio Reviews** (28th–1st) |
| Find expansion plays | **Expansions** |
| See churn risk by ARR & driver | **Risk → Risk & Escalation** |
| Track the team | **Team Leaderboard** |
| Watch onboarding handoffs | **Implementation Governance** |
| Fix missing contacts/data | **Management → Data Gaps** |
| Ask a question in plain English | **✦ Ask Jane** (any page) |
| Check what's connected | **Management → Integrations** (+ top-bar banner) |

---

## Two honesty notes (so nothing surprises you)

1. **Blanks are truthful, not broken.** If a panel says "no data" or an account has no
   score, it means the source genuinely has nothing for that account — the platform never
   fabricates a number. Those blanks double as your data-cleanup worklist (Data Gaps).
2. **Two things need a RevOps switch to go fully live** (not platform gaps): the **monthly
   digest email send** (needs a HubSpot template) and the **5 live inbound channels** (needs
   the Service Hub Pro decision). Everything else above is live today. See
   `Docs/REVOPS-HANDOFF-EXTERNAL-BLOCKERS.md`.
