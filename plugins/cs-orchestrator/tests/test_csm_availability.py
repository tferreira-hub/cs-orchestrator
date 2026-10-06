"""CSM availability / presence feed (Tech-Touch round-robin, WoW §3)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_presence_defaults_available_then_honours_ooo(monkeypatch):
    import engine
    engine._CSM_PRESENCE.clear()
    # No status set -> available by default (honest fallback so round-robin still works).
    assert engine._presence_for("Ann") is True
    # Mark OOO -> unavailable.
    engine.set_csm_availability("Ann", available=False, note="OOO today")
    assert engine._presence_for("Ann") is False
    # Back available.
    engine.set_csm_availability("Ann", available=True)
    assert engine._presence_for("Ann") is True
    engine._CSM_PRESENCE.clear()


def test_presence_until_expires_back_to_available(monkeypatch):
    import engine
    engine._CSM_PRESENCE.clear()
    past = engine.datetime.now(engine.timezone.utc).timestamp() - 10
    engine.set_csm_availability("Bob", available=False, until=int(past))
    # 'until' already elapsed -> treated as available again.
    assert engine._presence_for("Bob") is True
    engine._CSM_PRESENCE.clear()


def test_pooled_roster_reflects_presence(monkeypatch):
    import engine
    engine._CSM_PRESENCE.clear()
    monkeypatch.setattr(engine, "portfolio", lambda: {"accounts": [
        {"csm_owner": "Ann", "pooled": True},
        {"csm_owner": "Bob", "pooled": True},
    ]})
    engine.set_csm_availability("Bob", available=False, note="sick")
    r = engine.pooled_roster()
    assert "Ann" in r["available"] and "Bob" in r["unavailable"]
    assert r["presence_source"] == "live"
    engine._CSM_PRESENCE.clear()


def test_round_robin_skips_unavailable_via_live_roster(monkeypatch):
    """End-to-end: an OOO CSM is excluded from the roster the triage engine round-robins."""
    import engine, inbound
    engine._CSM_PRESENCE.clear()
    monkeypatch.setattr(engine, "portfolio", lambda: {"accounts": [
        {"csm_owner": "Ann", "pooled": True},
        {"csm_owner": "Bob", "pooled": True},
    ]})
    engine.set_csm_availability("Bob", available=False)
    roster = engine.pooled_roster()["roster"]
    items = [{"id": "1", "from": "u1@co.com", "subject": "Invoice", "body": "billing q",
              "received_at": 1_000_000}]
    r = inbound.triage_inbound(items, roster=roster, now=1_000_000)
    # Only Ann is available, so the one pooled ticket goes to Ann, never Bob.
    assert r["summary"]["available_csms"] == ["Ann"]
    assert r["tickets"][0]["assigned_to"] == "Ann"
    engine._CSM_PRESENCE.clear()


def test_inbound_queue_persists_and_resolves(monkeypatch):
    """record_inbound persists triaged tickets; inbound_queue lists them; resolve_inbound
    marks status. Validates the Option A queue surface."""
    import engine
    engine._INBOUND_QUEUE.clear()
    tickets = [
        {"id": "t1", "destination": "cs_pooled_queue", "intent": "billing", "sla_breached": False},
        {"id": "t2", "destination": "zendesk_handoff", "intent": "technical", "sla_breached": True},
    ]
    assert engine.record_inbound(tickets) == 2
    q = engine.inbound_queue()
    assert q["count"] == 2 and q["open"] == 2 and q["sla_breached"] == 1
    assert q["by_intent"]["billing"] == 1
    # Resolve one.
    engine.resolve_inbound("t1", status="resolved")
    assert engine.inbound_queue(status="open")["count"] == 1
    assert engine.inbound_queue(status="resolved")["count"] == 1
    engine._INBOUND_QUEUE.clear()
