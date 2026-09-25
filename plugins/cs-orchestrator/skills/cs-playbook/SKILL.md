---
name: cs-playbook
description: "The Customer Success Ways of Working (WoW) rules the orchestrator MUST apply: Three Mandates, Strategic vs Scaled routing, risk/expansion thresholds, T-120/90/60 renewal cadence, and the Day-15 payment rule. Load this whenever generating CSM tasks, triaging risk, or prioritising a portfolio."
---

# CS Playbook, Ways of Working Rules

These are the deterministic rules from the signed CS Ways of Working framework.
The orchestrator applies them to signals pulled from the `cs-stack` MCP tools.
**Rules are standardised, there is one way of working. Do not vary output by CSM preference.**

## The Three Core Mandates
Every generated task MUST be tagged with exactly one mandate:
- **MUST_PROTECT**, churn / risk (Priority 1, daily).
- **MUST_EXPAND**, renewals / upgrades.
- **MUST_USE**, adoption / onboarding.

## Segmentation & routing
- **Strategic (High-Touch):** named top-ARR accounts. Get 1:1 tasks for risk, renewal, expansion, QBRs.
- **Scaled (Tech-Touch):** pooled/long-tail. **Exception-based only**, a CSM task is generated ONLY on escalation from an automated flow, not for routine signals.
- Determine segment from `hubspot_get_account.segment`.

## MUST_PROTECT, Predictive Risk Playbook (Priority 1, 24h SLA)
Trigger a **Defensive Workflow** task when ANY of:
- `churn.ml_churn_score >= 0.70`, OR
- multi-signal risk: a **support ticket spike** (tickets_last_7d >= 2x tickets_prev_7d, on a PRIMARY instance) AND a **usage drop** (logins_last_7d <= 0.6x logins_prev_7d), OR
- an open **Sev-1** on a primary instance.
Action: root-cause analysis + executive outreach + internal escalation. SLA: acknowledge & initiate within 24h.
- **Scaled** accounts: do NOT create a manual CSM task for routine risk, stale usage, churn status, or Pendo advisory alone. Escalate only if churn_score >= 0.85 or an open Sev-1 creates a true exception.

## MUST_EXPAND, Expansion triggers
Flag an **upsell conversation** task (Strategic) when:
- `usage.license_utilization_pct >= 85`, OR
- API usage velocity surge: `api_calls_last_7d >= 1.4x api_calls_prev_7d`.
Only when the account is otherwise healthy (churn_score < 0.4, no open Sev-1).

## MUST_EXPAND, Proactive Renewal Cadence (days to renewal)
Based on days between today and `hubspot.renewal_date`:
- **T-120** → internal risk check task.
- **T-90** → value outreach task.
- **T-60** → commercial proposal task.
- **T-30** → ensure commercial close is in progress.
Compute the nearest upcoming milestone; do not spam every milestone at once.

## Payment Risk, the "No Chasing" rule
CSMs are NOT debt collectors.
- `stripe.dunning_stage` in {none, day_1_14}: **fully automated, NO CSM task.**
- `day_15_plus` AND **Strategic** AND high-ARR (arr_usd >= 100000): create a **Day-15 executive-outreach** task (prevent service disruption). Tag MUST_PROTECT.
- `day_15_plus` AND **Scaled**: **auto-suspend, NO CSM task** (note it as automated, do not queue for a human).

## Contact hygiene gate
If `hubspot.contacts` is missing any of {Executive Sponsor, Primary Champion, Finance Contact} for a **Strategic** account, add a low-priority **MUST_USE** data-hygiene task ("tag missing contact roles"), required for automation accuracy (WoW §5).

## MUST_USE, Adoption and onboarding
Adoption and onboarding are operational work, not merely dashboard fields.
- Strategic accounts: create a Priority-5 intervention when there has been no product visit for 14+ days, active users are below 60%, key feature adoption is below 50%, or onboarding is stalled/blocked/at-risk.
- Scaled accounts: route the same signals to the automated adoption program; do not create a manual CSM adoption task from stale usage alone. Create a CSM exception only when the automated flow escalates. Do not create routine manual contact-hygiene tasks for Scaled accounts.
- Use the live Pendo metadata mappings when configured. If a metric is unavailable, report a data gap and do not treat it as zero.
- Rocket Lane onboarding fields are optional until the connector is configured; missing onboarding data must remain visible as a source gap.

## Payment automation contract
The CS platform does not mutate Stripe or suspend service. It emits a governed handoff:
- days 1-14 -> `automated_dunning`, no CSM task;
- Day 15+ Strategic high-ARR -> `payment_risk_escalation`, with the existing CSM task;
- Day 15+ Scaled -> `auto_suspend`, no CSM task.
The billing/ERP workflow owns execution; the platform records the decision and provenance.

## Output contract
Produce a **prioritised task queue** (Priority 1 = MUST_PROTECT risk with 24h SLA first, then Day-15 payment, then expansion, then renewal cadence, then hygiene). For each task include:
`account`, `segment`, `mandate`, `priority`, `trigger` (the exact rule that fired), `evidence` (the tool values that fired it, cite real numbers), `recommended_action`, and (for risk/payment) a `draft_message`.
Every numeric claim in `evidence` MUST come from a tool result, never invent a number.
