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
    assoc = {"companies": {"results": [{"id": company_id}]}} if company_id else {}
    return {
        "id": tid,
        "properties": {
            "subject": subject, "content": content,
            "hs_pipeline_stage": stage, "createdate": created,
        },
        "associations": assoc,
    }


# --------------------------------------------------------------------------- #
# Adapter: inbound_tickets()
# --------------------------------------------------------------------------- #
def test_inbound_tickets_normalises_and_filters(monkeypatch):
    from adapters import sources
    hs = sources.HUBSPOT

    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(hours=2))
    old = _iso(now - timedelta(days=30))

    page = {"results": [
        _ticket("111", "Licence portal is down", "Cannot log in", "comp-1", recent),
        _ticket("222", "New Post in APAC Slack channel", "slack mirror", "comp-2", recent),  # excluded
        _ticket("333", "Old ticket", "stale", "comp-1", old),                                 # too old
        _ticket("444", "Closed already", "resolved", "comp-3", recent, stage="4"),            # closed stage
        _ticket("555", "Please add seats", "want more licences", "comp-9", recent),
    ]}

    monkeypatch.setattr(sources.HubSpot, "live", lambda self: True)
    monkeypatch.setattr(sources.config, "http_get", lambda url, headers, *a, **k: page)

    items = hs.inbound_tickets(window_days=7)
    ids = {i["id"] for i in items}
    # Only the two genuine, recent, open, non-Slack tickets survive.
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

    # Two accounts; one HubSpot company maps, one ticket's company has no account.
    accounts = {
        "AU1-1": {"hubspot": {"company_id": "comp-1", "name": "Acme", "csm_owner": "Sam"}},
        "AU1-2": {"hubspot": {"company_id": "comp-9", "name": "Globex", "csm_owner": "Dana"}},
    }
    monkeypatch.setattr(engine.orchestrate, "load_accounts", lambda *a, **k: accounts)
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: True)
    monkeypatch.setattr(engine._src.HUBSPOT, "inbound_tickets", lambda window_days=None: [
        {"id": "hs-1", "channel": "mailbox", "from": "", "subject": "add seats",
         "body": "please add 10 licence seats", "company_id": "comp-1", "received_at": 1_000_000},
        {"id": "hs-2", "channel": "mailbox", "from": "", "subject": "billing",
         "body": "invoice is wrong", "company_id": "comp-9", "received_at": 1_000_000},
        {"id": "hs-3", "channel": "mailbox", "from": "", "subject": "unknown co",
         "body": "hello", "company_id": "comp-404", "received_at": 1_000_000},  # no account
    ])
    monkeypatch.setattr(engine, "pooled_roster", lambda: {"roster": [
        {"name": "Sam", "available": True}, {"name": "Dana", "available": True}]})
    engine._INBOUND_QUEUE.clear()

    summary = engine.ingest_hubspot_inbound()
    assert summary["live"] is True
    assert summary["fetched"] == 3
    assert summary["ingested"] == 2            # two resolved, one skipped
    assert summary["skipped_no_account"] == 1

    # Persisted into the live queue, each carrying its resolved account_ref + an owner.
    q = engine._INBOUND_QUEUE
    assert set(q.keys()) == {"hs-1", "hs-2"}
    assert q["hs-1"]["account_ref"] == "AU1-1"
    assert q["hs-2"]["account_ref"] == "AU1-2"
    assert all(q[k]["assigned_to"] for k in q)   # least-loaded allocation ran
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
