"""Tests for the Option A HubSpot inbound PULL ingress.

Covers:
  * HubSpot.inbound_tickets() normalisation + filtering (recency, open-only, exclude
    Slack-mirror subjects, company association -> company_id).
  * engine.ingest_hubspot_inbound() mapping company_id -> account_id, triage + persist,
    skip of tickets with no matching account, and honest no-op when HubSpot not live.

All HubSpot access is monkeypatched; no network.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ticket(tid, subject, content, company_id, created, stage="1"):
    # Search results carry properties but NOT associations; the adapter resolves the
    # company by a per-id GET. _COMPANY_BY_TICKET records the association for the GET stub.
    return {
        "id": tid,
        "properties": {
            "subject": subject, "content": content,
            "hs_pipeline_stage": stage, "createdate": created,
        },
        "_company_id": company_id,  # test-only hint for the GET stub
    }


# --------------------------------------------------------------------------- #
# Adapter: inbound_tickets()
# --------------------------------------------------------------------------- #
def test_inbound_tickets_normalises_and_filters(monkeypatch):
    from adapters import sources
    hs = sources.HUBSPOT

    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(hours=2))

    # The search endpoint already applies the recency (createdate GTE) filter server-side,
    # so the stub returns only in-window tickets; the adapter still applies the open-only
    # and Slack-exclusion filters client-side.
    search_results = [
        _ticket("111", "Licence portal is down", "Cannot log in", "comp-1", recent),
        _ticket("222", "New Post in APAC Slack channel", "slack mirror", "comp-2", recent),  # excluded
        _ticket("444", "Closed already", "resolved", "comp-3", recent, stage="4"),            # closed
        _ticket("555", "Please add seats", "want more licences", "comp-9", recent),
    ]
    company_by_ticket = {t["id"]: t["_company_id"] for t in search_results}

    def _search(url, headers, body, *a, **k):
        if url.endswith("/tickets/search"):
            # Newest-first, createdate lower bound present.
            assert body["sorts"][0]["direction"] == "DESCENDING"
            assert body["filterGroups"][0]["filters"][0]["propertyName"] == "createdate"
            return {"results": [{"id": t["id"], "properties": t["properties"]} for t in search_results]}
        if url.endswith("/associations/tickets/companies/batch/read"):
            # One batch call resolves all candidate tickets' companies.
            out = []
            for inp in body.get("inputs", []):
                tid = inp["id"]
                cid = company_by_ticket.get(tid)
                out.append({"from": {"id": tid},
                            "to": ([{"toObjectId": cid}] if cid else [])})
            return {"results": out}
        raise AssertionError(f"unexpected POST url {url}")

    monkeypatch.setattr(sources.HubSpot, "live", lambda self: True)
    monkeypatch.setattr(sources.config, "http_post_readonly", _search)

    items = hs.inbound_tickets(window_days=7)
    ids = {i["id"] for i in items}
    # Only the two genuine, open, non-Slack tickets survive.
    assert ids == {"hs-111", "hs-555"}
    first = next(i for i in items if i["id"] == "hs-111")
    assert first["channel"] == "mailbox"
    assert first["subject"] == "Licence portal is down"
    assert first["body"] == "Cannot log in"
    assert first["company_id"] == "comp-1"
    assert isinstance(first["received_at"], int)


def test_inbound_tickets_empty_when_not_live(monkeypatch):
    from adapters import sources
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    assert sources.HUBSPOT.inbound_tickets() == []


# --------------------------------------------------------------------------- #
# Engine: ingest_hubspot_inbound()
# --------------------------------------------------------------------------- #
def test_ingest_maps_company_to_account_and_persists(monkeypatch):
    import engine
    from adapters import sources

    # Full-book roster: comp-1 is a CS-tracked (managed) account; comp-9 is a pooled
    # customer with no CS account_id tag; comp-404 is not in the roster at all.
    roster_rows = [
        {"company_id": "comp-1", "account_id": "au1-1", "name": "Acme"},
        {"company_id": "comp-9", "account_id": None, "name": "Globex Pooled"},
    ]
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: True)
    monkeypatch.setattr(engine._src.HUBSPOT, "list_all_companies", lambda *a, **k: roster_rows)
    monkeypatch.setattr(engine._src.HUBSPOT, "inbound_tickets", lambda window_days=None: [
        {"id": "hs-1", "channel": "mailbox", "from": "", "subject": "add seats",
         "body": "please add 10 licence seats", "company_id": "comp-1",
         "company_name": "Acme", "received_at": 1_000_000},
        {"id": "hs-2", "channel": "mailbox", "from": "", "subject": "billing",
         "body": "invoice is wrong", "company_id": "comp-9",
         "company_name": "Globex Pooled", "received_at": 1_000_000},
        {"id": "hs-3", "channel": "mailbox", "from": "", "subject": "no company",
         "body": "hello", "company_id": None, "company_name": None, "received_at": 1_000_000},
    ])
    monkeypatch.setattr(engine, "pooled_roster", lambda: {"roster": [
        {"name": "Sam", "available": True}, {"name": "Dana", "available": True}]})
    engine._INBOUND_QUEUE.clear()

    summary = engine.ingest_hubspot_inbound()
    assert summary["live"] is True
    assert summary["fetched"] == 3
    assert summary["ingested"] == 2            # managed + pooled resolved; no-company skipped
    assert summary["matched_managed"] == 1
    assert summary["matched_pooled"] == 1
    assert summary["skipped_no_company"] == 1

    q = engine._INBOUND_QUEUE
    assert q["hs-1"]["account_ref"] == "au1-1"             # managed -> CS account id
    assert q["hs-2"]["account_ref"] == "hs-company-comp-9" # pooled -> company-keyed
    assert q["hs-2"]["account_name"] == "Globex Pooled"
    assert all(q[k]["assigned_to"] for k in q)             # least-loaded allocation ran
    engine._INBOUND_QUEUE.clear()


def test_ingest_is_honest_noop_when_not_live(monkeypatch):
    import engine
    from adapters import sources
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    engine._INBOUND_QUEUE.clear()
    summary = engine.ingest_hubspot_inbound()
    assert summary["live"] is False
    assert summary["ingested"] == 0
    assert engine._INBOUND_QUEUE == {}


def test_warm_inbound_ingest_never_raises(monkeypatch):
    import engine
    monkeypatch.setattr(engine, "ingest_hubspot_inbound",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert engine.warm_inbound_ingest() == 0


def test_start_inbound_ingest_runs_in_background(monkeypatch):
    """The fire-and-forget trigger returns immediately and the background thread records a
    done status with the ingest summary (so the request never blocks on the slow pull)."""
    import engine
    import time

    def _slow_ingest(window_days=None):
        time.sleep(0.05)  # simulate a slow pull without blocking the caller
        return {"live": True, "fetched": 5, "ingested": 3, "skipped_no_account": 2,
                "by_intent": {"billing": 3}}

    monkeypatch.setattr(engine, "ingest_hubspot_inbound", _slow_ingest)
    # Reset status so the test is deterministic regardless of prior runs.
    engine._INBOUND_INGEST_STATUS.update({"state": "idle", "started_at": None,
                                          "finished_at": None, "summary": None, "error": None})

    status = engine.start_inbound_ingest(window_days=7)
    assert status["state"] == "running"           # returned immediately, work still going
    assert status["already_running"] is False

    # Poll the status until the background thread finishes.
    for _ in range(50):
        s = engine.inbound_ingest_status()
        if s["state"] == "done":
            break
        time.sleep(0.02)
    s = engine.inbound_ingest_status()
    assert s["state"] == "done"
    assert s["summary"]["ingested"] == 3
    assert s["error"] is None


def test_start_inbound_ingest_is_single_flight(monkeypatch):
    """A second trigger while one is running does not start a second pull."""
    import engine
    import time
    monkeypatch.setattr(engine, "ingest_hubspot_inbound",
                        lambda window_days=None: (time.sleep(0.1) or {"live": True, "ingested": 0}))
    engine._INBOUND_INGEST_STATUS.update({"state": "idle", "started_at": None,
                                          "finished_at": None, "summary": None, "error": None})
    first = engine.start_inbound_ingest()
    second = engine.start_inbound_ingest()
    assert first["already_running"] is False
    assert second["already_running"] is True
    # let it finish so it does not leak into other tests
    for _ in range(50):
        if engine.inbound_ingest_status()["state"] == "done":
            break
        time.sleep(0.02)


def test_inbound_queue_persists_across_restart(monkeypatch, tmp_path):
    """record_inbound writes to the JSONL store; after clearing in-memory state and the
    load flag (simulating a task restart), _load_inbound rehydrates the queue, including a
    status change made via resolve_inbound. This is what makes the pooled Inbox survive
    deploys without a manual re-pull."""
    import engine
    store = tmp_path / "inbound.jsonl"
    monkeypatch.setenv("CS_INBOUND_FILE", str(store))
    # Fresh state.
    engine._INBOUND_QUEUE.clear()
    engine._INBOUND_QUEUE_LOADED = False

    engine.record_inbound([
        {"id": "hs-1", "channel": "mailbox", "account_ref": "au1-1", "subject": "add seats",
         "intent": "general", "destination": "cs_pooled_queue", "assigned_to": "Sam",
         "received_at": 1_000_000, "sla_due": 1_086_400},
        {"id": "hs-2", "channel": "mailbox", "account_ref": "au1-2", "subject": "crash",
         "intent": "technical", "destination": "zendesk_handoff",
         "received_at": 1_000_000, "sla_due": 1_086_400},
    ])
    assert store.exists()
    # Resolve one so a status change is also persisted.
    engine.resolve_inbound("hs-2", status="resolved")

    # Simulate a restart: wipe memory + the load flag, then hydrate from disk only.
    engine._INBOUND_QUEUE.clear()
    engine._INBOUND_QUEUE_LOADED = False
    engine._load_inbound()

    q = engine._INBOUND_QUEUE
    assert set(q.keys()) == {"hs-1", "hs-2"}
    assert q["hs-1"]["status"] == "open"
    assert q["hs-2"]["status"] == "resolved"          # status change replayed
    assert q["hs-1"]["account_ref"] == "au1-1"
    engine._INBOUND_QUEUE.clear()
    engine._INBOUND_QUEUE_LOADED = False
