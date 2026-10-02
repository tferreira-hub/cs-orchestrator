# CS Platform — Write-Access Plan ("Work in One Place")

**Requirement (leadership):** CSMs must do all their work inside the CS Platform without
logging into other systems. That means the platform needs **write access** to the
connected systems, not just read.

This plan is grounded in the current codebase. It is deliberately incremental and
safety-gated, because write actions touch customer-facing systems (CRM, email, billing,
support).

---

## Where we are today (verified)

The platform already has a **safety-first write framework** — it is off by default, not
absent:

- `config.writes_allowed()` → reads `CS_ALLOW_WRITE` (default **off**).
- `config.http_post()` refuses POSTs to write-looking endpoints unless writes are enabled;
  `config.http_patch()` is **hard-blocked unless `CS_ALLOW_WRITE=1`**.
- **HubSpot write-back exists** (`sources.HubSpot.push_cs_data`, `engine.writeback`):
  pushes CS health / risk status / active playbook back to the company record.
  **Two-gate**: `apply=True` *and* `CS_ALLOW_WRITE=1`; dry-run otherwise. Audited
  (`record_audit`), exposed at `POST /api/accounts/{id}/writeback`.
- **Sequence enrolment exists** (`engine.enrol_sequence`): same two-gate, dry-run default.
- The **agent is read-only** and MCP tools are annotated `readOnlyHint`; the agent describes
  handoffs rather than executing them (grounding/honesty rule).
- Everything else (Zendesk, Stripe, Pendo, Jiminny) is **read-only** by design.

So "add write access" = **extend this existing gated framework** to the specific actions
each use case needs, with per-action audit + approval — not a new architecture.

---

## Write actions required, by use case

### Use Case 1 — Scaled Tech-Touch (in one place)
| Action | Target system | Status | Notes |
|---|---|---|---|
| Reply to / resolve an inbound item | Zendesk (or email) | **planned** | The inbound engine already triages + round-robins; needs a "send reply / close ticket" write. |
| Push technical item back to Zendesk, close in CS | Zendesk | **planned** | Create/transfer ticket + set `Ticket_Type`. |
| Enrol account in a re-engagement sequence | Sequencing tool / HubSpot | **built (gated, dry-run)** | `enrol_sequence`; wire a real send on apply. |
| Create / complete a pooled task | CS Platform (internal) | **partial** | Task events are recorded (`_record_task_event`); CSM "complete/snooze" write is small. |
| Flag CSQL / route expansion | HubSpot deal/CSQL | **planned** | Create a CSQL/deal or tag the owner. |

### Use Case 2 — Strategic (in one place)
| Action | Target system | Status | Notes |
|---|---|---|---|
| Push health / risk / playbook to CRM | HubSpot | **built (gated)** | `push_cs_data`. Turn on in prod. |
| Tag contact roles (Exec Sponsor / Champion / Finance) | HubSpot | **planned** | Needed for the Contact Role Validation gate; a contact-property write. |
| Log a call / meeting note | HubSpot timeline (or Jiminny) | **planned** | Engagement create on the company/contact. |
| Approve & send monthly performance report | Email/scheduler | **planned** | Depends on the reporting engine + outbound email (see prerequisites). |
| Advance a renewal-cadence task (T-120…T-30) | CS Platform + HubSpot | **partial** | Tasks exist; "mark done / add note" write + CRM sync. |
| Day-15 payment outreach logged | HubSpot timeline | **planned** | Record the executive-outreach action. |

### Use Case 3 — Leadership
Mostly **read** (dashboards). The only writes: SLA/queue config and capacity thresholds —
internal CS Platform settings, low risk.

---

## Per-system write scope + credential changes needed

| System | Current creds | Write change required |
|---|---|---|
| **HubSpot** | private-app token (write-back already uses PATCH, gated) | Confirm the private-app has **CRM write** scopes for companies, contacts, deals, engagements. |
| **Zendesk** | API token (read) | Needs a token/role with **ticket create/update/comment** scope. |
| **Stripe** | `sk_live_` (should be `rk_`) | **Keep read-only.** Do NOT grant Stripe write from the CS Platform — billing mutations are high-risk; "No Chasing" dunning stays in Stripe/HubSpot automation. |
| **Slack** | — (not yet integrated) | Inbound call-log form intake (Channel 2) is read-in; replies go via email/Zendesk, not Slack write. |
| **Email / sequencing** | — (none) | **New capability**: outbound send + scheduler for monthly digests, sequence sends, inbound replies. This is the biggest net-new piece. |

---

## Safety & governance model (keep, extend)

Every write must keep the existing guarantees:
1. **Two-gate**: a per-request `apply=true` **and** the environment `CS_ALLOW_WRITE=1`.
2. **Audit**: every applied write recorded via `record_audit` (immutable, hash-chained).
3. **Human-in-the-loop**: the agent proposes; a CSM approves. No autonomous customer-facing
   sends without explicit CSM action (matches the "human approves actions" footer).
4. **Scoped**: a CSM can only write to accounts they own (RBAC already enforces read scope;
   apply the same `_owns` check on writes).
5. **Reversibility first**: prefer internal/CRM-note writes (reversible) before
   customer-facing sends (irreversible). Confirm before irreversible actions.

---

## Prerequisites not yet in the platform

1. **Outbound email + scheduler** — required for monthly digests, sequence sends, and
   inbound reply threading. The platform has no send-email or cron capability today. This is
   a dedicated integration (e.g. SES + a scheduled task) and is the critical path for the
   Tech-Touch reporting/outreach writes.
2. **Zendesk write token** — a credential/role with ticket write scope.
3. **HubSpot write scopes** — confirm the private app can write companies/contacts/deals/
   engagements (write-back already PATCHes companies, so companies are covered; contacts/
   deals/engagements need confirming).

---

## Recommended increments (lowest risk → highest)

1. **Turn on the writes that already exist, in prod, gated**: HubSpot CS write-back +
   sequence enrolment, with `CS_ALLOW_WRITE=1`, per-account apply, audit, owner-scope. (Days.)
2. **CRM note / task writes** (reversible): log calls/notes to the HubSpot timeline; complete
   /snooze CS tasks; tag contact roles. Enables the Contact Role Validation gate. (Low risk.)
3. **Zendesk reply/close** from the inbound queue (Use Case 1). Needs the write token. (Medium.)
4. **Outbound email + scheduler** → monthly digests, sequence sends, inbound reply threading.
   The large net-new piece; unblocks the Tech-Touch reporting KPIs. (Larger.)
5. **CSQL / deal create** in HubSpot for expansion routing. (Medium.)
6. **Explicitly keep Stripe read-only.** Billing writes stay out of the CS Platform.

Each increment ships behind the two-gate + audit, owner-scoped, with a confirm step for any
customer-facing (irreversible) send.
