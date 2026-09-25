#!/usr/bin/env python3
"""Tests for the CS Orchestrator harness, the Ways-of-Working rules, the
multi-instance suppression hook, and the grounding gate.

Run:  python3 -m pytest tests/ -v      (or)   python3 tests/test_orchestrator.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "hooks" / "scripts"))

os.environ.setdefault("CS_TODAY", "2026-09-23")

import orchestrate  # noqa: E402
from suppression import ticket_spike_on_primary, primary_instance_ids  # noqa: E402


def _result():
    return orchestrate.orchestrate()


def test_northwind_is_priority1_risk():
    """Strategic account with churn>=0.70 + Sev-1 must be Priority-1 MUST_PROTECT."""
    tasks = _result()["tasks"]
    nw = [t for t in tasks if t["account"] == "Northwind Traders" and t["priority"] == 1]
    assert nw, "Northwind should have a Priority-1 risk task"
    assert nw[0]["mandate"] == "MUST_PROTECT"
    assert "draft_message" in nw[0], "Priority-1 risk task must include a drafted action"


def test_initech_day15_payment_task():
    """High-ARR Strategic account past day 15 gets a payment task (No-Chasing rule)."""
    tasks = _result()["tasks"]
    it = [t for t in tasks if t["account"] == "Initech" and "Day-15" in t["trigger"]]
    assert it, "Initech (Strategic, $350k, 16d past due) should get a Day-15 payment task"


def test_scaled_account_no_payment_task():
    """Scaled account past day 15 must AUTO-SUSPEND: no payment/Protect task queued
    (the No-Chasing rule). Non-payment hygiene tasks (e.g. contact roles) may still
    appear, since WoW §5 requires role tagging on all accounts."""
    tasks = _result()["tasks"]
    smallco = [t for t in tasks if t["account"] == "SmallCo"]
    # No payment/risk task: the Scaled day-15 path auto-suspends without human chasing.
    assert not any(t["mandate"] == "MUST_PROTECT" for t in smallco), \
        "SmallCo is Scaled; day_15_plus must auto-suspend with no Protect/payment task"
    assert not any("payment" in t["trigger"].lower() for t in smallco), \
        "SmallCo must not get a payment-chasing task"


def test_must_use_creates_adoption_intervention():
    account = {
        "hubspot": {"name": "Adoption Gap", "segment": "Strategic", "contacts": []},
        "usage": {"days_since_last_visit": 21, "active_users_pct": 45,
                   "key_feature_adoption_pct": 40},
        "onboarding": {}, "churn": {}, "zendesk": {}, "stripe": {},
    }
    tasks, _ = orchestrate.evaluate("au1-997", account)
    adoption = [task for task in tasks if task["mandate"] == "MUST_USE"]
    assert adoption
    assert adoption[0]["priority"] == 5
    assert "active_users_45%" in adoption[0]["evidence"]["drivers"]
    assert adoption[0]["task_id"]
    assert adoption[0]["due_on"]


def test_protect_suppresses_duplicate_adoption_and_hygiene_tasks():
    account = {
        "hubspot": {"name": "At Risk Adoption Gap", "segment": "Strategic", "contacts": []},
        "usage": {"days_since_last_visit": 300},
        "onboarding": {}, "churn": {"churn_status": "Churned"}, "zendesk": {}, "stripe": {},
    }
    tasks, _ = orchestrate.evaluate("au1-996", account)
    assert [task["mandate"] for task in tasks] == ["MUST_PROTECT"]


def test_scaled_risk_does_not_create_duplicate_use_tasks():
    account = {
        "hubspot": {"name": "Scaled Risk Gap", "segment": "Scaled", "contacts": []},
        "usage": {"days_since_last_visit": 300},
        "onboarding": {}, "churn": {"churn_status": "Churned"}, "zendesk": {}, "stripe": {},
    }
    tasks, _ = orchestrate.evaluate("au1-995", account)
    assert tasks == []


def test_churned_account_is_recovery_not_predictive_risk():
    account = {
        "hubspot": {"name": "Already Churned", "segment": "Strategic", "contacts": []},
        "usage": {}, "onboarding": {}, "churn": {"churn_status": "Churned"},
        "zendesk": {}, "stripe": {},
    }
    tasks, _ = orchestrate.evaluate("au1-994", account)
    assert tasks[0]["trigger"] == "Churned account recovery (today)"
    assert tasks[0]["priority"] == 2
    assert "Confirm the account lifecycle" in tasks[0]["recommended_action"]
    assert tasks[0]["draft_type"] == "internal_recovery"
    assert tasks[0]["draft_message"].startswith("Internal recovery note:")


def test_hubspot_churned_lifecycle_is_recovery_when_churn_source_is_unavailable():
    account = {
        "hubspot": {"name": "Lifecycle Churned", "segment": "Strategic",
                    "lifecycle_stage": "Churned Customer", "contacts": []},
        "usage": {"days_since_last_visit": 300}, "onboarding": {},
        "churn": {"_source": "not_connected"}, "zendesk": {}, "stripe": {},
    }

    tasks, _ = orchestrate.evaluate("au1-994b", account)

    assert tasks[0]["priority"] == 2
    assert tasks[0]["trigger"] == "Churned account recovery (today)"
    assert "HubSpot lifecycle=Churned Customer" in tasks[0]["evidence"]["drivers"]
    assert tasks[0]["draft_type"] == "internal_recovery"


def test_scaled_stale_usage_stays_automated():
    account = {
        "hubspot": {"name": "Scaled Adoption", "segment": "Scaled", "contacts": []},
        "usage": {"days_since_last_visit": 47}, "onboarding": {}, "churn": {},
        "zendesk": {}, "stripe": {},
    }
    tasks, _ = orchestrate.evaluate("au1-993", account)
    assert not any(task["mandate"] == "MUST_USE" for task in tasks)


def test_scaled_churn_status_and_pendo_advisory_stay_automated():
    account = {
        "hubspot": {"name": "Scaled Signals", "segment": "Scaled", "contacts": []},
        "usage": {"days_since_last_visit": 900, "pendo_risk_score": "High"},
        "onboarding": {}, "churn": {"churn_status": "Churned"},
        "zendesk": {}, "stripe": {},
    }
    tasks, _ = orchestrate.evaluate("au1-991", account)
    assert tasks == []


def test_judge_accepts_scaled_churn_recovery_priority():
    account = {
        "hubspot": {"name": "Scaled Churned", "segment": "Scaled", "contacts": []},
        "usage": {}, "onboarding": {}, "churn": {"churn_status": "Churned"},
        "zendesk": {}, "stripe": {},
    }
    tasks, suppressed = orchestrate.evaluate("au1-992", account)
    from playbook_judge import judge
    result = judge(tasks, {"au1-992": account}, suppressed)
    assert result["verdict"] == "PASS", result["violations"]


def test_payment_automation_contract_routes_without_mutation():
    strategic = {"hubspot": {"name": "Strategic Billing", "segment": "Strategic", "arr_usd": 200000},
                 "stripe": {"dunning_stage": "day_15_plus", "days_past_due": 16}}
    scaled = {"hubspot": {"name": "Scaled Billing", "segment": "Scaled", "arr_usd": 20000},
              "stripe": {"dunning_stage": "day_15_plus", "days_past_due": 16}}
    early = {"hubspot": {"name": "Early Billing", "segment": "Strategic", "arr_usd": 200000},
             "stripe": {"dunning_stage": "day_1_14", "days_past_due": 4}}
    assert orchestrate.payment_automation_status("au1-1", strategic)["workflow"] == "payment_risk_escalation"
    assert orchestrate.payment_automation_status("au1-2", scaled)["workflow"] == "auto_suspend"
    assert orchestrate.payment_automation_status("au1-3", early)["workflow"] == "automated_dunning"


def test_umbrella_spike_suppressed_and_stays_expansion():
    """Umbrella's ticket spike is on a TEST instance -> suppressed; it must remain an
    expansion opportunity, NOT a false-positive risk."""
    result = _result()
    suppressed = [s for s in result["suppressed"] if s["account"] == "Umbrella Ltd"]
    assert suppressed, "Umbrella's test-instance ticket spike must be suppressed"

    umbrella_tasks = [t for t in result["tasks"] if t["account"] == "Umbrella Ltd"]
    assert any(t["mandate"] == "MUST_EXPAND" for t in umbrella_tasks), "Umbrella should be expansion"
    assert not any(t["priority"] == 1 for t in umbrella_tasks), "Umbrella must NOT be a Priority-1 risk"


def test_priority_ordering():
    """Queue must be sorted by priority ascending (P1 first)."""
    priorities = [t["priority"] for t in _result()["tasks"]]
    assert priorities == sorted(priorities)


def test_churned_account_does_not_receive_expansion_task():
    account = {
        "hubspot": {"name": "Churned Strategic", "segment": "Strategic",
                    "renewal_date": "2026-12-01", "contacts": []},
        "usage": {"license_utilization_pct": 95, "days_since_last_visit": 2},
        "churn": {"churn_status": "Churned", "computed": False},
        "zendesk": {}, "stripe": {},
    }
    tasks, _ = orchestrate.evaluate("au1-999", account)
    assert not any(t["mandate"] == "MUST_EXPAND" for t in tasks)


def test_overdue_renewal_is_not_labelled_t60():
    account = {
        "hubspot": {"name": "Overdue Strategic", "segment": "Strategic",
                    "renewal_date": "2026-09-01", "contacts": []},
        "usage": {}, "churn": {}, "zendesk": {}, "stripe": {},
    }
    tasks, _ = orchestrate.evaluate("au1-998", account)
    renewal_tasks = [t for t in tasks if "renewal" in t["trigger"].lower()]
    assert renewal_tasks
    assert renewal_tasks[0]["trigger"] == "Overdue renewal escalation"


def test_ticket_spike_requires_primary_instance():
    """A raw spike driven by a non-primary instance must not fire."""
    primary = {"p"}
    zd = {"tickets_last_7d": 18, "tickets_prev_7d": 4, "by_instance": {"p": 2, "d": 16}}
    fired, ev = ticket_spike_on_primary(zd, primary)
    assert ev["raw_spike"] is True
    assert fired is False, "spike on non-primary instance must be suppressed"


def test_stripe_refuses_sk_live_key():
    """Security guard: the Stripe adapter must REFUSE a live secret key (sk_live_)
    and instruct the operator to use a restricted read-only key (rk_...)."""
    from adapters import config, sources

    prev_key = os.environ.get("STRIPE_KEY")
    prev_use = os.environ.get("CS_USE_LIVE")
    prev_override = os.environ.pop("CS_ALLOW_STRIPE_SECRET_KEY", None)  # ignore ambient .env
    os.environ["STRIPE_KEY"] = "sk_live_dummy_should_be_refused"
    os.environ["CS_USE_LIVE"] = "1"
    try:
        # live() only checks presence + master switch; the refusal happens at call time.
        assert sources.STRIPE.live() is True
        try:
            sources.STRIPE.payment("au1-12345")
        except config.SourceError as exc:
            assert "sk_live_" in str(exc), f"guard message should name sk_live_: {exc}"
        else:
            raise AssertionError("Stripe adapter must raise on an sk_live_ key, but did not")
    finally:
        # Restore environment so other tests / real runs are unaffected.
        if prev_key is None:
            os.environ.pop("STRIPE_KEY", None)
        else:
            os.environ["STRIPE_KEY"] = prev_key
        if prev_use is None:
            os.environ.pop("CS_USE_LIVE", None)
        else:
            os.environ["CS_USE_LIVE"] = prev_use
        if prev_override is not None:
            os.environ["CS_ALLOW_STRIPE_SECRET_KEY"] = prev_override


def test_stripe_secret_key_override():
    """CS_ALLOW_STRIPE_SECRET_KEY=1 is an explicit, off-by-default opt-in that lets
    an sk_live_ key past the guard (operator's informed choice, not the default)."""
    from adapters import sources

    prev_key = os.environ.get("STRIPE_KEY")
    prev_override = os.environ.get("CS_ALLOW_STRIPE_SECRET_KEY")
    os.environ["STRIPE_KEY"] = "sk_live_dummy_override"
    os.environ["CS_ALLOW_STRIPE_SECRET_KEY"] = "1"
    try:
        assert sources.STRIPE._guard_key() == "sk_live_dummy_override"
    finally:
        if prev_key is None:
            os.environ.pop("STRIPE_KEY", None)
        else:
            os.environ["STRIPE_KEY"] = prev_key
        if prev_override is None:
            os.environ.pop("CS_ALLOW_STRIPE_SECRET_KEY", None)
        else:
            os.environ["CS_ALLOW_STRIPE_SECRET_KEY"] = prev_override


def test_stripe_classifies_day_15_plus_invoice():
    """Live Stripe invoice aging must drive the No-Chasing Day-15 rule."""
    from adapters import sources

    previous_key = os.environ.get("STRIPE_KEY")
    previous_http_get = sources.config.http_get
    os.environ["STRIPE_KEY"] = "rk_live_test"
    due_date = int(time.time()) - (16 * 86400)

    def fake_get(url, headers, timeout=12):
        if "/customers/search" in url:
            return {"data": [{"id": "cus_test"}]}
        return {"data": [{"due_date": due_date, "amount_due": 12500}]}

    sources.config.http_get = fake_get
    try:
        payment = sources.STRIPE.payment("au1-12345")
        assert payment["past_due_invoices"] == 1
        assert payment["days_past_due"] == 16
        assert payment["dunning_stage"] == "day_15_plus"
        assert payment["amount_due_usd"] == 125.0
    finally:
        sources.config.http_get = previous_http_get
        if previous_key is None:
            os.environ.pop("STRIPE_KEY", None)
        else:
            os.environ["STRIPE_KEY"] = previous_key


def test_churn_live_requires_database_and_target():
    """Churn.live() must stay False until both REDSHIFT_DATABASE and a target
    (workgroup or cluster) are set - no network call should be attempted otherwise."""
    from adapters import sources

    keys = ("REDSHIFT_DATABASE", "REDSHIFT_WORKGROUP", "REDSHIFT_CLUSTER_ID",
            "REDSHIFT_CHURN_TABLE", "REDSHIFT_CHURN_MODE")
    prev = {k: os.environ.pop(k, None) for k in keys}
    try:
        assert sources.CHURN.live() is False, "must be False with no config"
        os.environ["REDSHIFT_DATABASE"] = "analytics"
        assert sources.CHURN.live() is False, "must be False without a workgroup/cluster"
        os.environ["REDSHIFT_WORKGROUP"] = "cs-churn-wg"
        assert sources.CHURN.live() is False, "must be False without an explicit score view"
        os.environ["REDSHIFT_CHURN_TABLE"] = "marts.cs_account_churn_scores"
        assert sources.CHURN.live() is True, "must be True once both are set"
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_churn_uses_uppercase_redshift_account_key():
    """Redshift stores nk_ja_account as uppercase-hyphen, regardless of input case."""
    from adapters import sources

    assert sources.identity.normalise("AU1_5005") == "au1-5005"
    assert sources.identity.normalise("AU1_5005").upper() == "AU1-5005"


def test_playbook_judge_passes_clean_queue():
    """The judge (executable feedback sensor) must PASS the orchestrator's own output,
    since the engine produces a WoW-conformant queue."""
    from playbook_judge import judge
    res = orchestrate.orchestrate()
    accts = orchestrate.load_accounts()
    verdict = judge(res["tasks"], accts, res.get("suppressed"))
    assert verdict["verdict"] == "PASS", f"clean queue should pass, got: {verdict['violations']}"
    # And the engine wires the judge into its result.
    assert res.get("judge", {}).get("verdict") == "PASS"


def test_playbook_judge_catches_violations():
    """Feed the judge deliberately-broken queues; it must flag each rule."""
    from playbook_judge import judge
    accts = orchestrate.load_accounts()
    # Pick a real Scaled account and a real Strategic account from the fixtures.
    scaled = next(a for a in accts.values()
                  if (a.get("hubspot", {}).get("segment") or "Scaled") != "Strategic")
    scaled_name = scaled["hubspot"]["name"]

    broken = [
        # priority ordering wrong (P5 before P1) + bad mandate + no evidence + no draft on P1 risk
        {"priority": 5, "account": scaled_name, "segment": "Scaled",
         "mandate": "MUST_USE", "trigger": "x", "evidence": {"a": 1}},
        {"priority": 1, "account": scaled_name, "segment": "Scaled",
         "mandate": "MUST_PROTECT", "trigger": "Predictive Risk Playbook (24h SLA)",
         "evidence": {}},  # no evidence + no draft_message
        # Scaled account getting a proactive expansion task (routing violation)
        {"priority": 3, "account": scaled_name, "segment": "Scaled",
         "mandate": "MUST_EXPAND", "trigger": "Expansion trigger", "evidence": {"x": 1}},
    ]
    v = judge(broken, accts)
    rules = {x["rule"] for x in v["violations"]}
    assert v["verdict"] == "NEEDS_CHANGES"
    assert "priority_ordering" in rules, rules
    assert "evidence_grounding" in rules, rules
    assert "draft_presence" in rules, rules
    assert "segment_routing" in rules, rules


def test_grounding_gate_flags_fabricated_number():
    """The grounding gate must warn when an answer cites a churn % not in evidence."""
    gate = PLUGIN / "hooks" / "scripts" / "grounding-gate.py"
    answer = "Northwind churn is 99% and ARR is 999999."  # neither in fixtures
    proc = subprocess.run(
        [sys.executable, str(gate)], input=answer, capture_output=True, text=True,
        env={**os.environ},
    )
    assert "WARNING" in proc.stderr
    assert "99" in proc.stderr or "999999" in proc.stderr


def test_grounding_gate_passes_real_numbers():
    """Grounded figures (72 == churn 0.72, 480000 ARR) must pass clean."""
    gate = PLUGIN / "hooks" / "scripts" / "grounding-gate.py"
    answer = "Northwind churn score maps to 72% risk; ARR is 480000."
    proc = subprocess.run(
        [sys.executable, str(gate)], input=answer, capture_output=True, text=True,
        env={**os.environ},
    )
    assert "OK" in proc.stderr, proc.stderr


def test_agent_answer_validator_blocks_unsupported_claims():
    """The runtime harness must block an answer with unsupported numbers."""
    from agent_runner import validate_answer

    queue = {"tasks": [{"account": "Northwind Traders", "priority": 1}],
             "judge": {"verdict": "PASS"}}
    accounts = {"au1-1": {"hubspot": {"name": "Northwind Traders"}, "churn": {"ml_churn_score": 0.72}}}
    findings = validate_answer("Northwind Traders has 99% churn risk.", queue, accounts, True)
    assert any("unsupported numeric claims" in finding for finding in findings)


def test_mcp_fixture_data_requires_explicit_opt_in(monkeypatch):
    """MCP must fail closed in live mode instead of silently serving fixtures."""
    import importlib.util

    path = PLUGIN / "mcp-servers" / "cs_stack_server.py"
    spec = importlib.util.spec_from_file_location("cs_stack_server_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_ADAPTERS", False)
    monkeypatch.setattr(module, "MCP_MODE", "live")
    try:
        module.tool_churn_get_score({"account_id": "acct_northwind"})
    except RuntimeError as exc:
        assert "fixture fallback" in str(exc)
    else:
        raise AssertionError("MCP live mode must not return fixture data")

    monkeypatch.setattr(module, "MCP_MODE", "fixture")
    value = module.tool_churn_get_score({"account_id": "acct_northwind"})
    assert value["_source"] == "fixture"


def test_hubspot_writeback_defaults_to_dry_run(monkeypatch):
    """HubSpot properties may be inspected, but no mutation occurs by default."""
    from adapters import config, sources

    previous_get = config.http_get
    previous_post = config.http_post
    previous_patch = config.http_patch
    previous_write = os.environ.pop("CS_ALLOW_WRITE", None)
    calls = []

    def fake_get(url, headers, timeout=12):
        if "/properties/companies" in url:
            return {"results": [{"name": name} for name in sources.HUBSPOT.WRITEBACK_PROPERTIES]}
        raise AssertionError(f"unexpected GET: {url}")

    def fake_post(url, headers, body, timeout=12):
        calls.append(("POST", url))
        return {"results": [{"id": "123", "properties": {"name": "Test"}}]}

    def fake_patch(url, headers, body, timeout=12):
        calls.append(("PATCH", url))
        return {}

    config.http_get = fake_get
    config.http_post = fake_post
    config.http_patch = fake_patch
    try:
        result = sources.HUBSPOT.push_cs_data("au1-12345", health_score=80,
                                               risk_status="watch", active_playbook="Test")
        assert result["mode"] == "dry-run"
        assert result["would_write"]["cs_health_score"] == 80
        assert not any(method == "PATCH" or "/crm/v3/properties/companies" in url
                   for method, url in calls)
    finally:
        config.http_get = previous_get
        config.http_post = previous_post
        config.http_patch = previous_patch
        if previous_write is not None:
            os.environ["CS_ALLOW_WRITE"] = previous_write




def test_computed_score_does_not_fire_priority1_rule():
    """A computed (non-ML) churn score >= CHURN_RISK must NOT fire the Priority-1
    Predictive Risk Playbook. Only a genuine Redshift ML score should do that.
    Other risk drivers (Sev-1, Pendo, etc.) can still fire independently."""
    account = {
        "hubspot": {"name": "Computed Risk Co", "segment": "Strategic",
                    "arr_usd": 500000, "contacts": []},
        "churn": {"ml_churn_score": 0.80, "computed": True},  # high but computed
        "zendesk": {}, "usage": {}, "stripe": {}, "onboarding": {},
    }
    tasks, _ = orchestrate.evaluate("au1-computed", account)
    p1 = [t for t in tasks if t["priority"] == 1 and t["mandate"] == "MUST_PROTECT"]
    assert not p1, (
        "A computed (non-ML) score of 0.80 must NOT fire a Priority-1 risk task; "
        f"got tasks: {tasks}"
    )


def test_scaled_computed_score_does_not_fire_exception():
    """A computed score above CHURN_SCALED_EXCEPTION must not escalate a Scaled
    account. Only a genuine Redshift ML score triggers the Scaled exception path."""
    account = {
        "hubspot": {"name": "Scaled Computed Co", "segment": "Scaled", "arr_usd": 20000,
                    "contacts": []},
        "churn": {"ml_churn_score": 0.90, "computed": True},
        "zendesk": {}, "usage": {}, "stripe": {}, "onboarding": {},
    }
    tasks, _ = orchestrate.evaluate("au1-scaled-computed", account)
    protect = [t for t in tasks if t["mandate"] == "MUST_PROTECT"]
    assert not protect, (
        "Scaled account with computed score 0.90 must NOT get a MUST_PROTECT task; "
        f"got: {protect}"
    )


def test_grounding_gate_warns_when_structured_packet_has_no_actions():
    """When the packet has an 'answer' key but empty actions, the gate must warn
    that it is falling back to fixture evidence (live-mode false-positive risk)."""
    import subprocess, sys
    gate = PLUGIN / "hooks" / "scripts" / "grounding-gate.py"
    # Structured packet with empty actions list — triggers the fixture fallback branch.
    packet = '{"answer": "Northwind churn is 72%.", "actions": []}'
    proc = subprocess.run(
        [sys.executable, str(gate)], input=packet, capture_output=True, text=True,
        env={**os.environ},
    )
    assert proc.returncode == 0, "grounding gate must always exit 0"
    assert "falling back to fixture evidence" in proc.stderr, (
        "expected fixture-fallback warning when structured packet has no actions; "
        f"got stderr: {proc.stderr!r}"
    )


if __name__ == "__main__":
    # Lightweight runner if pytest isn't available.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
