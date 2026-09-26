#!/usr/bin/env python3
"""CS Platform, engine layer.

Wraps the WoW orchestration (orchestrate.py) and adds the account **health score**
and portfolio roll-up the single-pane-of-glass UI needs. Kept dependency-free and
importable so both the API and the CLI/agent use the same logic (single source of
truth, WoW §1).
"""

from __future__ import annotations

import os
import json
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator"
sys.path.insert(0, str(Path(__file__).resolve().parent))  # platform/ (for dataaccess)
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "hooks" / "scripts"))

import orchestrate  # noqa: E402  (reference rules engine)
from suppression import primary_instance_ids, suppressed_signals  # noqa: E402

# Route the rules engine and the platform through the live data-access layer
# (live per-source with fixture fallback). One source of truth for UI + agent.
import dataaccess  # noqa: E402
from adapters import sources as _src  # noqa: E402

# --------------------------------------------------------------------------- #
# Per-request identity scoping
# --------------------------------------------------------------------------- #
# A CSM sees ONLY the accounts they own; an Admin sees all. We enforce this at the
# single source of truth: the account provider the rules engine reads from. The
# server sets the authenticated principal per request (thread-local); every
# downstream computation (portfolio, KPIs, data gaps, lifecycle, tasks, and the
# agent, which all read orchestrate.load_accounts()) is then automatically scoped,
# so no endpoint can accidentally leak another CSM's book.
import threading  # noqa: E402

_REQUEST = threading.local()


class ForbiddenError(Exception):
    """Raised when the current principal may not access a specific account (-> 403)."""


def set_principal(principal: dict | None) -> None:
    """Set the authenticated principal for the current request thread.
    principal = {"email","name","role","owner_id",...} or None (unscoped/admin)."""
    _REQUEST.principal = principal


def get_principal() -> dict | None:
    return getattr(_REQUEST, "principal", None)


def _owns(account: dict, owner_id: str | None) -> bool:
    return bool(owner_id) and str(account.get("hubspot", {}).get("csm_owner_id") or "") == str(owner_id)


def _scoped_accounts() -> dict:
    """The account roster visible to the current principal. Admin (or no principal,
    for legacy/open mode) sees everything; a CSM sees only accounts they own."""
    accounts = dataaccess.all_accounts()
    p = get_principal()
    if not p or p.get("role") == "admin":
        return accounts
    owner_id = p.get("owner_id")
    return {aid: a for aid, a in accounts.items() if _owns(a, owner_id)}


def can_view_account(account_id: str) -> bool:
    """Whether the current principal may view a specific account (for hard 403s)."""
    p = get_principal()
    if not p or p.get("role") == "admin":
        return True
    accounts = dataaccess.all_accounts()
    a = accounts.get(account_id)
    return bool(a) and _owns(a, p.get("owner_id"))


# The rules engine reads accounts through this provider, so scoping is uniform.
orchestrate.set_account_provider(_scoped_accounts)


# --------------------------------------------------------------------------- #
# Live-only data policy
# --------------------------------------------------------------------------- #
# The UI must never display a fabricated (fixture/sample) value. The rules engine
# still runs over fixtures for deterministic behaviour, but every value the API
# hands to the UI is either genuinely LIVE or explicitly marked "not connected".
#
# `sources` maps each signal block to 'live' or 'sample'. A block is only real if
# its source is 'live'. We map signal blocks to the source key that feeds them:
_SIGNAL_SOURCE = {
    "hubspot": "hubspot",
    "zendesk": "zendesk",
    "usage": "usage",     # Pendo
    "jiminny": "jiminny",
    "stripe": "stripe",
    # churn has a live adapter (Redshift) when configured; falls back to a
    # transparent computed score otherwise. onboarding has no adapter -> never 'live'.
    # Jiminny removed: no live source, was fixture-only ('remove all mock data').
    "churn": "churn",
    "onboarding": "onboarding",
}

# Pendo's live endpoint does not expose these usage metrics (the adapter hardcodes
# them to None); treat them as not-connected even when Pendo itself is live, so the
# UI never shows a fixture-derived number for them.
_PENDO_UNAVAILABLE_FIELDS = {
    "logins_last_7d", "logins_prev_7d", "active_users_pct",
    "license_utilization_pct", "key_feature_adoption_pct",
    "api_calls_last_7d", "api_calls_prev_7d",
}


def _is_live(sources: dict, block: str) -> bool:
    """True if the source feeding this block is real: either genuinely 'live', or
    'computed' (transparently derived from live signals — never fixture/sample)."""
    src_key = _SIGNAL_SOURCE.get(block, block)
    return sources.get(src_key) in ("live", "computed", "live_no_record")


def _live_block(account: dict, block: str) -> dict:
    """Return the signal block only if it is live-sourced; otherwise {} (not connected)."""
    if not _is_live(account.get("sources", {}), block):
        return {}
    data = dict(account.get(block, {}) or {})
    if block == "usage":
        # Strip only fields the live Pendo response did not supply. Configured
        # metadata mappings remain visible and can drive expansion rules.
        for f in _PENDO_UNAVAILABLE_FIELDS:
            if data.get(f) is None:
                data.pop(f, None)
    return data


def _live_account(account: dict) -> dict:
    """A copy of the account with all non-live signal blocks blanked out, so any
    downstream computation (health, KPIs, lifecycle) uses ONLY real data."""
    sources = account.get("sources", {})
    projected = {
        "hubspot": _live_block(account, "hubspot"),
        "zendesk": _live_block(account, "zendesk"),
        "usage": _live_block(account, "usage"),
        "jiminny": _live_block(account, "jiminny"),
        "churn": _live_block(account, "churn"),
        "stripe": _live_block(account, "stripe"),
        "onboarding": _live_block(account, "onboarding"),
        "sources": sources,
    }
    return projected


def _connected(account: dict) -> dict:
    """Per-block connection status the UI uses to render 'not connected' states."""
    sources = account.get("sources", {})
    return {b: _is_live(sources, b) for b in _SIGNAL_SOURCE}



def health_score(account: dict) -> dict:
    """Compute a 0-100 health score + RAG band from the signals.

    Deterministic and explainable: starts at 100, subtracts weighted penalties for
    churn risk, CSAT, usage drop, Sev-1, and payment status. This is the health
    score the UI shows per account and rolls up across the portfolio.
    """
    hs = account.get("hubspot", {})
    zd = account.get("zendesk", {})
    usage = account.get("usage", {})
    churn = account.get("churn", {})
    stripe = account.get("stripe", {})
    jiminny = account.get("jiminny", {})

    score = 100.0
    reasons: list[str] = []

    ml = churn.get("ml_churn_score") or 0.0
    if ml:
        pen = round(ml * 40)
        score -= pen
        if pen >= 12:
            reasons.append(f"churn risk {int(ml * 100)}% (-{pen})")

    if str(churn.get("churn_status") or "").lower() == "churned":
        score -= 40
        reasons.append("Redshift churn status: Churned (-40)")

    csat = zd.get("csat_30d")
    if csat is not None and csat < 75:
        pen = round((75 - csat) * 0.5)
        score -= pen
        reasons.append(f"CSAT {csat} (-{pen})")

    now, prev = usage.get("logins_last_7d") or 0, usage.get("logins_prev_7d") or 0
    if prev > 0 and now < prev:
        drop = (prev - now) / prev
        if drop >= 0.3:
            pen = round(drop * 25)
            score -= pen
            reasons.append(f"usage down {int(drop * 100)}% (-{pen})")

    if (zd.get("sev1_open") or 0) > 0:
        score -= 15
        reasons.append("open Sev-1 (-15)")

    if stripe.get("dunning_stage") == "day_15_plus":
        score -= 10
        reasons.append("payment 15+ days past due (-10)")

    # Live Pendo recency: no visit in a while is a real disengagement signal.
    dsv = usage.get("days_since_last_visit")
    if dsv is not None and dsv >= 14:
        pen = min(20, (dsv // 7) * 5)
        score -= pen
        reasons.append(f"no product visit in {dsv} days (-{pen})")

    # Live Pendo risk advisor.
    prisk = str(usage.get("pendo_risk_score") or "").lower()
    if prisk == "high":
        score -= 18
        reasons.append("Pendo risk advisor: High (-18)")
    elif prisk == "medium":
        score -= 8
        reasons.append("Pendo risk advisor: Medium (-8)")

    # Live Jiminny conversational intelligence: negative call sentiment is a real
    # relationship-health signal. Weighted modestly — it colours the score but does
    # not, on its own, dominate hard risk signals like churn or an open Sev-1.
    sentiment = str(jiminny.get("sentiment") or "").lower()
    if sentiment == "negative":
        score -= 10
        reasons.append("latest call sentiment: negative (-10)")
    elif sentiment == "positive":
        score = min(100.0, score + 3)
        reasons.append("latest call sentiment: positive (+3)")

    if str(churn.get("churn_status") or "").lower() == "churned":
        score = min(score, 49)
    score = max(0, min(100, round(score)))
    band = "green" if score >= 75 else "amber" if score >= 50 else "red"
    # Computable only if at least one real (live) health input contributed. When no
    # live signal is present, the score is not meaningful and the UI shows "no data".
    computable = bool(
        churn.get("ml_churn_score") is not None
        or churn.get("churn_status")
        or zd.get("csat_30d") is not None
        or (zd.get("sev1_open") is not None)
        or usage.get("days_since_last_visit") is not None
        or usage.get("pendo_risk_score")
        or stripe.get("dunning_stage")
        or jiminny.get("sentiment")
    )
    return {"score": score, "band": band, "reasons": reasons, "computable": computable}


def portfolio() -> dict:
    """The single-pane-of-glass payload: every account with health + segment + ARR +
    renewal, plus the prioritised task queue and suppressed signals across the book."""
    accounts = orchestrate.load_accounts()
    result = orchestrate.orchestrate()

    # Join tasks to accounts by the stable account_id (a display name can collide).
    tasks_by_account: dict[str, list] = {}
    for t in result["tasks"]:
        tasks_by_account.setdefault(t.get("account_id"), []).append(t)

    rows = []
    for aid, a in accounts.items():
        live = _live_account(a)
        hsobj = live.get("hubspot", {})           # {} unless HubSpot is live
        h = health_score(live)                    # computed from live signals only
        rows.append({
            "account_id": aid,
            "name": hsobj.get("name"),
            "segment": hsobj.get("segment_label") or hsobj.get("segment"),
            "arr_usd": hsobj.get("arr_usd"),
            "renewal_date": hsobj.get("renewal_date"),
            "subscription_type": hsobj.get("subscription_type"),
            "csm_owner": hsobj.get("csm_owner"),
            "lifecycle_stage": hsobj.get("lifecycle_stage"),
            "health": h,
            "connected": _connected(a),
            "open_task_count": len(tasks_by_account.get(aid, [])),
        })
    rows.sort(key=lambda r: r["health"]["score"])  # worst health first

    # Only sum ARR that is genuinely live-sourced (HubSpot live). No fixture ARR.
    total_arr = sum(r["arr_usd"] or 0 for r in rows)
    at_risk_arr = sum(r["arr_usd"] or 0 for r in rows
                      if r["health"]["computable"] and r["health"]["band"] == "red")

    return {
        "summary": {
            "accounts": len(rows),
            "priority1": sum(1 for t in result["tasks"] if t["priority"] == 1),
            "suppressed": len(result["suppressed"]),
            "total_arr_usd": total_arr,
            "at_risk_arr_usd": at_risk_arr,
            "live_sources": dataaccess.live_sources(),
            "data_mode": "live" if dataaccess.any_live() else "sample",
            "account_scope": ("all" if (not get_principal() or get_principal().get("role") == "admin")
                              else "csm"),
            "scope_owner": (get_principal() or {}).get("name") or (get_principal() or {}).get("email"),
            "principal": ({"name": get_principal().get("name"), "email": get_principal().get("email"),
                           "role": get_principal().get("role")} if get_principal() else None),
            "judge": result.get("judge", {"verdict": "UNKNOWN", "violations": []}),
        },
        "accounts": rows,
        "tasks": result["tasks"],
        "suppressed": result["suppressed"],
            "automations": result.get("automations", []),
    }


def _confidence(account: dict, evidence: dict) -> str:
    sources = account.get("sources", {})
    live_count = sum(1 for value in sources.values() if value == "live")
    if live_count >= 3 and evidence:
        return "high"
    if live_count >= 1 and evidence:
        return "medium"
    return "low"


def daily_brief() -> dict:
    """Human-ready daily brief derived only from the deterministic queue."""
    accounts = orchestrate.load_accounts()
    result = orchestrate.orchestrate()
    by_id = dict(accounts)
    actions = []
    for task in result["tasks"][:3]:
        account = by_id.get(task.get("account_id"), {})
        hs = account.get("hubspot", {})
        actions.append({
            "account": task.get("account"), "priority": task.get("priority"),
            "mandate": task.get("mandate"), "trigger": task.get("trigger"),
            "why": task.get("evidence", {}),
            "recommended_action": task.get("recommended_action"),
            "owner": hs.get("csm_owner") or "Unassigned",
            "sla": {1: "within 24 hours", 2: "today", 3: "this week"}.get(task.get("priority"), "review"),
            "confidence": _confidence(account, task.get("evidence", {})),
            "sources": [key for key, value in account.get("sources", {}).items() if value == "live"],
        })
    gaps = datagaps()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accounts_reviewed": len(accounts),
        "top_actions": actions,
        "priority1_count": sum(1 for task in result["tasks"] if task.get("priority") == 1),
        "at_risk_arr_usd": portfolio()["summary"]["at_risk_arr_usd"],
        "data_gap_accounts": gaps["summary"]["accounts_missing_roles"],
        "judge": result.get("judge", {}),
    }


def why_not(account_id: str) -> dict:
    if not can_view_account(account_id):
        raise ForbiddenError(account_id)
    accounts = orchestrate.load_accounts()
    account = accounts.get(account_id)
    if not account:
        raise KeyError(account_id)
    tasks, _ = orchestrate.evaluate(account_id, account)
    if tasks:
        return {"account_id": account_id, "has_tasks": True, "tasks": tasks}
    live = _live_account(account)
    health = health_score(live)
    usage = live.get("usage", {})
    churn = live.get("churn", {})
    reasons = []
    if health["computable"] and health["band"] == "green":
        reasons.append("health is currently green")
    if usage.get("days_since_last_visit") is not None and usage["days_since_last_visit"] <= 14:
        reasons.append("recent product activity")
    if not churn.get("ml_churn_score") and str(churn.get("churn_status", "")).lower() != "churned":
        reasons.append("no qualifying churn signal")
    if (live.get("zendesk", {}).get("sev1_open") or 0) == 0:
        reasons.append("no open Sev-1")
    return {"account_id": account_id, "has_tasks": False, "reasons": reasons or ["no deterministic rule fired"],
            "data_gaps": [key for key, value in account.get("sources", {}).items() if value == "not_live"]}


def account_detail(account_id: str) -> dict:
    if not can_view_account(account_id):
        raise ForbiddenError(account_id)
    accounts = orchestrate.load_accounts()
    if account_id not in accounts:
        raise KeyError(account_id)
    a = accounts[account_id]
    tasks, suppressed = orchestrate.evaluate(account_id, a)
    live = _live_account(a)
    h = health_score(live)
    return {
        "account_id": account_id,
        "hubspot": live.get("hubspot", {}),
        "signals": {
            "zendesk": live.get("zendesk", {}),
            "usage": live.get("usage", {}),
            "jiminny": live.get("jiminny", {}),
            "churn": live.get("churn", {}),
            "stripe": live.get("stripe", {}),
        },
        "onboarding": live.get("onboarding", {}),
        "health": h,
        "sources": a.get("sources", {}),
        "connected": _connected(a),
        "primary_instances": sorted(primary_instance_ids(live.get("hubspot", {}))),
        "tasks": tasks,
        "suppressed": suppressed,
        "hubspot_writeback": _writeback_payload(live, h, tasks) if _is_live(a.get("sources", {}), "hubspot") else None,
    }


def _writeback_payload(account: dict, health: dict, tasks: list) -> dict:
    """The CS data pushed BACK to HubSpot for Sales visibility (req §1 bi-directional).

    In fixture mode this is the payload that WOULD be written; swap for a live
    hubspot.crm.companies PATCH."""
    if not health["computable"]:
        return {
            "target": "hubspot.crm.companies",
            "cs_health_score": None,
            "cs_risk_status": "no_data",
            "cs_active_playbook": None,
            "note": "No live health signal is available; no health write-back is recommended.",
        }
    churned = str(account.get("churn", {}).get("churn_status") or "").lower() == "churned"
    risk = "at_risk" if churned or health["band"] == "red" else "watch" if health["band"] == "amber" else "healthy"
    playbook = next((t["trigger"] for t in tasks if t["priority"] == 1), None) \
        or next((t["trigger"] for t in tasks), None)
    return {
        "target": "hubspot.crm.companies",
        "cs_health_score": health["score"],
        "cs_risk_status": risk,
        "cs_active_playbook": playbook,
    }


def writeback(account_id: str, apply: bool = False) -> dict:
    """Prepare or apply the HubSpot CS write-back with an auditable result.

    Dry-run is the default. Applying requires both `apply=True` and
    `CS_ALLOW_WRITE=1`; the adapter enforces the same guard at the HTTP boundary.
    """
    account = orchestrate.load_accounts().get(account_id)
    if not account:
        raise KeyError(account_id)
    live = _live_account(account)
    health = health_score(live)
    tasks, _ = orchestrate.evaluate(account_id, account)
    payload = _writeback_payload(live, health, tasks)
    if not (dataaccess._ADAPTERS and _src.HUBSPOT.live()):
        result = {"synced": False, "mode": "not-connected", "would_write": payload}
    else:
        result = _src.HUBSPOT.push_cs_data(
            account_id,
            health_score=payload["cs_health_score"],
            risk_status=payload["cs_risk_status"],
            active_playbook=payload["cs_active_playbook"],
            apply=apply,
        )
    return {
        "audit_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "apply_requested": bool(apply),
        "write_enabled": bool(os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on")),
        "result": result,
    }


def _retention_metrics(accounts: dict, tasks_by_account: dict) -> dict:
    """Portfolio revenue retention, computed transparently from LIVE signals only.

    GRR (Gross Revenue Retention) is the headline retention metric and is computed
    from real churn: (base_arr - churned_arr) / base_arr, capped at 100% (no upside
    counted). It tracks the CS brief's GRR >= 92% goal.

    We deliberately DO NOT publish an NDR percentage here. True NDR requires *booked*
    expansion/contraction revenue, which this platform does not yet have. Reporting an
    NDR inflated by unrealised opportunity would be misleading for a board-level metric.
    Instead we surface the expansion PIPELINE separately: the ARR of healthy accounts
    carrying an active expansion trigger (license utilization / API surge / strong
    adoption). This is opportunity, not retention — labelled as such.

    Definitions (annualised, book-level):
      base_arr          = sum of live contract ARR across the book
      churned_arr       = ARR of accounts flagged churned (Redshift status or HubSpot
                          lifecycle) — revenue lost
      expansion_pipeline_arr = ARR of healthy accounts with an active expansion trigger
                          (pipeline/opportunity, NOT booked expansion)

    `computable` is False when no live ARR is available, so the UI shows "no data"
    rather than a misleading 0%.
    """
    base_arr = 0
    churned_arr = 0
    expansion_pipeline_arr = 0
    expansion_accounts = 0
    for aid, a in accounts.items():
        live = _live_account(a)
        hs = live.get("hubspot", {})
        arr = hs.get("arr_usd") or 0
        if not arr:
            continue
        base_arr += arr
        churn = live.get("churn", {})
        churned = str(churn.get("churn_status") or "").lower() == "churned" \
            or str(hs.get("lifecycle_stage") or "").lower() in {"churned", "churned customer"}
        if churned:
            churned_arr += arr
            continue  # a churned account is not an expansion opportunity
        acct_tasks = tasks_by_account.get(aid, [])
        if any(t.get("rule_id") in {
            orchestrate.RULE_EXPANSION_UTILIZATION,
            orchestrate.RULE_EXPANSION_API_SURGE,
            orchestrate.RULE_EXPANSION_ADOPTION,
        } for t in acct_tasks):
            expansion_pipeline_arr += arr
            expansion_accounts += 1

    if not base_arr:
        return {"computable": False, "grr_pct": None,
                "base_arr_usd": 0, "churned_arr_usd": 0,
                "expansion_pipeline_arr_usd": 0, "expansion_pipeline_accounts": 0,
                "target": {"grr_pct": 92},
                "note": "No live contract ARR available; retention is not computable."}

    grr = round(100 * (base_arr - churned_arr) / base_arr, 1)
    return {
        "computable": True,
        "grr_pct": grr,
        "base_arr_usd": base_arr,
        "churned_arr_usd": churned_arr,
        # Expansion PIPELINE (opportunity), reported separately from retention. Not NDR.
        "expansion_pipeline_arr_usd": expansion_pipeline_arr,
        "expansion_pipeline_accounts": expansion_accounts,
        "target": {"grr_pct": 92},
        "method": "live ARR; GRR from Redshift/HubSpot churned status. Expansion "
                  "pipeline = active expansion-trigger accounts (opportunity, not booked "
                  "revenue); no NDR is published until booked expansion data exists.",
    }


def kpis() -> dict:
    """Leadership KPI & capacity tracking (req §3): per-CSM portfolio allocation,
    task load, at-risk ARR, and health mix, to inform headcount/resourcing."""
    accounts = orchestrate.load_accounts()
    result = orchestrate.orchestrate()
    task_metrics = _task_metrics(result["tasks"])
    tasks_by_account = {}
    for t in result["tasks"]:
        tasks_by_account.setdefault(t.get("account_id"), []).append(t)
    status_by_id = task_metrics["status_by_id"]
    active_tasks = [t for t in result["tasks"]
                    if status_by_id.get(t.get("task_id"), "open") != "completed"]

    by_csm: dict[str, dict] = {}
    mandate_counts = {"MUST_PROTECT": 0, "MUST_EXPAND": 0, "MUST_USE": 0}
    for t in active_tasks:
        mandate_counts[t["mandate"]] = mandate_counts.get(t["mandate"], 0) + 1

    for aid, a in accounts.items():
        live = _live_account(a)
        hs = live.get("hubspot", {})              # {} unless HubSpot live
        csm = hs.get("csm_owner") or "Unassigned"
        h = health_score(live)
        tasks = tasks_by_account.get(aid, [])      # join by stable account_id
        active_account_tasks = [task for task in tasks
                    if status_by_id.get(task.get("task_id"), "open") != "completed"]
        rec = by_csm.setdefault(csm, {
            "csm": csm, "accounts": 0, "arr_usd": 0, "open_tasks": 0,
            "priority1_tasks": 0, "at_risk_accounts": 0, "completed_tasks": 0,
            "overdue_tasks": 0,
        })
        rec["accounts"] += 1
        rec["arr_usd"] += hs.get("arr_usd", 0) or 0   # only live ARR contributes
        rec["open_tasks"] += len(active_account_tasks)
        rec["priority1_tasks"] += sum(1 for t in active_account_tasks if t["priority"] == 1)
        rec["completed_tasks"] += sum(1 for t in tasks if task_metrics["status_by_id"].get(t.get("task_id")) == "completed")
        rec["overdue_tasks"] += sum(1 for t in active_account_tasks if t.get("task_id") in task_metrics["overdue_task_ids"])
        if h["computable"] and h["band"] == "red":
            rec["at_risk_accounts"] += 1

    capacity = _capacity_per_csm()
    for rec in by_csm.values():
        rec["capacity_utilization_pct"] = round(100 * rec["open_tasks"] / capacity, 1) if capacity else 0

    return {
        "by_csm": sorted(by_csm.values(), key=lambda r: -r["open_tasks"]),
        "mandate_load": mandate_counts,
        "retention": _retention_metrics(accounts, tasks_by_account),
        "task_metrics": {key: (sorted(value) if key == "overdue_task_ids" else value)
                 for key, value in task_metrics.items() if key != "status_by_id"},
        "totals": {
            "csms": len([c for c in by_csm if c not in ("Pooled", "Unassigned")]),
            "open_tasks": task_metrics["open_tasks"],
            "priority1_tasks": sum(1 for t in active_tasks if t["priority"] == 1),
        },
    }


def _task_events_path() -> Path:
    return Path(os.environ.get("CS_TASK_EVENTS_FILE", str(Path(__file__).resolve().parents[1] / ".cs-task-events.jsonl")))


def _load_task_events() -> dict[str, dict]:
    latest: dict[str, dict] = {}
    path = _task_events_path()
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("task_id"):
            latest[event["task_id"]] = event
    return latest


def _capacity_per_csm() -> int:
    try:
        return max(1, int(os.environ.get("CS_TASK_CAPACITY_PER_CSM", "20")))
    except ValueError:
        return 20


def _task_metrics(tasks: list[dict]) -> dict:
    events = _load_task_events()
    today = date.today()
    completed = in_progress = overdue = 0
    ages = []
    overdue_ids = set()
    on_time = late = 0
    for task in tasks:
        task_id = task.get("task_id")
        event = events.get(task_id, {})
        status = event.get("status", "open")
        if status == "completed":
            completed += 1
            completed_at = str(event.get("completed_at") or event.get("updated_at") or today.isoformat())[:10]
            if task.get("due_on") and completed_at <= task["due_on"]:
                on_time += 1
            else:
                late += 1
        elif status == "in_progress":
            in_progress += 1
        created = task.get("created_on")
        if created:
            try:
                ages.append(max(0, (today - date.fromisoformat(created)).days))
            except ValueError:
                pass
        if status != "completed" and task.get("due_on") and today.isoformat() > task["due_on"]:
            overdue += 1
            if task_id:
                overdue_ids.add(task_id)
    return {
        "completed_tasks": completed,
        "in_progress_tasks": in_progress,
        "open_tasks": len(tasks) - completed,
        "overdue_tasks": overdue,
        "average_task_age_days": round(sum(ages) / len(ages), 1) if ages else 0,
        "sla_adherence_pct": round(100 * on_time / (on_time + late), 1) if on_time + late else None,
        "status_by_id": {task_id: event.get("status", "open") for task_id, event in events.items()},
        "overdue_task_ids": overdue_ids,
    }


def lifecycle() -> dict:
    """Unified lifecycle view (req §3): onboarding velocity/health alongside adoption."""
    accounts = orchestrate.load_accounts()
    rows = []
    for aid, a in accounts.items():
        live = _live_account(a)
        hs = live.get("hubspot", {})
        ob = live.get("onboarding", {})   # {} always: no live onboarding adapter
        usage = live.get("usage", {})     # adoption fields stripped: Pendo can't supply them
        rows.append({
            "account_id": aid,
            "name": hs.get("name"),
            "segment": hs.get("segment_label") or hs.get("segment"),
            "onboarding_status": ob.get("status"),               # None -> not connected
            "time_to_value_days": ob.get("time_to_value_days"),  # None -> not connected
            "onboarding_health": ob.get("health"),
            "key_feature_adoption_pct": usage.get("key_feature_adoption_pct"),  # None
            "active_users_pct": usage.get("active_users_pct"),                   # None
            "days_since_last_visit": usage.get("days_since_last_visit"),         # real Pendo signal
            "connected": _connected(a),
        })
    return {"accounts": rows}


def integrations() -> dict:
    """Integration status map (req §1) for the single pane of glass. Reflects which
    connectors feed the platform, their sync direction, and what they contribute.
    Fixture mode reports 'connected (sample)'; swap adapters for live to flip to 'live'."""
    accounts = orchestrate.load_accounts()
    liveset = set(dataaccess.live_sources())
    # Count accounts actually carrying live data for each source.
    def synced(src_key):
        return sum(1 for a in accounts.values() if a.get("sources", {}).get(src_key) == "live")
    def st(name):
        return "connected (live)" if name in liveset else "not connected"
    hs_live = "HubSpot" in liveset
    return {
        "connectors": [
            {"system": "HubSpot", "category": "CRM", "direction": "bi-directional",
             "status": st("HubSpot"), "accounts_synced": synced("hubspot"),
             "pulls": ["contract value", "renewal date", "account hierarchy", "contacts"],
             "pushes": ["health score", "risk status", "active playbook"] if hs_live else []},
            {"system": "Stripe", "category": "Billing / Finance", "direction": "read-only",
             "status": st("Stripe"), "accounts_synced": synced("stripe"),
             "pulls": ["invoice status", "days past due", "ARR"], "pushes": []},
            {"system": "Zendesk", "category": "Support", "direction": "read-only",
             "status": st("Zendesk"), "accounts_synced": synced("zendesk"),
             "pulls": ["ticket volume", "CSAT", "Sev-1 flags"], "pushes": []},
            {"system": "Product Telemetry", "category": "Usage (Pendo)", "direction": "read-only",
             "status": st("Pendo"), "accounts_synced": synced("usage"),
             "pulls": ["risk advisor", "adoption", "days since last visit", "plan tier"], "pushes": []},
            {"system": "Jiminny", "category": "Conversational Intelligence", "direction": "read-only",
             "status": st("Jiminny"), "accounts_synced": synced("jiminny"),
             "pulls": ["last call", "sentiment", "summary", "customer talk ratio"], "pushes": []},
            {"system": "Rocket Lane", "category": "Onboarding", "direction": "read-only",
             "status": st("Rocket Lane"), "accounts_synced": synced("onboarding"),
             "pulls": ["onboarding status", "time to value", "onboarding health"], "pushes": []},
            {"system": "Churn Model (Redshift)", "category": "Data Platform · Redshift Data API",
             "direction": "read-only", "access": "read-only",
             "status": st("Churn Model"), "accounts_synced": synced("churn"),
             "pulls": ["ML churn score", "model version", "top risk drivers"], "pushes": []},
            {"system": "Entitlements", "category": "Licensing / Billing",
             "direction": "read-only", "access": "read-only",
             "status": st("Entitlements"), "accounts_synced": synced("entitlements"),
             "pulls": ["licensed seats", "active seats", "license utilization %"], "pushes": []},
        ]
    }


def datagaps() -> dict:
    """Data Gap Analysis (Requirements §4). For each account, report which source
    systems have it (coverage) and which required CS fields are empty in HubSpot,
    so RevOps can see exactly what cleanup is needed. Uses only live data."""
    accounts = orchestrate.load_accounts()
    # Required CS metadata per WoW §3/§5.
    HS_FIELDS = [
        ("segment_label", "Segment (ICP)"),
        ("arr_usd", "ARR"),
        ("renewal_date", "Renewal date"),
        ("csm_owner", "CSM owner"),
        ("subscription_type", "Subscription type"),
    ]
    ROLES = ["Executive Sponsor", "Primary Champion / Admin", "Finance Contact"]

    rows = []
    totals = {"zendesk": 0, "stripe": 0, "usage": 0, "hubspot": 0}
    field_missing = {label: 0 for _, label in HS_FIELDS}
    roles_missing_accounts = 0
    for aid, a in accounts.items():
        src = a.get("sources", {})
        hs = a.get("hubspot", {}) if _is_live(src, "hubspot") else {}
        coverage = {
            "hubspot": _is_live(src, "hubspot"),
            "zendesk": _is_live(src, "zendesk"),
            "stripe": _is_live(src, "stripe"),
            "usage": _is_live(src, "usage"),
        }
        coverage_status = {}
        for key in coverage:
            source_state = src.get(key, "not_live")
            coverage_status[key] = (
                "live" if source_state in ("live", "computed")
                else "no_record" if source_state == "live_no_record"
                else "not_connected"
            )
        for k, v in coverage.items():
            if v and src.get(k) not in ("live_no_record", "live_unavailable"):
                totals[k] += 1
        missing_fields = []
        for key, label in HS_FIELDS:
            if hs.get(key) in (None, ""):
                missing_fields.append(label)
                field_missing[label] += 1
        have_roles = {c.get("role") for c in hs.get("contacts", [])}
        missing_roles = [r for r in ROLES if r not in have_roles]
        if missing_roles:
            roles_missing_accounts += 1
        # Missing source systems (account not found in that vendor).
        missing_systems = [s.title() if s != "usage" else "Pendo"
                   for s in ("zendesk", "stripe", "usage")
                   if src.get(s) not in ("live", "computed")]
        rows.append({
            "account_id": aid,
            "name": hs.get("name") or aid,
            "coverage": coverage,
            "coverage_status": coverage_status,
            "missing_systems": missing_systems,
            "missing_fields": missing_fields,
            "missing_roles": missing_roles,
            "gap_count": len(missing_systems) + len(missing_fields) + (1 if missing_roles else 0),
        })
    rows.sort(key=lambda r: -r["gap_count"])  # worst-coverage first
    n = len(rows) or 1
    return {
        "accounts": rows,
        "summary": {
            "total_accounts": len(rows),
            "coverage_pct": {k: round(100 * v / n) for k, v in totals.items()},
            "not_in_zendesk": len(rows) - totals["zendesk"],
            "not_in_stripe": len(rows) - totals["stripe"],
            "not_in_pendo": len(rows) - totals["usage"],
            "hubspot_field_gaps": field_missing,
            "accounts_missing_roles": roles_missing_accounts,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(portfolio()["summary"], indent=2))
