# JobAdder Customer Success Platform - Complete Documentation

**Version:** Live (main `b32953e`)
**Audience:** CS Leadership, CSMs, RevOps, Engineering
**Status:** In production - JobAdder Tooling AWS account, `https://csplatform.jobadder.tools`

---

## 1. What the CS Platform Is

The CS Platform is an internally built **Customer Success orchestration engine** for JobAdder. It replaces the stalled third-party procurement (Planhat) with a purpose-built system that:

- **Synthesises live signals** from HubSpot, Stripe, Zendesk, Pendo, Jiminny, Rocket Lane, a Redshift ML churn model, and a ROI-AI webhook into **one prioritised queue of actions**.
- **Turns CSMs from data-gatherers into orchestrators** - the engine originates the tasks; the CSM executes and escalates.
- Serves the three strategic goals: **protect revenue (GRR > 92%, NDR > 100%), scale coverage across the sub-$10k book, and automate account value reporting.**

It is a single pane of glass + a deterministic rules engine + an AI analyst ("Jane"), governed by a signed Ways-of-Working (WoW) playbook that is enforced in code by an executable judge.

### Core design principles

| Principle | How it shows up |
|---|---|
| **Never fabricate data** | Every value is either genuinely live or explicitly labelled "not connected" / "no data" / "no record in X". The platform shows zeros/empties for dormant accounts rather than inventing numbers. |
| **Deterministic rules, explainable AI** | The task queue is produced by a pure, testable rules engine. The AI agent is grounded against that queue and the live signals; it never originates unverified numbers. |
| **Two-gate writes** | Any write to an external system (HubSpot, Zendesk) is dry-run by default; a real write requires BOTH an explicit `apply=true` AND the environment flag `CS_ALLOW_WRITE=1`. |
| **Owner-scoping** | A CSM sees and acts only on accounts they own; an admin sees the whole book. Enforced server-side from the signed session, not client hints. |
| **Audit everything** | Every mutation is written to a hash-chained, tamper-evident audit trail. |

---

## 2. Architecture & Integrations

### Runtime

- **Backend:** Python standard-library HTTP server (`platform/server.py`) on ECS Fargate.
- **Engine:** `platform/engine.py` - the scoring, KPI, report, digest, leaderboard, F2F and lifecycle logic.
- **Rules engine:** `plugins/cs-orchestrator/orchestrate.py` - the deterministic Ways-of-Working rules.
- **Judge:** `plugins/cs-orchestrator/playbook_judge.py` - an executable WoW compliance checker.
- **Adapters:** `plugins/cs-orchestrator/adapters/sources.py` - one class per vendor.
- **Frontend:** a single-page app (`platform/ui/index.html`), vanilla JS, no framework.
- **AI agent:** `plugins/cs-orchestrator/agent_runner.py` - "Jane", on Amazon Bedrock (Claude Sonnet).
- **Infra:** Terraform (`infra/`) - ECS, ALB (Cloudflare-only ingress), Cognito SSO, SSM secrets, EFS for persistent state, EventBridge scheduler.
- **Auth:** Cognito OIDC (Authorization Code + PKCE) federated to Okta via SAML; RBAC from Identity Center groups.

### Integration baseline

| System | Direction | What flows | Status |
|---|---|---|---|
| **HubSpot (CRM)** | Bi-directional | Pull: ARR, renewal dates, account hierarchy, contacts, segment, lifecycle. Push: health score, risk status, active playbook, notes, contact roles, CSQL deals, customer tier. | Live |
| **Stripe (Billing)** | Read-only | Invoice status, days past due, dunning stage, ARR. | Live |
| **Zendesk (Support)** | Read + write | Pull: ticket volume, CSAT, Sev-1 flags. Write: reply to / close ticket (gated). | Live |
| **Pendo (Telemetry)** | Read-only | Days since last visit, plan tier, plus **derived** active users & feature breadth (30-day) from the Aggregation API. | Live |
| **Churn Model (Redshift)** | Read-only | ML churn score, model version, risk drivers - or a transparent computed fallback. | Live |
| **Rocket Lane (Onboarding)** | Read-only | Project status, dates, health, stall detection. | Access pending (vendor API) |
| **Jiminny (Conversational AI)** | Read-only | Last call, sentiment, call metadata. | Access pending (vendor API) |
| **ROI AI** | Inbound webhook (HMAC-signed) | Adoption score, active ROI users, ROI realised, trend. | Configured; awaiting first vendor POST |
| **Tableau** | Embed (SSO) | Embedded dashboards via Connected App JWT. | Config-dependent |
| **Entitlements API** | Read-only | Licensed vs active seats, licence utilisation %. | Not yet configured (operator) |

### Persistent state (EFS)

Append-only JSONL stores are mounted on an encrypted EFS volume at `/data` so they survive deploys and are shared with the scheduler process: health-history snapshots, F2F log, ROI-AI events, task events, success plans, and monthly-digest review state. (Before EFS, these lived on ephemeral container storage and were wiped on every deploy - which is why health-trend detection needs calendar time to accumulate forward from first persistence.)

---

## 3. The Ways-of-Working Rules Engine

The heart of the platform. For every account it evaluates a fixed set of rules and emits prioritised, evidence-grounded tasks. Each task carries a stable `rule_id`, a priority (1 = most urgent), a mandate (MUST_PROTECT / MUST_EXPAND / MUST_USE), the evidence that fired it, an SLA due date, and a recommended action.

### The three mandates

1. **Must Protect** - churn and risk defence.
2. **Must Expand** - renewals and upgrades.
3. **Must Use** - adoption and onboarding.

### The rules (priority order)

| # | Rule | Priority | Mandate | Fires when | Segment |
|---|---|---|---|---|---|
| 1 | Predictive Risk Playbook | P1 | Protect | ML churn ≥ 70%, Pendo risk High, ticket-spike + usage-drop, Sev-1, or 180d no visit | Strategic |
| 2 | Churned Account Recovery | P2 | Protect | Account marked churned | Strategic |
| 3 | Sudden Seat/User Contraction | P2 | Protect | Logins or seats drop > 20% in a rolling 14 days | Both |
| 4 | Scaled Exception Escalation | P2 | Protect | Scaled account with ML churn ≥ 85% or open Sev-1 | Scaled |
| 5 | Day-15 Payment ("No Chasing") | P2 | Protect | Day-15+ past due, Strategic, high-ARR (≥ $100k) | Strategic |
| 6 | Overdue Renewal Escalation | P2 | Protect | Renewal date already passed | Strategic |
| 7 | **Success Plan At Risk** | P2 | Protect | A committed success plan is off-track / past deadline | Both |
| 8 | Expansion - Licence Utilisation | P3 | Expand | Licence utilisation ≥ 85% | Strategic (healthy) |
| 9 | Expansion - API Surge | P3 | Expand | API usage ≥ 1.4× prior period | Strategic (healthy) |
| 10 | Expansion - Strong Adoption | P3 | Expand | Strong/increasing Pendo adoption + recent visit | Strategic (healthy) |
| 11 | Expansion - ROI AI Spike | P3 | Expand | ROI AI adoption ≥ 75 and trend up | Strategic (healthy) |
| 12 | Onboarding Stagnation | P3 | Use | Rocket Lane project stalled / blocked / past due | Both |
| 13 | Proactive Renewal Cadence | P4 | Expand | Renewal in T-120 / T-90 / T-60 / T-30 windows | Strategic |
| 14 | Executive Sponsor F2F Cadence | P4 | Expand | Tier-1 strategic account with no F2F logged in 90 days | Strategic |
| 15 | Adoption / Onboarding Intervention | P5 | Use | Multi-signal adoption gap (idle, low active users, low feature adoption) | Strategic |
| 16 | Contact Hygiene | P5 | Use | Missing a required contact role | Strategic |

**Multi-instance suppression:** risk rules only fire on the primary revenue-generating instance; ticket spikes driven by secondary/test instances are suppressed as noise (and the suppression is surfaced for transparency).

**The "No Chasing" rule:** days 1-14 past due are 100% automated (Stripe/HubSpot dunning) with no CSM task. A CSM is only pulled in at **Day 15**, and only for Strategic high-ARR accounts. Scaled accounts auto-suspend.

### The executable judge

Every produced queue is checked by `playbook_judge.py` against the WoW. It verifies: mandate correctness, evidence grounding, priority ordering, the `rule_id` contract, segment routing (Scaled never gets MUST_EXPAND), the No-Chasing payment constraint, required outreach drafts on risk/payment tasks, and task completeness. The verdict (PASS / NEEDS_CHANGES) is shown in the top bar ("WoW judge: PASS").

---

## 4. Health, Expansion, Adoption & Lifecycle Scoring

### Health score (0-100)

Starts at 100, subtracts weighted penalties from live signals:

| Factor | Impact |
|---|---|
| ML churn score | − (score × 40) |
| Churned status | − 40 (caps ≤ 49) |
| CSAT < 75 | − (75 − csat) × 0.5 |
| Usage drop ≥ 30% | − (drop × 25) |
| Open Sev-1 | − 15 |
| Payment 15+ days past due | − 10 |
| No product visit ≥ 14 days | − up to 20 |
| Pendo risk High / Medium | − 18 / − 8 |
| Jiminny sentiment negative / positive | − 10 / + 3 |
| ROI AI adoption high / low | + 3 / − 5…8 |

Bands: **green ≥ 75, amber 50-74, red < 50.** A score is only "computable" when at least one live signal contributed - otherwise the account is honestly "not scored yet".

### Expansion readiness (0-100)

Rewards healthy, engaged accounts with room to grow: health foundation, engagement recency, login momentum, licence utilisation (≥ 85% is the signed trigger), API surge (≥ 1.4×), renewal proximity (T-120…T-30), ARR headroom vs segment median, positive sentiment. Suppressed for churned/red accounts.

### Product adoption (0-100)

Weighted blend of active users, feature adoption/breadth, licence utilisation, usage recency, and login momentum. Where Pendo exposes no pre-computed percentage, the score is derived from **real Aggregation-API usage** (distinct active users and features used in the last 30 days). Idle accounts correctly score 0.

### Lifecycle state

A state machine deriving the stage from live signals: Implementation → Onboarding → Adoption → Value Realisation → Mature, plus the reverse path At Risk and the terminal Churned - each with an evidence-based reason.

### Renewal forecast

Per dated account: **Churn Risk**, **Expansion**, or **Renewal**, with the rationale and evidence that produced it.

---

## 5. The Three Use Cases

### UC1 - Scaled CS (Pooled / Tech-Touch)

For the ~2,500 sub-$10k accounts managed by a pooled team.

- **5-channel inbound intake** → one triage engine: Zendesk misroutes, Slack call logs, the `accountmanagement@` mailbox, campaign replies, high-intent forms.
- **Automated triage** by intent: technical → Zendesk handoff; billing/account → pooled CS queue; expansion → high-priority CSQL.
- **Round-robin allocation** across available pooled CSMs, with a **24-hour SLA**, a **20-hour auto-reassign** (re-evaluated as tickets age, not just at intake), and **2-hour duplicate merge**.
- **Programmatic monthly digest** to Primary Admins with seat utilisation, logins, feature adoption, ROI AI, tickets resolved, CSAT, and an auto expansion CTA when utilisation ≥ 85%.
- **Team leaderboard** ranking CSMs by completion (or workload until completions exist) against weekly targets.
- Edge cases handled: missing admin on the 1st, OOO/SLA-breach auto-reassign, duplicate merging.

### UC2 - Strategic CSM (High-Touch)

For named, top-tier-ARR accounts.

- **Time-blocked operating rhythm:** Monday portfolio review; daily P1 defensive risk; weekly renewals/expansion; weekly adoption/QBR.
- **Monthly report cycle:** drafts pre-populate on the 28th; the CSM reviews, comments and approves through the 31st; on the 1st the platform dispatches approved reports and **auto-sends the baseline for any unreviewed draft** (now scheduled and with review state persisted so it survives restarts and the separate scheduler process).
- **Proactive renewal cadence:** T-120 (risk audit) → T-90 (value review) → T-60 (commercial proposal) → T-30 (contract execution).
- **Executive Sponsor F2F cadence:** tracks the sponsorship field, prompts and logs F2F meetings for tier-1 accounts, records outcomes on the Account 360.
- **Expansion triggers:** licence utilisation, API surge, strong adoption, ROI AI spike.
- **Contact-role close-gate:** renewal/onboarding tasks cannot be completed until Executive Sponsor, Primary Champion/Admin and Finance Contact are tagged.

### UC3 - Executive Visibility & Governance

For the Head of CS / CS Director.

- **Command Center KPI strip:** Total ARR, Portfolio GRR % (vs 92% target), Portfolio NDR % (vs 100%), ARR at Risk ($), Expansion Pipeline ($), Upcoming QBRs.
- **SLA governance reporting** and **capacity/KPI tracking** per CSM (account load, open/overdue/P1 tasks, managed ARR, capacity utilisation).
- **Rocket Lane onboarding governance:** active projects, time-in-onboarding, on-time handoff %, stalled-project callout and workload-by-owner.
- **ML churn-risk matrix:** accounts above the model threshold grouped by primary driver with ARR impact.
- **Admin Coverage Matrix:** book-level role coverage (Exec Sponsor / Primary Admin / Finance) with coverage % and ARR-at-risk per missing role.
- **Data ingestion warning banner** when a core source (HubSpot/Stripe/Zendesk/Churn) is degraded.
- **Segment toggle** between Strategic and Scaled across the dashboards.

---

## 6. The User Interface (navigation map)

- **Top-level:** Data Model, Data Explorer, Calendar, Notifications.
- **CS Day to Day:** Dashboard, Command Center, Operating Rhythm.
- **CS Leadership:** Leading Indicators, Lagging Indicators, Portfolio Reviews, Team Leaderboard, Exec F2F Cadence.
- **Onboarding:** Implementation Governance.
- **Scaled Customer Success:** The Pool, Pooled Dashboard, Inbound Queue.
- **Risk:** Risk & Escalation (Portfolio Health + ML Churn Matrix, Risk Mitigation).
- **Expansions**, **Objectives & Adoption**, **Pooled CS** (Inbox / Process / NRR).
- **Reports:** Tableau Reports, Payment Risk Report.
- **Management:** Integrations, Data Gaps (+ Admin Coverage Matrix), Playbook Controls, Audit Trail.

### Account 360 (company page)

Tabs: Overview, Related Data, Usage, Revenue, Account Planning. Shows the health score and breakdown, Signals (trend risks or current drivers, and positive signals for healthy accounts), key metrics from every live source, revenue & renewal, expansion readiness, lifecycle state, the Rocket Lane implementation board, the Executive Sponsor F2F log, ROI AI telemetry, contacts and role tagging, HubSpot write-back, CSQL creation, and the monthly digest preview.

### Jane - the AI analyst

A docked, account-aware assistant available on every page. It answers questions about the portfolio and specific accounts, grounded against the deterministic queue and live signals: it is numerically validated (fabricated figures are blocked), evidence-cited, judged against the signed playbook, and read-only (it proposes actions; a human approves). A feedback loop lets CSMs rate answers, which nudges future responses.

---

## 7. Security & Governance

- **Authentication:** Cognito OIDC + PKCE, federated to Okta (SCIM-provisioned users). Short-lived signed session cookie; role read only from the signed token (no self-elevation).
- **Authorization:** RBAC from Identity Center groups. `CS-Platform-Admins` → admin (whole book); CS user group → CSM (own book). Owner-scoping enforced in the engine and at every write endpoint.
- **Write safety:** two-gate (apply + `CS_ALLOW_WRITE`), owner-scoped, audited. External handoffs are described, never silently executed.
- **Audit trail:** hash-chained, tamper-evident JSONL of every mutation (writeback, note, role tag, CSQL, F2F, digest send, cohort move, Zendesk write, agent answer).
- **Network:** ALB reachable only from Cloudflare edge; tasks egress HTTPS to vendors + NFS to EFS only.
- **ROI AI webhook:** HMAC-SHA256 signed, verified before the auth gate, idempotent on event_id, fail-closed.

---

## 8. Operations & Deployment

- **Deploy path:** merge to `JobAdder/cs-orchestrator` `main` → GitHub Actions. `build-and-deploy.yml` builds the image and registers a new ECS task-def revision (image swapped onto the latest Terraform-owned shape). `infra.yml` applies Terraform on `infra/**` changes.
- **Terraform owns the task-def shape** (env, secrets, roles, volumes); the deploy pipeline swaps only the image. The service uses deployment circuit breaker + rollback.
- **Caching:** reports use a 600s stale-while-revalidate cache; a cold miss returns a "warming" placeholder and builds in the background so a page load never blocks. Reports are warmed at boot.
- **Scheduler:** EventBridge runs the monthly digest dispatch on the 1st from the same task definition.
- **Verification:** 249 automated tests, SPA JS syntax-checked, `terraform fmt` clean on every change.

---

## 9. Current Data Coverage (honest status)

| Signal | State |
|---|---|
| HubSpot (ARR, renewal, owner, segment, contacts, lifecycle) | Live on the active book |
| Stripe payment/dunning | Live |
| Zendesk tickets / CSAT | Live (CSAT shows "no data" where no survey exists) |
| Churn model | Live (ML where present, else transparent computed fallback) |
| Pendo days-since-visit, plan tier | Live |
| Pendo active users / feature breadth (30d) | Live (derived from the Aggregation API) |
| Pendo adoption/engagement/risk-advisor scores | Not produced by Pendo on this install (genuinely empty) |
| Licence utilisation | Awaiting Entitlements API config |
| Jiminny, Rocket Lane | Awaiting vendor API access |
| ROI AI | Webhook ready; awaiting first vendor POST |
| Health trend risks | Accumulating forward from first EFS persistence (needs ~45 days of history) |

Active accounts show rich data; churned/dormant accounts correctly show zeros/empties - that is the no-fabrication principle working as designed.

---

## 10. What's Pending (operator / vendor actions, not code)

1. **ROI AI feed** - share the signing secret with Chris Coombs so his report can POST to `/api/webhooks/roi-ai`.
2. **Entitlements API** - set `ENTITLEMENTS_API_URL` / `ENTITLEMENTS_KEY` to light up licence utilisation.
3. **Pendo metric mappings** - only if/when Pendo Predict scores become available on the account.
4. **Jiminny & Rocket Lane** - vendor API access.
5. **CS_CSM_TARGETS** - per-CSM outreach targets to activate full leaderboard compliance.
6. **CS_EMAIL_PROVIDER** - connect an outbound provider to actually send the monthly digests (compiled + gated until then).

---

*This document reflects the live platform at main `b32953e`. It is generated from the codebase; where a capability depends on external configuration or vendor access, that is stated explicitly rather than implied.*
