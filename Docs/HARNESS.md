# Harness, end to end

**Team: The Observers.** This is the "Harness the power" story: we **used** the JobAdder
agent harness (in GitHub Copilot) to **build** a harness (the `cs-orchestrator` plugin)
that powers a product (the Helm CS Platform).

`Agent = Model + Harness`. We built one of each of the four primitives:

| Primitive | What we built | File |
|---|---|---|
| Custom instructions | `cs-playbook` rules (the signed Ways of Working) | `plugins/cs-orchestrator/skills/cs-playbook/SKILL.md` |
| Custom Agents | `cs-orchestrator` delegator + `risk-analyst`, `renewal-planner`, `outreach-drafter` specialists + `cs-playbook-judge` feedback sensor (multi-agent, `agent/runSubagent` + handoffs + self-correction loop) | `plugins/cs-orchestrator/agents/*.agent.md` |
| Tools / MCP | CS stack as tools (HubSpot, Zendesk, Stripe, Pendo, Jiminny, Redshift churn) + HubSpot write-back | `plugins/cs-orchestrator/mcp-servers/` |
| Hooks | multi-instance suppression + grounding gate (enforcement) | `plugins/cs-orchestrator/hooks/` |

> Aligned to the JobAdder `agent-plugins` schema (verified against the local clone at
> `~/github/agent-plugins`): `plugin.json` (name/version/description/author/skills/agents/hooks),
> `agents/*.agent.md` with YAML frontmatter (name/description/tools), `skills/<name>/SKILL.md`,
> `hooks.json` (version + lifecycle events using `${PLUGIN_ROOT}`), and a
> `.github/plugin/marketplace.json`. Per that repo's `copilot-instructions.md`, bump both
> `plugin.json` and `marketplace.json` versions together on any plugin change.

## Feedforward + feedback (harness-engineering model)

Per Fowler/Böckeler, `Agent = Model + Harness`, and a harness is **Guides (feedforward)** +
**Sensors (feedback)**, each **computational** (deterministic) or **inferential** (LLM).
This harness has both halves:

| Control | Type | Direction |
|---|---|---|
| `cs-playbook` rules + `copilot-instructions.md` + agent defs | inferential | feedforward |
| `orchestrate.py` deterministic rules engine | computational | feedforward |
| `suppression.py` multi-instance filter | computational | feedback |
| `grounding-gate.py` (Stop hook) | computational | feedback |
| `pytest` (33 tests) | computational | feedback |
| **`playbook_judge.py`** (deterministic queue checker → PASS/NEEDS_CHANGES, wired into `orchestrate()`) | **computational** | **feedback** |
| **`cs-playbook-judge`** agent (LLM-as-judge, semantic review beyond the code checks) | **inferential** | **feedback** |

The deterministic orchestrator runs the judge before returning results. The live Agent Automation
flow (surfaced in the UI as Ask the CS Agent)
receives that queue, evidence, suppression list, and judge verdict through `get_task_queue`
before making recommendations. Inferential specialist delegation remains available for
semantic drafting and review; the deterministic queue is the final source of truth.
The runtime blocks answers that skip the queue, fail the deterministic judge, mention
unsupported numeric claims, or omit all queued account context. It retries correction at
most twice before returning a blocked result. MCP is live-only by default; fixture data
requires the explicit `CS_MCP_MODE=fixture` offline-demo setting.
Ask Agent numeric claims are scoped to the accounts named in the answer, and the response
also returns a deterministic action packet containing evidence, source provenance, owner,
SLA, and unresolved data gaps.
The deterministic judge independently checks exact trigger priorities and rejects risk
evidence that is demonstrably concentrated on secondary/test instances. Bedrock contract
tests cover queue-tool usage, delegation, correction retries, and structured output without
requiring a production model call.
The Stop grounding hook consumes the same structured action packet when supplied, scopes
numeric evidence to the named account, and retains a legacy fixture fallback only for older
plain-text hook invocations. The judge also recomputes expected tasks and flags omissions.

---

## Part A. Use the harness to build (the JobAdder agent-plugins workflow)

This mirrors the JobAdder demo videos (planner, developer, reviewer, subagent loops).

1. Install the GitHub Copilot CLI:
   ```bash
   npm install -g @github/copilot
   ```
2. Add the JobAdder harness marketplace and install the agent plugin:
   ```bash
   copilot plugin marketplace add JobAdder/agent-plugins
   copilot plugin install ja-agile-agents@jobadder-agent-plugin-marketplace
   copilot plugin list
   ```
   > Note: JobAdder is merging `jobadder-agent-plugin` into `ja-agile-agents` and
   > extracting the out-of-the-box MCP servers (GitHub, Playwright, Atlassian, New Relic)
   > into a separate plugin you can disable when you don't need them. If that lands, the
   > install name above may change (run `copilot plugin marketplace list` to confirm the
   > current entry). This only affects the bootstrap harness we build *with*; our
   > `cs-orchestrator` plugin defines its own MCP config and is unaffected. Disabling the
   > OOTB MCP servers keeps build sessions lean when working on the CS plugin.
3. In a `copilot` session (or VS Code agent mode), drive the build with the harness agents:
   - `feature-planner`: turn a CS requirement into a `plan.md` (for example "add the Jiminny call-sentiment signal to the risk playbook").
   - `developer`: implement the plan in `plugins/cs-orchestrator` and `platform/`.
   - `code-reviewer`: review the change.
   - Use subagent implement/test loops so each change is verified against `tests/`.

Retro (judged): use a stronger model for exploration and planning (the JobAdder reflection
notes Sol for exploration, or Luna at max thinking), keep subagent loops tight, and let the
tests gate each step.

## Part B. Install and run the harness we built

Our plugin is a Copilot marketplace of its own.

1. Install from the local working tree (fastest for the demo):
   ```bash
   copilot plugin install /Users/tferreira/github/cs-orchestrator/plugins/cs-orchestrator
   copilot plugin list
   ```
   Or add it as a marketplace: `copilot plugin marketplace add <this repo>` then
   `copilot plugin install cs-orchestrator@cs-orchestrator-marketplace`.
2. In a `copilot` session, invoke the agent:
   > "@cs-orchestrator what are my top CS actions today?"
   The agent calls the `cs-stack` MCP tools, applies the `cs-playbook` rules, the
   suppression hook drops test-instance noise, and the grounding gate checks every figure
   on `Stop`.

## Part C. Run the product (the CS Platform UI)

```bash
CS_TODAY=2026-09-23 python3 platform/server.py        # http://localhost:8787
```
The dashboard, task queue, mandate playbooks, lifecycle, leadership KPIs, and integrations
map are all driven by the same rules engine the agent uses. One source of truth.

---

## Proof it is a real harness (verified)
- `plugins/cs-orchestrator/plugin.json` + `.github/plugin/marketplace.json` follow the
  JobAdder agent-plugins format (matched field for field against the local clone).
- `.mcp.json` uses `type: stdio`.
- `hooks.json` fires `grounding-gate.py` on `Stop` using the `${PLUGIN_ROOT}` convention.
- MCP server and hook resolve their data from their own location (no env needed).
- **33 tests pass** (`plugins/cs-orchestrator/tests/`): rules, suppression, grounding gate,
  Stripe secret-key guard, and the playbook judge (PASS on a clean queue; catches
  priority/evidence/draft/routing violations on a deliberately-broken queue).
- The **playbook judge runs on every queue** (`orchestrate()` returns a `judge` verdict; the
  API exposes it in `summary.judge`; the UI shows a "WoW judge: PASS / N issues" badge).

## Live data (verified end to end)
The platform runs on **live data** across five sources — no mock data is displayed:
- HubSpot (roster, ARR via `arr__v2_`, segment via `icp_sales_segment`, owner, industry, contacts)
- Zendesk (tickets/CSAT/Sev-1; org matched via `external_id` then `workato_unique_id`)
- Stripe (invoice/dunning via `metadata['ja_account_id']`, API version 2023-10-16)
- Pendo (risk advisor, adoption, usage recency)
- Jiminny (latest call, sentiment, summary, customer talk ratio)
- Redshift churn status via the Data API (read-only; `calculated_churn_status`)
A transparent computed risk remains the fallback when Redshift is unavailable.
A configurable Pendo metadata mapping is available for utilization, active users,
feature adoption, API calls, and login velocity; the current live endpoint did not expose
those keys, so they remain unavailable until the data owner supplies the mappings.
A **Data Gaps** governance view surfaces per-account source coverage and missing HubSpot
fields — the RevOps "Data Gap Analysis" deliverable.

## Remaining production dependencies
- Enable the live HubSpot write-back (create `cs_health_score` / `cs_risk_status` /
  `cs_active_playbook` company properties, set `CS_ALLOW_WRITE=1`).
- Provide the Rocket Lane API contract and credentials, then add onboarding status,
   time-to-value, and onboarding health to the same live account payload.
- Publish a numeric Redshift probability view when the data platform makes one available;
   the current status view is already integrated safely without inventing a probability.
