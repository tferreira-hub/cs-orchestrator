#!/usr/bin/env python3
"""CS Orchestrator, deterministic dry-run driver.

Applies the cs-playbook Ways-of-Working rules to the fixtures and prints the
prioritised CSM task queue. This is the reference implementation of the rules the
`cs-orchestrator` agent applies, it lets us (a) run the demo deterministically,
(b) unit-test the WoW logic, and (c) show judges the harness produces the same
standardised queue every time.

Usage:  python3 orchestrate.py            # whole portfolio
        python3 orchestrate.py acct_northwind
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks" / "scripts"))
from suppression import (  # noqa: E402
    primary_instance_ids,
    ticket_spike_on_primary,
    suppressed_signals,
)

FIXTURES = os.environ.get(
    "CS_FIXTURES",
    str(Path(__file__).resolve().parent / "mcp-servers" / "fixtures" / "accounts.json"),
)
TODAY = date.fromisoformat(os.environ.get("CS_TODAY", "2026-09-23"))

CHURN_RISK = 0.70
CHURN_SCALED_EXCEPTION = 0.85
UTIL_EXPANSION = 85
API_SURGE = 1.4
USAGE_DROP = 0.6
HIGH_ARR = 100000
REQUIRED_ROLES = {"Executive Sponsor", "Primary Champion", "Finance Contact"}


def _load() -> dict:
    return json.loads(Path(FIXTURES).read_text(encoding="utf-8"))["accounts"]


def _days_to(d: str) -> int:
    return (date.fromisoformat(d) - TODAY).days


def evaluate(account_id: str, a: dict) -> tuple[list[dict], list[dict]]:
    hs, zd = a.get("hubspot", {}), a.get("zendesk", {})
    usage, churn, stripe = a.get("usage", {}), a.get("churn", {}), a.get("stripe", {})
    jiminny = a.get("jiminny", {})
    segment = hs.get("segment", "Scaled")
    arr = hs.get("arr_usd", 0)
    name = hs.get("name", account_id)
    primary_ids = primary_instance_ids(hs)

    tasks: list[dict] = []

    def add(priority, mandate, trigger, evidence, action, draft=None):
        t = {"priority": priority, "account": name, "segment": segment,
             "mandate": mandate, "trigger": trigger, "evidence": evidence,
             "recommended_action": action}
        if draft:
            t["draft_message"] = draft
        tasks.append(t)

    # --- MUST_PROTECT: churn / multi-signal risk ---
    score = churn.get("ml_churn_score", 0.0)
    spike_fired, spike_ev = ticket_spike_on_primary(zd, primary_ids)
    logins_now = usage.get("logins_last_7d", 0)
    logins_prev = usage.get("logins_prev_7d", 0)
    usage_drop = logins_prev > 0 and logins_now <= USAGE_DROP * logins_prev
    sev1 = zd.get("sev1_open", 0) > 0

    risk_fired = False
    drivers = []
    if segment == "Strategic":
        if score >= CHURN_RISK:
            risk_fired = True
            drivers.append(f"ml_churn_score={score}")
        if spike_fired and usage_drop:
            risk_fired = True
            drivers.append(f"ticket_spike({spike_ev['tickets_prev_7d']}->{spike_ev['tickets_last_7d']})+usage_drop({logins_prev}->{logins_now})")
        if sev1:
            risk_fired = True
            drivers.append("sev1_open")
        if jiminny.get("sentiment") == "negative":
            risk_fired = True
            drivers.append("negative_call_sentiment")
        if risk_fired:
            add(1, "MUST_PROTECT", "Predictive Risk Playbook (24h SLA)",
                {"churn_score": score, "csat_30d": zd.get("csat_30d"), "drivers": drivers},
                "Initiate Defensive Workflow: root-cause analysis, executive outreach, internal escalation.",
                f"Hi {(_first_contact(hs,'Executive Sponsor') or 'there')}, I'd like to book 30 minutes this week to review recent changes on your account and make sure we're delivering value. I've noticed a dip in usage and want to get ahead of it.")
    else:  # Scaled, exception only
        if score >= CHURN_SCALED_EXCEPTION:
            add(1, "MUST_PROTECT", "Scaled exception escalation (churn >= 0.85)",
                {"churn_score": score}, "Escalate from automated flow; scaled playbook.")

    # --- Payment risk (No Chasing rule) ---
    stage = stripe.get("dunning_stage", "none")
    if stage == "day_15_plus":
        if segment == "Strategic" and arr >= HIGH_ARR:
            add(2, "MUST_PROTECT", "Day-15 payment (high-ARR Strategic)",
                {"days_past_due": stripe.get("days_past_due"), "amount_due_usd": stripe.get("amount_due_usd"), "arr_usd": arr},
                "Executive outreach to Finance Contact to prevent service disruption.",
                f"Hi {(_first_contact(hs,'Finance Contact') or 'there')}, our records show an invoice about {stripe.get('days_past_due')} days past due. I want to make sure there's no disruption to your service. Could you point me to the right person to resolve it?")
        # Scaled day_15_plus => auto-suspend, NO task (intentionally omitted)

    # --- MUST_EXPAND: expansion triggers (healthy only) ---
    healthy = score < 0.4 and not sev1
    if segment == "Strategic" and healthy:
        util = usage.get("license_utilization_pct", 0)
        api_now = usage.get("api_calls_last_7d")
        api_prev = usage.get("api_calls_prev_7d")
        if util >= UTIL_EXPANSION:
            add(3, "MUST_EXPAND", "Expansion trigger (license utilization >= 85%)",
                {"license_utilization_pct": util}, "Prompt commercial upsell conversation.")
        elif api_now and api_prev and api_now >= API_SURGE * api_prev:
            add(3, "MUST_EXPAND", "Expansion trigger (API usage surge)",
                {"api_calls_last_7d": api_now, "api_calls_prev_7d": api_prev},
                "Prompt commercial upsell conversation (API/add-on velocity).")

    # --- MUST_EXPAND: renewal cadence ---
    if hs.get("renewal_date"):
        dtr = _days_to(hs["renewal_date"])
        milestone = None
        if 0 < dtr <= 30:
            milestone = ("T-30", "Ensure commercial close is in progress.")
        elif dtr <= 60:
            milestone = ("T-60", "Send commercial proposal.")
        elif dtr <= 90:
            milestone = ("T-90", "Value outreach / health check.")
        elif dtr <= 120:
            milestone = ("T-120", "Internal risk check.")
        if milestone and segment == "Strategic":
            add(4, "MUST_EXPAND", f"Proactive renewal {milestone[0]}",
                {"days_to_renewal": dtr, "renewal_date": hs["renewal_date"]}, milestone[1])

    # --- MUST_USE: contact hygiene gate ---
    if segment == "Strategic":
        have = {c.get("role") for c in hs.get("contacts", [])}
        missing = REQUIRED_ROLES - have
        if missing:
            add(5, "MUST_USE", "Contact hygiene (missing required roles)",
                {"missing_roles": sorted(missing)}, "Tag missing contact roles in CRM (WoW §5).")

    return tasks, suppressed_signals(hs, zd)


def _first_contact(hs: dict, role: str):
    for c in hs.get("contacts", []):
        if c.get("role") == role:
            return c.get("name")
    return None


def orchestrate(account_ids: list[str] | None = None) -> dict:
    accounts = _load()
    ids = account_ids or list(accounts)
    all_tasks, all_suppressed = [], []
    for aid in ids:
        tasks, suppressed = evaluate(aid, accounts[aid])
        all_tasks.extend(tasks)
        for s in suppressed:
            s["account"] = accounts[aid].get("hubspot", {}).get("name", aid)
            all_suppressed.append(s)
    all_tasks.sort(key=lambda t: t["priority"])
    return {"reviewed": len(ids), "tasks": all_tasks, "suppressed": all_suppressed}


def _print(result: dict) -> None:
    print(f"Portfolio: {result['reviewed']} accounts reviewed, "
          f"{len(result['suppressed'])} suppressed signal(s), "
          f"{sum(1 for t in result['tasks'] if t['priority'] == 1)} Priority-1 risk task(s).\n")
    print(f"{'P':<2} {'Account':<18} {'Segment':<10} {'Mandate':<13} Trigger")
    print("-" * 90)
    for t in result["tasks"]:
        print(f"{t['priority']:<2} {t['account']:<18} {t['segment']:<10} {t['mandate']:<13} {t['trigger']}")
    print()
    for t in result["tasks"]:
        if "draft_message" in t:
            print(f"[P{t['priority']}] {t['account']}, draft:\n  {t['draft_message']}\n")
    if result["suppressed"]:
        print("Suppressed (multi-instance noise, WoW §5):")
        for s in result["suppressed"]:
            print(f"  - {s['account']}: {s['signal']}, {s['reason']} "
                  f"(non-primary {s['non_primary_instances']} tickets={s['non_primary_tickets']} vs primary={s['primary_tickets']})")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    _print(orchestrate(args or None))
