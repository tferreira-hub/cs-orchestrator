# CS Platform — Requirements Status & Gap Matrix

Traceability of the *Detailed Functional Requirements & Use Case Specifications* against
what the platform implements today. Status is grounded in the actual modules (not
intent):

- **Built** — implemented and covered by tests / live adapters.
- **Partial** — core exists but a specified behaviour is missing or softer than the spec.
- **Planned** — not yet implemented.

Source documents: Requirements Brief · Ways of Working (WoW) · Meeting Notes (Jul 30, 2026)
· Tech Touch for CS V3.

Strategic goals: GRR > 92%, NDR > 100%, scale coverage, automate value reporting, move
CSMs from activity originators to workflow orchestrators.

---

## Summary matrix

| # | Requirement area | Spec source | Status | Where it lives / what's missing |
|---|------------------|-------------|--------|---------------------------------|
| 1 | HubSpot bi-directional sync (contract value, renewal, hierarchy, health, risk, roles) | Brief, WoW | **Built** | `adapters/sources.py:HubSpot`; write-back is dry-run by default (`writeback`, `push_cs_data`) |
| 2 | Stripe read-only (invoice status, ARR, payment-failure, dunning) | Brief | **Built** | `adapters/sources.py:Stripe` → `dunning_stage` none/day_1_14/day_15_plus |
| 3 | Zendesk ingestion (ticket volume, CSAT, Sev-1) | Brief | **Built** | `adapters/sources.py:Zendesk`; CSAT bounded to 30-day window |
| 4 | Product telemetry (adoption, licence utilisation, API velocity) | Brief | **Built** | `adapters/sources.py:Pendo`, `Entitlements`; utilisation drives expansion trigger |
| 5 | ML churn prediction (>70%) | Brief | **Built** | `adapters/sources.py:Churn` (Redshift Data API, cross-account assume-role); computed fallback is never labelled ML |
| 6 | Jiminny meeting metadata / sentiment / summaries | Brief | **Built** | `adapters/sources.py:Jiminny`; negative sentiment lowers health |
| 7 | Multi-instance alert suppression (primary revenue instance only) | Brief, WoW | **Built** | `hooks/scripts/suppression.py`; `identity.py` instance family |
| 8 | ML churn >70% → Defensive Risk Task, 24h SLA | Brief, WoW | **Built** | `orchestrate.py` `RULE_PREDICTIVE_RISK` (P1) |
| 9 | Payment "No Chasing": days 1–14 automated, Day-15 Strategic playbook | WoW, Brief | **Built** | `orchestrate.py` `payment_disposition`, `RULE_DAY15_PAYMENT`; Scaled auto-suspend path |
| 10 | Proactive renewal cadence T-120 / T-90 / T-60 / T-30 | Brief, WoW | **Built** | `orchestrate.py` `RULE_RENEWAL_CADENCE`; `engine.renewal_forecast`; Command Center cadence viz |
| 11 | Expansion triggers (85% licence utilisation, API/feature spikes) | Brief, WoW | **Built** | `engine.expansion_score` + `_expansion_qualified`; Entitlements/Pendo signals |
| 12 | Health scoring (usage + CSAT + call sentiment) | Brief | **Built** | `engine.health_score` (weighted live signals) |
| 13 | Strategic vs Scaled segmentation; owner-scoped books | WoW, Tech Touch | **Built** | `rbac.py` (admin/csm), cohort filter, pooled cohort views |
| 14 | Executive governance: SLA, capacity, GRR | WoW | **Built** | `engine.kpis`, `_capacity_per_csm`, `_retention_metrics` (GRR) |
| 15 | Contact role architecture (Exec Sponsor, Champion/Admin, Finance) | WoW | **Built** | Surfaced on Data Gaps **and** enforced as a hard close-gate: `engine.required_roles_missing` + `/api/tasks/status` returns 409 when completing a renewal/onboarding task with untagged roles |
| 16 | NDR > 100% reporting | Brief, WoW | **Built (live)** | `_retention_metrics` computes NDR from `rpt_account_ndr_monthly` (current vs prior-year revenue). Cross-account warehouse access **provisioned + verified 2026-10**: created the `cs-platform-churn-reader` role in the Data Platform account (`503561421603`) trusting `cs-platform-task`, granted least-privilege reads on `rpt`/`marts`/`stg`. Verified real NDR flowing (e.g. 91% sample) and 10,278 churn-scored accounts. |
| 17 | Tableau embedded reporting (keep Tableau, surface in-tool, SSO) | (ops decision) | **Built** | `tableau.py` connected-app JWT; Reports page; Integrations entry |
| 18 | **5-channel inbound ingestion** (Zendesk misroute, Slack call-log, mailbox, campaign replies, high-intent forms) | Tech Touch | **Built (platform seam); channel config = Option A** | `POST /api/inbound/hubspot` normalises the 5 channels + runs triage/round-robin with the live roster. HubSpot Service Hub channel wiring + Service Hub Pro licence are RevOps config (`PROVISIONING-CHECKLIST.md`). |
| 19 | **Automated triage** (technical→Zendesk, billing→CS ticket, expansion→CSQL) | Tech Touch | **Built** | `inbound.py` deterministic intent classifier + route map; tested |
| 20 | **Round-robin allocation + availability + 24h SLA + 20h reassign** | Tech Touch | **Built** | `inbound.py` rotate/SLA/20h-reassign/dedupe + `engine.pooled_roster()` live CSM presence feed (`set_csm_availability`, GET/POST `/api/csm/availability`, UI toggle). Optional: native Help Desk presence sync. |
| 21 | Programmatic monthly account performance digest (1st-of-month, 85% CTA, reply→CSQL) | Tech Touch, Brief | **Built (dry-run-safe); live send pending email template** | `monthly_digest`/`send_digest`/`run_monthly_digests` + EventBridge scheduler + HubSpot transactional seam. Live send needs `CS_HS_TRANSACTIONAL_EMAIL_ID`. |
| 22 | Strategic monthly draft/review/approve (28th–31st; auto-send baseline if unreviewed) | Tech Touch | **Built** | `monthly_review_queue`/`add_review_comment`/`approve_digest`/`dispatch_reviewed_digests` (approved→send, draft→auto-baseline, commented→held); endpoints + UI panel on Portfolio Reviews |
| 23 | Duplicate ticket merging (same user, 2h window) | Tech Touch | **Built** | `inbound.py` dedupe (2h window, same sender) |
| 24 | Onboarding stagnation alert | Tech Touch | **Built** | `orchestrate.RULE_ONBOARDING_STAGNATION` (P3) off the live Rocket Lane signal (stalled/blocked/red/past-due); flows into the queue + account 360. Deeper lifecycle view = Phase 2. |
| 25 | Weekly time-blocked operating rhythm (Mon review, daily P1) | WoW | **Built** | `engine.operating_rhythm()` groups the queue into Monday review / daily P1 risk / weekly renewals+expansion / weekly adoption+QBR; `/api/operating-rhythm` + 'Operating Rhythm' page |
| 26 | Move 1–20 Agency + Corporate to pooled structure (platform-executed) | WoW, Tech Touch | **Built** | `engine.pooled_cohort` + `move_to_pooled` + `HubSpot.set_customer_tier` (tier=Pooled, pooled-team, clear owner); gated/owner-scoped/audited/reversible; UI with per-account selection + team picker on Scaled → The Pool |

---

## KPI data dependencies

| KPI | Target | Computable today? | Dependency |
|-----|--------|-------------------|------------|
| GRR | ≥ 92% | Yes (live ARR + churn) | `_retention_metrics` |
| NDR | > 100% | **Yes (live)** | `rpt_account_ndr_monthly` via the provisioned cross-account reader (current vs prior-year revenue) |
| First-response SLA | ≥ 95% in 24h | Engine ready; needs live channels (#18) | `inbound.py` SLA timing |
| Monthly report delivery | ≥ 98% | Engine ready; needs email template | digest engine (#21) + HubSpot transactional template id |
| Triage accuracy (0 tech tickets in CS queue) | 0 | Engine ready; needs live channels | `inbound.py` classifier routes technical → Zendesk handoff |

---

## Unstated architectural prerequisites (status)

1. **Scheduler + outbound email** — **built.** EventBridge 1st-of-month scheduler
   (`infra/scheduler.tf`, gated off until ready) + HubSpot transactional single-send seam
   (`HubSpot.send_transactional_email`). Live send needs a HubSpot transactional-email
   template id (`CS_HS_TRANSACTIONAL_EMAIL_ID`) — an account-side config, not code.
2. **Real-time CSM availability feed** — **built** (in-platform Available/OOO presence feed
   drives round-robin via `pooled_roster()`; `/api/csm/availability` + UI toggle). Optional
   upgrade: sync native HubSpot Help Desk status to replace the manual toggle.
3. **Cross-account warehouse trust** — **resolved.** The `cs-platform-churn-reader` role was
   created in the Data Platform account (`503561421603`) trusting `cs-platform-task`, with
   least-privilege reads on `rpt`/`marts`/`stg`. NDR (#16) and live ML churn (#5) now flow
   (verified 2026-10). NOTE: these grants were applied imperatively; codify them in the
   Data Platform Terraform stack (`infra/data-platform/cs-platform-churn-reader.tf.example`)
   to prevent drift.

---

## Phase alignment

The Meeting Notes defer **Rocket Lane / onboarding to Phase 2**, and the build reflects
that (Rocket Lane = access-pending). The documented **Phase-1 priority (sub-$10k "1–20"
Scaled segment)** is exactly where the largest build gap sits — the Tech-Touch inbound
engine (#18–#23). This change begins that engine with the deterministic triage +
round-robin core (#19, #20, #23); channel adapters (#18) and the monthly digest (#21/#22)
remain the next planned increments.
