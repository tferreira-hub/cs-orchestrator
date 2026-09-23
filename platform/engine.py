#!/usr/bin/env python3
"""CS Platform, engine layer.

Wraps the WoW orchestration (orchestrate.py) and adds the account **health score**
and portfolio roll-up the single-pane-of-glass UI needs. Kept dependency-free and
importable so both the API and the CLI/agent use the same logic (single source of
truth, WoW §1).
"""

from __future__ import annotations

import os
import sys
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
orchestrate.set_account_provider(dataaccess.all_accounts)


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

    score = 100.0
    reasons: list[str] = []

    ml = churn.get("ml_churn_score", 0.0)
    if ml:
        pen = round(ml * 40)
        score -= pen
        if pen >= 12:
            reasons.append(f"churn risk {int(ml * 100)}% (-{pen})")

    csat = zd.get("csat_30d")
    if csat is not None and csat < 75:
        pen = round((75 - csat) * 0.5)
        score -= pen
        reasons.append(f"CSAT {csat} (-{pen})")

    now, prev = usage.get("logins_last_7d", 0), usage.get("logins_prev_7d", 0)
    if prev > 0 and now < prev:
        drop = (prev - now) / prev
        if drop >= 0.3:
            pen = round(drop * 25)
            score -= pen
            reasons.append(f"usage down {int(drop * 100)}% (-{pen})")

    if zd.get("sev1_open", 0) > 0:
        score -= 15
        reasons.append("open Sev-1 (-15)")

    if stripe.get("dunning_stage") == "day_15_plus":
        score -= 10
        reasons.append("payment 15+ days past due (-10)")

    if account.get("jiminny", {}).get("sentiment") == "negative":
        score -= 8
        reasons.append("negative call sentiment (-8)")

    score = max(0, min(100, round(score)))
    band = "green" if score >= 75 else "amber" if score >= 50 else "red"
    return {"score": score, "band": band, "reasons": reasons}


def portfolio() -> dict:
    """The single-pane-of-glass payload: every account with health + segment + ARR +
    renewal, plus the prioritised task queue and suppressed signals across the book."""
    accounts = orchestrate.load_accounts()
    result = orchestrate.orchestrate()

    tasks_by_account: dict[str, list] = {}
    for t in result["tasks"]:
        tasks_by_account.setdefault(t["account"], []).append(t)

    rows = []
    for aid, a in accounts.items():
        hsobj = a.get("hubspot", {})
        h = health_score(a)
        rows.append({
            "account_id": aid,
            "name": hsobj.get("name"),
            "segment": hsobj.get("segment"),
            "arr_usd": hsobj.get("arr_usd"),
            "renewal_date": hsobj.get("renewal_date"),
            "health": h,
            "open_task_count": len(tasks_by_account.get(hsobj.get("name"), [])),
        })
    rows.sort(key=lambda r: r["health"]["score"])  # worst health first

    total_arr = sum(r["arr_usd"] or 0 for r in rows)
    at_risk_arr = sum(r["arr_usd"] or 0 for r in rows if r["health"]["band"] == "red")

    return {
        "summary": {
            "accounts": len(rows),
            "priority1": sum(1 for t in result["tasks"] if t["priority"] == 1),
            "suppressed": len(result["suppressed"]),
            "total_arr_usd": total_arr,
            "at_risk_arr_usd": at_risk_arr,
            "live_sources": dataaccess.live_sources(),
            "data_mode": "live" if dataaccess.any_live() else "sample",
        },
        "accounts": rows,
        "tasks": result["tasks"],
        "suppressed": result["suppressed"],
    }


def account_detail(account_id: str) -> dict:
    accounts = orchestrate.load_accounts()
    if account_id not in accounts:
        raise KeyError(account_id)
    a = accounts[account_id]
    tasks, suppressed = orchestrate.evaluate(account_id, a)
    h = health_score(a)
    return {
        "account_id": account_id,
        "hubspot": a.get("hubspot", {}),
        "signals": {
            "zendesk": a.get("zendesk", {}),
            "usage": a.get("usage", {}),
            "churn": a.get("churn", {}),
            "stripe": a.get("stripe", {}),
            "jiminny": a.get("jiminny", {}),
        },
        "onboarding": a.get("onboarding", {}),
        "health": h,
        "sources": a.get("sources", {}),
        "primary_instances": sorted(primary_instance_ids(a.get("hubspot", {}))),
        "tasks": tasks,
        "suppressed": suppressed,
        "hubspot_writeback": _writeback_payload(a, h, tasks),
    }


def _writeback_payload(account: dict, health: dict, tasks: list) -> dict:
    """The CS data pushed BACK to HubSpot for Sales visibility (req §1 bi-directional).

    In fixture mode this is the payload that WOULD be written; swap for a live
    hubspot.crm.companies PATCH."""
    risk = "at_risk" if health["band"] == "red" else "watch" if health["band"] == "amber" else "healthy"
    playbook = next((t["trigger"] for t in tasks if t["priority"] == 1), None) \
        or next((t["trigger"] for t in tasks), None)
    return {
        "target": "hubspot.crm.companies",
        "cs_health_score": health["score"],
        "cs_risk_status": risk,
        "cs_active_playbook": playbook,
    }


def kpis() -> dict:
    """Leadership KPI & capacity tracking (req §3): per-CSM portfolio allocation,
    task load, at-risk ARR, and health mix, to inform headcount/resourcing."""
    accounts = orchestrate.load_accounts()
    result = orchestrate.orchestrate()
    tasks_by_account = {}
    for t in result["tasks"]:
        tasks_by_account.setdefault(t["account"], []).append(t)

    by_csm: dict[str, dict] = {}
    mandate_counts = {"MUST_PROTECT": 0, "MUST_EXPAND": 0, "MUST_USE": 0}
    for t in result["tasks"]:
        mandate_counts[t["mandate"]] = mandate_counts.get(t["mandate"], 0) + 1

    for aid, a in accounts.items():
        hs = a.get("hubspot", {})
        csm = hs.get("csm_owner", "Unassigned")
        name = hs.get("name")
        h = health_score(a)
        tasks = tasks_by_account.get(name, [])
        rec = by_csm.setdefault(csm, {
            "csm": csm, "accounts": 0, "arr_usd": 0, "open_tasks": 0,
            "priority1_tasks": 0, "at_risk_accounts": 0,
        })
        rec["accounts"] += 1
        rec["arr_usd"] += hs.get("arr_usd", 0) or 0
        rec["open_tasks"] += len(tasks)
        rec["priority1_tasks"] += sum(1 for t in tasks if t["priority"] == 1)
        if h["band"] == "red":
            rec["at_risk_accounts"] += 1

    return {
        "by_csm": sorted(by_csm.values(), key=lambda r: -r["open_tasks"]),
        "mandate_load": mandate_counts,
        "totals": {
            "csms": len([c for c in by_csm if c not in ("Pooled", "Unassigned")]),
            "open_tasks": len(result["tasks"]),
            "priority1_tasks": sum(1 for t in result["tasks"] if t["priority"] == 1),
        },
    }


def lifecycle() -> dict:
    """Unified lifecycle view (req §3): onboarding velocity/health alongside adoption."""
    accounts = orchestrate.load_accounts()
    rows = []
    for aid, a in accounts.items():
        hs = a.get("hubspot", {})
        ob = a.get("onboarding", {})
        usage = a.get("usage", {})
        rows.append({
            "account_id": aid,
            "name": hs.get("name"),
            "segment": hs.get("segment"),
            "onboarding_status": ob.get("status"),
            "time_to_value_days": ob.get("time_to_value_days"),
            "onboarding_health": ob.get("health"),
            "key_feature_adoption_pct": usage.get("key_feature_adoption_pct"),
            "active_users_pct": usage.get("active_users_pct"),
        })
    return {"accounts": rows}


def integrations() -> dict:
    """Integration status map (req §1) for the single pane of glass. Reflects which
    connectors feed the platform, their sync direction, and what they contribute.
    Fixture mode reports 'connected (sample)'; swap adapters for live to flip to 'live'."""
    accounts = orchestrate.load_accounts()
    n = len(accounts)
    liveset = set(dataaccess.live_sources())
    def st(name):
        return "live" if name in liveset else "connected (sample)"
    return {
        "connectors": [
            {"system": "HubSpot", "category": "CRM", "direction": "bi-directional",
             "status": st("HubSpot"), "accounts_synced": n,
             "pulls": ["contract value", "renewal date", "account hierarchy", "contacts"],
             "pushes": ["health score", "risk status", "active playbook"]},
            {"system": "Stripe", "category": "Billing / Finance", "direction": "read-only",
             "status": st("Stripe"), "accounts_synced": n,
             "pulls": ["invoice status", "days past due", "ARR"], "pushes": []},
            {"system": "Zendesk", "category": "Support", "direction": "read-only",
             "status": st("Zendesk"), "accounts_synced": n,
             "pulls": ["ticket volume", "CSAT", "Sev-1 flags"], "pushes": []},
            {"system": "Product Telemetry", "category": "Usage (Pendo)", "direction": "read-only",
             "status": st("Pendo"), "accounts_synced": n,
             "pulls": ["login frequency", "feature adoption", "license utilization", "API usage"], "pushes": []},
            {"system": "Jiminny", "category": "Conversational Intelligence", "direction": "read-only",
             "status": st("Jiminny"), "accounts_synced": n,
             "pulls": ["call sentiment", "meeting summary", "talk ratio"], "pushes": []},
            {"system": "Rocket Lane", "category": "Onboarding", "direction": "read-only",
             "status": "planned", "accounts_synced": 0,
             "pulls": ["onboarding velocity", "time to value"], "pushes": []},
        ]
    }


if __name__ == "__main__":
    import json
    print(json.dumps(portfolio()["summary"], indent=2))
