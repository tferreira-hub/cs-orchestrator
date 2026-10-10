"""Lifecycle-stage model (Customer 360 §6) — deterministic, evidence-driven.

engine.lifecycle_state() derives WHERE an account sits in its lifecycle from live signals,
as a state machine: Implementation → Onboarding → Adoption → Value Realisation → Mature,
plus Renewal (T-90 window) and the reverse states At Risk / Churned. This is distinct from
the raw HubSpot lifecycle_stage (Customer/Churned) and from the Account Status filter
(Active/Churned). These tests pin the ordering/precedence and the honest paths.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("CS_TODAY", "2026-09-23")


def _acct(**kw):
    base = {"hubspot": {"name": "X", "segment": "Strategic"}, "zendesk": {}, "usage": {},
            "churn": {}, "stripe": {}, "jiminny": {}, "onboarding": {}, "metrics": {}, "roi_ai": {}}
    for k, v in kw.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


def test_churned_is_terminal():
    import engine
    s = engine.lifecycle_state(_acct(churn={"churn_status": "churned"}))
    assert s["stage"] == "Churned" and s["flow"] == "exited"


def test_red_health_is_at_risk():
    import engine
    # A combination of hard negatives drives health red (not churned, so not terminal) ->
    # At Risk (reverse flow). A bare ML score of 0.9 alone is only amber by design.
    s = engine.lifecycle_state(_acct(
        churn={"ml_churn_score": 0.9, "churn_status": "Not churned"},
        zendesk={"sev1_open": 1, "csat_30d": 20},
        usage={"days_since_last_visit": 60, "pendo_risk_score": "high"},
        jiminny={"sentiment": "negative"}))
    assert s["stage"] == "At Risk" and s["flow"] == "reverse"


def test_onboarding_from_status():
    import engine
    s = engine.lifecycle_state(_acct(onboarding={"status": "in_progress"},
                                     churn={"churn_status": "Not churned"}))
    assert s["stage"] == "Onboarding"


def test_renewal_window_takes_precedence_over_mature():
    import engine
    s = engine.lifecycle_state(_acct(hubspot={"renewal_date": "2026-12-01"},
                                     churn={"churn_status": "Not churned"}))
    assert s["stage"] == "Renewal"
    assert "T-90" in s["reason"] or "renewal" in s["reason"].lower()


def test_mature_or_value_realisation_when_healthy_no_renewal_window():
    import engine
    s = engine.lifecycle_state(_acct(hubspot={"renewal_date": "2027-06-01"},
                                     metrics={"ndr_pct": 115},
                                     churn={"churn_status": "Not churned"}))
    assert s["stage"] in ("Mature", "Value Realisation")


def test_portfolio_exposes_stage_and_mix(monkeypatch):
    """portfolio() rows carry cs_lifecycle_stage and the summary carries a stage mix."""
    import engine
    monkeypatch.setattr(engine, "_batch_metrics_for", lambda ids: {})
    monkeypatch.setattr(engine, "warm_batch_metrics", lambda: 0)
    acct = {
        "au1-1": {"hubspot": {"name": "A", "segment": "Strategic", "arr_usd": 120000,
                              "renewal_date": "2026-12-01", "csm_owner": "O", "csm_owner_id": "o1",
                              "contacts": [], "account_id": "au1-1",
                              "instances": [{"instance_id": "au1-1", "instance_type": "primary"}]},
                  "sources": {"hubspot": "live"}, "zendesk": {}, "usage": {},
                  "churn": {"churn_status": "Not churned"}, "stripe": {}, "jiminny": {},
                  "onboarding": {}, "metrics": {}},
    }
    engine.orchestrate.set_account_provider(lambda: acct)
    engine.set_principal(None)

    class _FakeHS:
        def live(self):
            return False  # no whole-book roster merge; just the enriched slice
    monkeypatch.setattr(engine._src, "HUBSPOT", _FakeHS())

    p = engine.portfolio()
    row = next(r for r in p["accounts"] if r["account_id"] == "au1-1")
    assert row["cs_lifecycle_stage"] == "Renewal"           # T-90 window
    mix = {s["stage"]: s for s in p["summary"]["lifecycle_stage_mix"]}
    assert "Renewal" in mix and mix["Renewal"]["accounts"] >= 1
    engine.orchestrate.set_account_provider(engine.orchestrate._load)
