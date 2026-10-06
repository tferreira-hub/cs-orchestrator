"""Tests for engine.onboarding_governance() — the Rocket Lane implementation/onboarding
governance view (V5 UC3). Covers active-project counting, stalled-before-handoff alerts,
the on-time handoff KPI, owner-scoping, and the honest 'not connected' state."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _clear_principal():
    yield
    import engine
    engine.set_principal(None)


def _acct(aid, owner_id, name, onboarding, csm="Owner"):
    return {aid: {
        "hubspot": {"name": name, "segment": "Strategic", "arr_usd": 50000,
                    "csm_owner": csm, "csm_owner_id": owner_id, "contacts": [],
                    "account_id": aid,
                    "instances": [{"instance_id": aid, "instance_type": "primary"}]},
        "sources": {"hubspot": "live", "onboarding": "live"},
        "zendesk": {}, "usage": {}, "churn": {}, "stripe": {}, "jiminny": {},
        "onboarding": onboarding, "metrics": {},
    }}


def _book():
    data = {}
    # Active + healthy, in onboarding 20 days.
    data.update(_acct("au1-active", "o1", "ActiveCo", {
        "_matched": True, "status": "In Progress", "health": "green",
        "project_name": "ActiveCo Onboarding", "start_date": "2026-09-16",
        "due_date": "2026-12-01", "archived": False}))
    # Stalled (red health), should surface as stalled-before-handoff.
    data.update(_acct("au1-stalled", "o1", "StalledCo", {
        "_matched": True, "status": "blocked", "health": "red",
        "project_name": "StalledCo Onboarding", "start_date": "2026-08-01",
        "due_date": "2026-09-15", "archived": False}))
    # Completed on time (today 2026-10-06 <= due 2026-10-10).
    data.update(_acct("au1-ontime", "o2", "OnTimeCo", {
        "_matched": True, "status": "completed", "health": "green",
        "project_name": "OnTimeCo", "start_date": "2026-08-01",
        "due_date": "2026-10-10", "archived": True}))
    # Completed late (today > due) -> counts against the handoff KPI.
    data.update(_acct("au1-late", "o2", "LateCo", {
        "_matched": True, "status": "completed", "health": "green",
        "project_name": "LateCo", "start_date": "2026-06-01",
        "due_date": "2026-08-01", "archived": True}))
    # No matched Rocket Lane project -> contributes nothing (data-gap).
    data.update(_acct("au1-nomatch", "o3", "NoMatchCo", {"_matched": False, "status": None}))
    return data


def _patch(monkeypatch, rocket_live=True):
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _book)
    monkeypatch.setattr(dataaccess, "live_sources",
                        lambda: (["HubSpot", "Rocket Lane"] if rocket_live else ["HubSpot"]))
    monkeypatch.setenv("CS_TODAY", "2026-10-06")
    engine.set_principal(None)
    return engine


def test_governance_counts_active_and_stalled(monkeypatch):
    engine = _patch(monkeypatch)
    g = engine.onboarding_governance()
    assert g["connected"] is True
    # active = In Progress + blocked (not the two completed, not the unmatched)
    assert g["active_projects"] == 2
    assert g["stalled_projects"] == 1
    assert {r["name"] for r in g["stalled"]} == {"StalledCo"}
    # Handoff KPI: 1 on-time + 1 late = 50%.
    assert g["handoff_completed"] == 2
    assert g["handoff_on_time_pct"] == 50
    assert g["handoff_on_time_target_pct"] == 90
    # avg days-in-onboarding only over active projects, and is a real number
    assert isinstance(g["avg_days_in_onboarding"], int)


def test_governance_not_connected_is_honest(monkeypatch):
    engine = _patch(monkeypatch, rocket_live=False)
    g = engine.onboarding_governance()
    assert g["connected"] is False
    assert g["note"] and "not connected" in g["note"].lower()


def test_governance_owner_scoped(monkeypatch):
    engine = _patch(monkeypatch)
    # CSM owning only o1's accounts sees just those two projects (active + stalled).
    engine.set_principal({"email": "a@x.com", "name": "A", "role": "csm", "owner_id": "o1"})
    g = engine.onboarding_governance()
    names = {r["name"] for r in g["projects"]} | {r["name"] for r in g["stalled"]}
    assert names <= {"ActiveCo", "StalledCo"}
    assert "OnTimeCo" not in names and "LateCo" not in names
    engine.set_principal(None)


def test_governance_handoff_kpi_none_when_no_completions(monkeypatch):
    import engine, dataaccess
    # Only an active project, no completed ones -> KPI denominator 0 -> None (data-gap).
    only_active = _acct("au1-a", "o1", "ActiveOnly", {
        "_matched": True, "status": "In Progress", "health": "green",
        "start_date": "2026-10-01", "due_date": "2026-12-01", "archived": False})
    monkeypatch.setattr(dataaccess, "all_accounts", lambda: only_active)
    monkeypatch.setattr(dataaccess, "live_sources", lambda: ["HubSpot", "Rocket Lane"])
    monkeypatch.setenv("CS_TODAY", "2026-10-06")
    engine.set_principal(None)
    g = engine.onboarding_governance()
    assert g["handoff_completed"] == 0
    assert g["handoff_on_time_pct"] is None
    engine.set_principal(None)
