"""Tests for the Payment Risk Report (engine.payment_risk_report).

The report now sources failed/past-due payments DIRECTLY from Stripe
(Stripe.list_payment_problems) and joins to the account roster for billing contact + CSM.
These tests mock the Stripe problem list and the roster, then assert the 4/5 pages are
bucketed correctly and each row's links are derived right. No pasted data.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLATFORM))


def _problem(ref, days_past_due, *, cid="cus_X", email=None, failed=True, attempts=5):
    stage = "day_15_plus" if (days_past_due or 0) >= 15 else "day_1_14"
    return {"account_ref": ref, "customer_id": cid, "customer_email": email,
            "amount_due_usd": 174.90, "days_past_due": days_past_due,
            "dunning_stage": stage, "failed_attempts": attempts,
            "past_due_invoices": 1, "payment_failed": failed, "_source": "stripe-live"}


def _roster_acct(name, csm="Clair Davies", finance_email=None, owner_id=None):
    contacts = [{"role": "Finance Contact", "email": finance_email}] if finance_email else []
    hs = {"name": name, "csm_owner": csm, "contacts": contacts}
    if owner_id:
        hs["csm_owner_id"] = owner_id
    return {"hubspot": hs, "sources": {"hubspot": "live"}}


def _run(monkeypatch, problems, roster=None, env=None, principal=None):
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", lambda: (roster or {}))
    monkeypatch.setattr(dataaccess, "live_sources", lambda: ["HubSpot", "Stripe"])
    monkeypatch.setattr(sources.STRIPE, "list_payment_problems", lambda *a, **k: problems)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    engine.set_principal(principal)
    try:
        return engine.payment_risk_report()
    finally:
        engine.set_principal(None)


def test_payment_failed_lists_all_problems(monkeypatch):
    problems = [_problem("au5-402271", 30), _problem("au1-3242", 2, failed=False, attempts=0)]
    roster = {"au5-402271": _roster_acct("Alex James"), "au1-3242": _roster_acct("Edmen")}
    r = _run(monkeypatch, problems, roster)
    assert {x["name"] for x in r["pages"]["payment_failed"]} == {"Alex James", "Edmen"}
    assert r["counts"]["payment_failed"] == 2


def test_access_risk_buckets_by_threshold(monkeypatch):
    # Default access-suspend threshold is 21 days.
    problems = [_problem("au5-1", 16), _problem("au5-2", 9), _problem("au5-3", 2)]
    roster = {"au5-1": _roster_acct("Near7"), "au5-2": _roster_acct("Near14"),
              "au5-3": _roster_acct("Far")}
    r = _run(monkeypatch, problems, roster)
    assert {x["name"] for x in r["pages"]["access_risk_7d"]} == {"Near7"}   # 21-16=5
    assert {x["name"] for x in r["pages"]["access_risk_14d"]} == {"Near14"}  # 21-9=12


def test_cancellation_risk_split_7d_14d(monkeypatch):
    # Default cancel threshold is 30 days.
    problems = [_problem("au5-1", 25), _problem("au5-2", 20), _problem("au5-3", 5)]
    roster = {"au5-1": _roster_acct("Cancel7"), "au5-2": _roster_acct("Cancel14"),
              "au5-3": _roster_acct("Early")}
    r = _run(monkeypatch, problems, roster)
    assert {x["name"] for x in r["pages"]["cancellation_risk_7d"]} == {"Cancel7"}    # 30-25=5
    assert {x["name"] for x in r["pages"]["cancellation_risk_14d"]} == {"Cancel14"}  # 30-20=10


def test_thresholds_are_configurable(monkeypatch):
    problems = [_problem("au5-1", 5)]
    roster = {"au5-1": _roster_acct("A")}
    r = _run(monkeypatch, problems, roster, env={"CS_ACCESS_SUSPEND_DAYS": "10"})  # 10-5=5
    assert {x["name"] for x in r["pages"]["access_risk_7d"]} == {"A"}
    assert r["thresholds"]["access_suspend_days"] == 10


def test_admin_url_and_stripe_url_derivation(monkeypatch):
    problems = [_problem("au5-402271", 3, cid="cus_ABC"), _problem("eu2-2148", 3, cid="cus_DEF")]
    roster = {"au5-402271": _roster_acct("AU5 Co"), "eu2-2148": _roster_acct("EU2 Co")}
    r = _run(monkeypatch, problems, roster)
    by = {x["name"]: x for x in r["pages"]["payment_failed"]}
    assert by["AU5 Co"]["admin_url"] == "https://au5admin.jobadder.com/accounts/402271"
    assert by["EU2 Co"]["admin_url"] == "https://eu2admin.jobadder.com/accounts/2148"
    assert by["AU5 Co"]["stripe_url"] == "https://dashboard.stripe.com/customers/cus_ABC"


def test_billing_contact_precedence(monkeypatch):
    problems = [_problem("au5-1", 3, email="billing@stripe.com"),
                _problem("au5-2", 3, email="billing@stripe.com")]
    roster = {"au5-1": _roster_acct("HasFinance", finance_email="finance@co.com"),
              "au5-2": _roster_acct("StripeOnly")}
    r = _run(monkeypatch, problems, roster)
    by = {x["name"]: x for x in r["pages"]["payment_failed"]}
    assert by["HasFinance"]["billing_contact"] == "finance@co.com"   # Finance Contact wins
    assert by["StripeOnly"]["billing_contact"] == "billing@stripe.com"  # falls back to Stripe email


def test_owner_scoping_limits_report_to_csm_book(monkeypatch):
    problems = [_problem("au5-1", 3), _problem("au5-2", 3)]
    roster = {"au5-1": _roster_acct("Mine", owner_id="owner-1"),
              "au5-2": _roster_acct("Theirs", owner_id="owner-2")}
    r = _run(monkeypatch, problems, roster, principal={"role": "csm", "owner_id": "owner-1"})
    assert {x["name"] for x in r["pages"]["payment_failed"]} == {"Mine"}


def test_honest_empty_when_no_problems(monkeypatch):
    r = _run(monkeypatch, [], {})
    assert r["counts"] == {"payment_failed": 0, "access_risk_7d": 0, "access_risk_14d": 0,
                           "cancellation_risk_7d": 0, "cancellation_risk_14d": 0}
    assert r["stripe_connected"] is True
