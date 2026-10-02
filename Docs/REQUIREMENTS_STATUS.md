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
| 15 | Contact role architecture (Exec Sponsor, Champion/Admin, Finance) | WoW | **Partial** | Surfaced on **Data Gaps**; spec wants a *hard close-gate* on renewal/onboarding tasks — not yet blocking |
| 16 | NDR > 100% reporting | Brief, WoW | **Partial** | GRR computed; **NDR needs prior-period ARR history** (warehouse `rpt_account_ndr_monthly`) — not yet wired as a time series |
| 17 | Tableau embedded reporting (keep Tableau, surface in-tool, SSO) | (ops decision) | **Built** | `tableau.py` connected-app JWT; Reports page; Integrations entry |
| 18 | **5-channel inbound ingestion** (Zendesk misroute, Slack call-log, mailbox, campaign replies, high-intent forms) | Tech Touch | **Planned** | No channel adapters yet; biggest Phase-1 gap |
| 19 | **Automated triage** (technical→Zendesk, billing→CS ticket, expansion→CSQL) | Tech Touch | **Partial → in progress** | `inbound.py` keyword/intent classifier (this change); channel wiring still Planned |
| 20 | **Round-robin allocation + availability + 24h SLA + 20h reassign** | Tech Touch | **Partial → in progress** | `inbound.py` rotate/availability/SLA/dup-merge (this change); real-time CSM status feed still Planned |
| 21 | Programmatic monthly account performance digest (1st-of-month, 85% CTA, reply→CSQL) | Tech Touch, Brief | **Planned** | Needs a scheduler + email-send capability (new architectural prerequisite) |
| 22 | Strategic monthly draft/review/approve (28th–31st; auto-send baseline if unreviewed) | Tech Touch | **Planned** | Depends on #21 |
| 23 | Duplicate ticket merging (same user, 2h window) | Tech Touch | **In progress** | `inbound.py` dedupe (this change) |
| 24 | Onboarding stagnation alert | Tech Touch | **Planned** | Onboarding source is Rocket Lane (access-pending, Phase-2 per Meeting Notes) |
| 25 | Weekly time-blocked operating rhythm (Mon review, daily P1) | WoW | **Partial** | Prioritised queue + Command Center exist; explicit day-block scheduling view not built |

---

## KPI data dependencies

| KPI | Target | Computable today? | Dependency |
|-----|--------|-------------------|------------|
| GRR | ≥ 92% | Yes (live ARR + churn) | `_retention_metrics` |
| NDR | > 100% | **No** | Needs monthly ARR time series (`rpt_account_ndr_monthly`) wired as prior-vs-current |
| First-response SLA | ≥ 95% in 24h | Partial | Needs the inbound queue (#18–20) producing SLA-timed tasks |
| Monthly report delivery | ≥ 98% | **No** | Needs the monthly digest engine (#21) + email send |
| Triage accuracy (0 tech tickets in CS queue) | 0 | Partial | `inbound.py` classifier routes technical → Zendesk handoff; needs live channels |

---

## Unstated architectural prerequisites (flagged for the team)

1. **Scheduler + outbound email** — required for the monthly digest (#21/#22) and for
   campaign-reply threading. The platform currently has no cron/scheduler or send-email
   capability; this is a net-new integration line, not a config change.
2. **Real-time CSM availability feed** — round-robin (#20) needs an "Available/OOO" status
   source (Help Desk presence). Until then the engine round-robins across a configured
   roster and treats everyone as available.
3. **Monthly ARR history** — for NDR (#16); the warehouse table exists in config but is not
   yet read as a time series.

---

## Phase alignment

The Meeting Notes defer **Rocket Lane / onboarding to Phase 2**, and the build reflects
that (Rocket Lane = access-pending). The documented **Phase-1 priority (sub-$10k "1–20"
Scaled segment)** is exactly where the largest build gap sits — the Tech-Touch inbound
engine (#18–#23). This change begins that engine with the deterministic triage +
round-robin core (#19, #20, #23); channel adapters (#18) and the monthly digest (#21/#22)
remain the next planned increments.
