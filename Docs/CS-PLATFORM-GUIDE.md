# CS Platform Guide

## Purpose

The CS Platform is the single pane of glass for Customer Success. It combines live account signals, signed Customer Success Ways of Working (WoW), deterministic routing, and Agent Automation into one governed action system.

The operating principle is:

> The platform originates the prioritised work. The CSM orchestrates the resolution.

The platform does not silently invent actions, replace the CSM, or treat missing data as healthy data.

## How the platform works

The platform follows a simple path from customer signals to CSM action:

1. **Collect live signals.** Connectors read account information from HubSpot, Zendesk, Stripe, Pendo, and Redshift. Jiminny and Rocket Lane can add call intelligence and onboarding data when they are configured. These connectors read data; they do not silently change customer records.

2. **Join the account story.** The platform uses the canonical account ID to connect records from different systems. This lets it recognise that the CRM account, support organisation, billing customer, product account, and churn record refer to the same customer. Each value keeps its source label, so a CSM can see where it came from.

3. **Apply the signed CS playbook.** The approved Ways of Working classify each account signal into one of three mandates:
	- **Protect:** act on active risk, payment exceptions, or lifecycle recovery.
	- **Expand:** act on eligible renewal or growth signals.
	- **Adopt:** act on adoption, onboarding, or Strategic contact hygiene.

4. **Respect the customer segment.** Strategic accounts receive high-touch actions. Scaled accounts stay in automated, exception-based flows unless a real exception requires a CSM. This prevents the long-tail portfolio from becoming a manual queue.

5. **Remove misleading noise.** The multi-instance guard checks whether support or usage activity came from the primary customer instance. Spikes concentrated on test or secondary instances are recorded as suppressed instead of becoming false churn alerts.

6. **Build one prioritised queue.** The rules add the priority, owner, trigger, evidence, SLA, recommended action, and any data gaps. P1 active risk appears before P2 recovery, expansion, renewal, and hygiene work.

7. **Check the result before release.** The deterministic judge checks that every action has evidence, the priority is correct, the segment routing is respected, suppressed signals did not create risk, and no expected task was omitted. The Agent also checks its own answer for unsupported numbers or account claims.

8. **Show the same truth everywhere.** The Dashboard, Task Queue, Accounts drawer, and CS Agent use the same verified action packet. A CSM does not get one answer in the UI and a different answer from the Agent.

9. **Keep the CSM in control.** The CSM opens the evidence, checks the owner and source coverage, reviews the draft or recovery note, and decides what to send or escalate. The platform recommends and explains; it does not replace the CSM's judgement.

10. **Improve through governed feedback.** If a recommendation is wrong or incomplete, the CSM can record feedback. The system uses anonymous feedback to improve prompts, tests, validators, or playbook proposals. A signed rule changes only after review, testing, versioning, and release.

In one sentence:

> Live signals become source-labelled evidence, signed rules turn evidence into priorities, the judge checks the result, and the CSM decides what happens next.

## Workspace

### Dashboard

**Purpose:** Start the day with the most important portfolio decisions.

**Shows:**

- Account count and current portfolio scope
- Priority 1 active risk requiring action within 24 hours
- Portfolio ARR and ARR in red health accounts
- Suppressed multi-instance noise
- Daily CS Brief with the top actions, owner, SLA, confidence, and source context
- Portfolio health ordered from worst to best

**How to use it:**

1. Start with the Daily CS Brief.
2. Open the first action and confirm the evidence and owner.
3. Review suppressed noise before treating a support spike as churn.
4. Use the account drawer for the full signal and data-gap context.

**Important:** P1 is active intervention risk. Churned-account recovery is a separate P2 action for today and should not be interpreted as a live save motion.

### Task Queue

**Purpose:** Show every system-generated action in priority and SLA order.

**Priority model:**

- P1: active risk, within 24 hours
- P2: payment exception, overdue renewal escalation, or churned-account recovery, today
- P3: expansion trigger, this week
- P4: renewal milestone, before the renewal milestone
- P5: Strategic contact or adoption hygiene, this week
- P6: automated or programmatic work for Scaled flows

**Each action includes:** account, segment, mandate, trigger, evidence, owner, SLA, source provenance, data gaps, and where appropriate a reviewed draft or internal recovery note.

**How to use it:**

1. Work from the top of the queue; P1 active risk comes before P2 recovery and P5 data hygiene.
2. Click an action row or the `Open account evidence` link in the Daily CS Brief.
3. Confirm the account drawer's evidence, owner, source coverage, data gaps, contacts, and recommended action.
4. Use the draft or recovery note as a starting point, then apply human judgement before sending or escalating.
5. Do not create a second personal priority list outside the queue without recording why.

**Why an account can have no open task:** An account can be At risk without appearing in the manual queue. For example, a Scaled account with churned status, stale usage, or a Pendo advisory alone remains in an automated workflow under the signed exception-only rule. Its account drawer can show health and payment context while `Tasks` correctly says `No open tasks`.

### Accounts

**Purpose:** Inspect the whole book and open a detailed account view.

**Shows:**

- Account and segment
- Explicit health state: Healthy, Watch, or At risk
- Numeric health score
- ARR and renewal date when available
- Live source coverage, such as 5/7 sources

**Account drawer includes:** churn status or score, CSAT, support signals, usage recency, payment state, instances, lifecycle, health drivers, contacts, tasks, source coverage, and guarded HubSpot write-back preview.

**How to use it:**

1. Start at the top of the worst-health-first list.
2. Click anywhere on an account row to open its evidence drawer; the account name is not the only clickable target.
3. Read the health drivers before deciding whether the account needs a task.
4. Use `Live coverage` to interpret `no data`: a lower ratio means fewer source systems returned usable live signals.
5. Review the drawer's Tasks section. An At risk account can correctly show `No open tasks` when its Scaled workflow is automated and no manual exception applies.

For example, a row such as `At risk · 30` with `5/7 sources` means the health score is 30 and five of seven tracked source blocks are live; it does not mean every missing source is a failure.

### Agent Automation

**Purpose:** Ask the CS Agent natural-language questions about the live portfolio.

**Interaction model:**

- Enter a command in the `@cs-orchestrator:` composer.
- Press Enter to send.
- Use Shift+Enter for a new line.
- Continue the conversation with follow-up questions.
- Review the concise human answer first.
- Expand evidence and verified actions when needed.
- Open the audit trace only when reviewing self-correction or delegation details.

**What the CS Agent does:**

- Uses the deterministic queue as its source of truth.
- Explains priorities in natural language.
- Separates active risk, lifecycle recovery, expansion, adoption, and data quality.
- Delegates specialist analysis when needed.
- Validates numeric and categorical claims against account-scoped evidence.
- Corrects unsupported answers before release.

**What it does not do:**

- It does not silently change signed rules.
- It does not apply HubSpot writes by default.
- It does not execute destructive vendor actions.
- It does not silently train on customer conversations.

**Self-correction:** The normal conversation shows a simple status such as "Initial draft corrected before release". Raw validator details remain in the collapsed audit trace.

**Self-learning loop:** The Agent improves through governed feedback rather than silent model training:

1. The deterministic validator corrects an answer during the current run.
2. CSM feedback is stored as metadata only: rating, category, action count, judge result, and an anonymised question hash. Customer answer prose is not stored.
3. Repeated feedback categories become calibration guidance for future Agent runs, such as citing account-specific evidence, explaining priority choices, or avoiding irrelevant queue repetition.
4. A recurring issue can become a Playbook Controls proposal with evidence, owner, risk, tests, and rollback.
5. Approved changes are reviewed, versioned in the signed playbook, regression-tested, and released explicitly.

The loop can improve prompts, validators, tests, and signed rules. It does not automatically fine-tune a model, mutate the playbook, or learn silently from customer conversations.

**Where to give feedback:** Harness Feedback appears below the Agent conversation
because it evaluates the answer the CSM just received. It is not a separate
navigation page. Use Playbook Controls when repeated feedback needs to become a
formal signed-rule proposal with evidence, tests, owner, and rollback.

## Playbooks - Three Mandates

### Protect - Actions

**Purpose:** Manage active risk, payment exceptions, and lifecycle recovery.

**Includes:**

- Active predictive risk and severe disengagement
- Open Sev-1 and validated multi-signal risk
- Strategic Day-15 high-ARR payment escalation
- Churned-account recovery or closure routing
- Scaled exceptions only when the signed threshold or Sev-1 rule is met

**Key distinction:** A churned account is not the same as an active save opportunity. It receives a recovery action, not a predictive-risk outreach message.

**How to read the page:**

- A Strategic account such as Ignite with severe disengagement and no confirmed churn status appears as a P1 Predictive Risk action with a reviewed customer outreach draft.
- A Strategic account already marked `Churned`, such as Naval Group Australia, Salt Solutions, or Football Federation Australia, appears as a P2 recovery action for today with an internal recovery note rather than customer outreach.
- A Scaled account such as PlusPeople can show At risk health, payment ageing, and Churned lifecycle status while showing `No open tasks`. Under the signed Scaled exception rule, routine lifecycle, stale-usage, Pendo, and payment signals remain automated unless a true manual exception is raised.
- Click any Protect row to open the account evidence drawer and confirm the source signals, owner, data gaps, and recommended action.

On the current portfolio, the Protect page count includes four actions: one active P1 risk and three P2 lifecycle recoveries. It does not mean four active customers are currently at risk of churn.

### Expand - Renewals

**Purpose:** Find eligible growth and renewal actions.

**Renewal cadence:** T-120, T-90, T-60, and T-30 for eligible Strategic accounts.

**Expansion triggers:**

- License utilisation at or above 85%
- API usage velocity at or above 1.4x the previous period
- Strong live adoption on a healthy Strategic account

**Gating:** The account must be healthy, not churned, and free of an open Sev-1. Scaled accounts do not receive routine expansion tasks.

**Empty state meaning:** If no trigger is eligible, the page explains the missing gate, such as unavailable renewal dates or unmapped Pendo telemetry. No action is invented.

### Adopt - Use

**Purpose:** Manage adoption, onboarding, and Strategic contact hygiene.

**Examples:**

- Stale product activity for a Strategic account
- Low active-user percentage
- Low key-feature adoption
- Stalled or at-risk onboarding when Rocket Lane is connected
- Missing Executive Sponsor, Primary Champion, or Finance Contact on Strategic accounts

**Scaled behavior:** Stale usage, Pendo advisory, churn status, and routine contact hygiene remain automated for Scaled accounts. A manual CSM task is created only when an explicit exception is raised.

## Data and Governance

### Lifecycle

**Purpose:** Show onboarding velocity alongside adoption.

**Shows:** onboarding status, time to value, onboarding health, feature adoption, and days since last visit.

If Rocket Lane or deeper Pendo fields are not connected, the page shows `not connected` or `no data`. It does not convert missing telemetry into a score.

### Leadership KPIs

**Purpose:** Support capacity planning and portfolio allocation.

**Shows:**

- Portfolio allocation by CSM
- Book ARR
- Open and Priority 1 tasks
- At-risk accounts
- Completed and overdue tasks
- Average task age
- SLA adherence
- Capacity utilisation
- Task load by Protect, Expand, and Adopt

A blank KPI result indicates a backend or data problem, not that the team has no workload. The API must return a JSON-safe KPI payload.

### Data Gaps

**Purpose:** Make data quality work visible and actionable.

**Shows:**

- Source coverage by account
- Missing HubSpot fields
- Missing required contact roles
- Accounts absent from Zendesk, Stripe, Pendo, Jiminny, Rocket Lane, or Redshift

The source legend separates connector availability from account coverage: green source labels mean the connector returned usable data; grey labels mean the connector is configured but no matching account record was found. A missing-system cell names the specific account-to-source mapping that needs attention. For example, if only `Pendo` appears for one account, Pendo is the only missing source record; it does not mean all sources are disconnected.

Data Gaps is where routine Scaled hygiene belongs. It should not pollute the manual Scaled action queue.

### Integrations

**Purpose:** Show which systems are connected, what they contribute, and whether they are read-only or bi-directional.

**Default safety:**

- Vendor reads are read-only.
- Stripe uses a restricted read-only key by default.
- Redshift uses read-only Data API queries.
- HubSpot write-back is dry-run by default and requires explicit configuration plus an apply flag.
- Missing mappings appear as source gaps.

### Playbook Controls

**Purpose:** Govern changes to the signed CS Ways of Working.

**Proposal process:**

1. Submit the problem, affected accounts, rule section, evidence source, proposed behavior, risk, owner, rollback, and tests.
2. CS Leadership assesses intent and customer impact.
3. RevOps verifies source evidence and routing.
4. Engineering adds positive, negative, boundary, suppression, and rollback tests.
5. The proposal may be requested for changes, approved for implementation, rejected, or archived.
6. An approved change is versioned in the signed playbook, tested, and released.

Approval does not silently change runtime behavior. The implementation and release remain explicit and auditable.

## Source and trust language

Use these labels consistently when explaining an account, task, or Agent answer. They describe what the platform knows, how it knows it, and what still requires human judgement.

| Label | Meaning | Example | CSM interpretation |
| --- | --- | --- | --- |
| **Live** | A configured source adapter returned a value for this account. | `Redshift churn status: Churned` | Treat it as source-backed evidence, while still checking whether the signal is current and relevant. |
| **Computed** | The platform derived a transparent score from live signals; it is not an ML prediction. | `computed risk: weighted live signals` | Use it as explainable supporting evidence, not as a claim that a model predicted churn. |
| **Not connected** | The source, credential, account mapping, or telemetry mapping is unavailable. | `Jiminny: not connected` or `Rocket Lane: not connected` | Do not infer a healthy or unhealthy value. Route the issue to Data Gaps or RevOps. |
| **No data** | The source is available, but it returned no value for this particular field or account. | `renewal date: no data` | Treat the field as unknown. Do not use it to qualify a renewal or expansion action. |
| **Data gap** | A required field, role, source record, or mapping is missing. | `missing Finance Contact` or `10 accounts not in Stripe` | This is a remediation item, not evidence of customer risk by itself. |
| **Suppressed** | A signal was deliberately excluded by the multi-instance guard because it came from a test or secondary instance. | Ticket spike concentrated on `um-dev` | The noise was checked and intentionally did not become a risk task. |
| **Verified** | The deterministic queue, evidence checks, and WoW judge passed before the Agent answer was released. | `WoW judge: PASS` | The answer is grounded in the current action packet, but still requires CSM review. |
| **Self-corrected** | An initial Agent draft failed a validation check and was regenerated before release. | `Initial draft corrected before release` | The final answer passed; the detailed correction history is available in the audit trace. |
| **Human approval** | A person remains responsible for the consequential action. | Sending outreach or applying HubSpot write-back | Review the evidence, adapt the draft, and approve the action before it leaves the platform. |

### Three rules for interpreting trust

1. **Missing is not healthy.** `Not connected`, `no data`, and `data gap` never become a guessed score or a positive health assumption.
2. **Computed is not predicted.** A transparent fallback can support prioritisation, but it must not be presented as an ML churn probability.
3. **Verified is not automatic.** The harness validates the recommendation; the CSM still owns the customer-facing decision and any write action.

## Judge/demo summary

The strongest five-minute story is:

1. Start on Dashboard and show the daily queue.
2. Open Agent Automation and ask: `What are my top 3 actions today?`
3. Show Ignite as active P1 risk.
4. Show churned accounts as recovery actions, not active saves.
5. Open evidence only when a judge asks how the answer is trusted.
6. Show Playbook Controls to demonstrate governed improvement rather than silent self-training.

The memorable operating principle remains:

> The CS Agent explains. The rules decide.
