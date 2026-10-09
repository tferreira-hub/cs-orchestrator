"""Tests for the Executive Sponsor F2F cadence engine (V5 UC2):
- engine F2F append-only log (record_f2f / f2f_log_for / last_f2f);
- engine.f2f_cadence() KPI over tier-1 strategic accounts;
- orchestrate RULE_EXEC_F2F_CADENCE fires for a tier-1 strategic account with no in-window
  F2F and stays silent once a recent F2F is logged; judge PASS throughout.
History/log isolated via CS_F2F_LOG_FILE so the engine and the judge's recompute agree."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture()
def f2f_env(tmp_path, monkeypatch):
    """Isolate CS_F2F_LOG_FILE + anchor CS_TODAY; reload engine+orchestrate so both read
    the temp path."""
    log = tmp_path / "f2f.jsonl"
    monkeypatch.setenv("CS_F2F_LOG_FILE", str(log))
    monkeypatch.setenv("CS_TODAY", "2026-10-06")
    import engine, orchestrate
    importlib.reload(engine)
    importlib.reload(orchestrate)
    yield engine, orchestrate
    engine.set_principal(None)


def _tier1(aid="au1-big", arr=500000, sponsor=True):
    contacts = [{"role": "Executive Sponsor", "name": "Eve Exec", "email": "eve@big.com"}] if sponsor else []
    return {aid: {
        "hubspot": {"name": "BigCo", "segment": "Strategic", "arr_usd": arr,
                    "csm_owner": "Owner", "csm_owner_id": "o1", "contacts": contacts,
                    "account_id": aid, "renewal_date": None,
                    "instances": [{"instance_id": aid, "instance_type": "primary"}]},
        "zendesk": {}, "usage": {}, "churn": {}, "stripe": {}, "onboarding": {},
        "sources": {"hubspot": "live"},
    }}


def test_f2f_store_roundtrip(f2f_env):
    engine, _ = f2f_env
    e = engine.record_f2f({"account_id": "au1-big", "met_on": "2026-10-01",
                           "cs_leadership": ["Daniel Hill"], "notes": "QBR"}, {"email": "me@x.com"})
    assert e["f2f_id"]
    assert engine.last_f2f("au1-big") == "2026-10-01"
    assert len(engine.f2f_log_for("au1-big")) == 1


def test_rule_fires_for_tier1_without_f2f_and_judge_passes(f2f_env):
    engine, orchestrate = f2f_env
    import playbook_judge
    importlib.reload(playbook_judge)
    acct = _tier1()["au1-big"]
    tasks, _ = orchestrate.evaluate("au1-big", acct)
    f2f = [t for t in tasks if t["rule_id"] == orchestrate.RULE_EXEC_F2F_CADENCE]
    assert len(f2f) == 1 and f2f[0]["priority"] == 4 and f2f[0]["mandate"] == "MUST_EXPAND"
    assert f2f[0]["evidence"]["executive_sponsor"] == "Eve Exec"
    assert playbook_judge.judge(tasks, {"au1-big": acct})["verdict"] == "PASS"


def test_rule_silent_after_recent_f2f(f2f_env):
    engine, orchestrate = f2f_env
    engine.record_f2f({"account_id": "au1-big", "met_on": "2026-09-20"}, None)  # 16 days ago
    acct = _tier1()["au1-big"]
    tasks, _ = orchestrate.evaluate("au1-big", acct)
    assert not [t for t in tasks if t["rule_id"] == orchestrate.RULE_EXEC_F2F_CADENCE]


def test_rule_fires_when_f2f_is_stale(f2f_env):
    engine, orchestrate = f2f_env
    engine.record_f2f({"account_id": "au1-big", "met_on": "2026-01-01"}, None)  # >90 days ago
    acct = _tier1()["au1-big"]
    tasks, _ = orchestrate.evaluate("au1-big", acct)
    assert [t for t in tasks if t["rule_id"] == orchestrate.RULE_EXEC_F2F_CADENCE]


def test_rule_not_for_small_strategic(f2f_env):
    _, orchestrate = f2f_env
    acct = _tier1(arr=20000)["au1-big"]   # Strategic but below tier-1 ARR proxy
    tasks, _ = orchestrate.evaluate("au1-big", acct)
    assert not [t for t in tasks if t["rule_id"] == orchestrate.RULE_EXEC_F2F_CADENCE]


def test_cadence_kpi(f2f_env, monkeypatch):
    engine, _ = f2f_env
    import dataaccess
    book = {}
    book.update(_tier1("au1-a", 300000))     # tier-1, no F2F -> overdue
    book.update(_tier1("au1-b", 400000))     # tier-1, will log recent F2F -> in window
    monkeypatch.setattr(dataaccess, "all_accounts", lambda: book)
    engine.set_principal(None)
    engine.record_f2f({"account_id": "au1-b", "met_on": "2026-09-25"}, None)
    k = engine.f2f_cadence()
    assert k["tier1_strategic_accounts"] == 2
    assert k["with_in_window_touchpoint"] == 1
    assert k["tier1_exec_touchpoint_pct"] == 50
    assert k["target_pct"] == 100
    assert {r["account_id"] for r in k["overdue_accounts"]} == {"au1-a"}


# --------------------------------------------------------------------------- #
# V5 UC2: Exec Sponsor F2F bi-directional write-back to HubSpot
# --------------------------------------------------------------------------- #
def test_record_f2f_calls_hubspot_writeback(f2f_env, monkeypatch):
    """record_f2f must push the F2F back to HubSpot (two-gated at the adapter) and attach
    the result, not just log locally — closing the UC2 bi-directional sync gap."""
    engine, _ = f2f_env
    import dataaccess
    calls = {}

    class _FakeHS:
        def live(self):
            return True

        def set_exec_f2f(self, account_ref, met_on, outcome=None, apply=False):
            calls["args"] = {"ref": account_ref, "met_on": met_on, "outcome": outcome, "apply": apply}
            return {"synced": bool(apply), "mode": "applied" if apply else "dry-run",
                    "would_write": {"cs_last_exec_f2f_date": met_on}}

    monkeypatch.setattr(dataaccess, "_ADAPTERS", True, raising=False)
    monkeypatch.setattr(engine._src, "HUBSPOT", _FakeHS())

    e = engine.record_f2f({"account_id": "au1-big", "met_on": "2026-10-01",
                           "outcome": "Renewal aligned", "apply": True}, {"email": "me@x.com"})
    # Adapter was called with the logged date + outcome, honouring apply.
    assert calls["args"] == {"ref": "au1-big", "met_on": "2026-10-01",
                             "outcome": "Renewal aligned", "apply": True}
    # The write-back result is attached to the entry for the UI/audit trail.
    assert e["hubspot_writeback"]["synced"] is True
    # Local log is still the source of record.
    assert engine.last_f2f("au1-big") == "2026-10-01"


def test_record_f2f_survives_hubspot_failure(f2f_env, monkeypatch):
    """A CRM write failure must never lose the locally-logged touchpoint."""
    engine, _ = f2f_env
    import dataaccess

    class _BoomHS:
        def live(self):
            return True

        def set_exec_f2f(self, *a, **k):
            raise RuntimeError("hubspot 500")

    monkeypatch.setattr(dataaccess, "_ADAPTERS", True, raising=False)
    monkeypatch.setattr(engine._src, "HUBSPOT", _BoomHS())
    e = engine.record_f2f({"account_id": "au1-big", "met_on": "2026-10-02"}, None)
    assert e["hubspot_writeback"]["mode"] == "error"
    assert engine.last_f2f("au1-big") == "2026-10-02"   # local log intact


def test_set_exec_f2f_dry_run_payload():
    """The adapter builds the right company payload and stays dry-run without the gates."""
    from adapters import sources
    hs = sources.HubSpot()
    hs._find_company = lambda ref: {"id": "C1"}      # stub the read-only lookup
    out = hs.set_exec_f2f("au1-big", met_on="2026-10-01", outcome="Great QBR", apply=False)
    assert out["mode"] == "dry-run" and out["synced"] is False
    assert out["would_write"]["cs_last_exec_f2f_date"] == "2026-10-01"
    assert out["would_write"]["cs_last_exec_f2f_outcome"] == "Great QBR"
