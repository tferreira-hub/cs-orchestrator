"""'This Quarter' KPI scorecard for the personal Dashboard.

engine.quarter_scorecard() reports progress against the CS team's quarterly goals from
LIVE data, owner-scoped, with each KPI as {label, value, unit, target, computable, note}.
It must be honest: a real value where computable, and computable=False / value=None with a
note where the underlying change-event is not captured (never a fabricated number).
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    import engine
    engine.set_principal(None)
    yield
    engine.set_principal(None)


def test_quarter_bounds():
    import engine
    assert engine._quarter_bounds(date(2026, 1, 15)) == (date(2026, 1, 1), "Q1 2026")
    assert engine._quarter_bounds(date(2026, 8, 14)) == (date(2026, 7, 1), "Q3 2026")
    assert engine._quarter_bounds(date(2026, 10, 9)) == (date(2026, 10, 1), "Q4 2026")
    assert engine._quarter_bounds(date(2026, 12, 31)) == (date(2026, 10, 1), "Q4 2026")


def _acct(aid, **hs):
    base = {"name": aid, "segment": "Strategic", "arr_usd": 50000, "account_id": aid,
            "csm_owner": "O", "csm_owner_id": "o1", "contacts": [],
            "instances": [{"instance_id": aid, "instance_type": "primary"}]}
    base.update(hs)
    return {aid: {"hubspot": base, "sources": {"hubspot": "live"}, "zendesk": {}, "usage": {},
                  "churn": {}, "stripe": {}, "jiminny": {}, "onboarding": {}, "metrics": {}}}


def _setup(monkeypatch, accounts):
    import engine
    monkeypatch.setenv("CS_TODAY", "2026-10-09")  # Q4 2026
    monkeypatch.delenv("CS_QUARTER_TARGETS", raising=False)
    engine.orchestrate.set_account_provider(lambda: accounts)
    engine.set_principal(None)
    # Isolate from live warehouse / HubSpot / F2F so the test is deterministic.
    monkeypatch.setattr(engine, "_retention_metrics", lambda *a, **k: {"ndr_pct": 108.0})
    monkeypatch.setattr(engine, "_retention_accounts", lambda: accounts)
    monkeypatch.setattr(engine.orchestrate, "orchestrate", lambda: {"tasks": []})
    monkeypatch.setattr(engine, "last_f2f", lambda aid: None)
    monkeypatch.setattr(engine, "_health_history_for", lambda aid: [])

    class _HS:
        def live(self):
            return False
    monkeypatch.setattr(engine._src, "HUBSPOT", _HS())
    return engine


def _kpi(sc, label):
    return next(k for k in sc["kpis"] if k["label"] == label)


def test_scorecard_shape_and_quarter(monkeypatch):
    engine = _setup(monkeypatch, _acct("au1-1"))
    sc = engine.quarter_scorecard()
    assert sc["quarter"] == "Q4 2026"
    assert sc["quarter_start"] == "2026-10-01"
    labels = {k["label"] for k in sc["kpis"]}
    assert labels == {"MRR growth", "Churned", "M2M → fixed-term", "Pro upgrades",
                      "Portfolio met with", "Client saves"}


def test_mrr_growth_from_ndr(monkeypatch):
    engine = _setup(monkeypatch, _acct("au1-1"))
    sc = engine.quarter_scorecard()
    mrr = _kpi(sc, "MRR growth")
    assert mrr["computable"] is True
    assert mrr["value"] == 8.0           # NDR 108 - 100
    assert mrr["unit"] == "%"


def test_churned_counts_only_in_quarter(monkeypatch):
    accts = {}
    accts.update(_acct("au1-live"))
    accts.update(_acct("au1-churned-q", lifecycle_stage="Churned Customer", renewal_date="2026-10-05"))
    accts.update(_acct("au1-churned-old", lifecycle_stage="Churned Customer", renewal_date="2025-01-01"))
    engine = _setup(monkeypatch, accts)
    sc = engine.quarter_scorecard()
    churned = _kpi(sc, "Churned")
    assert churned["computable"] is True
    assert churned["value"] == 1         # only the in-quarter churn counts


def test_m2m_fixed_split(monkeypatch):
    accts = {}
    accts.update(_acct("au1-a", subscription_type="Month to Month"))
    accts.update(_acct("au1-b", subscription_type="Annual Upfront"))
    accts.update(_acct("au1-c", subscription_type="Annual Monthly"))
    engine = _setup(monkeypatch, accts)
    sc = engine.quarter_scorecard()
    m = _kpi(sc, "M2M → fixed-term")
    # Headline value is now the in-quarter MOVE count. With no subscription_type history
    # (health history is empty in _setup), the move count is an honest None.
    assert m["value"] is None
    assert m["computable"] is False
    # The current book split is carried in the breakdown: 1 M2M, 2 fixed-term.
    bd = m["breakdown"]
    assert bd["m2m"] == 1
    assert bd["fixed"] == 2
    assert bd["known"] == 3
    assert bd["moved_this_quarter"] is None


def test_m2m_move_counted_from_subscription_history(monkeypatch):
    """A true in-quarter M2M -> fixed-term MOVE is counted when the subscription_type
    history shows M2M earlier in the quarter and fixed-term later (evidence-based, like
    client_saves)."""
    accts = {}
    accts.update(_acct("au1-moved", subscription_type="Annual Upfront"))
    accts.update(_acct("au1-stayed", subscription_type="Month to Month"))
    engine = _setup(monkeypatch, accts)
    # In-quarter history: au1-moved went M2M -> Annual; au1-stayed stayed M2M.
    hist = {
        "au1-moved": [
            {"date": "2026-10-02", "subscription_type": "Month to Month"},
            {"date": "2026-10-08", "subscription_type": "Annual Upfront"},
        ],
        "au1-stayed": [
            {"date": "2026-10-02", "subscription_type": "Month to Month"},
            {"date": "2026-10-08", "subscription_type": "Month to Month"},
        ],
    }
    monkeypatch.setattr(engine, "_health_history_for", lambda aid: hist.get(aid, []))
    sc = engine.quarter_scorecard()
    m = _kpi(sc, "M2M → fixed-term")
    assert m["value"] == 1
    assert m["computable"] is True
    assert m["breakdown"]["moved_this_quarter"] == 1


def test_pro_upgrades_honest_none_when_hubspot_offline(monkeypatch):
    engine = _setup(monkeypatch, _acct("au1-1"))
    sc = engine.quarter_scorecard()
    pro = _kpi(sc, "Pro upgrades")
    assert pro["computable"] is False and pro["value"] is None
    assert pro["note"]


def test_pro_upgrades_precise_count_when_hubspot_live(monkeypatch):
    """When HubSpot is live, Pro upgrades uses the exact in-quarter closed-won count from
    upsell_count_since() rather than inferring from a capped deal list."""
    engine = _setup(monkeypatch, _acct("au1-1"))

    class _HSLive:
        def live(self):
            return True
        def upsell_count_since(self, since_iso):
            assert since_iso == "2026-10-01"   # quarter start
            return 7
    monkeypatch.setattr(engine._src, "HUBSPOT", _HSLive())
    sc = engine.quarter_scorecard()
    pro = _kpi(sc, "Pro upgrades")
    assert pro["computable"] is True
    assert pro["value"] == 7


def test_portfolio_met_breakdown(monkeypatch):
    """'Portfolio met with' carries a meetings/calls/JBRs breakdown counted from in-quarter
    F2F log entries by interaction_type."""
    accts = {}
    accts.update(_acct("au1-a"))
    accts.update(_acct("au1-b"))
    engine = _setup(monkeypatch, accts)
    f2f = {
        "au1-a": [
            {"met_on": "2026-10-03", "interaction_type": "meeting"},
            {"met_on": "2026-10-05", "interaction_type": "jbr"},
        ],
        "au1-b": [
            {"met_on": "2026-10-04", "interaction_type": "call"},
            {"met_on": "2025-09-01", "interaction_type": "meeting"},  # prior quarter, ignored
        ],
    }
    monkeypatch.setattr(engine, "f2f_log_for", lambda aid: f2f.get(aid, []))
    sc = engine.quarter_scorecard()
    m = _kpi(sc, "Portfolio met with")
    bd = m["breakdown"]
    assert bd["meetings"] == 1
    assert bd["calls"] == 1
    assert bd["jbrs"] == 1
    assert bd["touchpoints"] == 3
    assert bd["accounts_met"] == 2       # both accounts met in-quarter
    assert m["value"] == 100.0           # 2 of 2 accounts


def test_targets_from_env(monkeypatch):
    engine = _setup(monkeypatch, _acct("au1-1"))
    monkeypatch.setenv("CS_QUARTER_TARGETS", '{"mrr_growth_pct": 5, "churned": 3}')
    sc = engine.quarter_scorecard()
    assert _kpi(sc, "MRR growth")["target"] == 5
    assert _kpi(sc, "Churned")["target"] == 3
    assert _kpi(sc, "Client saves")["target"] is None   # unset -> no fabricated target
