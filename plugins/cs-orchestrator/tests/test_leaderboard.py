"""Tests for engine.leaderboard() — the V5 team-performance leaderboard. Covers ranking
by completion rate, the weekly target-compliance KPI, target data-gap honesty, and the
owner-scoped privacy projection (CSM sees self named + peers anonymised)."""

from __future__ import annotations

import json
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


def _acct(aid, owner, name, arr=50000):
    return {aid: {
        "hubspot": {"name": name, "segment": "Strategic", "arr_usd": arr,
                    "csm_owner": owner, "csm_owner_id": owner, "contacts": [],
                    "account_id": aid, "renewal_date": None,
                    "instances": [{"instance_id": aid, "instance_type": "primary"}]},
        "sources": {"hubspot": "live"}, "zendesk": {}, "usage": {}, "churn": {},
        "stripe": {}, "jiminny": {}, "onboarding": {}, "metrics": {},
    }}


def _book():
    data = {}
    data.update(_acct("au1-a", "Alice", "Alpha"))
    data.update(_acct("au1-b", "Alice", "Beta"))
    data.update(_acct("au1-c", "Bob", "Gamma"))
    return data


def _setup(monkeypatch):
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _book)
    engine.orchestrate.set_account_provider(_book)
    engine.set_principal(None)
    return engine


def test_leaderboard_ranks_and_compliance(monkeypatch):
    engine = _setup(monkeypatch)
    lb = engine.leaderboard()
    board = lb["leaderboard"]
    # Alice + Bob present, Unassigned excluded; ranks assigned 1..n.
    csms = {r["csm"] for r in board}
    assert "Alice" in csms and "Bob" in csms
    assert [r["rank"] for r in board] == list(range(1, len(board) + 1))
    # Compliance KPI present with the 90% target band.
    assert lb["weekly_target_compliance_target_pct"] == 90
    # No deterministic tasks here means completion rate may be None -> honest data-gap,
    # measurable count reflects only rows with a completion rate.
    assert "measurable_csms" in lb


def test_leaderboard_targets_data_gap_when_unset(monkeypatch):
    engine = _setup(monkeypatch)
    monkeypatch.delenv("CS_CSM_TARGETS", raising=False)
    lb = engine.leaderboard()
    assert lb["targets_configured"] is False
    assert lb["note"] and "not configured" in lb["note"].lower()
    for r in lb["leaderboard"]:
        assert r["outreach_target"] is None   # data-gap, never fabricated


def test_leaderboard_targets_from_env(monkeypatch):
    engine = _setup(monkeypatch)
    monkeypatch.setenv("CS_CSM_TARGETS", json.dumps({"Alice": {"outreach": 15, "completion_pct": 80}}))
    lb = engine.leaderboard()
    alice = next(r for r in lb["leaderboard"] if r["csm"] == "Alice")
    assert alice["outreach_target"] == 15
    assert alice["completion_target_pct"] == 80
    assert lb["targets_configured"] is True


def test_leaderboard_ranking_basis_workload_without_completions(monkeypatch):
    """With no completed tasks anywhere, the board ranks by workload (open+overdue) and
    reports ranking_basis='workload' so the UI can label it honestly."""
    engine = _setup(monkeypatch)
    d = engine.leaderboard()
    assert d["ranking_basis"] == "workload"
    assert [b["rank"] for b in d["leaderboard"]] == list(range(1, len(d["leaderboard"]) + 1))


def test_leaderboard_anonymises_peers_for_csm(monkeypatch):
    engine = _setup(monkeypatch)
    # Alice is the signed-in CSM: her row stays named, Bob is anonymised.
    engine.set_principal({"email": "alice@x.com", "name": "Alice", "role": "csm", "owner_id": "Alice"})
    lb = engine.leaderboard()
    names = [r["csm"] for r in lb["leaderboard"]]
    assert "Alice" in names
    assert "Bob" not in names                       # peer anonymised
    assert any(n.startswith("CSM ") for n in names)
    # Peer book size/ARR hidden.
    peer = next(r for r in lb["leaderboard"] if r["csm"] != "Alice")
    assert peer["accounts"] is None and peer["arr_usd"] is None
    engine.set_principal(None)
