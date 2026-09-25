# Copilot Instructions for cs-orchestrator

This repository is the **Helm CS Platform** and its agent harness (the `cs-orchestrator`
plugin). `Agent = Model + Harness`. See [HARNESS.md](../HARNESS.md) for how the harness maps
to the four primitives (custom instructions, agents, tools/MCP, hooks) and how it aligns to
the JobAdder `agent-plugins` structure.

## Single source of truth (Ways of Working)

The `cs-playbook` skill is the signed CS Ways of Working. Identical inputs MUST always
produce the identical prioritised task queue, regardless of who asks. Do not add
per-user or ad-hoc workflows. The rules live in
`plugins/cs-orchestrator/skills/cs-playbook/SKILL.md` and are implemented deterministically
in `plugins/cs-orchestrator/orchestrate.py`.

## Data honesty (no mock data)

The platform runs on live data with per-signal provenance. Never display or fabricate a
value that a source system does not provide. If a source lacks data for an account, surface
it as "not connected" / "no data" — the **Data Gaps** view exists precisely to make those
gaps visible. Any derived value (e.g. computed churn risk) MUST be labelled as computed,
never presented as an ML model output.

## Read-only by default

Live adapters are read-only. `http_patch` is hard-blocked unless `CS_ALLOW_WRITE=1`;
`http_post` is allowed only for vendor search / batch-read endpoints. The Stripe adapter
refuses `sk_live_` secret keys — use a restricted `rk_` key. Do not weaken these guards.

## Plugin versioning (always required)

Whenever any file under `plugins/<plugin-name>/` changes (agents, skills, hooks, MCP
config, adapters), bump the plugin's version in BOTH files, keeping them equal:

- `plugins/<plugin-name>/plugin.json` — the `version` field.
- `.github/plugin/marketplace.json` — the `version` field on that plugin's entry.

Use semantic versioning: patch for fixes/docs, minor for new agents/skills/hooks/adapters,
major for breaking structural changes. Don't double-bump within a single uncommitted change
set (compare against `git diff HEAD` first).

## Verification

Run `python3 -m pytest -q` in `plugins/cs-orchestrator/` before presenting changes. The
grounding-gate hook checks that any figure in an answer exists in the evidence set.
