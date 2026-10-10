"""CSM availability / presence feed (Tech-Touch round-robin, WoW §3)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_presence_defaults_available_then_honours_ooo(monkeypatch, tmp_path):
    import engine
    monkeypatch.setenv("CS_PRESENCE_FILE", str(tmp_path / "p.jsonl"))
    engine._CSM_PRESENCE.clear(); engine._CSM_PRESENCE_LOADED = False
    # No status set -> available by default (honest fallback so round-robin still works).
    assert engine._presence_for("Ann") is True
    # Mark OOO -> unavailable.
    engine.set_csm_availability("Ann", available=False, note="OOO today")
    assert engine._presence_for("Ann") is False
    # Back available.
    engine.set_csm_availability("Ann", available=True)
    assert engine._presence_for("Ann") is True
    engine._CSM_PRESENCE.clear(); engine._CSM_PRESENCE_LOADED = False


def test_presence_until_expires_back_to_available(monkeypatch, tmp_path):
    import engine
    monkeypatch.setenv("CS_PRESENCE_FILE", str(tmp_path / "p.jsonl"))
    engine._CSM_PRESENCE.clear(); engine._CSM_PRESENCE_LOADED = False
    past = engine.datetime.now(engine.timezone.utc).timestamp() - 10
    engine.set_csm_availability("Bob", available=False, until=int(past))
    # 'until' already elapsed -> treated as available again.
    assert engine._presence_for("Bob") is True
    engine._CSM_PRESENCE.clear(); engine._CSM_PRESENCE_LOADED = False


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


def test_presence_persists_across_reload(monkeypatch, tmp_path):
    """OOO status must survive a process restart (EFS JSONL store), so the inbound
    round-robin and reassignment keep skipping an OOO CSM after a redeploy."""
    import engine
    pfile = str(tmp_path / "p.jsonl")
    monkeypatch.setenv("CS_PRESENCE_FILE", pfile)
    engine._CSM_PRESENCE.clear(); engine._CSM_PRESENCE_LOADED = False
    engine.set_csm_availability("Dana", available=False, note="leave")
    # Simulate a fresh process: wipe in-memory state, force re-hydrate from the file.
    engine._CSM_PRESENCE.clear(); engine._CSM_PRESENCE_LOADED = False
    assert engine._presence_for("Dana") is False  # loaded from disk
    engine._CSM_PRESENCE.clear(); engine._CSM_PRESENCE_LOADED = False


def test_inbound_queue_auto_reassigns_when_owner_goes_ooo(monkeypatch, tmp_path):
    """A stale/OOO ticket is actually moved to the least-loaded available CSM on read,
    not merely flagged - closing the 20h / owner-OOO reassignment loop."""
    import engine, time
    monkeypatch.setenv("CS_PRESENCE_FILE", str(tmp_path / "p.jsonl"))
    engine._CSM_PRESENCE.clear(); engine._CSM_PRESENCE_LOADED = False
    engine._INBOUND_QUEUE.clear()
    # Pooled roster = Alice (OOO) + Bob (available). Make Alice OOO.
    engine.set_csm_availability("Alice", available=False)
    engine.set_csm_availability("Bob", available=True)
    monkeypatch.setattr(engine, "pooled_roster", lambda with_availability=True: {
        "roster": [{"name": "Alice", "available": engine._presence_for("Alice")},
                   {"name": "Bob", "available": engine._presence_for("Bob")}]})
    now = int(time.time())
    engine._INBOUND_QUEUE["t1"] = {
        "id": "t1", "status": "open", "assigned_to": "Alice",
        "received_at": now - 3600, "sla_due": now + 20 * 3600,  # not SLA-breached
        "destination": "cs_pooled_queue", "intent": "billing",
    }
    try:
        q = engine.inbound_queue()
        t1 = next(t for t in q["tickets"] if t["id"] == "t1")
        assert t1["assigned_to"] == "Bob"          # moved off OOO Alice
        assert t1["reassigned_from"] == "Alice"
        assert t1["needs_reassign"] is False        # loop closed, not just flagged
    finally:
        engine._INBOUND_QUEUE.clear()
        engine._CSM_PRESENCE.clear(); engine._CSM_PRESENCE_LOADED = False
