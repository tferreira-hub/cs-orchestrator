# RevOps Handoff — the 3 external-dependency blockers

**For:** RevOps / CS Leadership · **From:** Platform (Tiago) · **Updated:** 9 October 2026

The 9 Oct end-to-end audit confirmed every functional requirement is **built and tested**.
Three items are **not switched on**, and none are code gaps — each is an external
provisioning or decision step that only RevOps / an account owner can complete. This note
is the checklist to flip each one on.

Each section lists: **what it unlocks**, **what the platform already has**, **what you
need to do**, and **how to verify** it is live.

---

## 1. Live inbound channels (Tech-Touch UC1)

**Unlocks:** the 5-channel pooled inbound queue (Zendesk misroute, Slack call-log,
mailbox, campaign replies, high-intent forms) running on real traffic instead of the
HubSpot ticket pull.

**Already built (no code needed):**
- Deterministic triage + round-robin + 24h SLA + 20h reassign + 2h dedupe (`inbound.py`).
- All 5 channel normalisers (`inbound.py` `from_*` adapters) with tests.
- A live PULL ingress today via `POST /api/inbound/ingest-hubspot` → `inbound_tickets()`
  (reads Service Hub tickets with the existing HubSpot token — works now).
- The webhook seam `POST /api/inbound/hubspot` for a push model.

**Your decision (A or B) — see `Docs/INBOUND-OPTIONS-SCENARIOS.md`:**
- **Option A (recommended, lowest lift):** upgrade CS to **HubSpot Service Hub Pro** and
  stand up the Account-Management ticket pipeline. The platform already pulls those
  tickets; this just makes the 5 channels land in HubSpot. Needs: Service Hub Pro licence
  + the ticket pipeline + the Zendesk misroute macro (`Ticket_Type = Account_Management`).
- **Option B:** wire each channel's native webhook (mailbox, Slack workflow form, website
  high-intent forms) to `POST /api/inbound/hubspot`. More wiring, no HubSpot upgrade.

**Verify live:** trigger `POST /api/inbound/ingest-hubspot`, then `GET /api/inbound/queue`
shows real tickets with intents/owners; the Pooled CS → Inbox UI populates.

---

## 2. Monthly performance-report live send (Tech-Touch UC1 / UC2)

**Unlocks:** the automated 1st-of-month customer digest actually being emailed to Primary
Admins (+ Exec Sponsor on named accounts), with the 85%-utilisation expansion CTA.

**Already built (no code needed):**
- Digest compile (`engine.monthly_digest`), gated send (`send_digest`), batch runner
  (`run_monthly_digests`), the 28–31 strategic review/approve workflow, and the
  EventBridge 1st-of-month scheduler (`infra/scheduler.tf`, gated off).
- The HubSpot transactional single-send path (`HubSpot.send_transactional_email`).
- Today it safely reports **"prepared"** (dry-run) — it never sends without the config below.

**What you need to do:**
1. Create the transactional email **template in HubSpot** and set its id as
   **`CS_HS_TRANSACTIONAL_EMAIL_ID`**.
2. Set **`CS_EMAIL_PROVIDER`** (the send provider, e.g. `hubspot`).
3. Set **`CS_ALLOW_WRITE=1`** (the platform's global write gate; off by default).
4. Flip the EventBridge schedule on in `infra/scheduler.tf` when ready.

See `Docs/PROVISIONING-CHECKLIST.md` for the SSM parameter names.

**Verify live:** `run_monthly_digests(apply=False)` summary shows `missing_primary_admin`
≈ 0; a single `send_digest(apply=true)` on a test account returns `mode: applied`; the KPI
`kpis().report_delivery.report_delivery_pct` moves off its readiness figure.

---

## 3. Security key rotation (no feature impact)

**Unlocks:** nothing functional — this is hygiene to close two over-privileged credentials.

**a) Stripe key → restricted read-only.**
- The platform only ever reads invoice/ARR/dunning from Stripe. It must use a
  **restricted `rk_live_...` key**, not a secret `sk_live_...` key.
- The adapter already **refuses** an `sk_live_` key unless `CS_ALLOW_STRIPE_SECRET_KEY=1`
  is explicitly set (a deliberate guard). Action: issue a restricted `rk_live_` key with
  read scopes, set it as **`STRIPE_KEY`** in SSM, and ensure `CS_ALLOW_STRIPE_SECRET_KEY`
  is **unset**.

**b) Tableau connected-app secret.**
- Rotate the Tableau embed secret and update the SSM parameter.

Full step-by-step in `Docs/SECURITY-ROTATION-RUNBOOK.md`.

**Verify:** `GET /api/integrations` still shows Stripe + Tableau as `connected (live)`
after rotation; the Reports page still renders embedded dashboards.

---

## Status summary

| Blocker | Owner | Platform ready? | Needs |
|---|---|---|---|
| 1. Live inbound channels | RevOps + CS | ✅ engine + pull ingress live | Service Hub Pro (A) or webhook wiring (B) |
| 2. Monthly digest live send | RevOps + Marketing Ops | ✅ compile/schedule/send path | HubSpot email template + 3 env vars |
| 3. Key rotation | RevOps / Security | ✅ guard already enforced | restricted Stripe key + Tableau secret |

Once these three are done, the platform's remaining spec items are 100% live. Everything
else from the V5 requirements is built, tested (327 passing), and running today.
