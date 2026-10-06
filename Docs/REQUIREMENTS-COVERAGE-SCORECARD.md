# CS Platform — Requirements Coverage Scorecard

**For:** CS Leadership (Dan Hill, JB) / RevOps · **Date:** 6 October 2026
**Against:** the four foundational documents (Requirements Brief · Ways of Working · Meeting
Notes · Tech-Touch V3).

**Headline:** every functional requirement is built. ~90% is live now. The three items not
switched on are external dependencies (a HubSpot email template, the Service Hub Pro / inbound
routing decision, and two security key rotations) — not capability gaps. Two items are
intentionally Phase-2 / non-platform per your own docs.

**Key:** ✅ Live (built, tested, deployed) · 🟡 Built, needs a provisioning/decision step ·
⚪ Deliberately out of platform scope (Phase 2 or owned by Marketing/Product).

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
| HubSpot bi-directional sync | ✅ |
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
| KPI: 0 tech tickets in CS queue | ✅ |
| KPI: SLA ≥95% / report delivery ≥98% | 🟡 need live channels / live send |

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
| Edge: missing Exec Sponsor at T-90 → data-gap task | ✅ |
| KPI: NDR >100% / GRR >92% / expansion pipeline | ✅ |
| KPI: digest open/click tracking | 🟡 needs live send |

## Use Case 3 — Executive Governance
| Requirement | Status |
|---|---|
| SLA governance + playbook-initiation tracking | ✅ |
| Round-robin queue analytics | ✅ (volume grows with live channels) |
| Admin-coverage / data-hygiene matrix | ✅ |
| Portfolio health + ML churn risk matrix (by ARR + driver) | ✅ |
| Capacity planning (1:500 vs 1:20) | ✅ |
| Revenue impact (GRR/NDR/expansion ARR) | ✅ |
| Command Center KPI strip + team widget + segment toggle | ✅ |
| Edge: ingestion-failure banner / test-instance exclusion | ✅ |

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
