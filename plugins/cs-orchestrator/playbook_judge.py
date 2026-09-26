#!/usr/bin/env python3
"""CS Playbook Judge, executable feedback sensor.

This is the *code* behind the `cs-playbook-judge` agent: a deterministic
(computational) sensor that inspects the produced task queue and checks it
against the Ways of Working. It returns a verdict (PASS / NEEDS_CHANGES) plus a
list of concrete violations, so the harness can self-correct before results
reach a human.

Pure function, unit-testable, no I/O. Mirrors the thresholds in orchestrate.py.

Engine/judge contract — `rule_id`
----------------------------------
Every task the rules engine emits MUST carry a stable `rule_id` (see the RULE_*
constants and RULE_PRIORITY map in orchestrate.py). The judge keys its
priority-mapping, suppression, payment, and draft-presence checks on that
`rule_id`, never on the human-readable `trigger` prose — so rewording a trigger
can never silently break this feedback sensor. A task with no `rule_id` is itself
a violation (`rule_id_missing`): the contract fails loud rather than degrading
back to fragile string matching. Any caller constructing tasks by hand must set a
valid `rule_id`; tasks should normally come only from `orchestrate.evaluate()`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks" / "scripts"))
from suppression import primary_instance_ids, ticket_spike_on_primary  # noqa: E402

# The rule_id -> expected-priority contract lives in the rules engine. Judging on
# these stable IDs (never on the human-readable trigger prose) keeps the feedback
# sensor coupled to the engine's intent: editing a trigger string can no longer
# silently break the judge. Fall back to an empty map only if the import fails.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from orchestrate import (  # noqa: E402
        RULE_PRIORITY,
        RULE_DAY15_PAYMENT,
        RULE_PREDICTIVE_RISK,
        RULE_SCALED_EXCEPTION,
        RULE_CHURNED_RECOVERY,
    )
except Exception:  # noqa: BLE001
    RULE_PRIORITY = {}
    RULE_DAY15_PAYMENT = "day15_payment_strategic"
    RULE_PREDICTIVE_RISK = "predictive_risk_playbook"
    RULE_SCALED_EXCEPTION = "scaled_exception_escalation"
    RULE_CHURNED_RECOVERY = "churned_account_recovery"

# rule_ids that must carry a drafted outreach message.
_DRAFT_REQUIRED_RULES = {RULE_PREDICTIVE_RISK, RULE_DAY15_PAYMENT, RULE_CHURNED_RECOVERY}

VALID_MANDATES = {"MUST_PROTECT", "MUST_EXPAND", "MUST_USE"}
HIGH_ARR = 100000
# Expected priority band per mandate/trigger, for ordering checks.
MANDATE_MIN_PRIORITY = {"MUST_PROTECT": 1, "MUST_EXPAND": 3, "MUST_USE": 5}


def judge(tasks: list[dict], accounts: dict[str, dict], suppressed: list[dict] | None = None) -> dict:
    """Check a produced task queue against the WoW rules.

    Returns {"verdict": "PASS"|"NEEDS_CHANGES", "violations": [ {rule, account, detail, fix} ]}.
    `accounts` is keyed by account_id; each value is the assembled account object
    (hubspot/zendesk/usage/churn/stripe/sources) the queue was built from.
    """
    violations: list[dict] = []
    # Index accounts by BOTH stable account_id and display name. Tasks now carry
    # account_id (a name can collide); we resolve by id first and fall back to name
    # for hand-constructed tasks that only set `account`.
    by_id: dict[str, dict] = dict(accounts)
    by_name: dict[str, dict] = {}
    for a in accounts.values():
        nm = a.get("hubspot", {}).get("name")
        if nm:
            by_name[nm] = a

    def resolve_account(task: dict) -> dict | None:
        return by_id.get(task.get("account_id")) or by_name.get(task.get("account"))

    def add(rule, account, detail, fix):
        violations.append({"rule": rule, "account": account, "detail": detail, "fix": fix})

    # --- Rule 2: mandate correctness ---
    for t in tasks:
        if t.get("mandate") not in VALID_MANDATES:
            add("mandate_correctness", t.get("account"),
                f"invalid mandate {t.get('mandate')!r}", "assign one of MUST_PROTECT/MUST_EXPAND/MUST_USE")

    # --- Rule 1: evidence grounding (every task cites evidence; numbers trace to account) ---
    for t in tasks:
        ev = t.get("evidence") or {}
        if not ev:
            add("evidence_grounding", t.get("account"),
                f"task '{t.get('trigger')}' has no evidence", "attach the tool values that fired the rule")

    # --- Rule 5: priority ordering (queue ascending; mandate priority bands sane) ---
    priorities = [t.get("priority") for t in tasks]
    if priorities != sorted(priorities):
        add("priority_ordering", None, f"queue not sorted ascending: {priorities}",
            "sort the queue by priority ascending")
    for t in tasks:
        mn = MANDATE_MIN_PRIORITY.get(t.get("mandate"))
        if mn is not None and t.get("priority", 99) < mn:
            add("priority_ordering", t.get("account"),
                f"{t.get('mandate')} at priority {t.get('priority')} (min expected {mn})",
                "align priority to the mandate band")

        trig = (t.get("trigger") or "").lower()
        # Priority mapping is keyed on the stable rule_id, not the trigger prose.
        # This is the engine/judge contract: RULE_PRIORITY is imported from the
        # rules engine, so the two can never drift on wording alone.
        rule_id = t.get("rule_id")
        expected = RULE_PRIORITY.get(rule_id)
        # Contact hygiene is P5 on Strategic, P6 on Scaled — the only rule whose
        # expected priority depends on segment.
        if rule_id == "contact_hygiene" and t.get("segment") != "Strategic":
            expected = 6
        if rule_id is None:
            add("rule_id_missing", t.get("account"),
                f"task '{t.get('trigger')}' has no rule_id",
                "attach a stable rule_id in orchestrate.py so the judge can verify it")
        elif expected is not None and t.get("priority") != expected:
            add("priority_mapping", t.get("account"),
                f"rule '{rule_id}' has priority {t.get('priority')}, expected {expected}",
                "align the task priority with the WoW rule_id mapping")

    # --- Rules 3,4,6,7: per-account, need the account signals ---
    for t in tasks:
        a = resolve_account(t)
        if not a:
            continue
        hs = a.get("hubspot", {})
        stripe = a.get("stripe", {})
        segment = hs.get("segment") or "Scaled"
        arr = hs.get("arr_usd") or 0
        rule_id = t.get("rule_id")

        # Independently recompute the primary-instance rule for risk tasks.
        if t.get("mandate") == "MUST_PROTECT" and rule_id in (RULE_PREDICTIVE_RISK, RULE_SCALED_EXCEPTION):
            zd = a.get("zendesk", {}) or {}
            primary_ids = primary_instance_ids(hs)
            fired, spike_evidence = ticket_spike_on_primary(zd, primary_ids)
            if "ticket_spike" in str(t.get("evidence", {})).lower() and not fired:
                add("suppression", t.get("account"),
                    "risk evidence is driven by a non-primary/test-instance ticket spike",
                    "remove the task or use primary-instance evidence")

        # Rule 4: No-Chasing payment.
        if rule_id == RULE_DAY15_PAYMENT:
            if segment != "Strategic":
                add("no_chasing_payment", t.get("account"),
                    "Day-15 payment task on a non-Strategic account (Scaled must auto-suspend)",
                    "drop the task; Scaled day-15 auto-suspends")
            elif arr < HIGH_ARR:
                add("no_chasing_payment", t.get("account"),
                    f"Day-15 payment task but ARR {arr} < {HIGH_ARR} threshold",
                    "drop the task; not high-ARR")

        # Rule 3: segment routing — Scaled must only be exception-based, never routine.
        if segment != "Strategic" and t.get("mandate") == "MUST_EXPAND":
            add("segment_routing", t.get("account"),
                "Scaled account received a proactive MUST_EXPAND task (should be Strategic-only)",
                "remove; Scaled is exception-based")

        # Rule 7: draft presence for the risk/payment rules that require an outreach draft.
        if rule_id in _DRAFT_REQUIRED_RULES and not t.get("draft_message"):
            add("draft_presence", t.get("account"),
                f"risk/payment task '{t.get('trigger')}' ({rule_id}) has no draft_message",
                "add a drafted outreach message")

    # Completeness: recompute the deterministic expected queue and ensure every
    # expected task for every in-scope account was emitted.
    try:
        import orchestrate as _rules
        # Match on (account_id, rule_id) — both stable — falling back to name/trigger
        # for any hand-constructed task missing the newer fields.
        emitted = {(t.get("account_id") or t.get("account"),
                    t.get("rule_id") or t.get("trigger")) for t in tasks}
        for account_id, account in accounts.items():
            expected, _ = _rules.evaluate(account_id, account)
            for task in expected:
                key = (task.get("account_id") or task.get("account"),
                       task.get("rule_id") or task.get("trigger"))
                if key not in emitted:
                    add("task_completeness", task.get("account"),
                        f"expected task '{task.get('trigger')}' was not emitted",
                        "re-run the responsible specialist/rules stage and include the task")
    except Exception as exc:  # noqa: BLE001
        add("task_completeness", None,
            f"could not recompute expected tasks: {type(exc).__name__}: {exc}",
            "repair the deterministic rules/judge integration")

    # --- Rule 4 (payment, portfolio-level): no task for dunning days 1-14 ---
    for t in tasks:
        a = resolve_account(t)
        if not a:
            continue
        stage = (a.get("stripe", {}) or {}).get("dunning_stage")
        if stage == "day_1_14" and t.get("rule_id") == RULE_DAY15_PAYMENT:
            add("no_chasing_payment", t.get("account"),
                "payment task exists for dunning days 1-14 (must be fully automated)",
                "remove; days 1-14 are automated, no CSM task")

    verdict = "PASS" if not violations else "NEEDS_CHANGES"
    return {"verdict": verdict, "violations": violations, "checked_tasks": len(tasks)}


if __name__ == "__main__":  # tiny self-check against orchestrate output
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import orchestrate
    res = orchestrate.orchestrate()
    accts = orchestrate.load_accounts()
    v = judge(res["tasks"], accts, res.get("suppressed"))
    print(f"verdict: {v['verdict']} ({v['checked_tasks']} tasks, {len(v['violations'])} violations)")
    for x in v["violations"]:
        print(f"  - [{x['rule']}] {x['account']}: {x['detail']}")
