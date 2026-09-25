# CS Orchestrator: CS Platform + agent harness

**Team: The Observers** · JobAdder "Harness The Hack" 2026.09

This repo has two things in it, and they share one rules engine (the signed Ways of Working):

1. **CS Platform** (`platform/`), a single pane of glass for Customer Success: an integrations layer over the CS stack, a rules/orchestration engine, a backend API, and a CSM dashboard UI with portfolio health, a prioritised task queue, drafted actions, and suppressed signals.
2. **Agent harness** (`plugins/cs-orchestrator/`), the same engine exposed as a Copilot plugin with MCP tools, a multi-agent orchestrator, a rules skill, and enforcement hooks. This is the "harness the power" part.

The point of keeping them together is that a CSM using the dashboard and the `cs-orchestrator` agent will always get the identical prioritised queue. One source of truth, no drift.

## Running the CS Platform

```bash
CS_TODAY=2026-09-23 python3 platform/server.py     # http://localhost:8787
```

The UI at `http://localhost:8787/` shows portfolio health (worst accounts first), the task queue with mandate and priority badges, drafted actions, and a suppressed signals panel. Click any account to open a detail drawer with signals, health drivers, contacts, and tasks.

The API endpoints are:
`/api/portfolio`, `/api/accounts`, `/api/accounts/{id}`, `/api/tasks`, `/api/suppressed`, `/api/kpis`, `/api/lifecycle`, `/api/integrations`, `/api/datagaps`

The sidebar groups everything as a CS Platform: Workspace, the Three-Mandate Playbooks (Protect / Expand / Adopt), and Data & Governance (Lifecycle, Leadership KPIs, Integrations map, Data Gaps).

## Architecture

Live sources are read-only by default. MCP fixtures need the explicit `CS_MCP_MODE=fixture` opt-in. HubSpot write-back needs a separate `CS_ALLOW_WRITE=1` double opt-in.

```
CS stack (HubSpot, Zendesk, Stripe, Pendo, Jiminny, Rocket Lane, Redshift)
      |  integrations layer  (live adapters, fixture fallback only for tests)
      v
Rules / orchestration engine  --- signed Ways of Working -------------------+
(plugins/cs-orchestrator/orchestrate.py + suppression                      |
 + health scoring in platform/engine.py)                                   |
      |                                                                     |
+-----+------------------+                         +----------------------+--+
v                        v                         v                      v
Platform API          Platform UI            MCP tools + agent       Enforcement hooks
(platform/            (platform/             (plugins/cs-            (suppression +
 server.py)            ui/index.html)         orchestrator/           grounding gate)
                                              agents/ + mcp-servers/)
```

## Integrations

The platform calls live adapters when credentials are present. If a source is missing it shows up as a data gap. Fixtures are only used for deterministic offline tests.

| System | Direction | What it supplies |
|--------|-----------|-----------------|
| HubSpot | bi-directional | roster, ARR, renewal, contacts, owner, instance family; health/risk/playbook write-back |
| Zendesk | read-only | tickets, CSAT, Sev-1, per-instance support signals |
| Stripe | read-only | invoice ageing, dunning/suspension handoff decisions |
| Pendo | read-only | risk advisor, adoption, usage recency, plan tier |
| Jiminny | read-only | latest call, sentiment, summary, customer talk ratio |
| Rocket Lane | read-only | onboarding status, health, time-to-value |
| Redshift | read-only | ML churn score / status via the Data API |

The queue also tracks adoption/onboarding tasks, multi-instance suppression, payment automation decisions, task ageing, SLA adherence, and CSM capacity metrics.

## Why we built this

CS work is spread across HubSpot, Zendesk, Stripe, Pendo, and an ML churn model. CSMs spend a big chunk of their day just gathering and cross-referencing data before they can act on anything. We wanted to fix that.

CS Orchestrator pulls all those signals together, runs them through the Ways of Working rules, suppresses noise from test/secondary instances, and hands the CSM a single prioritised queue with the evidence already cited and the outreach message already drafted. The goal is to shift CSMs from data-gatherers to orchestrators, and to make the process consistent across the whole team rather than dependent on individual habits.

This is not an AI that guesses. The WoW rules are encoded as deterministic code, enforced by the system, not prompted into an LLM and hoped for.

## The four harness primitives

`Agent = Model + Harness`. We built one of each:

| Component | Mechanism | File |
|-----------|-----------|------|
| CS stack as agent tools (HubSpot, Zendesk, Pendo, Stripe, Jiminny, Redshift) | Tools / MCP | `plugins/cs-orchestrator/mcp-servers/cs_stack_server.py` |
| `cs-orchestrator` orchestrator + 4 specialist agents (`risk-analyst`, `renewal-planner`, `outreach-drafter`, `cs-playbook-judge`) | Custom agents (multi-agent, subagent loops, self-correction) | `plugins/cs-orchestrator/agents/*.agent.md` |
| WoW rules (Three Mandates, routing, thresholds, cadence, payment) | Automatic skill | `plugins/cs-orchestrator/skills/cs-playbook/SKILL.md` |
| Multi-instance suppression + grounding gate + deterministic playbook judge | Enforcement hooks + feedback sensor | `plugins/cs-orchestrator/hooks/`, `plugins/cs-orchestrator/playbook_judge.py` |

### Feedforward and feedback

Following Fowler/Böckeler's model, a harness is Guides (feedforward) + Sensors (feedback), each either computational (deterministic) or inferential (LLM). We have both:

| Control | Type | Direction |
|---------|------|-----------|
| `cs-playbook` SKILL.md + `copilot-instructions.md` + agent definitions | inferential | feedforward |
| `orchestrate.py` deterministic rules engine | computational | feedforward |
| `suppression.py` multi-instance filter | computational | feedback |
| `grounding-gate.py` (Stop hook) | computational | feedback |
| `pytest` (78 tests) | computational | feedback |
| `playbook_judge.py` (queue checker, returns PASS / NEEDS_CHANGES, wired into `orchestrate()`) | computational | feedback |
| `cs-playbook-judge` agent (LLM-as-judge, semantic review on top of the code checks) | inferential | feedback |

The deterministic judge runs on every queue before results go back to the user. The agent gets the queue, evidence, suppression list, and judge verdict via `get_task_queue` before it makes any recommendations. If the judge returns `NEEDS_CHANGES`, the agent re-delegates only the flagged items to the right specialist, re-assembles, and judges again. It caps at two correction cycles.

## How the WoW rules map in

- **Three Core Mandates**: every task is tagged `MUST_PROTECT`, `MUST_EXPAND`, or `MUST_USE`.
- **Strategic vs Scaled routing**: Strategic accounts get 1:1 tasks; Scaled is exception-only.
- **Predictive Risk Playbook**: ML churn >= 0.70, or ticket spike + usage drop, or Sev-1, or Pendo risk = High fires a Priority-1 task with a 24h SLA and a drafted exec outreach.
- **Expansion triggers**: license utilization >= 85% or API surge >= 1.4x on healthy accounts.
- **Proactive Renewal Cadence**: T-120 / T-90 / T-60 / T-30 from the renewal date.
- **Payment "No Chasing" rule**: days 1-14 are fully automated with no CSM task; day 15+ on high-ARR Strategic accounts gets a CSM task; Scaled auto-suspends with no task.
- **Contact hygiene** (WoW §5): Strategic accounts missing an Exec Sponsor, Champion, or Finance Contact get a hygiene task.
- **Multi-instance suppression** (WoW §5): signals from `test` or `secondary` instances never drive alerts.
- **Computed vs ML scores**: when Redshift is not connected, a transparent signals-based score from Pendo + recency + CSAT is used as a fallback. It never fires the ML-calibrated Priority-1 threshold.

## Running it

**Deterministic dry-run:**
```bash
cd plugins/cs-orchestrator
python3 orchestrate.py                 # whole portfolio
python3 orchestrate.py acct_northwind  # one account
```

**MCP server:**
```bash
python3 plugins/cs-orchestrator/mcp-servers/cs_stack_server.py
```

**Tests:**
```bash
pip install -r requirements-dev.txt
python3 -m pytest plugins/cs-orchestrator/tests/ -v
```
78 tests covering rules, adapters, MCP, agent contracts, and the API/UI.

**As a Copilot plugin:**

Install the plugin and ask the agent:
> "What are my top CS actions today?"

It calls the `cs-stack` MCP tools, applies the `cs-playbook` skill, the suppression hook drops test-instance noise, the grounding gate checks every figure, and you get the prioritised queue with drafts.

The demo runs from API-shaped fixtures with `CS_MCP_MODE=fixture`. The platform uses live vendor adapters when credentials are configured. No unavailable signal gets replaced with a made-up value.

## The demo story

Ask for today's actions. Northwind comes back as a Priority-1 churn risk (ML churn 72% + open Sev-1, with a drafted exec outreach ready to go). Initech has a Day-15 payment task because it's a high-ARR Strategic account. SmallCo is Scaled, so it auto-suspends with no CSM task at all. Globex and Umbrella are both expansion plays. The interesting one is Umbrella: 16 of its 18 tickets are on the `um-dev` test instance, so a naive tool would flag it as a churn risk. The harness suppresses that noise and keeps Umbrella as an expansion opportunity.

## Docs

- [Docs/HARNESS.md](Docs/HARNESS.md): end-to-end harness story, feedforward/feedback breakdown, install instructions, live data verification, and remaining production dependencies.
- [Docs/CS-PLATFORM-GUIDE.md](Docs/CS-PLATFORM-GUIDE.md): full CS Platform feature guide.
- [Docs/DATA-ACCESS.md](Docs/DATA-ACCESS.md): live-only data access policy and adapter contracts.
- [Docs/CHURN-MODEL-CONTRACT.md](Docs/CHURN-MODEL-CONTRACT.md): churn model interface contract (ML vs computed score).
