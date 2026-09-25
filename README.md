# CS Orchestrator — CS Platform + agent harness

**Team: The Observers** · JobAdder "Harness The Hack" 2026.09

Two things in one repo, sharing **one rules engine** (the signed Ways of Working):

1. **CS Platform** (`platform/`), the "single pane of glass": an integrations layer
   over the CS stack, a rules/orchestration engine, a backend API, and a CSM dashboard
   UI (portfolio health, prioritised task queue, drafted actions, suppressed signals).
2. **Agent harness** (`plugins/cs-orchestrator/`), the same engine exposed as a
   Copilot plugin — MCP tools, a multi-agent orchestrator, a rules skill, and
   enforcement hooks — satisfying the hackathon's "harness the power" criterion.

---

## Run the CS Platform (single pane of glass)
```bash
CS_TODAY=2026-09-23 python3 platform/server.py     # http://localhost:8787
```
- **UI** `http://localhost:8787/` — portfolio health (worst-first), the prioritised
  task queue with mandate/priority badges + drafted actions, and the suppressed-signals
  panel. Click any account for the detail drawer (signals, health drivers, contacts,
  tasks).
- **API** `/api/portfolio`, `/api/accounts`, `/api/accounts/{id}`, `/api/tasks`,
  `/api/suppressed`, `/api/kpis`, `/api/lifecycle`, `/api/integrations`, `/api/datagaps`.

The sidebar is grouped as a CS Platform: Workspace, the Three-Mandate Playbooks (Protect /
Expand / Adopt), and Data & Governance (Lifecycle, Leadership KPIs, Integrations map,
Data Gaps).

---

## Architecture (single source of truth)

The platform and agent share one deterministic WoW engine. Live sources are read-only by
default; MCP fixtures require the explicit `CS_MCP_MODE=fixture` opt-in; HubSpot
write-back requires a separate `CS_ALLOW_WRITE=1` double opt-in.

```
CS stack (HubSpot, Zendesk, Stripe, Pendo, Jiminny, Rocket Lane, Redshift)
      │  integrations layer  (live adapters · fixture fallback only for tests)
      ▼
Rules / orchestration engine  ─── signed Ways of Working ───────────────┐
(plugins/cs-orchestrator/orchestrate.py + suppression                   │
 + health scoring in platform/engine.py)                                │
      │                                                                  │
┌─────┴──────────────┐                              ┌────────────────────┴──────┐
▼                    ▼                              ▼                           ▼
Platform API      Platform UI                 MCP tools + agent          Enforcement hooks
(platform/        (platform/                 (plugins/cs-orchestrator/  (suppression +
 server.py)        ui/index.html)             agents/ + mcp-servers/)    grounding gate)
```

The **same** WoW logic powers the product UI and the agent, so a CSM in the dashboard and
the `cs-orchestrator` agent always produce the identical prioritised queue (WoW §1).

---

## Integrations layer

The platform uses live adapters when credentials are present. Missing sources are
returned as explicit data gaps; fixtures are used only for deterministic offline tests.

| System | Direction | What it supplies |
|---|---|---|
| **HubSpot** | bi-directional | roster, ARR, renewal, contacts, owner, instance family; health/risk/playbook write-back |
| **Zendesk** | read-only | tickets, CSAT, Sev-1, per-instance support signals |
| **Stripe** | read-only | invoice ageing, dunning/suspension handoff decisions |
| **Pendo** | read-only | risk advisor, adoption, usage recency, plan tier |
| **Jiminny** | read-only | latest call, sentiment, summary, customer talk ratio |
| **Rocket Lane** | read-only | onboarding status, health, time-to-value |
| **Redshift** | read-only | ML churn score / status via the Data API |

The structured queue also exposes adoption/onboarding tasks, multi-instance suppression,
payment automation decisions, task status, ageing, SLA adherence, and CSM capacity metrics.

---

> Turns Customer Success Managers from **data-gatherers into orchestrators**: the
> harness pulls signals from the CS stack, applies the signed **Ways of Working (WoW)**
> rules, suppresses multi-instance noise, and produces the CSM's **prioritised daily
> task queue** with cited evidence and drafted actions.

This is not "an AI that guesses." It is the WoW framework **encoded as a harness** so
the process is *enforced by the system*, standardised for every CSM (WoW §1: "one
standardised way of working; individual preferences no longer supported").

---

## Why this matters (Impact)

CS is fragmented across HubSpot, Zendesk, Stripe, usage telemetry, and ML churn.
CSMs spend hours compiling data instead of acting. This harness synthesises those
signals into prioritised, revenue-protecting actions, directly serving the strategic
goals (shrink NDR, lift GRR, protect revenue, drive expansion).

---

## The four harness primitives (Harness the power)

`Agent = Model + Harness`. We built one of each mechanism:

| Component | Harness mechanism | File |
|---|---|---|
| CS stack as agent tools (HubSpot, Zendesk, Pendo, Stripe, Jiminny, Redshift) | **Tools / MCP** | `plugins/cs-orchestrator/mcp-servers/cs_stack_server.py` |
| `cs-orchestrator` orchestrator + 4 specialist agents (`risk-analyst`, `renewal-planner`, `outreach-drafter`, `cs-playbook-judge`) | **Custom agents** (multi-agent, subagent loops, self-correction) | `plugins/cs-orchestrator/agents/*.agent.md` |
| WoW rules (Three Mandates, routing, thresholds, cadence, payment) | **Automatic** (skill) | `plugins/cs-orchestrator/skills/cs-playbook/SKILL.md` |
| Multi-instance suppression + grounding gate + deterministic playbook judge | **Enforcement** (hooks + feedback sensor) | `plugins/cs-orchestrator/hooks/`, `plugins/cs-orchestrator/playbook_judge.py` |

### Feedforward + feedback

Per Fowler/Böckeler, a harness is **Guides (feedforward)** + **Sensors (feedback)**,
each **computational** (deterministic) or **inferential** (LLM). This harness has both:

| Control | Type | Direction |
|---|---|---|
| `cs-playbook` SKILL.md + `copilot-instructions.md` + agent definitions | inferential | feedforward |
| `orchestrate.py` deterministic rules engine | computational | feedforward |
| `suppression.py` multi-instance filter | computational | feedback |
| `grounding-gate.py` (Stop hook) | computational | feedback |
| `pytest` (78 tests) | computational | feedback |
| `playbook_judge.py` (deterministic queue checker → PASS / NEEDS_CHANGES, wired into `orchestrate()`) | **computational** | **feedback** |
| `cs-playbook-judge` agent (LLM-as-judge, semantic review beyond code checks) | **inferential** | **feedback** |

The deterministic judge runs on **every queue** before results are returned. The agent
receives the queue, evidence, suppression list, and judge verdict through `get_task_queue`
before making recommendations. If the judge returns `NEEDS_CHANGES`, the agent re-delegates
only the flagged items to the relevant specialist, re-assembles, and judges again — capped
at two correction cycles.

---

## How the WoW framework maps in

- **Three Core Mandates** → every task tagged `MUST_PROTECT` / `MUST_EXPAND` / `MUST_USE`.
- **Strategic vs Scaled routing** → Strategic gets 1:1 tasks; Scaled is exception-only.
- **Predictive Risk Playbook** → ML churn ≥ 0.70, or ticket-spike + usage-drop, or Sev-1, or Pendo risk=High → Priority-1, 24h SLA, drafted exec outreach.
- **Expansion triggers** → license utilization ≥ 85% or API surge ≥ 1.4× (healthy accounts only).
- **Proactive Renewal Cadence** → T-120 / T-90 / T-60 / T-30 from the renewal date.
- **Payment "No Chasing" rule** → days 1–14 automated (no task); day 15+ high-ARR Strategic → CSM task; Scaled → auto-suspend (no task).
- **Contact hygiene** (§5) → Strategic accounts missing Exec Sponsor / Champion / Finance get a hygiene task.
- **Multi-instance suppression** (§5) → signals on `test`/`secondary` instances never drive alerts.
- **Computed vs ML scores** → a transparent signals-based score (Pendo + recency + CSAT) is the fallback when Redshift is not connected; it never fires ML-calibrated thresholds.

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
python3 plugins/cs-orchestrator/mcp-servers/cs_stack_server.py   # stdio JSON-RPC
```

**Tests:**
```bash
pip install -r requirements-dev.txt
python3 -m pytest plugins/cs-orchestrator/tests/ -v   # 78 tests: rules, adapters, MCP, agent, API/UI contracts
```

**As a Copilot plugin (VS Code / Copilot CLI):**
Install the plugin, then ask the `cs-orchestrator` agent:
> "What are my top CS actions today?"

It calls the `cs-stack` MCP tools, applies the `cs-playbook` skill, the suppression hook
drops test-instance noise, the grounding gate checks every figure, and it returns the
prioritised queue + drafts.

> **Data:** the demo runs deterministically from API-shaped fixtures (`CS_MCP_MODE=fixture`),
> while the platform uses live vendor adapters when configured. No unavailable signal is
> replaced with a guessed value.

---

## The demo in one line

Ask for today's actions → **Northwind = Priority-1 churn risk** (ML churn 72% + Sev-1,
drafted exec outreach) → **Initech = Day-15 payment** (high-ARR Strategic; SmallCo,
Scaled, correctly auto-suspends with no task) → **Globex & Umbrella = expansion** → and
**Umbrella's ticket spike is suppressed** because 16 of 18 tickets are on its `um-dev`
**test** instance, so a naive tool would false-alarm churn, but the harness keeps Umbrella
an expansion play.

---

## Docs

- [`Docs/HARNESS.md`](Docs/HARNESS.md) — end-to-end harness story: how we used the JobAdder harness to build this harness, feedforward/feedback model, install + run instructions, live data verification, and remaining production dependencies.
- [`Docs/CS-PLATFORM-GUIDE.md`](Docs/CS-PLATFORM-GUIDE.md) — full CS Platform feature guide.
- [`Docs/DATA-ACCESS.md`](Docs/DATA-ACCESS.md) — live-only data access policy and adapter contracts.
- [`Docs/CHURN-MODEL-CONTRACT.md`](Docs/CHURN-MODEL-CONTRACT.md) — churn model interface contract (ML vs computed score).
