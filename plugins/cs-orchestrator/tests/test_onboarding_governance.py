"""Tests for engine.onboarding_governance() — the Rocket Lane implementation/onboarding
governance view (V5 UC3). Source-first: it pulls projects from RocketLane.list_active_projects()
directly and joins them to the account roster by company name for the CSM owner + scoping.
Covers active/stalled counting, the on-time handoff KPI, owner-scoping, and the honest
'not connected' state."""

from __future__ import annotations

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


def _proj(company, status, start, due, health=None):
    return {"company_name": company, "company_id": None, "project_name": company + " Onboarding",
            "status": status, "start_date": start, "due_date": due, "archived": False,
            "owner": "Impl Team", "health": health, "_source": "rocket-lane-live"}


def _projects():
    return [
        _proj("ActiveCo", "In Progress", "2026-09-16", "2026-12-01", "green"),
        _proj("StalledCo", "blocked", "2026-08-01", "2026-09-15", "red"),
        _proj("OnTimeCo", "completed", "2026-08-01", "2026-10-10"),   # today <= due -> on-time
        _proj("LateCo", "completed", "2026-06-01", "2026-08-01"),     # today > due -> late
    ]


def _roster():
    # Full lightweight book rows (what list_all_companies returns): name + owner_id.
    def row(name, owner_id, aid):
        return {"name": name, "account_id": aid, "owner_id": owner_id, "csm_owner": name + " CSM",
                "company_id": aid}
    return [
        row("ActiveCo", "o1", "au1-active"),
        row("StalledCo", "o1", "au1-stalled"),
        row("OnTimeCo", "o2", "au1-ontime"),
        row("LateCo", "o2", "au1-late"),
    ]


def _patch(monkeypatch, rocket_live=True, projects=None):
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", lambda: {})
    monkeypatch.setattr(dataaccess, "live_sources",
                        lambda: (["HubSpot", "Rocket Lane"] if rocket_live else ["HubSpot"]))
    monkeypatch.setattr(sources.ROCKET_LANE, "list_active_projects",
                        lambda *a, **k: (projects if projects is not None else _projects()))
    monkeypatch.setattr(sources.HUBSPOT, "live", lambda: True)
    monkeypatch.setattr(sources.HUBSPOT, "list_all_companies", lambda *a, **k: _roster())
    monkeypatch.setattr(sources.HUBSPOT, "_owner_name", lambda oid: {"o1": "ActiveCo CSM", "o2": "OnTimeCo CSM"}.get(str(oid)))
    monkeypatch.setattr(dataaccess, "_ADAPTERS", True, raising=False)
    monkeypatch.setenv("CS_TODAY", "2026-10-06")
    engine.set_principal(None)
    return engine


def test_governance_counts_active_and_stalled(monkeypatch):
    engine = _patch(monkeypatch)
    g = engine.onboarding_governance()
    assert g["connected"] is True
    # active = In Progress + blocked (the two completed are excluded)
    assert g["active_projects"] == 2
    assert g["stalled_projects"] == 1
    assert {r["name"] for r in g["stalled"]} == {"StalledCo"}
    # Handoff KPI: 1 on-time + 1 late = 50%.
    assert g["handoff_completed"] == 2
    assert g["handoff_on_time_pct"] == 50
    assert g["handoff_on_time_target_pct"] == 90
    assert isinstance(g["avg_days_in_onboarding"], int)
    # Owner joined from the roster by company name.
    active = next(r for r in g["projects"] if r["name"] == "ActiveCo")
    assert active["owner"] == "ActiveCo CSM" and active["matched_account"] is True


def test_governance_not_connected_is_honest(monkeypatch):
    engine = _patch(monkeypatch, rocket_live=False)
    g = engine.onboarding_governance()
    assert g["connected"] is False
    assert g["active_projects"] == 0
    assert g["note"] and "not connected" in g["note"].lower()


def test_governance_owner_scoped(monkeypatch):
    engine = _patch(monkeypatch)
    # CSM owning o1 sees only ActiveCo + StalledCo projects (joined by company name).
    engine.set_principal({"email": "a@x.com", "name": "A", "role": "csm", "owner_id": "o1"})
    g = engine.onboarding_governance()
    names = {r["name"] for r in g["projects"]} | {r["name"] for r in g["stalled"]}
    assert names <= {"ActiveCo", "StalledCo"}
    assert "OnTimeCo" not in names and "LateCo" not in names
    engine.set_principal(None)


def test_governance_handoff_kpi_none_when_no_completions(monkeypatch):
    engine = _patch(monkeypatch, projects=[_proj("ActiveOnly", "In Progress", "2026-10-01", "2026-12-01")])
    g = engine.onboarding_governance()
    assert g["handoff_completed"] == 0
    assert g["handoff_on_time_pct"] is None
    assert g["active_projects"] == 1


def test_governance_admin_sees_unmatched_projects(monkeypatch):
    # A project whose company is not in the roster still shows for an admin (owner '-').
    engine = _patch(monkeypatch, projects=[_proj("GhostCo", "In Progress", "2026-09-01", "2026-12-01")])
    g = engine.onboarding_governance()
    assert g["active_projects"] == 1
    row = g["projects"][0]
    assert row["name"] == "GhostCo" and row["matched_account"] is False and row["owner"] == "-"
