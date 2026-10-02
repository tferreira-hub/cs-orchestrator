"""Tests for the Payment Risk Report (engine.payment_risk_report).

Verifies the 4 pages are bucketed from live Stripe dunning + configurable thresholds, and
that each row's billing contact, Stripe link and derived JobAdder admin link are correct.
No pasted data — accounts are injected and the engine derives everything.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLATFORM))


def _acct(aid, name, days_past_due, stage, *, cid="cus_X", csm="Clair Davies",
          finance_email=None, stripe_email=None):
    contacts = []
    if finance_email:
        contacts.append({"role": "Finance Contact", "email": finance_email})
    return {
        "hubspot": {"name": name, "csm_owner": csm, "contacts": contacts},
        "stripe": {"days_past_due": days_past_due, "dunning_stage": stage,
                   "amount_due_usd": 100.0, "customer_id": cid,
                   "customer_email": stripe_email},
        "sources": {"hubspot": "live", "stripe": "live"},
    }


def _run(monkeypatch, accounts, env=None):
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", lambda: accounts)
    monkeypatch.setattr(dataaccess, "live_sources", lambda: ["HubSpot", "Stripe"])
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    engine.set_principal(None)  # admin / open mode -> whole book
    return engine.payment_risk_report()


def test_payment_failed_lists_all_past_due(monkeypatch):
    accts = {
        "au5-402271": _acct("au5-402271", "Alex James", 3, "day_1_14"),
        "au1-3242": _acct("au1-3242", "Edmen", 20, "day_15_plus"),
        "au6-2701": _acct("au6-2701", "Lumia Care", None, "none"),  # not past due -> excluded
    }
    r = _run(monkeypatch, accts)
    names = {row["name"] for row in r["pages"]["payment_failed"]}
    assert names == {"Alex James", "Edmen"}
    assert r["counts"]["payment_failed"] == 2


def test_access_risk_buckets_by_threshold(monkeypatch):
    # Default access-suspend threshold is 21 days.
    accts = {
        "au5-1": _acct("au5-1", "Near7", 16, "day_15_plus"),   # 21-16=5 -> 7d bucket
        "au5-2": _acct("au5-2", "Near14", 9, "day_1_14"),      # 21-9=12 -> 14d bucket
        "au5-3": _acct("au5-3", "Far", 2, "day_1_14"),         # 21-2=19 -> neither
    }
    r = _run(monkeypatch, accts)
    assert {x["name"] for x in r["pages"]["access_risk_7d"]} == {"Near7"}
    assert {x["name"] for x in r["pages"]["access_risk_14d"]} == {"Near14"}


def test_cancellation_risk_within_14_days_of_threshold(monkeypatch):
    # Default cancel threshold is 30 days; within 14 -> days_past_due >= 16.
    accts = {
        "au5-1": _acct("au5-1", "Cancelish", 20, "day_15_plus"),  # 30-20=10 -> in
        "au5-2": _acct("au5-2", "Early", 5, "day_1_14"),          # 30-5=25 -> out
    }
    r = _run(monkeypatch, accts)
    assert {x["name"] for x in r["pages"]["cancellation_risk"]} == {"Cancelish"}


def test_thresholds_are_configurable(monkeypatch):
    accts = {"au5-1": _acct("au5-1", "A", 5, "day_1_14")}
    # Set suspend threshold to 10 -> 10-5=5 -> 7d bucket.
    r = _run(monkeypatch, accts, env={"CS_ACCESS_SUSPEND_DAYS": "10"})
    assert {x["name"] for x in r["pages"]["access_risk_7d"]} == {"A"}
    assert r["thresholds"]["access_suspend_days"] == 10


def test_admin_url_derivation_per_region(monkeypatch):
    accts = {
        "au5-402271": _acct("au5-402271", "AU5 Co", 3, "day_1_14"),
        "eu2-2148": _acct("eu2-2148", "EU2 Co", 3, "day_1_14"),
    }
    r = _run(monkeypatch, accts)
    by = {row["name"]: row for row in r["pages"]["payment_failed"]}
    assert by["AU5 Co"]["admin_url"] == "https://au5admin.jobadder.com/accounts/402271"
    assert by["EU2 Co"]["admin_url"] == "https://eu2admin.jobadder.com/accounts/2148"


def test_stripe_url_and_billing_contact_precedence(monkeypatch):
    accts = {
        # Finance Contact wins over stripe email.
        "au5-1": _acct("au5-1", "HasFinance", 3, "day_1_14",
                       cid="cus_ABC", finance_email="finance@co.com", stripe_email="billing@stripe.com"),
        # No finance role -> falls back to stripe email.
        "au5-2": _acct("au5-2", "StripeOnly", 3, "day_1_14",
                       cid="cus_DEF", stripe_email="billing@stripe.com"),
    }
    r = _run(monkeypatch, accts)
    by = {row["name"]: row for row in r["pages"]["payment_failed"]}
    assert by["HasFinance"]["billing_contact"] == "finance@co.com"
    assert by["HasFinance"]["stripe_url"] == "https://dashboard.stripe.com/customers/cus_ABC"
    assert by["StripeOnly"]["billing_contact"] == "billing@stripe.com"


def test_owner_scoping_limits_report_to_csm_book(monkeypatch):
    import engine, dataaccess
    mine = _acct("au5-1", "Mine", 3, "day_1_14")
    mine["hubspot"]["csm_owner_id"] = "owner-1"
    theirs = _acct("au5-2", "Theirs", 3, "day_1_14")
    theirs["hubspot"]["csm_owner_id"] = "owner-2"
    monkeypatch.setattr(dataaccess, "all_accounts", lambda: {"au5-1": mine, "au5-2": theirs})
    monkeypatch.setattr(dataaccess, "live_sources", lambda: ["HubSpot", "Stripe"])
    engine.set_principal({"role": "csm", "owner_id": "owner-1"})
    try:
        r = engine.payment_risk_report()
        assert {x["name"] for x in r["pages"]["payment_failed"]} == {"Mine"}
    finally:
        engine.set_principal(None)


def test_honest_empty_when_no_past_due(monkeypatch):
    accts = {"au5-1": _acct("au5-1", "Fine", None, "none")}
    r = _run(monkeypatch, accts)
    assert r["counts"] == {"payment_failed": 0, "access_risk_7d": 0,
                           "access_risk_14d": 0, "cancellation_risk": 0}
    assert r["stripe_connected"] is True
