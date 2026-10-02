# CS Platform — End-to-End Review & Agreement Scenarios

Purpose: a single, shareable review of the CS Platform against all four foundational
documents, plus **worked scenarios** for the team to walk through and agree on, and an
honest "what it takes to get to 100%". Grounded in the live system (production task
`cs-platform:58`, 188 automated tests passing).

Status key: **[LIVE]** in production · **[BUILT]** coded, needs a credential/flag to go
live · **[PLANNED]** not yet built · **[DECISION]** blocked on a leadership choice.

---

## Part 1 — End-to-end coverage by requirement

### 1. Architecture & integrations
| Requirement (source) | State | Detail |
|---|---|---|
| HubSpot bi-directional sync | **[LIVE]** | Pull: contract value, renewal, hierarchy, contacts. Push (write): health, risk, active playbook, **notes, contact roles, expansion deals** — all gated/owner-scoped/audited. |
| Stripe read-only (invoice, ARR, dunning) | **[LIVE]** | Powers the Payment Risk Report. Stays read-only by design. |
| Zendesk (tickets, CSAT, Sev-1) read | **[LIVE]** | Feeds health. |
| Zendesk reply/close (write) | **[BUILT]** | Needs a Zendesk **ticket-write token**; dry-run-safe until then. |
| Product telemetry (adoption, utilisation, API) | **[LIVE]** | Pendo + Entitlements. |
| ML churn (>70%) | **[LIVE]** | Redshift; honest computed fallback, never mislabelled. |
| Jiminny (sentiment, summaries) | **[LIVE]** | Feeds health. |
| Monthly reporting engine (digest) | **[PLANNED]/[DECISION]** | Needs email + scheduler; approach depends on the routing decision. |
| 5-channel inbound ingestion | **[PLANNED]/[DECISION]** | Triage + round-robin engine is **[LIVE]**; the 5 live channel adapters depend on the routing decision. |

### 2. Core workflow & orchestration (all "Must Have")
| Requirement | State |
|---|---|
| Automated task queues + segment routing (Strategic/Scaled) | **[LIVE]** |
| Predictive risk playbooks (ML >70%, multi-signal, 24h SLA) | **[LIVE]** |
| Expansion triggers (85% utilisation, API spikes) | **[LIVE]** |
| Multi-instance suppression (primary vs test) | **[LIVE]** |
| Proactive renewal cadence T-120/90/60/30 | **[LIVE]** |
| Payment risk "No Chasing" / Day-15 | **[LIVE]** (5-page Payment Risk Report) |

### 3. Data, governance & KPIs
| Requirement | State |
|---|---|
| Contact-role architecture (Sponsor/Champion/Finance) | **[LIVE]** — tagging write + **hard close-gate** on renewal/onboarding tasks |
| Unified lifecycle (Rocket Lane onboarding) | **[PLANNED]** — access-pending; Phase 2 per Meeting Notes |
| KPI & capacity tracking (load, completion, ARR/CSM) | **[LIVE]** |
| GRR ≥ 92% | **[LIVE]** (computed from live ARR + churn) |
| NDR > 100% | **[LIVE when warehouse has a prior period]** — computed from `rpt_account_ndr_monthly`; honest "needs history" otherwise |
| Data-hygiene / Data Gaps dashboard | **[LIVE]** |
| Executive Command Center + segment toggle | **[LIVE]** (segment via the global cohort filter) |

**Net:** the entire Must-Have orchestration core, the write-back "work in one place" model,
contact-role governance, GRR/NDR, and the Payment Risk Report are **live**. The outstanding
items are the **Tech-Touch inbound/digest layer** and **Rocket Lane**, both gated on a
decision and/or provisioning — not on engineering capability.

---

## Part 2 — Scenarios to walk through with the team

Each scenario is a concrete, end-to-end story mapped to the live platform, with the KPI it
serves. Use these to get agreement that the platform does what each persona needs.

### Scenario A — Scaled CSM: inbound billing query (Use Case 1)
1. A sub-$10k customer emails `accountmanagement@`. *(Channel 3 — intake [PLANNED])*
2. The item lands in the **Pooled Inbox**; the triage engine classifies **billing** and
   routes it to the CS pooled queue (not Zendesk). *(triage [LIVE])*
3. Round-robin assigns it to the next **available** pooled CSM with a **24-hour SLA**;
   duplicates from the same sender within 2h are merged. *(round-robin/SLA/dedupe [LIVE])*
4. The CSM opens it in one pane (ARR tier, health, payment status, contacts) and **replies /
   closes** from the platform. *(reply/close [BUILT], needs Zendesk write token for a
   Zendesk-origin item)*
- **KPIs:** First-response SLA ≥95%/24h; 0 technical tickets in the CS queue.
- **To agree:** the routing layer (see Decision below) and the Zendesk write token.

### Scenario B — Scaled CSM: expansion hand-raiser (Use Case 1, Pillar 2)
1. A customer hits 85% licence utilisation → the platform raises an **expansion trigger**. *([LIVE])*
2. The CSM reviews and creates a **CSQL / expansion deal** in HubSpot from the account page. *([LIVE])*
3. (Future) a high-intent web form bypasses the queue as an urgent CSQL. *(Channel 5 [PLANNED])*
- **KPI:** Expansion pipeline (CSQL volume) from automated triggers.
- **To agree:** nothing — this path is live today.

### Scenario C — Strategic CSM: defensive risk (Use Case 2, Must Protect)
1. ML churn >70% (or ticket-spike + usage-drop) raises a **P1 Defensive Risk task**, 24h SLA. *([LIVE])*
2. Multi-instance suppression ensures the alert is on the **primary** instance, not a sandbox. *([LIVE])*
3. The CSM works the account in one pane (health trend, call sentiment, payment, contacts),
   **logs a call note** and **syncs health/risk back to HubSpot** for Sales. *([LIVE])*
- **KPI:** 100% defensive-playbook initiation within 24h.
- **To agree:** nothing — live today.

### Scenario D — Strategic CSM: proactive renewal + contact-role gate (Use Case 2)
1. T-120/90/60/30 tasks fire automatically before renewal. *([LIVE])*
2. At T-90, if no **Executive Sponsor** is tagged, a data-gap is surfaced. *([LIVE])*
3. The CSM **cannot complete** the renewal task until all three contact roles are tagged;
   the platform shows exactly which are missing and lets them **tag roles** inline. *([LIVE])*
- **KPI:** 100% renewal-cadence compliance; clean contact architecture.
- **To agree:** nothing — live today.

### Scenario E — Strategic CSM: monthly performance digest (Use Case 2)
1. On the 28th the platform drafts a monthly report per named account. *([PLANNED])*
2. The CSM adds executive commentary by the 31st; unreviewed drafts auto-send a baseline. *([PLANNED])*
3. On the 1st it dispatches to the Primary Admin + Exec Sponsor and logs to HubSpot. *([PLANNED])*
- **KPI:** ≥98% monthly report delivery; admin open/click tracking.
- **To agree:** the **email + scheduler** capability (see Decision) — this is the main build gap.

### Scenario F — CS Leader: governance & revenue (Use Case 3)
1. Command Center KPI strip: Total ARR, **GRR%, NDR%**, ARR-at-risk, expansion pipeline. *([LIVE])*
2. SLA compliance, capacity per CSM (1:500 vs 1:20), ML churn risk matrix by ARR. *([LIVE])*
3. Day-15 payment-risk escalation view for high-ARR accounts. *([LIVE] — Payment Risk Report)*
- **KPIs:** GRR ≥92%, NDR >100%, SLA ≥95%, admin coverage 100%.
- **To agree:** NDR shows a number once the warehouse has a prior period; admin coverage
  depends on Scenario E.

---

## Part 3 — The one decision that unlocks the rest

**Tech-Touch routing layer: HubSpot Service Hub Pro vs the CS Platform.** (Full ADR:
`Docs/DECISION-tech-touch-routing.md`.) The Tech Touch V3 paper assumes Service Hub Pro is
the Help Desk; the platform currently implements triage/round-robin itself. This single
choice determines how the **5 inbound channels** and the **monthly digest/email** are built,
and whether the Service Hub Pro licence is purchased (relevant to the 50–60k budget).

- **Option A (Service Hub Pro):** channels + email are HubSpot configuration; the platform
  surfaces and acts on HubSpot tickets. Lower build, licence cost.
- **Option B (CS Platform):** build the 5 channel adapters + SES/scheduler + CSM presence
  feed in the platform. No licence; more bespoke build.

---

## Part 4 — Path to "100% working"

| Item | Needs | Owner |
|---|---|---|
| Routing-layer decision (A vs B) | Leadership choice | CS Leadership / RevOps |
| Monthly digest + outreach (Scenario E) | Email + scheduler (per decision) | Eng, after decision |
| 5 live inbound channels (Scenario A) | Channel wiring (per decision) + CSM presence feed | Eng, after decision |
| Zendesk reply/close live (Scenario A) | Zendesk ticket-write token | RevOps/IT |
| NDR shows a number | `rpt_account_ndr_monthly` live + a prior period | Data Platform |
| Rocket Lane onboarding (unified lifecycle) | `ROCKET_LANE_KEY` (Phase 2) | RevOps/IT |
| Security hygiene | Rotate Stripe key to `rk_`; rotate Tableau secret | IT/Security |

Everything not dependent on the above is **already live and tested**. The platform already
delivers the Must-Have orchestration core, the "CSMs work in one place" write model, contact-
role governance, GRR/NDR, and the Payment Risk Report in production.
