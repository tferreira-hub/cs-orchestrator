# CS Platform — Provisioning Checklist (owner actions, not code)

Everything in the four requirements documents is now **built in the platform**. The items
below are the only things left, and none is code — each is a licence, a credential, a config
in another system, or a secret rotation. They are owned by RevOps / IT / Data Platform.

Each row says what it unblocks and where the platform already meets it.

| # | Item | Owner | Unblocks | Platform side (already done) |
|---|------|-------|----------|------------------------------|
| 1 | **HubSpot Service Hub Pro licence** + configure the 5 inbound channels (Zendesk misroute macro, Slack call-log form, `accountmanagement@` mailbox, campaign reply-to, high-intent forms) to post into HubSpot Service, and point them at `POST /api/inbound/hubspot`. | RevOps | 5-channel inbound (#18); UC1 SLA + triage-accuracy KPIs | Intake seam `/api/inbound/hubspot` (5 channels normalised) + triage/round-robin/SLA/dedupe + live CSM availability feed. Decision = Option A (`DECISION-tech-touch-routing.md`). |
| 2 | **HubSpot transactional email template** with the digest merge tokens; set `CS_HS_TRANSACTIONAL_EMAIL_ID` + `CS_EMAIL_PROVIDER=hubspot`; then enable the scheduler (`digest_schedule_enabled=true`, `digest_apply=true`). | RevOps / IT | Live monthly digest send (#21) + strategic dispatch (#22) + report-delivery KPI | `monthly_digest` compile, `send_digest` (honest/gated), `run_monthly_digests` batch, EventBridge scheduler (`infra/scheduler.tf`, off), HubSpot single-send seam. Tokens listed in `infra/variables.tf`. |
| 3 | **Stripe key rotation** `sk_live_` → restricted `rk_live_` (Customers/Invoices/Subscriptions read-only), then drop `CS_ALLOW_STRIPE_SECRET_KEY`. | IT / Security | Security hygiene | Adapter already refuses `sk_live_` once the override is dropped (test proves it). Steps: `SECURITY-ROTATION-RUNBOOK.md` §1. |
| 4 | **Tableau connected-app secret rotation** (generate-new-first, no downtime). | IT / Security | Security hygiene | Steps: `SECURITY-ROTATION-RUNBOOK.md` §2. |
| 5 | **Codify the Data Platform warehouse access** (role + grants were applied live this session) into the `infra/data-platform` stack so it doesn't drift. | Data Platform | NDR + live churn durability | Role + grants documented in `cs-platform-churn-reader.tf.example` + `GRANTS-RUNBOOK.md`. NDR + churn are **already live**. |
| 6 | **Live HubSpot Help Desk presence sync** (optional upgrade). | RevOps | Replace the in-platform Available/OOO toggle with native Help Desk status | `pooled_roster()` + the presence store already drive round-robin; a live sync can replace the manual toggle without changing the wiring. |

## Phase 2 (deferred per Meeting Notes)
- **Rocket Lane deeper onboarding** — the key is live and the stagnation alert (#24) fires on
  real signals today; the fuller onboarding-velocity lifecycle view is Phase-2 scope.

## What is NOT pending
All application logic for the requirements is built, tested (205 tests), and deployed:
HubSpot bi-directional + gated writes, Stripe dunning, Zendesk ingest + reply/close,
telemetry, **live ML churn**, Jiminny, multi-instance suppression, churn→P1/24h, No-Chasing,
renewal cadence, expansion→CSQL, health scoring, segmentation + **platform-executed
move-to-pooled**, governance/capacity, **GRR + live NDR**, Tableau, contact-role close-gate,
triage + round-robin + **CSM availability**, duplicate merge, monthly digest + scheduler +
**strategic review/approve window**, **onboarding stagnation**, **weekly operating rhythm**,
and the **Option A inbound intake seam**.
