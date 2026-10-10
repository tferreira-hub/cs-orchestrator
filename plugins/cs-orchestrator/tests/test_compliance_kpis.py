"""V5 operational-compliance KPIs computed against their targets.

UC1/UC3 specify two targeted KPIs that were previously only raw counts:
  * First-response SLA compliance >= 95% within 24h (pooled inbound queue).
  * Monthly performance-report delivery >= 98% to Primary Admins.

These tests pin that engine.kpis() now surfaces both as computed percentages vs their
targets, honestly None when not yet computable (no tickets / no eligible accounts) rather
than a fabricated 100%.
"""
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
def _clear(monkeypatch):
    import engine
    engine.set_principal(None)
    yield
    engine.set_principal(None)


# --------------------------------------------------------------------------- #
# SLA compliance (first response within 24h, target 95%)
# --------------------------------------------------------------------------- #
def test_sla_compliance_computes_pct_against_95_target(monkeypatch):
    import engine
    # 9 compliant + 1 breached = 90% (below the 95% target).
    queue = {}
    for i in range(9):
        queue[f"t{i}"] = {"id": f"t{i}", "sla_breached": False}
    queue["t9"] = {"id": "t9", "sla_breached": True}
    monkeypatch.setattr(engine, "_INBOUND_QUEUE", queue)
    monkeypatch.setattr(engine, "_load_inbound", lambda: None)

    s = engine._sla_compliance()
    assert s["sla_compliance_pct"] == 90.0
    assert s["target_pct"] == 95
    assert s["meets_target"] is False
    assert s["tickets"] == 10 and s["breached"] == 1
    assert s["computable"] is True


def test_sla_compliance_meets_target(monkeypatch):
    import engine
    queue = {f"t{i}": {"id": f"t{i}", "sla_breached": False} for i in range(20)}
    monkeypatch.setattr(engine, "_INBOUND_QUEUE", queue)
    monkeypatch.setattr(engine, "_load_inbound", lambda: None)
    s = engine._sla_compliance()
    assert s["sla_compliance_pct"] == 100.0 and s["meets_target"] is True


def test_sla_compliance_honest_none_when_no_tickets(monkeypatch):
    import engine
    monkeypatch.setattr(engine, "_INBOUND_QUEUE", {})
    monkeypatch.setattr(engine, "_load_inbound", lambda: None)
    s = engine._sla_compliance()
    assert s["sla_compliance_pct"] is None and s["computable"] is False


# --------------------------------------------------------------------------- #
# Report-delivery compliance (to Primary Admins, target 98%)
# --------------------------------------------------------------------------- #
def test_report_delivery_pct_from_digest_run(monkeypatch):
    import engine
    # 100 eligible, 3 missing a Primary Admin, 0 errors -> 97 deliverable -> 97% (<98%).
    monkeypatch.setattr(engine, "run_monthly_digests", lambda apply=False: {
        "summary": {"accounts_in_scope": 100, "missing_primary_admin": 3, "errors": 0}})
    r = engine._report_delivery_compliance()
    assert r["report_delivery_pct"] == 97.0
    assert r["target_pct"] == 98 and r["meets_target"] is False
    assert r["eligible_accounts"] == 100 and r["deliverable_accounts"] == 97


def test_report_delivery_meets_target(monkeypatch):
    import engine
    monkeypatch.setattr(engine, "run_monthly_digests", lambda apply=False: {
        "summary": {"accounts_in_scope": 100, "missing_primary_admin": 1, "errors": 0}})
    r = engine._report_delivery_compliance()
    assert r["report_delivery_pct"] == 99.0 and r["meets_target"] is True


def test_report_delivery_honest_none_when_no_accounts(monkeypatch):
    import engine
    monkeypatch.setattr(engine, "run_monthly_digests", lambda apply=False: {
        "summary": {"accounts_in_scope": 0, "missing_primary_admin": 0, "errors": 0}})
    r = engine._report_delivery_compliance()
    assert r["report_delivery_pct"] is None and r["computable"] is False


# --------------------------------------------------------------------------- #
# Wired into kpis()
# --------------------------------------------------------------------------- #
def test_kpis_payload_carries_sla_and_report_delivery(monkeypatch):
    import engine
    # Isolate from the heavy live orchestration/warehouse path: stub the parts kpis()
    # fans out to so this test only asserts the SLA + report-delivery wiring, fast.
    monkeypatch.setattr(engine.orchestrate, "load_accounts", lambda: {})
    monkeypatch.setattr(engine.orchestrate, "orchestrate", lambda: {"tasks": []})
    monkeypatch.setattr(engine, "_retention_accounts", lambda: {})
    monkeypatch.setattr(engine, "_retention_metrics", lambda *a, **k: {"computable": False})
    monkeypatch.setattr(engine, "_INBOUND_QUEUE", {"t0": {"id": "t0", "sla_breached": False}})
    monkeypatch.setattr(engine, "_load_inbound", lambda: None)
    monkeypatch.setattr(engine, "run_monthly_digests", lambda apply=False: {
        "summary": {"accounts_in_scope": 10, "missing_primary_admin": 0, "errors": 0}})
    k = engine.kpis()
    assert "sla" in k and k["sla"]["target_pct"] == 95
    assert k["sla"]["sla_compliance_pct"] == 100.0
    assert "report_delivery" in k and k["report_delivery"]["target_pct"] == 98
    assert k["report_delivery"]["report_delivery_pct"] == 100.0
