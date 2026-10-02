# CS Platform — Overarching Scope & Action Plan

One consolidated view across the four foundational documents, reconciled against what the
platform does **today** (verified in code), with the remaining work as a sequenced plan.

**Source documents**
1. CS Platform Requirements Brief for RevOps (architecture, integrations, risk/renewal/expansion rules)
2. Ways of Working (WoW) Framework (operating model, 3 mandates, segmentation, SLAs, "No Chasing")
3. CS Requirements Gathering Notes, 30 Jul 2026 (budget 50–60k, Phase-1 = sub-$10k "1–20", Rocket Lane deferred)
4. Tech Touch for CS V3 (5-channel inbound, triage, round-robin, monthly digest, 1:500 pooled)

**Strategic goals:** GRR ≥ 92%, NDR > 100%, scale coverage, automate value reporting, move
CSMs from originators to orchestrators.

**Status legend:** `[built]` Built · `~` Partial · `[planned]` Planned

---

## A. Architecture & Integrations

| Capability | Docs | Status | Evidence / gap |
|---|---|---|---|
| HubSpot bi-directional sync (pull + push CS data) | 1,2,3,4 | [built] | Read everywhere; **write-back live** (health/risk/playbook) gated+audited (increment 1). |
| Stripe read-only (invoice, ARR, payment-risk, dunning) | 1,3,4 | [built] | `Stripe` adapter; Payment Risk Report live. Stays **read-only** by design. |
| Zendesk (tickets, CSAT, Sev-1) | 1,3,4 | [built] read | Read live. Ticket **write** (reply/close) = increment 3 (needs write token). |
| Product telemetry (adoption, licence util, API velocity) | 1,2,4 | [built] | Pendo + Entitlements adapters. |
| ML churn (>70%) | 1,3 | [built] | Redshift churn adapter; computed fallback never labelled ML. |
| Jiminny (sentiment, summaries) | 1,3 | [built] | Adapter live; feeds health. |
| Jane (AI analyst, Bedrock) | — | [built] | Claude Sonnet 4.5 now enabled; evidence-grounded, human-approves. |
| Tableau embedded reporting (SSO) | ops | [built] | Connected-app embed live. |
| 5-channel inbound ingestion | 4 | [planned] | Triage+round-robin **engine built** (increment); the 5 live channel adapters (Zendesk-misroute macro, Slack form, mailbox, campaign replies, forms) are **planned** (need email + Slack intake). |
| Monthly reporting engine (digest send) | 1,4 | [planned] | Needs outbound email + scheduler (increment 4). |

## B. Core Workflow & Orchestration (Requirements Brief §2)

| Requirement | Priority | Status | Evidence / gap |
|---|---|---|---|
| Automated task queues & segment routing | Must | [built] | Rules engine → prioritised queue; Strategic/Scaled cohorts; owner-scoped. |
| Predictive risk playbooks (ML >70%, multi-signal, 24h SLA) | Must | [built] | `RULE_PREDICTIVE_RISK` P1; defensive workflow. |
| Expansion triggers (85% util, API spikes) | Must | [built] | `expansion_score` + Entitlements/Pendo. |
| Multi-instance suppression (primary vs test) | Must | [built] | `suppression.py` + `identity.py`. |
| Proactive renewal cadence T-120/90/60/30 | Must | [built] | `RULE_RENEWAL_CADENCE`; forecast + Command Center viz. |
| Payment risk "No Chasing" / Day-15 | Must | [built] | `payment_disposition`, `RULE_DAY15_PAYMENT`; Payment Risk Report (5 pages). |

## C. Ways of Working (operating model)

| Element | Status | Evidence / gap |
|---|---|---|
| CSMs as orchestrators (system originates tasks) | [built] | Prioritised queue is the single work surface. |
| 3 mandates (Protect/Expand/Use) | [built] | Tasks carry mandate; views per mandate. |
| Strategic vs Scaled segmentation | [built] | Cohort filter + Pooled CS section. |
| 24-hr risk SLA | [built] | P1 risk tasks carry the SLA. |
| Weekly time-blocked rhythm (Mon review, daily P1) | ~ | Queue + Command Center exist; an explicit day-block schedule view is not built. |
| Contact-role enforcement (Sponsor/Champion/Finance) | ~ | Surfaced on Data Gaps; **hard close-gate** on tasks + role **tagging write** = increment 2. |
| "CSMs work in one place" (write to systems) | ~ | **Write framework live** (increment 1). Rolling out writes increment by increment. |

## D. Data, Governance & KPIs

| Item | Status | Evidence / gap |
|---|---|---|
| Standardised contact roles synced to HubSpot | ~ | Read + gap tracking; role-tag write = increment 2. |
| Unified lifecycle (onboarding via Rocket Lane) | [planned] | Rocket Lane **access-pending** (Phase 2 per Meeting Notes — correctly deferred). |
| KPI & capacity tracking (load, completion, ARR/CSM) | [built] | `kpis`, `_capacity_per_csm`, retention metrics. |
| GRR ≥ 92% | [built] | `_retention_metrics` computes GRR. |
| NDR > 100% | [planned] | Needs prior-period monthly ARR series (warehouse `rpt_account_ndr_monthly` as time series). |
| Data Gaps / hygiene dashboard | [built] | Data Gaps page (ranked field gaps, filters). |
| Executive Command Center (KPI strip, segment toggle) | ~ | Command Center + KPIs live; a one-click Strategic/Scaled **segment toggle** uses the cohort filter rather than a dedicated control. |
| Data-ingestion warning banner (sync failure) | ~ | "Live sources" mode badge exists; a per-source **sync-failure** banner is not explicit. |

## E. KPI data dependencies (what blocks each target)

| KPI | Target | Computable now? | Dependency |
|---|---|---|---|
| GRR | ≥92% | [built] | live ARR + churn |
| NDR | >100% | [planned] | monthly ARR time series |
| First-response SLA | ≥95%/24h | ~ | inbound queue producing SLA-timed tasks (increments 1→3) |
| Monthly report delivery | ≥98% | [planned] | digest engine + outbound email (increment 4) |
| Triage accuracy (0 tech in CS queue) | 0 | ~ | triage engine built; needs the live channels |
| Admin reporting coverage | 100% | [planned] | digest engine (increment 4) |

---

## F. Unstated prerequisites (must be provisioned)

1. **Outbound email + scheduler** — blocks the monthly digest, campaign-reply threading, and
   sequence sends. No send/cron capability today. Critical path for the Tech-Touch KPIs.
2. **Zendesk write token** — for reply/close from the inbound queue.
3. **Monthly ARR history** — for NDR.
4. **Slack intake + mailbox + web-form** wiring — for the 5-channel ingestion.
5. **Rocket Lane access** — for the unified lifecycle (Phase 2).

Security hygiene: rotate the Stripe key to a restricted `rk_` key (drop the override) and
rotate the Tableau connected-app secret.

---

## G. Action plan (sequenced)

**Write access — "CSMs work in one place" (in progress, increment by increment):**
1. [built] HubSpot CS write-back + sequence enrolment, gated/owner-scoped/audited. *(done)*
2. [planned] Reversible CRM writes: call/note logging, task complete/snooze, **contact-role tagging**
   (also satisfies the WoW contact-role enforcement). *(next)*
3. [planned] Zendesk reply/close from the inbound queue. *(needs write token)*
4. [planned] Outbound email + scheduler → monthly digests, sequence sends, inbound reply threading.
   *(largest build; unblocks several KPIs)*
5. [planned] CSQL / deal create for expansion routing.

**Parallel data/UX closers:**
- [planned] NDR: wire the monthly ARR series.
- ~→[built] Contact-role **hard close-gate** on renewal/onboarding tasks (with increment 2).
- ~→[built] Explicit Strategic/Scaled segment toggle on the Command Center.
- ~→[built] Per-source data-ingestion failure banner.
- [planned] 5 live inbound channels (depends on email/Slack/web-form wiring).

**Stays as-is by design:** Stripe read-only; Rocket Lane deferred to Phase 2.

Every write ships behind the two-gate (`apply=true` + `CS_ALLOW_WRITE`), owner-scoped, in the
immutable audit log, human-approved, with a confirm step before any irreversible send.
