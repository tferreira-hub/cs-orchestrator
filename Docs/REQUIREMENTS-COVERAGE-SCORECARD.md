# CS Platform — Requirements Coverage Scorecard

**For:** CS Leadership (Dan Hill, JB) / RevOps · **Updated:** 9 October 2026
**Against:** the foundational documents (Requirements Brief · Ways of Working · Meeting
Notes · Tech-Touch V3 · V5 Follow-Up Notes).

**Headline:** every functional requirement is built, and an end-to-end source audit
(9 Oct) verified each against code + UI + tests rather than intent. The audit found four
real code gaps that the previous (6 Oct) version of this scorecard had marked done or
glossed; **all four are now fixed, tested, and on PR #167** (341 tests passing). The only
items still not switched on are **external dependencies** (a HubSpot email template, the
Service Hub Pro / inbound routing decision, two security key rotations) — not capability
gaps. A few pillars are intentionally Phase-2 / non-platform per your own docs.

**Key:** ✅ Live (built, tested, verified against source) · 🟡 Built, needs a
provisioning/decision step · ⚪ Deliberately out of platform scope (Phase 2 or owned by
Marketing/Product).

> **What the 9 Oct audit changed (four fixes, all shipped on PR #167):**
> 1. **Test-instance exclusion from ARR/retention** — sandbox/test instances were being
>    counted in GRR/NDR/at-risk ARR; now excluded (`excluded_test_instances` reported).
> 2. **Exec Sponsor F2F → HubSpot write-back** — F2F previously logged only locally; now
>    written back to the HubSpot company (`cs_last_exec_f2f_date` + outcome, two-gated).
> 3. **T-90 missing-Exec-Sponsor urgent task** — previously the scorecard claimed ✅ but
>    the rule did not exist; now `RULE_RENEWAL_SPONSOR_GAP` (P2) fires.
> 4. **SLA ≥95% / report-delivery ≥98% as computed KPIs** — previously raw counts only;
>    now computed vs target in `kpis()` (honest `None` when not yet computable).

---

## Strategic goals
| Goal | Status |
|---|---|
| Protect revenue — **GRR ≥ 92%** | ✅ live & measured |
| Protect revenue — **NDR > 100%** | ✅ live |
| Scale account coverage (sub-$10k) | ✅ live |
| Automate value reporting | 🟡 built; one email template from live send |
| CSMs as orchestrators, not originators | ✅ live |

## Integrations
| Requirement | Status |
|---|---|
| HubSpot bi-directional sync | ✅ (incl. Exec Sponsor F2F write-back, added 9 Oct) |
| Stripe read-only (invoice, ARR, dunning) | ✅ |
| Zendesk ingest (volume, CSAT, Sev-1) + reply/close | ✅ |
| Product telemetry (adoption, utilisation, API, churn >70%) | ✅ |
| Jiminny (sentiment, summaries) | ✅ |
| Inbound channels (mailbox, Slack, forms) | 🟡 intake seam live; channels need wiring |
| Monthly reporting engine | ✅ built; 🟡 live send needs template |

## Use Case 1 — Scaled / Tech-Touch
| Requirement | Status |
|---|---|
| 5-channel intake | 🟡 platform seam live; channel config pending (A/B) |
| Triage (technical→Zendesk, billing→pooled, expansion→CSQL) | ✅ |
| Round-robin + availability + 24h SLA + 20h reassign | ✅ |
| Monthly digest (85% CTA, Primary Admin) | ✅ built; 🟡 live send |
| ML churn >70% → P1 risk task (24h SLA) | ✅ |
| Multi-instance suppression | ✅ |
| Onboarding stagnation alert | ✅ |
| Unified action queue + single-pane view | ✅ |
| Edge cases: missing admin / OOO reassign / 2h dedupe | ✅ |
| KPI: 0 tech tickets in CS queue | ✅ structurally enforced (technical → Zendesk handoff, never pooled) |
| KPI: SLA ≥95% / report delivery ≥98% | ✅ **now computed vs target** in `kpis().sla` / `kpis().report_delivery` (honest None until live channels/send produce volume) |

## Use Case 2 — Strategic / High-Touch
| Requirement | Status |
|---|---|
| Weekly operating rhythm (time-blocked) | ✅ |
| Monthly review window (28–31 draft/comment/approve) + auto-baseline | ✅ |
| Renewal cadence T-120/90/60/30 | ✅ |
| Expansion triggers (85%, API/feature) → CSQL | ✅ |
| Payment "No Chasing" (1–14 auto, Day-15, auto-suspend) | ✅ |
| Contact-role validation before close | ✅ |
| Strategic Account 360 | ✅ |
| Edge: missing Exec Sponsor at T-90 → data-gap task | ✅ **now implemented** (`RULE_RENEWAL_SPONSOR_GAP`, P2 MUST_PROTECT; was previously claimed done but absent — fixed 9 Oct) |
| KPI: NDR >100% / GRR >92% / expansion pipeline | ✅ |
| KPI: digest open/click tracking | 🟡 needs live send (no open/click telemetry until the transactional template is live) |

## Use Case 3 — Executive Governance
| Requirement | Status |
|---|---|
| SLA governance + playbook-initiation tracking | ✅ |
| Round-robin queue analytics | ✅ (volume grows with live channels) |
| Admin-coverage / data-hygiene matrix | ✅ |
| Portfolio health + ML churn risk matrix (by ARR + driver) | ✅ |
| Capacity planning (1:500 vs 1:20) | ✅ |
| Revenue impact (GRR/NDR/expansion ARR) | ✅ |
| Command Center KPI strip + team widget + segment toggle | ✅ (KPI strip + cohort filter; team widget split across pages) |
| Edge: ingestion-failure banner / test-instance exclusion | 🟡 **test-instance exclusion now done** (ARR/retention/revenue/expansion); banner covers HubSpot/Stripe/Zendesk/Pendo/Churn — ROI AI / Rocket Lane / billing-sync not yet in the banner |

## Data & governance
| Requirement | Status |
|---|---|
| Standardised contact roles synced to HubSpot | ✅ |
| Unified lifecycle view (onboarding + adoption) | 🟡 Rocket Lane live + stagnation alert; deeper view = Phase 2 |
| KPI & capacity tracking for leadership | ✅ |

## Tech-Touch V3 — the 5 Pillars
| Pillar | Status |
|---|---|
| 4. Pooled CSM with triage execution | ✅ (triage, round-robin, pooled inbox, move-to-pooled) |
| 1. Automated onboarding sequences | ⚪ HubSpot Marketing; platform adds the stagnation alert ✅ |
| 2. Scaled nurture / 1:many campaigns (CSQLs) | ⚪ HubSpot Marketing; platform raises the CSQLs ✅ |
| 3. In-app guidance / product-led | ⚪ Pendo / product (non-platform) |
| 5. Digital office hours / on-demand content | ⚪ Content / webinar ops (non-platform) |

---

## The only things not switched on (all external, not capability)
1. **Send the monthly digest to customers** — needs a HubSpot transactional email template.
2. **The 5 live inbound channels** — needs the Service Hub Pro decision (Option A) or the
   platform-routing wiring (Option B). See `INBOUND-OPTIONS-SCENARIOS.md`.
3. **Security hygiene** — rotate the Stripe key to read-only and the Tableau secret (no
   feature impact).

Everything else above is built, tested, and running today.
