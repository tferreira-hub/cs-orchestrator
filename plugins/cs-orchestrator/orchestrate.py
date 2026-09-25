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
import hashlib
import os
import sys
from datetime import date, timedelta
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
ADOPTION_DAYS_SINCE_VISIT = 14
ACTIVE_USERS_ADOPTION_FLOOR = 60
FEATURE_ADOPTION_FLOOR = 50


def _load() -> dict:
    return json.loads(Path(FIXTURES).read_text(encoding="utf-8"))["accounts"]


# Pluggable account source. Defaults to the fixture loader; the platform sets this
# to a live data-access provider so the SAME rules run over real data.
_ACCOUNT_PROVIDER = _load


def set_account_provider(fn) -> None:
    global _ACCOUNT_PROVIDER
    _ACCOUNT_PROVIDER = fn


def load_accounts() -> dict:
    return _ACCOUNT_PROVIDER()


def _days_to(d: str) -> int:
    return (date.fromisoformat(d) - TODAY).days


def evaluate(account_id: str, a: dict) -> tuple[list[dict], list[dict]]:
    hs, zd = a.get("hubspot", {}), a.get("zendesk", {})
    usage, churn, stripe = a.get("usage", {}), a.get("churn", {}), a.get("stripe", {})
    segment = hs.get("segment") or "Scaled"
    arr = hs.get("arr_usd", 0)
    name = hs.get("name", account_id)
    primary_ids = primary_instance_ids(hs)

    tasks: list[dict] = []

    def add(priority, mandate, trigger, evidence, action, draft=None):
        sla_hours = {1: 24, 2: 24, 3: 168, 4: 336, 5: 168, 6: 336}.get(priority)
        t = {"priority": priority, "account": name, "segment": segment,
             "mandate": mandate, "trigger": trigger, "evidence": evidence,
             "recommended_action": action,
             "task_id": hashlib.sha256(f"{account_id}:{trigger}".encode()).hexdigest()[:16],
             "created_on": TODAY.isoformat(),
             "sla_hours": sla_hours,
             "due_on": (TODAY + timedelta(hours=sla_hours)).isoformat() if sla_hours else None}
        if draft:
            t["draft_message"] = draft
        tasks.append(t)

    def risk_draft():
        if churn_status == "churned":
            return ("Internal recovery note: verify the churned lifecycle status, confirm any remaining service or billing obligations, "
                    "and route the account to the approved recovery or closure workflow."), "internal_recovery"
        sponsor = _first_contact(hs, "Executive Sponsor")
        recipient = sponsor or f"{name} team"
        signals = []
        if churn_status == "churned":
            signals.append("our account health records indicate the account is at risk")
        dsv = usage.get("days_since_last_visit")
        if dsv is not None and dsv >= 180:
            signals.append(f"there has been no product activity recorded for {dsv} days")
        if sev1:
            signals.append("there is an open critical support issue")
        if pendo_risk == "high":
            signals.append("our product risk signals have increased")
        detail = " and ".join(signals) if signals else "recent account health signals"
        return (f"Hi {recipient}, I'd like to book 30 minutes this week to review your account. "
            f"We noticed {detail}. I want to make sure we are addressing the right priorities."), "customer_outreach"

    # --- MUST_PROTECT: churn / multi-signal risk ---
    score = churn.get("ml_churn_score") or 0.0
    spike_fired, spike_ev = ticket_spike_on_primary(zd, primary_ids)
    logins_now = usage.get("logins_last_7d") or 0
    logins_prev = usage.get("logins_prev_7d") or 0
    usage_drop = logins_prev > 0 and logins_now <= USAGE_DROP * logins_prev
    sev1 = (zd.get("sev1_open") or 0) > 0
    pendo_risk = str(usage.get("pendo_risk_score") or "").lower()  # live Pendo risk advisor

    risk_fired = False
    drivers = []
    churn_is_ml = bool(churn.get("ml_churn_score") is not None and not churn.get("computed"))
    churn_status = str(churn.get("churn_status") or "").lower()
    lifecycle_stage = str(hs.get("lifecycle_stage") or "").lower()
    churn_source = "Redshift churn status=Churned"
    if not churn_status and lifecycle_stage in {"churned", "churned customer"}:
        churn_status = "churned"
        churn_source = f"HubSpot lifecycle={hs.get('lifecycle_stage')}"
    risk_label = "ML churn risk" if churn_is_ml else "risk score"
    if segment == "Strategic":
        if churn_status == "churned":
            risk_fired = True
            drivers.append(churn_source)
        # A computed score (derived from live signals when no ML model is connected) is
        # NOT an ML churn prediction. It must not fire the Priority-1 Predictive Risk
        # Playbook on its own — that threshold is calibrated for the ML model output.
        # Computed scores still contribute to other drivers (Pendo risk, Sev-1, etc.)
        # and to the health score, so the account remains visible when other signals fire.
        if score >= CHURN_RISK and churn_is_ml:
            risk_fired = True
            drivers.append(f"{risk_label} {int(score*100)}% (>70%)")
        if pendo_risk == "high":
            risk_fired = True
            drivers.append("pendo_risk_advisor=High")
        if spike_fired and usage_drop:
            risk_fired = True
            drivers.append(f"ticket_spike({spike_ev.get('tickets_prev_7d')}->{spike_ev.get('tickets_last_7d')})+usage_drop({logins_prev}->{logins_now})")
        if sev1:
            risk_fired = True
            drivers.append("sev1_open")
        # Severe product disengagement (live Pendo recency): a Strategic account with no
        # product visit in a very long time is genuine churn risk on its own.
        dsv = usage.get("days_since_last_visit")
        if dsv is not None and dsv >= 180:
            risk_fired = True
            drivers.append(f"no_product_visit_{dsv}d")
        if risk_fired:
            risk_trigger = "Churned account recovery (today)" if churn_status == "churned" else "Predictive Risk Playbook (24h SLA)"
            risk_action = ("Confirm the account lifecycle, route to the recovery or closure workflow, and review any remaining service obligations."
                           if churn_status == "churned" else
                           "Initiate Defensive Workflow: root-cause analysis, executive outreach, internal escalation.")
            risk_evidence = {"drivers": drivers}
            if churn.get("ml_churn_score") is not None:
                risk_evidence["churn_score"] = churn.get("ml_churn_score")
            if usage.get("pendo_risk_score") is not None:
                risk_evidence["pendo_risk"] = usage.get("pendo_risk_score")
            if zd.get("csat_30d") is not None:
                risk_evidence["csat_30d"] = zd.get("csat_30d")
            draft_message, draft_type = risk_draft()
            recovery_priority = 2 if churn_status == "churned" else 1
            add(recovery_priority, "MUST_PROTECT", risk_trigger,
                risk_evidence,
                risk_action,
                draft_message)
            tasks[-1]["draft_type"] = draft_type
    else:  # Scaled: strictly exception-based (system-driven escalation only)
        scaled_drivers = []
        # Same guard as Strategic: computed scores must not substitute for the ML
        # model threshold. Only a genuine Redshift ML score triggers this escalation.
        if score >= CHURN_SCALED_EXCEPTION and churn_is_ml:
            scaled_drivers.append(f"ml_churn_score={score}")
        if sev1:
            scaled_drivers.append("sev1_open")
        if scaled_drivers:
            risk_fired = True
            risk_trigger = "Scaled exception escalation (system-driven)"
            risk_evidence = {"drivers": scaled_drivers}
            if churn.get("ml_churn_score") is not None:
                risk_evidence["churn_score"] = churn.get("ml_churn_score")
            if usage.get("pendo_risk_score") is not None:
                risk_evidence["pendo_risk"] = usage.get("pendo_risk_score")
            if zd.get("csat_30d") is not None:
                risk_evidence["csat_30d"] = zd.get("csat_30d")
            add(2, "MUST_PROTECT", risk_trigger,
                risk_evidence,
                "Exception from the automated flow: review and, if warranted, escalate via the scaled playbook.")

    # --- Payment risk (No Chasing rule) ---
    stage = stripe.get("dunning_stage", "none")
    if stage == "day_15_plus":
        if segment == "Strategic" and arr >= HIGH_ARR:
            risk_fired = True
            add(2, "MUST_PROTECT", "Day-15 payment (high-ARR Strategic)",
                {"days_past_due": stripe.get("days_past_due"), "amount_due_usd": stripe.get("amount_due_usd"), "arr_usd": arr},
                "Executive outreach to Finance Contact to prevent service disruption.",
                f"Hi {(_first_contact(hs,'Finance Contact') or f'{name} Finance team')}, our records show an invoice about {stripe.get('days_past_due')} days past due. I want to make sure there's no disruption to your service. Could you point me to the right person to resolve it?")
        # Scaled day_15_plus => auto-suspend, NO task (intentionally omitted)

    # --- MUST_EXPAND: expansion triggers (healthy only) ---
    healthy = score < 0.4 and not sev1 and churn_status != "churned"
    if segment == "Strategic" and healthy:
        util = usage.get("license_utilization_pct") or 0
        api_now = usage.get("api_calls_last_7d")
        api_prev = usage.get("api_calls_prev_7d")
        # Live Pendo adoption/engagement signal (real). Strong adoption + recent activity
        # on a healthy Strategic account is a genuine expansion cue.
        adoption = str(usage.get("pendo_adoption") or usage.get("pendo_adoption_engagement") or "").lower()
        dsv = usage.get("days_since_last_visit")
        recent = (dsv is not None and dsv <= 14)
        if util >= UTIL_EXPANSION:
            add(3, "MUST_EXPAND", "Expansion trigger (license utilization >= 85%)",
                {"license_utilization_pct": util}, "Prompt commercial upsell conversation.")
        elif api_now and api_prev and api_now >= API_SURGE * api_prev:
            add(3, "MUST_EXPAND", "Expansion trigger (API usage surge)",
                {"api_calls_last_7d": api_now, "api_calls_prev_7d": api_prev},
                "Prompt commercial upsell conversation (API/add-on velocity).")
        elif adoption in ("high", "strong", "increasing") and recent:
            add(3, "MUST_EXPAND", "Expansion trigger (strong live adoption)",
                {"pendo_adoption": usage.get("pendo_adoption"),
                 "days_since_last_visit": dsv},
                "High product adoption on a healthy account, explore upsell / additional seats.")

    # --- MUST_USE: adoption and onboarding ---
    adoption_drivers = []
    days_since_visit = usage.get("days_since_last_visit")
    if days_since_visit is not None and days_since_visit >= ADOPTION_DAYS_SINCE_VISIT:
        adoption_drivers.append(f"no_product_visit_{days_since_visit}d")
    active_users = usage.get("active_users_pct")
    if active_users is not None and active_users < ACTIVE_USERS_ADOPTION_FLOOR:
        adoption_drivers.append(f"active_users_{active_users}%")
    feature_adoption = usage.get("key_feature_adoption_pct")
    if feature_adoption is not None and feature_adoption < FEATURE_ADOPTION_FLOOR:
        adoption_drivers.append(f"key_feature_adoption_{feature_adoption}%")
    onboarding = a.get("onboarding", {}) or {}
    onboarding_status = str(onboarding.get("status") or "").lower()
    onboarding_health = str(onboarding.get("health") or "").lower()
    if onboarding_status in {"stalled", "blocked", "at_risk"} or onboarding_health in {"red", "at_risk"}:
        adoption_drivers.append(f"onboarding_{onboarding_status or onboarding_health}")
    # Protect is the owning intervention when the same inactivity signal is also
    # driving churn risk. Keep the queue actionable instead of duplicating work.
    if adoption_drivers and segment == "Strategic" and not risk_fired and stage != "day_15_plus":
        adoption_priority = 5 if segment == "Strategic" else 6
        adoption_action = ("Initiate the adoption playbook: review activation blockers, contact the Primary Champion, "
                           "and schedule a value check-in." if segment == "Strategic" else
                           "Route to the automated adoption program and escalate only if the exception persists.")
        add(adoption_priority, "MUST_USE", "Adoption / onboarding intervention",
            {"drivers": adoption_drivers,
             "days_since_last_visit": days_since_visit,
             "active_users_pct": active_users,
             "key_feature_adoption_pct": feature_adoption,
             "onboarding_status": onboarding.get("status"),
             "onboarding_health": onboarding.get("health")},
            adoption_action)

    # --- MUST_EXPAND: renewal cadence ---
    if hs.get("renewal_date"):
        dtr = _days_to(hs["renewal_date"])
        milestone = None
        if dtr <= 0:
            if segment == "Strategic":
                add(2, "MUST_PROTECT", "Overdue renewal escalation",
                    {"days_since_renewal": abs(dtr), "renewal_date": hs["renewal_date"]},
                    "Escalate the overdue renewal internally and confirm the commercial owner.")
        elif dtr <= 30:
            milestone = ("T-30", "Ensure commercial close is in progress.")
        elif dtr <= 60:
            milestone = ("T-60", "Send commercial proposal.")
        elif dtr <= 90:
            milestone = ("T-90", "Value outreach / health check.")
        elif dtr <= 120:
            milestone = ("T-120", "Internal risk check.")
        if milestone and segment == "Strategic" and churn_status != "churned":
            add(4, "MUST_EXPAND", f"Proactive renewal {milestone[0]}",
                {"days_to_renewal": dtr, "renewal_date": hs["renewal_date"]}, milestone[1])

    # --- MUST_USE: contact hygiene gate (WoW §5: roles maintained on ALL accounts) ---
    have = {c.get("role") for c in hs.get("contacts", [])}
    missing = REQUIRED_ROLES - have
    # Contact hygiene remains visible in Data Gaps, but should not compete with
    # an urgent risk/payment action in the CSM's daily queue.
    if missing and segment == "Strategic" and not risk_fired and stage != "day_15_plus":
        add(5, "MUST_USE", "Contact hygiene (missing required roles)",
            {"missing_roles": sorted(missing)}, "Tag missing contact roles in CRM (WoW §5).")

    return tasks, suppressed_signals(hs, zd)


def _first_contact(hs: dict, role: str):
    for c in hs.get("contacts", []):
        if c.get("role") == role:
            return c.get("name")
    return None


def orchestrate(account_ids: list[str] | None = None) -> dict:
    accounts = load_accounts()
    ids = account_ids or list(accounts)
    all_tasks, all_suppressed, automations = [], [], []
    for aid in ids:
        account = accounts[aid]
        tasks, suppressed = evaluate(aid, account)
        all_tasks.extend(tasks)
        for s in suppressed:
            s["account"] = accounts[aid].get("hubspot", {}).get("name", aid)
            all_suppressed.append(s)
        payment = payment_automation_status(aid, account)
        if payment:
            automations.append(payment)
    all_tasks.sort(key=lambda t: t["priority"])
    result = {"reviewed": len(ids), "tasks": all_tasks, "suppressed": all_suppressed,
              "automations": automations}
    # Feedback sensor: judge the produced queue against the WoW rules.
    try:
        from playbook_judge import judge as _judge
        result["judge"] = _judge(all_tasks, accounts, all_suppressed)
    except Exception as exc:  # noqa: BLE001
        result["judge"] = {"verdict": "UNKNOWN", "violations": [], "error": f"{type(exc).__name__}: {exc}"}
    return result


def payment_automation_status(account_id: str, account: dict) -> dict | None:
    """Describe the external billing workflow without mutating Stripe or CRM.

    Stripe/ERP remains the system that executes dunning or suspension. The CS
    platform records the governed handoff so the dashboard and agent can explain
    what is automated and when a human exception is required.
    """
    stripe = account.get("stripe", {}) or {}
    stage = stripe.get("dunning_stage", "none")
    if stage not in {"day_1_14", "day_15_plus"}:
        return None
    hs = account.get("hubspot", {}) or {}
    segment = hs.get("segment") or "Scaled"
    arr = hs.get("arr_usd") or 0
    name = hs.get("name", account_id)
    if stage == "day_1_14":
        return {"account_id": account_id, "account": name, "workflow": "automated_dunning",
                "status": "handoff_required", "requires_csm": False,
                "days_past_due": stripe.get("days_past_due"),
                "note": "Days 1-14 are handled by the billing automation; no CSM task is created."}
    if segment == "Strategic" and arr >= HIGH_ARR:
        return {"account_id": account_id, "account": name, "workflow": "payment_risk_escalation",
                "status": "cs_task_created", "requires_csm": True,
                "days_past_due": stripe.get("days_past_due"),
                "note": "High-ARR Strategic Day-15 payment exception is routed to the CSM."}
    return {"account_id": account_id, "account": name, "workflow": "auto_suspend",
            "status": "handoff_required", "requires_csm": False,
            "days_past_due": stripe.get("days_past_due"),
            "note": "Scaled Day-15 payment is handled by automated suspension; no CSM task is created."}


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
    if result.get("automations"):
        print("Automated billing workflows:")
        for item in result["automations"]:
            print(f"  - {item['account']}: {item['workflow']} ({item['status']})")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    _print(orchestrate(args or None))
