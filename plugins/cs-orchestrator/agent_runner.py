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
                f"{scoped_count} account{'s' if scoped_count != 1 else ''}, so everything below is "
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
    """Keep model prose natural and consistent with the UI style.

    Remove em/en dashes entirely (the user does not want them). A dash used as a
    parenthetical becomes a comma; keep the sentence readable."""
    out = text.replace(" — ", ", ").replace(" – ", ", ")
    out = out.replace("—", ", ").replace("–", ", ")
    # Collapse any accidental double punctuation from the substitution.
    out = out.replace(", ,", ",").replace(",,", ",").replace(" ,", ",")
    return out


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


def _actions_for_answer(all_actions: list[dict], answer: str, accounts: dict,
                        account_id: str | None = None, action_intent: bool = False) -> list[dict]:
    """Return only the evidence relevant to THIS answer, not the whole queue.

    - For a specific account in focus: that account's actions.
    - For an ACTION question (what to do / priorities / risk): the accounts named in the
      answer that actually have queued actions.
    - For everything else (list, roster, ARR, health summary, knowledge): NO task-queue
      evidence, because those answers are not action recommendations and attaching the
      queue made the evidence look identical and unrelated.
    """
    if account_id:
        focus_name = (accounts.get(account_id, {}).get("hubspot", {}) or {}).get("name")
        return [a for a in all_actions if a.get("account") == focus_name]
    if not action_intent:
        return []
    low = answer.lower()
    negative = any(p in low for p in ("no expansion", "there are no", "no eligible",
                                      "nothing ", "no active", "no signals", "none of"))
    if negative:
        return []
    def _named(name):
        if not name: return False
        if name in answer: return True
        stop={"group","limited","ltd","inc","llc","pty","the","co","company","solutions",
              "recruitment","services","international","australia"}
        toks=[t for t in re.findall(r"[A-Za-z0-9]+", name.lower()) if t not in stop and len(t)>2][:2]
        return bool(toks) and all(t in low for t in toks)
    return [a for a in all_actions if _named(a.get("account"))]


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
    """Numbers valid for portfolio-level claims, not individual accounts.

    Includes ARR aggregates (per-account, portfolio total, by-segment, by-state) and their
    percentages so the agent can legitimately answer 'ARR by state/segment/total' questions
    without the harness flagging real, derivable figures as unsupported."""
    values = {
        str(len(accounts)),
        str(len(queue.get("tasks", []))),
        str(len(queue.get("suppressed", []))),
        str(sum(1 for task in queue.get("tasks", []) if task.get("priority") == 1)),
        str(queue.get("reviewed", len(accounts))),
    }

    def _norm(n):
        try:
            f = float(n)
        except (TypeError, ValueError):
            return
        values.add(str(int(f)) if f == int(f) else str(f))

    # Per-account ARR + running aggregates.
    try:
        import engine as _eng
        _norm_state = _eng._normalise_state
    except Exception:  # noqa: BLE001
        _norm_state = lambda s: str(s or "").strip().upper() or "Unknown"
    total_arr = 0.0
    by_seg: dict[str, float] = {}
    by_state: dict[str, float] = {}
    seg_counts: dict[str, int] = {}
    for a in accounts.values():
        hs = a.get("hubspot", {}) if isinstance(a.get("hubspot"), dict) else a
        arr = hs.get("arr_usd")
        if isinstance(arr, (int, float)):
            total_arr += arr
            seg = hs.get("segment_label") or hs.get("segment") or "Unsegmented"
            by_seg[seg] = by_seg.get(seg, 0) + arr
            st = _norm_state(hs.get("state"))
            by_state[st] = by_state.get(st, 0) + arr
        seg = hs.get("segment_label") or hs.get("segment") or "Unsegmented"
        seg_counts[seg] = seg_counts.get(seg, 0) + 1
    _norm(total_arr)
    for v in list(by_seg.values()) + list(by_state.values()):
        _norm(v)
    for c in seg_counts.values():
        _norm(c)
    # Percentages of total (0-100) for share-of-ARR statements.
    if total_arr:
        for v in list(by_seg.values()) + list(by_state.values()):
            _norm(round(v / total_arr * 100))
    # Counts 0..len(accounts) are always safe to cite.
    for i in range(0, len(accounts) + 1):
        values.add(str(i))
    # Retention figures (GRR, target, churned ARR) are legitimate portfolio facts.
    try:
        import engine as _eng
        ret = _eng._retention_metrics(accounts, {}) if hasattr(_eng, "_retention_metrics") else {}
        for k in ("grr_pct", "base_arr_usd", "churned_arr_usd", "expansion_pipeline_arr_usd"):
            _norm(ret.get(k))
        _norm((ret.get("target") or {}).get("grr_pct"))
    except Exception:  # noqa: BLE001
        pass
    return values


def validate_answer(answer: str, queue: dict, accounts: dict, queue_called: bool,
                    question: str = "", account_focus: bool = False) -> list[str]:
    """Return blocking harness findings for a model answer.

    `account_focus` is True when the question is about a single named account (or one
    is in view). Such questions are answered from that account's own tool evidence and
    do not require the portfolio task queue, so we do not demand get_task_queue for them.
    """
    findings = []
    judge = queue.get("judge", {})
    # The deterministic queue is required only for portfolio-level / recommendation
    # answers. A single-account explainer is grounded by that account's own tools.
    if not account_focus:
        if not queue_called:
            findings.append("deterministic get_task_queue was not called")
        if judge.get("verdict") != "PASS":
            findings.append(f"queue judge verdict is {judge.get('verdict', 'UNKNOWN')}")
    by_name = {a.get("hubspot", {}).get("name"): a for a in accounts.values()}
    answer_lower = answer.lower()
    def _is_mentioned(name):
        if name in answer:
            return True
        # Also match when the model uses a shortened form (e.g. "Salt Solutions" for
        # "Salt Solutions Group Limited"). Use the distinctive leading tokens, ignoring
        # common suffixes, so per-account numeric evidence is still credited.
        stop = {"group", "limited", "ltd", "inc", "llc", "pty", "the", "co", "company",
                "solutions", "recruitment", "services", "international", "australia"}
        tokens = [t for t in re.findall(r"[A-Za-z0-9]+", name.lower()) if t not in stop and len(t) > 2]
        head = tokens[:2]
        return bool(head) and all(t in answer_lower for t in head)
    mentioned = [name for name in by_name if name and _is_mentioned(name)]
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
    # Aggregate/analytical questions (ARR by state/segment, totals, distributions) let the
    # model legitimately SUM and ABBREVIATE real ARR figures (e.g. $348k, $7.6M). Allow any
    # remaining number that, at face value or scaled by 1e3/1e6, equals a sum of known ARR
    # values (within tolerance), so real arithmetic/abbreviation is not flagged as invented.
    aggregate_q = any(t in question.lower() for t in
                      ("arr", "revenue", "total", "by state", "by segment", "by region",
                       "distribution", "breakdown", "how much", "portfolio value", "combined",
                       "state of the portfolio", "worry", "health", "overall"))
    if unsupported and aggregate_q:
        arr_vals = []
        for a in accounts.values():
            v = (a.get("hubspot", {}) or {}).get("arr_usd")
            if isinstance(v, (int, float)) and v:
                arr_vals.append(float(v))
        total_all = sum(arr_vals)
        def _matches_arr(t):
            # Subset-sum with generous tolerance for a single scaled value.
            tol = max(500.0, t * 0.03)
            if any(abs(t - v) <= max(500.0, v * 0.03) for v in arr_vals):
                return True
            if abs(t - total_all) <= max(1000.0, total_all * 0.03):
                return True
            remaining = t
            for v in sorted(arr_vals, reverse=True):
                if v <= remaining + tol:
                    remaining -= v
                if abs(remaining) <= tol:
                    return True
            return abs(remaining) <= tol
        def _is_sum_of_arr(target):
            try:
                t = float(target)
            except ValueError:
                return False
            # Try face value and abbreviated scales ($348k -> 348, $7.6M -> 7.6).
            for scale in (1.0, 1_000.0, 1_000_000.0):
                if _matches_arr(t * scale):
                    return True
            return False
        unsupported = [v for v in unsupported if not _is_sum_of_arr(v)]
    if unsupported:
        findings.append("unsupported numeric claims: " + ", ".join(unsupported))
    # Only guard against fabricating a churned status when the question is about
    # accounts/risk, not aggregate ARR/revenue questions where 'churn' appears in general
    # commentary. And require the word to sit tightly next to the account name.
    check_status = not any(term in question.lower() for term in
                           ("expansion", "expand", "upsell", "arr", "revenue", "total",
                            "segment", "state", "region", "distribution", "how much"))
    for name in mentioned if check_status else []:
        account = by_name[name]
        account_status = str(account.get("churn", {}).get("churn_status") or "").lower()
        lifecycle_stage = str(account.get("hubspot", {}).get("lifecycle_stage") or "").lower()
        is_churned = account_status == "churned" or lifecycle_stage in {"churned", "churned customer"}
        window = answer.lower()
        name_position = window.find(name.lower())
        # Tight window right after the name: catch '<Account> is (also) churned' style
        # direct assertions, not any distant mention.
        local_text = window[name_position:name_position + 90] if name_position >= 0 else ""
        directly_called_churned = bool(re.search(r"\bis\b[^.]{0,20}\bchurned\b", local_text)
                                       or "has churned" in local_text
                                       or "churned customer" in local_text
                                       or ", churned" in local_text)
        if directly_called_churned and not is_churned:
            findings.append(f"unsupported churn status for account: {name}")
    if queue.get("tasks") and not mentioned and not account_focus:
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
    # Only handle EXPLICIT directory / ownership-lookup questions here. Anything
    # analytical (most critical, why, compare, risk, next action) must go to the real
    # agent, which reasons over evidence. A brittle keyword match must never hijack a
    # smart question just because it contains the word "owner".
    directory_intent = any(t in question_lower for t in (
        "who are the owners", "who are all", "list owners", "list the owners",
        "portfolio owners", "all owners", "all the owners", "who owns what",
        "owners and accounts", "show owners", "who are our owners"))
    my_book_intent = bool(csm_owner) and any(t in question_lower for t in (
        "my accounts", "my book", "my portfolio", "what should i work",
        "my next", "my tasks", "accounts do i", "do i own"))
    named_owner_intent = any(
        (str(a.get("hubspot", {}).get("csm_owner") or "").strip().casefold() in question_lower)
        for a in accounts.values() if a.get("hubspot", {}).get("csm_owner"))
    # Analytical questions (most/critical/why/risk/compare/priority) are NOT owner lookups.
    analytical = any(t in question_lower for t in (
        "critical", "most", "why", "risk", "compare", "priorit", "urgent", "worst",
        "fastest", "faster", "act ", "action", "churn", "health", "expansion", "renew"))
    if analytical and not directory_intent:
        return None
    if not (directory_intent or my_book_intent or named_owner_intent):
        return None

    grouped: dict[str, list[dict]] = {}
    for account in accounts.values():
        hubspot = account.get("hubspot", {})
        owner = str(hubspot.get("csm_owner") or "Unassigned").strip()
        grouped.setdefault(owner, []).append(account)

    all_owner_terms = ("who are the owners", "portfolio owners", "all owners",
                       "list owners", "who are all portfolio owners", "all the owners",
                       "who owns what", "owners and accounts", "show owners")
    if any(term in question_lower for term in all_owner_terms):
        assigned = {o: v for o, v in grouped.items() if o != "Unassigned"}
        unassigned = grouped.get("Unassigned", [])
        lines = []
        for owner in sorted(assigned, key=lambda value: value.casefold()):
            names = sorted(account.get("hubspot", {}).get("name") or "Unnamed account"
                           for account in grouped[owner])
            n = len(names)
            lines.append(f"- {owner} looks after {n} account{'s' if n != 1 else ''}: {', '.join(names)}.")
        intro = (f"There are {len(assigned)} CSMs with named accounts across the portfolio. "
                 "Here is who owns what:")
        body = "\n".join(lines)
        tail = ""
        if unassigned:
            un = sorted(a.get("hubspot", {}).get("name") or "Unnamed account" for a in unassigned)
            tail = (f"\n\n{len(un)} account{'s are' if len(un) != 1 else ' is'} currently unassigned: "
                    f"{', '.join(un)}. These are worth routing to an owner, since no one is actively "
                    "watching them today.")
        follow = ("\n\nWould you like me to show which of these owners is carrying the most risk right "
                  "now, or dig into any single owner's book?")
        return intro + "\n\n" + body + tail + follow

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
        # No specific owner matched: let the real agent answer rather than emitting a
        # confusing 'no CSM identity' message.
        return None

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
    q_stripped = question_lower.strip().rstrip("!.").strip()
    # Short acknowledgements / social replies get a brief, human response, never the menu.
    ACKS = {"ok", "okay", "k", "thanks", "thank you", "ty", "cool", "great", "nice",
            "got it", "sure", "yes", "yep", "no", "nope", "cheers", "perfect", "awesome"}
    if q_stripped in ACKS:
        return "Anytime. Just tell me what you'd like to look at next and I'll dig in."
    if any(term in question_lower for term in ("who are you", "what are you", "how are you")):
        answer = "I'm Jane, your Customer Success specialist. I read the live signals across your accounts, tell you what they mean, and point you to the smartest next move, always grounded in real evidence and your signed Ways of Working."
        if csm_owner:
            answer += f" I'm working from **{csm_owner}**'s book."
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
        return ("Happy to help. What would be most useful right now? I can walk you through "
                "your top actions for today ranked by priority, show you which accounts need "
                "protecting first, point out where the expansion signals are, or dig into a "
                "single account if you name it. Which one would you like to start with?")
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
    # Jane answers everything herself via the model, grounded in the live evidence and
    # portfolio knowledge we pass in. No canned/templated replies.
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

    roster_lines = []
    try:
        import engine as _engine
    except Exception:  # noqa: BLE001
        _engine = None
    for aid, a in accounts.items():
        hs = a.get("hubspot", {})
        health = ""
        if _engine is not None:
            try:
                h = _engine.health_score(a)
                if h.get("computable"):
                    health = f" | health={h.get('score')}({h.get('band')})"
            except Exception:  # noqa: BLE001
                pass
        churn = a.get("churn", {}) or {}
        cstat = churn.get("churn_status")
        lifecycle = hs.get("lifecycle_stage")
        flags = ""
        if cstat:
            flags += f" | churn_status={cstat}"
        if lifecycle:
            flags += f" | lifecycle={lifecycle}"
        # Health trajectory so Jane can explain 'why did health change' and flag declines.
        if _engine is not None:
            try:
                tr = _engine.trend_risks(aid)
                if tr:
                    flags += " | trend=" + tr[0]["title"]
            except Exception:  # noqa: BLE001
                pass
            try:
                ad = _engine.adoption_score(a)
                if ad.get("computable"):
                    flags += f" | adoption={ad.get('score')}({ad.get('band')})"
            except Exception:  # noqa: BLE001
                pass
        roster_lines.append(
            f"- {aid}: {hs.get('name')} | segment={hs.get('segment_label') or hs.get('segment')} | "
            f"ARR={hs.get('arr_usd')} | state={hs.get('state') or 'unknown'} | "
            f"owner={hs.get('csm_owner') or 'Unassigned'}{health}{flags}")
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
    # A compact portfolio-knowledge block so Jane can answer aggregate questions (totals,
    # health mix, retention, ARR distribution) directly and accurately, scoped to what she
    # can see. Built from the same live data; nothing invented.
    knowledge = ""
    try:
        import engine as _eng2
        total_arr = sum((a.get("hubspot", {}).get("arr_usd") or 0) for a in accounts.values()
                        if isinstance(a.get("hubspot", {}).get("arr_usd"), (int, float)))
        bands = {"green": 0, "amber": 0, "red": 0}
        seg: dict = {}
        state: dict = {}
        churned = 0
        for a in accounts.values():
            try:
                b = _eng2.health_score(a).get("band")
                if b in bands:
                    bands[b] += 1
            except Exception:  # noqa: BLE001
                pass
            hs = a.get("hubspot", {})
            arr = hs.get("arr_usd") or 0
            s = hs.get("segment_label") or hs.get("segment") or "Unsegmented"
            seg[s] = seg.get(s, 0) + (arr if isinstance(arr, (int, float)) else 0)
            st = _eng2._normalise_state(hs.get("state"))
            state[st] = state.get(st, 0) + (arr if isinstance(arr, (int, float)) else 0)
            lc = str(hs.get("lifecycle_stage") or "").lower()
            if lc in {"churned", "churned customer"}:
                churned += 1
        ret = _eng2._retention_metrics(accounts, {}) if hasattr(_eng2, "_retention_metrics") else {}
        seg_str = ", ".join(f"{k} ${int(v):,}" for k, v in sorted(seg.items(), key=lambda x: -x[1]) if v)
        state_str = ", ".join(f"{k} ${int(v):,}" for k, v in sorted(state.items(), key=lambda x: -x[1]) if v)
        # Top computed expansion candidates (upsell readiness), so Jane can answer growth
        # questions with real, scored candidates instead of 'none'.
        exp_str = ""
        try:
            from statistics import median as _median
            seg_arr = {}
            for a in accounts.values():
                hs2 = a.get("hubspot", {}); v = hs2.get("arr_usd")
                s2 = hs2.get("segment_label") or hs2.get("segment") or "Unsegmented"
                if isinstance(v, (int, float)):
                    seg_arr.setdefault(s2, []).append(v)
            seg_med = {s: _median(v) for s, v in seg_arr.items() if v}
            cands = []
            for a in accounts.values():
                hs2 = a.get("hubspot", {})
                s2 = hs2.get("segment_label") or hs2.get("segment") or "Unsegmented"
                es = _eng2.expansion_score(a, segment_median_arr=seg_med.get(s2))
                if es.get("computable") and es.get("score", 0) >= 35 and es.get("drivers"):
                    cands.append((es["score"], hs2.get("name") or "Unnamed", es["drivers"]))
            cands.sort(key=lambda c: -c[0])
            if cands:
                exp_str = "Top expansion candidates (computed readiness): " + "; ".join(
                    f"{n} (score {sc}: {', '.join(dr[:2])})" for sc, n, dr in cands[:5]) + ".\n"
        except Exception:  # noqa: BLE001
            exp_str = ""
        knowledge = (
            "\n[Portfolio knowledge — use these aggregates directly, they are correct]\n"
            f"Total accounts in scope: {len(accounts)}. Total ARR: ${int(total_arr):,}. "
            f"Churned (lifecycle): {churned}. Health mix: {bands['green']} healthy, {bands['amber']} watch, {bands['red']} at risk.\n"
            f"ARR by segment: {seg_str or 'n/a'}.\n"
            f"ARR by state: {state_str or 'n/a'}.\n"
            + exp_str
            + (f"Gross revenue retention: {ret.get('grr_pct')}% vs target {(ret.get('target') or {}).get('grr_pct')}%; "
               f"churned ARR ${int(ret.get('churned_arr_usd') or 0):,}.\n" if ret and ret.get("computable") else "")
        )
    except Exception:  # noqa: BLE001
        knowledge = ""
    # Is this an ACTION question (what to do) or a KNOWLEDGE question (facts/analysis)?
    q_low = question.lower()
    action_intent = any(t in q_low for t in
                        ("action", "do today", "should i", "priorit", "next", "focus on",
                         "protect", "who needs", "what needs", "recommend", "handle", "work on"))
    if action_intent:
        queue_guidance = ("\n\n[Answer guidance] This is an action question. Lead with the specific "
                          "recommended actions from the deterministic queue (call get_task_queue), each "
                          "with the account, what to do, why, and its SLA.\n")
    else:
        queue_guidance = ("\n\n[Answer guidance] This is an analysis/knowledge question, NOT a to-do "
                          "request. Answer it directly and specifically using the Portfolio knowledge and "
                          "roster above (and per-account tools if needed). Do NOT default to listing the "
                          "task queue or the same protect actions, answer exactly what was asked with the "
                          "figures and accounts relevant to THIS question.\n")
    persona = (
        "[You are Jane] You are Jane, a fantastic senior Customer Success specialist: sharp, "
        "warm, and genuinely helpful. You think like a seasoned CSM who has saved big accounts "
        "and grown others. You do not just report data, you interpret it: connect the signals "
        "(churn score, product usage, support, payments, sentiment, renewal timing) into a clear "
        "story, say what it means, and recommend the smartest next move. You are proactive: if you "
        "spot something the user did not ask about but should know, mention it briefly. You are "
        "precise with facts (every number is grounded in the live evidence provided) but you speak "
        "like a trusted colleague, not a dashboard. Be concise and specific; never pad.\n\n"
    )
    primed = (persona + question.strip() + account_guidance + intent_guidance + scope_guidance + knowledge + queue_guidance +
              "\n\n[Portfolio in scope — already fetched, use tools only for deeper per-account signals]\n" +
              "\n".join(roster_lines) +
              "\n\n[Harness] The deterministic queue and its judge are authoritative for recommendations; do not invent figures.\n" +
              "\n[Response style] Speak like a thoughtful senior CS partner, not a system log. For top-actions questions, lead with the requested actions and keep the answer concise (roughly 150-250 words). Use natural prose or a short numbered list, not a repeated full queue table. For each action, give the account, what to do, why it matters, and the exact SLA from its priority: P1 means within 24 hours, P2 means today, P3 means this week, P4 means before the renewal milestone, and P5 means this week. Distinguish active risk from churned-account recovery and contact hygiene. Do not say all Protect actions have a 24-hour SLA. Do not mention tool calls, harness checks, judge verdicts, correction attempts, raw source JSON, null values, or internal implementation terms. Mention data gaps only when they change the recommendation, in one short closing note. Do not repeat the structured action packet because the UI already displays it.\n"+
              "[Evidence rule] Do not generalize categorical facts such as Churned status across accounts. Say an account is Churned only when that account's own Redshift evidence says Churned. "
              "Do not claim that an external dunning, suspension, write-back, or outreach action has executed unless the tool result explicitly confirms execution; describe a handoff as a handoff.\n"
              "[Voice] Write like a warm, sharp CS colleague talking to another person: natural sentences, plain English, no jargon dumps. NEVER use an em dash or en dash (— or –); use a comma or a full stop instead. Do not answer with a bare bullet list of facts when a sentence would read better. When you mention who owns an account, name the actual owner from the roster (the owner= field), never a vague phrase like 'the Strategic CSM book'. Always close with one short, specific follow-up question that offers a sensible next step (for example, offering to draft an outreach, open an account, or compare owners), so it feels like a real conversation.")
    try:
        # Single-account questions are grounded by that account's own tools and do not
        # require the portfolio queue. Detect focus from an explicit account_id or a
        # named account in the question.
        q_lower = question.lower()
        named_account = any((a.get("hubspot", {}).get("name") or "").lower() in q_lower
                            for a in accounts.values() if a.get("hubspot", {}).get("name"))
        # Comparisons and named-account questions are account-focused: grounded by those
        # accounts' own signals, not the portfolio queue.
        is_comparison = any(t in q_lower for t in ("compare", " vs ", "versus", "which is worse",
                                                   "which is better", "difference between"))
        # Knowledge/analysis questions (retention, health summary, ARR, distribution) are
        # answered from portfolio knowledge, not the task queue, so they are exempt from the
        # queue requirement just like account-focused questions.
        knowledge_intent = (not action_intent) and any(t in q_low for t in
            ("retention", "grr", "ndr", "health", "arr", "revenue", "segment", "state",
             "region", "distribution", "overall", "summary", "how many", "total",
             "who owns", "owner", "expansion", "churn"))
        account_focus = bool(account_id) or named_account or is_comparison or knowledge_intent
        # Greetings, identity and other social/non-portfolio messages are answered
        # conversationally and must not require the task queue.
        ql_s = q_low.strip()
        social = (len(ql_s) <= 40 and any(t in ql_s for t in
                  ("hi", "hello", "hey", "thanks", "thank", "who are you", "what are you",
                   "how are you", "your name", "good morning", "good afternoon",
                   "good evening", "help", "ok", "cool", "great", "nice"))) or not accounts
        if social:
            account_focus = True
        answer = ""
        findings = []
        for attempt in range(MAX_CORRECTIONS + 1):
            if attempt:
                if account_focus:
                    correction = ("Your previous answer failed validation:\n- " +
                                  "\n- ".join(findings) +
                                  "\nUse only that account's own tool evidence (hubspot_get_account, "
                                  "usage_get_metrics, zendesk_get_tickets, churn_get_score, stripe_get_payment). "
                                  "Do not invent figures. Write naturally, like a CS colleague.")
                else:
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
            findings = validate_answer(answer, tools[3], accounts, queue_called, question,
                                       account_focus=account_focus)
            transcript.append({"harness": "answer_validation", "attempt": attempt + 1,
                               "findings": findings})
            if not findings:
                break
        if findings:
            return {"ok": False, "error": "Agent answer blocked by harness after two corrections: " +
                    "; ".join(findings), "transcript": transcript,
                    "harness": {"corrections": MAX_CORRECTIONS, "findings": findings}}
        return {"ok": True, "answer": answer, "scope": identity_line, "transcript": transcript,
                "model": MODEL, "region": REGION, "profile": PROFILE,
            "accounts_available": len(tools[0]),
                "actions": _actions_for_answer(_structured_actions(tools[3], tools[0]), answer, tools[0], account_id, action_intent=action_intent),
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
