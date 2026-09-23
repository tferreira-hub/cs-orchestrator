# Harness, end to end

**Team: The Observers.** This is the "Harness the power" story: we **used** the JobAdder
agent harness (in GitHub Copilot) to **build** a harness (the `cs-orchestrator` plugin)
that powers a product (the Helm CS Platform).

`Agent = Model + Harness`. We built one of each of the four primitives:

| Primitive | What we built | File |
|---|---|---|
| Custom instructions | `cs-playbook` rules (the signed Ways of Working) | `plugins/cs-orchestrator/skills/cs-playbook/SKILL.md` |
| Custom Agents | `cs-orchestrator` (plan, pull, apply rules, prioritise, draft) | `plugins/cs-orchestrator/agents/cs-orchestrator.agent.md` |
| Tools / MCP | CS stack as tools (HubSpot, Zendesk, Stripe, usage, churn, Jiminny) + HubSpot write-back | `plugins/cs-orchestrator/mcp-servers/` |
| Hooks | multi-instance suppression + grounding gate (enforcement) | `plugins/cs-orchestrator/hooks/` |

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
  JobAdder agent-plugins format (matched field for field).
- `.mcp.json` uses `type: stdio` and the installed-plugin path convention.
- `hooks.json` fires `grounding-gate.py` on `Stop` using the installed-plugin path.
- MCP server and hook resolve their data from their own location (no env needed), so they
  work from the Copilot install path.
- 8 tests pass (`plugins/cs-orchestrator/tests/`): rules, suppression, grounding gate.

## What we would do next
- Wire live adapters (bi-directional HubSpot, read-only Stripe/Zendesk, Jiminny) behind the
  same tool contracts.
- Add a `skill-creator`-generated onboarding-handoff skill (Rocket Lane).
- Per-account grounding in the gate (today it checks figures against the full evidence set).
