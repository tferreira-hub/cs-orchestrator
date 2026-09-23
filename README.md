# CS Orchestrator, the CS Platform + its agent harness

**Team: The Observers** · JobAdder "Harness The Hack" 2026.09

Two things in one repo, sharing **one rules engine** (the signed Ways of Working):

1. **CS Platform** (`platform/`), the "single pane of glass" the CS requirements ask
   for: an integrations layer over the CS stack, a rules/orchestration engine, a
   backend API, and a CSM dashboard UI (portfolio health, prioritised task queue,
   drafts, suppressed signals).
2. **Agent harness** (`plugins/cs-orchestrator/`), the same engine exposed as a
   Copilot plugin (MCP tools + custom agent + rules skill + enforcement hooks), for the
   hackathon's "harness the power" criterion.

## Run the CS Platform (single pane of glass)
```bash
CS_TODAY=2026-09-23 python3 platform/server.py     # http://localhost:8787
```
- UI: `http://localhost:8787/`, portfolio health (worst-first), the prioritised task
  queue with mandate/priority badges + drafted actions, and the suppressed-signals panel.
  Click any account for the detail drawer (signals, health drivers, contacts, tasks).
- API: `/api/portfolio`, `/api/accounts`, `/api/accounts/{id}`, `/api/tasks`, `/api/suppressed`, `/api/kpis`, `/api/lifecycle`, `/api/integrations`.

The UI is organised as a CS Platform: a grouped sidebar (Workspace, the Three-Mandate
Playbooks, and Data & Governance) with Dashboard, Task Queue, Accounts, Protect / Expand /
Adopt playbook views, Lifecycle, Leadership KPIs, and an Integrations map.

## Architecture (single source of truth)
```
CS stack (HubSpot, Zendesk, Stripe, usage, churn)
        │  integrations layer (adapters; fixtures now, live APIs later)
        ▼
   Rules / orchestration engine  ── the signed Ways of Working ──┐
   (orchestrate.py + suppression + health scoring in platform/engine.py)
        │                                                         │
   ┌────┴───────────────┐                            ┌───────────┴───────────┐
   ▼                    ▼                            ▼                       ▼
 Platform API        Platform UI                MCP tools + agent        Enforcement hooks
 (platform/server.py)(platform/ui)              (plugins/cs-orchestrator)(suppression, grounding)
```
The **same** WoW logic powers the product UI and the agent, so a CSM in the dashboard
and the `cs-orchestrator` agent always produce the identical standardised queue (WoW §1).

## Integrations layer
The MCP tools (`mcp-servers/cs_stack_server.py`) and the engine read from
`mcp-servers/fixtures/accounts.json`, whose shapes mirror the real vendor APIs
(HubSpot company object, Zendesk tickets/CSAT, Stripe invoices, usage telemetry, ML
churn, and Jiminny call sentiment). HubSpot is bi-directional: the platform pushes
health score, risk status, and active playbook back for Sales visibility
(`hubspot_push_cs_data` tool + the `hubspot_writeback` payload). Swap the fixture reads
for live API clients, bi-directional HubSpot, read-only Stripe, real-time Zendesk, with
no change to the engine, API, or UI. This is exactly the RevOps "data gap analysis /
telemetry assessment" next step.

---

> Turns Customer Success Managers from **data-gatherers into orchestrators**: the
> harness pulls signals from the CS stack, applies the signed **Ways of Working (WoW)**
> rules, suppresses multi-instance noise, and produces the CSM's **prioritised daily
> task queue**, with cited evidence and drafted actions.

This is not "an AI that guesses." It is the WoW framework **encoded as a harness** so
the process is *enforced by the system*, standardised for every CSM (WoW §1: "one
standardised way of working; individual preferences no longer supported").

---

## Why this matters (Impact)
CS is fragmented across HubSpot, Zendesk, Stripe, usage telemetry, and ML churn.
CSMs spend hours compiling data instead of acting. This harness synthesises those
signals into prioritised, revenue-protecting actions, directly serving the strategic
goals (shrink NDR, lift GRR, protect revenue, drive expansion).

## The four harness primitives (Harness the power)
`Agent = Model + Harness`. We built one of each mechanism:

| Component | Harness mechanism | File |
|---|---|---|
| CS stack as agent tools (HubSpot, Zendesk, usage, churn, Stripe) | **Tools / MCP** | `mcp-servers/.mcp.json`, `mcp-servers/cs_stack_server.py` |
| `cs-orchestrator` agent (plan → pull → apply rules → prioritise → draft) | **Deterministic** (custom agent) | `agents/cs-orchestrator.agent.md` |
| WoW rules (Three Mandates, routing, thresholds, cadence, payment) | **Automatic** (skill) | `skills/cs-playbook/SKILL.md` |
| Multi-instance suppression + grounding gate | **Enforcement** (hooks) | `hooks/hooks.json`, `hooks/scripts/*.py` |

## How the WoW framework maps in
- **Three Core Mandates** → every task tagged `MUST_PROTECT` / `MUST_EXPAND` / `MUST_USE`.
- **Strategic vs Scaled routing** → Strategic gets 1:1 tasks; Scaled is exception-only.
- **Predictive Risk Playbook** → churn ≥ 0.70, or ticket-spike + usage-drop, or Sev-1 → Priority-1, 24h SLA, drafted exec outreach.
- **Expansion triggers** → license utilization ≥ 85% or API surge ≥ 1.4× (healthy accounts only).
- **Proactive Renewal Cadence** → T-120 / T-90 / T-60 / T-30 from the renewal date.
- **Payment "No Chasing" rule** → days 1–14 automated (no task); day 15+ high-ARR Strategic → CSM task; Scaled → auto-suspend (no task).
- **Contact hygiene** (§5) → Strategic accounts missing Exec Sponsor / Champion / Finance get a hygiene task.
- **Multi-instance suppression** (§5) → signals on `test`/`secondary` instances never drive alerts.

---

## Run it

**Deterministic dry-run (reference implementation of the rules):**
```bash
cd plugins/cs-orchestrator
python3 orchestrate.py                 # whole portfolio
python3 orchestrate.py acct_northwind  # one account
```

**MCP server (what the agent calls):**
```bash
python3 mcp-servers/cs_stack_server.py   # stdio JSON-RPC MCP server
```

**Tests:**
```bash
python3 -m pytest tests/ -v      # 8 tests: rules, suppression, grounding gate
```

**As a Copilot plugin (VS Code / Copilot CLI):**
Install the plugin, then ask the `cs-orchestrator` agent:
> "What are my top CS actions today?"

It calls the `cs-stack` MCP tools, applies the `cs-playbook` skill, the suppression
hook drops test-instance noise, the grounding gate checks every figure, and it returns
the prioritised queue + drafts.

> **Data:** tools are backed by API-shaped fixtures (`mcp-servers/fixtures/accounts.json`)
> so the demo is deterministic and offline. Swap the fixture reads for live API clients
> to go to production, schemas already mirror the vendors (per the RevOps "data gap
> analysis" next step).

## The demo in one line
Ask for today's actions → **Northwind = Priority-1 churn risk** (churn 72% + Sev-1, drafted exec outreach) → **Initech = Day-15 payment** (high-ARR Strategic; SmallCo, Scaled, correctly auto-suspends with no task) → **Globex & Umbrella = expansion** → and **Umbrella's ticket spike is suppressed** because 16 of 18 tickets are on its `um-dev` **test** instance, so a naive tool would false-alarm churn, but the harness keeps Umbrella an expansion play.
