#!/usr/bin/env python3
"""CS Agent Runner — actually EXECUTES the cs-orchestrator agents via Amazon Bedrock.

This turns the `.agent.md` definitions from documentation into running agents:
  - loads each agent's frontmatter + body (the system prompt),
  - exposes the cs-stack as real tools to the model (Bedrock Converse tool-use),
    - requires the deterministic queue and judge, validates evidence, and retries the
        Bedrock answer at most twice when the harness finds unsupported claims,
  - returns a real transcript.

Auth: Amazon Bedrock in the account the platform runs in. In production this is the
DevOps account (880082283556) via the ECS task role — no named profile. For local
development you may set CS_BEDROCK_PROFILE to an SSO profile. Configure with env:
  CS_BEDROCK_PROFILE   (default: empty → use the ambient/task-role credential chain)
  CS_BEDROCK_REGION    (default: ap-southeast-2 — DevOps region with Claude enabled)
  CS_BEDROCK_MODEL     (default: au.anthropic.claude-sonnet-4-5-20250929-v1:0 inference profile)

If credentials are absent it reports 'Bedrock not authenticated' rather than crashing.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN.parents[1] / "platform"))  # dataaccess / engine

AGENTS_DIR = PLUGIN / "agents"

PROFILE = os.environ.get("CS_BEDROCK_PROFILE", "").strip()
REGION = os.environ.get("CS_BEDROCK_REGION", "ap-southeast-2")
MODEL = os.environ.get("CS_BEDROCK_MODEL", "au.anthropic.claude-sonnet-4-5-20250929-v1:0")
MAX_TURNS = int(os.environ.get("CS_AGENT_MAX_TURNS", "12"))
MAX_CORRECTIONS = 2
POLICY_NUMBERS = {"0.4", "0.45", "0.6", "0.7", "0.85", "1.4", "24", "60", "85", "90", "120", "180"}

# The final response is not trusted merely because the model stopped generating:
# queue provenance, judge status, and evidence validation are required first.


def _principal_role(principal: dict | None) -> str:
    """admin (all accounts) or csm (own book). Unknown/absent → admin-equivalent read."""
    if not principal:
        return "admin"
    return "csm" if str(principal.get("role")) == "csm" else "admin"


def _scope_accounts(accounts: dict, principal: dict | None) -> tuple[dict, str | None]:
    """Return (accounts_in_scope, owner_name). Admins see all; CSMs see only the
    accounts they own (matched on hubspot.csm_owner_id == principal.owner_id),
    mirroring the platform's engine._owns scoping so the agent and dashboard agree."""
    if _principal_role(principal) != "csm":
        return accounts, None
    owner_id = str((principal or {}).get("owner_id") or "")
    if not owner_id:
        # A CSM with no resolvable owner id sees an empty book rather than everything.
        return {}, ((principal or {}).get("name") or (principal or {}).get("email"))
    scoped = {aid: a for aid, a in accounts.items()
              if str(a.get("hubspot", {}).get("csm_owner_id") or "") == owner_id}
    # Prefer the actual HubSpot owner name from the scoped accounts; fall back to session name.
    owner_name = None
    for a in scoped.values():
        owner_name = a.get("hubspot", {}).get("csm_owner") or owner_name
    owner_name = owner_name or (principal or {}).get("name") or (principal or {}).get("email")
    return scoped, owner_name


def _identity_line(principal: dict | None, scoped_count: int, total_count: int) -> str:
    """A short, honest identity + scope statement the agent leads with."""
    who = (principal or {}).get("name") or (principal or {}).get("email") or "this session"
    if _principal_role(principal) == "csm":
        return (f"You're signed in as {who} (CSM). I'm scoped to your book of "
                f"{scoped_count} account{'s' if scoped_count != 1 else ''} — everything below is "
                f"limited to accounts you own.")
    return (f"You're signed in as {who} (Admin). I can see the full portfolio of "
            f"{total_count} account{'s' if total_count != 1 else ''} across every CSM book.")


# --------------------------------------------------------------------------- #
# Agent definitions: parse .agent.md (frontmatter + body)
# --------------------------------------------------------------------------- #
def load_agent(name: str) -> dict:
    path = AGENTS_DIR / f"{name}.agent.md"
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path} missing frontmatter")
    fm_raw, body = m.group(1), m.group(2)
    fm = {}
    for line in fm_raw.splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    skill_path = PLUGIN / "skills" / "cs-playbook" / "SKILL.md"
    skill = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
    runtime = ("# Runtime\nYou are running in the CS Platform through Amazon Bedrock "
               f"using the configured model `{MODEL}`.")
    return {"name": name, "frontmatter": fm,
        "system": body.strip() + "\n\n" + runtime + "\n\n# Signed CS Playbook\n" + skill}


# --------------------------------------------------------------------------- #
# Tools exposed to the model — backed by the SAME live data as the platform
# --------------------------------------------------------------------------- #
def _tools_impl(accounts_override: dict | None = None):
    import dataaccess
    import orchestrate
    accounts = accounts_override if accounts_override is not None else dataaccess.all_accounts()  # live or scoped
    previous_provider = orchestrate._ACCOUNT_PROVIDER
    orchestrate.set_account_provider(lambda: accounts)
    try:
        queue = orchestrate.orchestrate()
    finally:
        orchestrate.set_account_provider(previous_provider)
    state = {"queue_called": False}

    def list_accounts(_):
        return [{"account_id": aid,
                 "name": a.get("hubspot", {}).get("name"),
                 "segment": a.get("hubspot", {}).get("segment_label") or a.get("hubspot", {}).get("segment"),
                 "arr_usd": a.get("hubspot", {}).get("arr_usd")}
                for aid, a in accounts.items()]

    def _get(aid):
        return accounts.get(aid) or accounts.get(aid.upper()) or accounts.get(aid.lower()) or {}

    def get_task_queue(_):
        """Return the deterministic queue, evidence, suppression, and judge result."""
        state["queue_called"] = True
        return queue

    def get_portfolio_snapshot(_):
        """Return the single approval portfolio snapshot used by the Agent."""
        return {
            "reviewed": len(accounts),
            "accounts": accounts,
            "tasks": queue.get("tasks", []),
            "suppressed": queue.get("suppressed", []),
            "automations": queue.get("automations", []),
            "judge": queue.get("judge", {}),
        }

    def hubspot_writeback_preview(args):
        account = _get(args["account_id"])
        return {
            "mode": "dry-run",
            "account_id": args["account_id"],
            "note": "Preview only. The agent cannot apply HubSpot writes.",
            "health": account.get("hubspot", {}).get("name"),
            "source": account.get("sources", {}).get("hubspot", "not_live"),
        }

    return accounts, {
        "list_accounts": (list_accounts, "List all CS accounts (account_id, name, segment, ARR)."),
        "get_portfolio_snapshot": (get_portfolio_snapshot,
                                    "Get the live portfolio, deterministic task queue, suppressed signals, automation handoffs, and WoW judge result in one operation."),
        "get_task_queue": (get_task_queue, "Get the deterministic CS task queue with evidence, suppression, and WoW judge result."),
        "hubspot_get_account": (lambda a: _get(a["account_id"]).get("hubspot", {}),
                                 "HubSpot company: segment, ARR, renewal, owner, industry, contacts, instances."),
        "zendesk_get_tickets": (lambda a: _get(a["account_id"]).get("zendesk", {}),
                                 "Zendesk: open tickets, 7d vs prior-7d, Sev-1, CSAT, per-instance."),
        "usage_get_metrics": (lambda a: _get(a["account_id"]).get("usage", {}),
                               "Pendo usage: risk advisor, adoption, days since last visit."),
        "churn_get_score": (lambda a: _get(a["account_id"]).get("churn", {}),
                     "Redshift churn status or ML score, with source provenance; computed fallback is explicitly marked."),
        "stripe_get_payment": (lambda a: _get(a["account_id"]).get("stripe", {}),
                               "Stripe: past-due invoices, amount due, dunning stage."),
        "jiminny_get_calls": (lambda a: _get(a["account_id"]).get("jiminny", {}),
                       "Jiminny: latest call date, sentiment, summary, customer talk ratio."),
        "hubspot_writeback_preview": (hubspot_writeback_preview,
                           "Prepare a read-only HubSpot CS write-back preview; never apply changes."),
    }, state, queue


def _answer_numbers(answer: str) -> set[str]:
    tokens = re.findall(r"\$?\d[\d,]*(?:\.\d+)?%?", answer)
    values = set()
    for token in tokens:
        value = token.lstrip("$").rstrip("%").replace(",", "")
        try:
            number = float(value)
        except ValueError:
            continue
        values.add(str(int(number)) if number.is_integer() else str(number).rstrip("0").rstrip("."))
    return values


def _clean_agent_text(text: str) -> str:
    """Keep model prose presentation-neutral and consistent with the UI style."""
    return text.replace("—", " - ").replace("–", " - ")


def _evidence_numbers(queue: dict, accounts: dict) -> set[str]:
    raw = json.dumps({"queue": queue, "accounts": accounts}, default=str)
    values = _answer_numbers(raw)
    for value in list(values):
        try:
            number = float(value)
        except ValueError:
            continue
        if 0 < number < 1:
            values.add(str(int(round(number * 100))))
    return values


def _structured_actions(queue: dict, accounts: dict) -> list[dict]:
    by_name = {a.get("hubspot", {}).get("name"): a for a in accounts.values()}
    actions = []
    for task in queue.get("tasks", []):
        account = by_name.get(task.get("account"), {})
        hs = account.get("hubspot", {})
        sources = account.get("sources", {})
        gaps = [name for name, value in sources.items() if value not in ("live", "computed")]
        missing_roles = sorted({"Executive Sponsor", "Primary Champion / Admin", "Finance Contact"} -
                               {c.get("role") for c in hs.get("contacts", [])})
        if missing_roles:
            gaps.append("missing contact roles: " + ", ".join(missing_roles))
        priority = task.get("priority")
        sla = {1: "within 24 hours", 2: "today", 3: "this week", 4: "before renewal milestone", 5: "this week", 6: "programmatic"}.get(priority, "review")
        actions.append({
            "account": task.get("account"),
            "segment": task.get("segment"),
            "priority": priority,
            "mandate": {"MUST_PROTECT": "Protect", "MUST_EXPAND": "Expand", "MUST_USE": "Adopt"}.get(task.get("mandate"), task.get("mandate")),
            "trigger": task.get("trigger"),
            "evidence": task.get("evidence", {}),
            "source": sources,
            "owner": hs.get("csm_owner") or "Unassigned",
            "sla": sla,
            "recommended_action": task.get("recommended_action"),
            "data_gaps": gaps,
        })
    return actions


def _account_evidence_numbers(account: dict, tasks: list[dict]) -> set[str]:
    values = _answer_numbers(json.dumps({"account": account, "tasks": tasks}, default=str))
    for value in list(values):
        try:
            number = float(value)
        except ValueError:
            continue
        if 0 < number < 1:
            values.add(str(int(round(number * 100))))
    try:
        import engine
        values.add(str(engine.health_score(account).get("score")))
    except Exception:  # noqa: BLE001
        pass
    return values


def _rounded_claim_is_grounded(claim: str, evidence: set[str]) -> bool:
    """Allow ordinary prose rounding when it remains close to source evidence."""
    try:
        value = float(claim)
    except ValueError:
        return False
    if value < 100:
        return False
    for source in evidence:
        try:
            source_value = float(source)
        except ValueError:
            continue
        tolerance = max(5.0, abs(source_value) * 0.01)
        if abs(value - source_value) <= tolerance:
            return True
    return False


def _portfolio_evidence_numbers(queue: dict, accounts: dict) -> set[str]:
    """Numbers valid for portfolio-level claims, not individual accounts."""
    values = {
        str(len(accounts)),
        str(len(queue.get("tasks", []))),
        str(len(queue.get("suppressed", []))),
        str(sum(1 for task in queue.get("tasks", []) if task.get("priority") == 1)),
        str(queue.get("reviewed", len(accounts))),
    }
    return values


def validate_answer(answer: str, queue: dict, accounts: dict, queue_called: bool, question: str = "") -> list[str]:
    """Return blocking harness findings for a model answer."""
    findings = []
    judge = queue.get("judge", {})
    if not queue_called:
        findings.append("deterministic get_task_queue was not called")
    if judge.get("verdict") != "PASS":
        findings.append(f"queue judge verdict is {judge.get('verdict', 'UNKNOWN')}")
    by_name = {a.get("hubspot", {}).get("name"): a for a in accounts.values()}
    mentioned = [name for name in by_name if name and name in answer]
    queue_tasks = queue.get("tasks", [])
    portfolio_evidence = _portfolio_evidence_numbers(queue, accounts)
    if mentioned:
        scoped = set()
        for name in mentioned:
            scoped.update(_account_evidence_numbers(by_name[name],
                           [task for task in queue_tasks if task.get("account") == name]))
        evidence = scoped | portfolio_evidence
    else:
        # Portfolio answers may cite deterministic aggregates, but still need
        # to name at least one queued account when the queue has work.
        evidence = _evidence_numbers(queue, accounts) | portfolio_evidence
    unsupported = sorted(_answer_numbers(answer) - evidence - POLICY_NUMBERS - {str(i) for i in range(0, 13)})
    unsupported = [value for value in unsupported if not _rounded_claim_is_grounded(value, evidence)]
    if unsupported:
        findings.append("unsupported numeric claims: " + ", ".join(unsupported))
    check_status = not any(term in question.lower() for term in ("expansion", "expand", "upsell"))
    for name in mentioned if check_status else []:
        account = by_name[name]
        account_status = str(account.get("churn", {}).get("churn_status") or "").lower()
        lifecycle_stage = str(account.get("hubspot", {}).get("lifecycle_stage") or "").lower()
        is_churned = account_status == "churned" or lifecycle_stage in {"churned", "churned customer"}
        window = answer.lower()
        name_position = window.find(name.lower())
        local_text = window[name_position:name_position + 260] if name_position >= 0 else ""
        if "churned" in local_text and not is_churned:
            findings.append(f"unsupported churn status for account: {name}")
    if queue.get("tasks") and not mentioned:
        findings.append("answer does not reference any account from the deterministic queue")
    return findings


def _bedrock_tool_specs(tools):
    specs = []
    for name, (_fn, desc) in tools.items():
        props = {} if name in ("list_accounts", "get_portfolio_snapshot", "get_task_queue") else {
            "account_id": {"type": "string", "description": "AUx-yyyyy account id"}}
        required = [] if name in ("list_accounts", "get_portfolio_snapshot", "get_task_queue") else ["account_id"]
        specs.append({"toolSpec": {
            "name": name, "description": desc,
            "inputSchema": {"json": {"type": "object", "properties": props, "required": required}},
        }})
    # Delegation tool so the orchestrator can invoke sub-agents.
    specs.append({"toolSpec": {
        "name": "run_subagent",
        "description": "Delegate to a specialist sub-agent: risk-analyst, renewal-planner, "
                       "outreach-drafter, or cs-playbook-judge. Returns its result.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "agent": {"type": "string"}, "task": {"type": "string"}}, "required": ["agent", "task"]}},
    }})
    return specs


# --------------------------------------------------------------------------- #
# Bedrock Converse loop with tool-use
# --------------------------------------------------------------------------- #
def _client():
    import boto3
    session = boto3.Session(profile_name=PROFILE) if PROFILE else boto3.Session()
    return session.client("bedrock-runtime", region_name=REGION)


def _converse(client, system, messages, tool_specs):
    return client.converse(
        modelId=MODEL,
        system=[{"text": system}],
        messages=messages,
        toolConfig={"tools": tool_specs},
        inferenceConfig={"maxTokens": 2000, "temperature": 0},
    )


def _owner_account_answer(question: str, accounts: dict, queue: dict,
                          csm_owner: str | None = None) -> str | None:
    """Return a grounded owner directory or named-owner brief when explicitly requested."""
    question_lower = question.lower()
    owner_terms = ("owner", "csm", "my accounts", "account book", "portfolio book")
    scoped_terms = ("next", "task", "account", "portfolio", "book", "own")
    if not any(term in question_lower for term in owner_terms) \
            and not (csm_owner and any(term in question_lower for term in scoped_terms)):
        return None

    grouped: dict[str, list[dict]] = {}
    for account in accounts.values():
        hubspot = account.get("hubspot", {})
        owner = str(hubspot.get("csm_owner") or "Unassigned").strip()
        grouped.setdefault(owner, []).append(account)

    all_owner_terms = ("who are the owners", "portfolio owners", "all owners",
                       "list owners", "who are all portfolio owners")
    if any(term in question_lower for term in all_owner_terms):
        lines = []
        for owner in sorted(grouped, key=lambda value: (value == "Unassigned", value.casefold())):
            names = sorted(account.get("hubspot", {}).get("name") or "Unnamed account"
                           for account in grouped[owner])
            lines.append(f"- **{owner}**: {', '.join(names)}")
        return "**Portfolio owners and accounts**\n\n" + "\n".join(lines)

    selected_owner = next((owner for owner in grouped if owner != "Unassigned" and
                           ((csm_owner and owner.casefold() == csm_owner.casefold())
                            or owner.casefold() in question_lower)), None)
    if selected_owner and "own" in question_lower:
        mentioned_account = next((account for account in accounts.values()
                                  if (account.get("hubspot", {}).get("name") or "").casefold()
                                  in question_lower), None)
        if mentioned_account:
            hubspot = mentioned_account.get("hubspot", {})
            account_name = hubspot.get("name") or "that account"
            actual_owner = hubspot.get("csm_owner") or "Unassigned"
            if str(actual_owner).casefold() == selected_owner.casefold():
                return f"Yes. **{account_name}** is owned by **{selected_owner}**."
            return f"No. **{account_name}** is owned by **{actual_owner}**, not **{selected_owner}**."
    if selected_owner is None:
        owners = sorted(owner for owner in grouped if owner != "Unassigned")
        return "I do not have a CSM identity for this session yet. Available owners: " + ", ".join(owners) + "."

    tasks_by_account: dict[str, list[dict]] = {}
    for task in queue.get("tasks", []):
        tasks_by_account.setdefault(task.get("account"), []).append(task)
    lines = [f"**{selected_owner}'s accounts**"]
    owner_tasks = []
    for account in sorted(grouped[selected_owner], key=lambda item: item.get("hubspot", {}).get("name") or ""):
        hubspot = account.get("hubspot", {})
        name = hubspot.get("name") or "Unnamed account"
        try:
            import engine
            health = engine.health_score(account)
            health_text = f"{health.get('score')} ({health.get('band')})"
        except Exception:  # noqa: BLE001
            health_text = "unavailable"
        account_tasks = tasks_by_account.get(name, [])
        owner_tasks.extend(account_tasks)
        lines.append(
            f"- **{name}**: health {health_text}; lifecycle {hubspot.get('lifecycle_stage') or 'unknown'}; "
            f"ARR {hubspot.get('arr_usd') if hubspot.get('arr_usd') is not None else 'unavailable'}; "
            f"renewal {hubspot.get('renewal_date') or 'unavailable'}; open tasks {len(account_tasks)}."
        )
    owner_tasks.sort(key=lambda task: task.get("priority", 99))
    if owner_tasks:
        task = owner_tasks[0]
        lines.append(f"\n**Work next:** {task.get('account')} - {task.get('recommended_action')} "
                     f"(P{task.get('priority')}, due {task.get('due_on')}).")
    else:
        lines.append("\n**Work next:** No governed manual task is currently open for this book.")
    return "\n".join(lines)


def _conversation_answer(question: str, csm_owner: str | None = None) -> str | None:
    question_lower = question.lower()
    if any(term in question_lower for term in ("who are you", "what are you", "how are you")):
        answer = "I’m your AI Customer Success partner. I help you understand your account book, explain live signals, and identify the next governed action."
        if csm_owner:
            answer += f" I’ll use **{csm_owner}** as the current portfolio owner."
        return answer
    return None


# Vague openers that carry no concrete CS intent on their own.
_VAGUE = ("help", "hi", "hey", "hello", "what can you do", "what do you do",
          "anything", "something", "update me")
# Concrete CS intents that never need clarification.
_CONCRETE = ("action", "risk", "protect", "expand", "renewal", "churn", "expansion",
             "adopt", "onboard", "payment", "invoice", "health", "ticket", "top ",
             "priority", "who owns", "owner", "csm", "book", "brief", "next", "account",
             "today", "do ", "should", "focus", "attention", "urgent")


def _maybe_clarify(question: str, accounts: dict, account_id: str | None) -> str | None:
    """Offer one focused clarifying question when the ask is genuinely ambiguous.

    Deterministic and cheap (no model call). Only triggers when: the user is not
    looking at a specific account, names no account, expresses no concrete intent,
    and there is a real book to narrow. This makes the agent feel attentive without
    guessing what the user meant."""
    q = question.strip().lower().rstrip("?").strip()
    if account_id or not accounts:
        return None
    names = [a.get("hubspot", {}).get("name") for a in accounts.values() if a.get("hubspot", {}).get("name")]
    if any(n and n.lower() in q for n in names):
        return None  # a specific account is named
    if any(term in q for term in _CONCRETE):
        return None  # a concrete intent is present
    if q in _VAGUE or len(q) <= 12:
        return ("Happy to help. To point you at the right thing, which would you like:\n"
                "- **Top actions today** across your accounts, ranked by priority;\n"
                "- **Risk** — accounts that need protecting first;\n"
                "- **Expansion** — where the growth signals are; or\n"
                "- A **single account** — name it and I'll give you the full picture.")
    return None


def _run_agent(client, agent, user_task, tools, tool_specs, transcript, depth=0, history=None):
    """Run one agent to completion (resolving its tool calls). Returns final text.

    `history` is an optional list of prior {role, text} turns that seed the
    conversation so follow-up questions ('and the second one?') have context."""
    accounts, tool_fns = tools[:2]
    messages = []
    for turn in (history or []):
        role = "assistant" if turn.get("role") == "assistant" else "user"
        text = str(turn.get("content") or turn.get("text") or "").strip()
        if text:
            messages.append({"role": role, "content": [{"text": text}]})
    messages.append({"role": "user", "content": [{"text": user_task}]})
    for _turn in range(MAX_TURNS):
        resp = _converse(client, agent["system"], messages, tool_specs)
        out = resp["output"]["message"]
        messages.append(out)
        stop = resp.get("stopReason")
        tool_uses = [c["toolUse"] for c in out.get("content", []) if "toolUse" in c]
        text = " ".join(c["text"] for c in out.get("content", []) if "text" in c).strip()
        if text:
            transcript.append({"agent": agent["name"], "say": text})
        if stop != "tool_use":
            return text
        # Resolve each tool call.
        results = []
        for tu in tool_uses:
            nm, inp = tu["name"], tu.get("input", {})
            if nm == "run_subagent" and depth < 3:
                sub = inp.get("agent", "").strip()
                transcript.append({"delegate": f"{agent['name']} -> {sub}", "task": inp.get("task", "")[:200]})
                try:
                    sub_agent = load_agent(sub)
                    sub_out = _run_agent(client, sub_agent, inp.get("task", ""), tools, tool_specs, transcript, depth + 1)
                    payload = {"result": sub_out}
                except Exception as e:  # noqa: BLE001
                    payload = {"error": f"{type(e).__name__}: {e}"}
            elif nm in tool_fns:
                try:
                    payload = {"data": tool_fns[nm][0](inp)}
                except Exception as e:  # noqa: BLE001
                    payload = {"error": f"{type(e).__name__}: {e}"}
            else:
                payload = {"error": f"unknown tool {nm}"}
            results.append({"toolResult": {"toolUseId": tu["toolUseId"],
                                            "content": [{"json": payload}]}})
        messages.append({"role": "user", "content": results})
    return "(max turns reached)"


def run(question: str, account_id: str | None = None,
    csm_owner: str | None = None, principal: dict | None = None,
    history: list | None = None) -> dict:
    """Execute the cs-orchestrator agent on a question. Returns {ok, answer, transcript}.

    The authenticated principal scopes what the agent can see: admins get the whole
    portfolio; CSMs are limited to the accounts they own, matching the platform's
    engine scoping so the agent and dashboard never disagree.
    """
    role = _principal_role(principal)
    # Derive the owner name from the session when the caller is a CSM (so the agent
    # can answer "my accounts" without the client having to pass csm_owner).
    if role == "csm" and not csm_owner:
        csm_owner = (principal or {}).get("name") or (principal or {}).get("email")
    conversation_answer = _conversation_answer(question, csm_owner)
    if conversation_answer is not None:
        return {"ok": True, "answer": conversation_answer, "transcript": [],
                "accounts_available": 0, "actions": [],
                "harness": {"route": "conversation_identity"}}
    try:
        import boto3  # noqa: F401
    except Exception:
        return {"ok": False, "error": "boto3 not installed"}
    try:
        client = _client()
        # Fail fast with a clear message if not authenticated.
        client.meta.region_name  # noqa: B018
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Bedrock client init failed: {e}"}

    # Load accounts and scope to the caller's book if they are a CSM. Admins keep the
    # original path (tools load the full roster) so the tool layer stays mockable.
    if role == "csm":
        import dataaccess
        all_accounts = dataaccess.all_accounts()
        scoped_accounts, owner_name = _scope_accounts(all_accounts, principal)
        if owner_name:
            csm_owner = owner_name
        tools = _tools_impl(accounts_override=scoped_accounts)
        total_count = len(all_accounts)
    else:
        tools = _tools_impl()
        scoped_accounts = tools[0]
        total_count = len(scoped_accounts)
    identity_line = _identity_line(principal, len(scoped_accounts), total_count)

    tool_specs = _bedrock_tool_specs(tools[1])
    orchestrator = load_agent("cs-orchestrator")
    transcript: list[dict] = []
    accounts = tools[0]

    # Clarifying question for genuinely ambiguous asks (deterministic, no model call):
    # if the user names no account, asks nothing specific, and there is a real book to
    # narrow, offer one focused question rather than guessing.
    clarify = _maybe_clarify(question, accounts, account_id)
    if clarify is not None:
        return {"ok": True, "answer": identity_line + "\n\n" + clarify, "transcript": [],
                "accounts_available": len(accounts), "actions": [],
                "harness": {"route": "clarifying_question"}}

    owner_answer = _owner_account_answer(question, accounts, tools[3], csm_owner=csm_owner)
    if owner_answer is not None:
        return {"ok": True, "answer": identity_line + "\n\n" + owner_answer, "transcript": transcript,
                "model": MODEL, "region": REGION, "profile": PROFILE,
                "accounts_available": len(accounts),
                "actions": _structured_actions(tools[3], accounts),
                "harness": {"queue_judge": tools[3].get("judge", {}),
                            "route": "deterministic_owner_brief"}}
    roster_lines = []
    for aid, a in accounts.items():
        hs = a.get("hubspot", {})
        roster_lines.append(f"- {aid}: {hs.get('name')} | segment={hs.get('segment_label') or hs.get('segment')} | ARR={hs.get('arr_usd')}")
    account_guidance = ""
    if account_id:
        selected = accounts.get(account_id) or accounts.get(account_id.upper()) or accounts.get(account_id.lower())
        if selected:
            selected_name = selected.get("hubspot", {}).get("name") or account_id
            account_guidance = (
                f"\n[Selected account context] The user is viewing account {account_id} ({selected_name}). "
                "For questions referring to 'this account', answer only about this account. "
                "Use its own tool evidence; do not infer identity from demo documentation.\n"
            )
    intent_guidance = ""
    question_lower = question.lower()
    if any(term in question_lower for term in ("expansion", "expand", "upsell")):
        intent_guidance = ("\n[Expansion question] Answer only with eligible healthy Strategic expansion or renewal signals. "
                           "Do not discuss unrelated churn statuses or recovery accounts unless directly asked. "
                           "If there are no eligible signals, say so plainly and name the missing data gate.\n")
    if role == "csm":
        scope_guidance = ("\n[Scope] You are answering for a single CSM. The portfolio below is ONLY the "
                          f"accounts they own ({len(accounts)} in total). Never reference or imply accounts "
                          "outside this book, and never suggest there are more accounts you cannot see.\n")
    else:
        scope_guidance = ("\n[Scope] You are answering for an Admin with full-portfolio visibility across "
                          f"every CSM book ({len(accounts)} accounts). You may compare across owners.\n")
    primed = (question.strip() + account_guidance + intent_guidance + scope_guidance +
              "\n\n[Portfolio in scope — already fetched, use tools only for deeper per-account signals]\n" +
              "\n".join(roster_lines) +
              "\n\n[Harness requirement] Call get_task_queue before making portfolio recommendations. Treat its evidence and judge verdict as authoritative; do not invent figures.\n" +
              "\n[Response style] Speak like a thoughtful senior CS partner, not a system log. For top-actions questions, lead with the requested actions and keep the answer concise (roughly 150-250 words). Use natural prose or a short numbered list, not a repeated full queue table. For each action, give the account, what to do, why it matters, and the exact SLA from its priority: P1 means within 24 hours, P2 means today, P3 means this week, P4 means before the renewal milestone, and P5 means this week. Distinguish active risk from churned-account recovery and contact hygiene. Do not say all Protect actions have a 24-hour SLA. Do not mention tool calls, harness checks, judge verdicts, correction attempts, raw source JSON, null values, or internal implementation terms. Mention data gaps only when they change the recommendation, in one short closing note. Do not repeat the structured action packet because the UI already displays it.\n"+
              "[Evidence rule] Do not generalize categorical facts such as Churned status across accounts. Say an account is Churned only when that account's own Redshift evidence says Churned. "
              "Do not claim that an external dunning, suspension, write-back, or outreach action has executed unless the tool result explicitly confirms execution; describe a handoff as a handoff.")
    try:
        answer = ""
        findings = []
        for attempt in range(MAX_CORRECTIONS + 1):
            if attempt:
                correction = ("Your previous answer failed harness validation:\n- " +
                              "\n- ".join(findings) +
                              "\nRegenerate using only the deterministic queue evidence. Call get_task_queue first. "
                              "Do not mention internal policy thresholds or rule constants; explain the customer action in plain language.")
                task = primed + "\n\n" + correction
            else:
                task = primed
            answer = _run_agent(client, orchestrator, task, tools, tool_specs, transcript, history=history)
            answer = _clean_agent_text(answer)
            queue_called = tools[2]["queue_called"]
            findings = validate_answer(answer, tools[3], accounts, queue_called, question)
            transcript.append({"harness": "answer_validation", "attempt": attempt + 1,
                               "findings": findings})
            if not findings:
                break
        if findings:
            return {"ok": False, "error": "Agent answer blocked by harness after two corrections: " +
                    "; ".join(findings), "transcript": transcript,
                    "harness": {"corrections": MAX_CORRECTIONS, "findings": findings}}
        return {"ok": True, "answer": identity_line + "\n\n" + answer, "transcript": transcript,
                "model": MODEL, "region": REGION, "profile": PROFILE,
            "accounts_available": len(tools[0]),
                "actions": _structured_actions(tools[3], tools[0]),
                "harness": {"queue_judge": tools[3].get("judge", {}),
                    "live_sources": sorted({value.get("_source") for account in tools[0].values() for value in account.values() if isinstance(value, dict) and value.get("_source")})}}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if any(k in msg for k in ("ExpiredToken", "InvalidGrant", "NoCredentials",
                                  "UnrecognizedClient", "sso", "SSO", "token has expired",
                                  "could not be found", "AccessDenied")):
            profile_hint = (f"local dev: run aws sso login and set CS_BEDROCK_PROFILE, "
                            if PROFILE else "the ECS task role needs bedrock:InvokeModel, ")
            return {"ok": False, "error": "Bedrock not available. " + profile_hint +
                    f"region {REGION}, model {MODEL}.",
                    "transcript": transcript}
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "transcript": transcript}


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What are my top 3 CS actions today and why?"
    result = run(q)
    print(json.dumps(result, indent=2, default=str))
