"""Designated-CSM field resolution.

The platform scopes/filters by the designated CSM — a custom HubSpot company field
(default `customer_success_manager`, a user reference) — rather than only the record
owner (`hubspot_owner_id`). The CSM field wins when set; the record owner is the fallback.
Configurable via CS_CSM_FIELD. These tests pin that resolution in the whole-book roster.
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

from adapters import config, sources  # noqa: E402


def _company(cid, account_id, *, csm=None, owner=None, name="Co"):
    props = {"name": name, "account_id": account_id, "lifecyclestage": "customer"}
    if csm is not None:
        props["customer_success_manager"] = csm
    if owner is not None:
        props["hubspot_owner_id"] = owner
    return {"id": cid, "properties": props}


def _run_roster(monkeypatch, companies):
    """Drive list_all_companies with a single-page scan over `companies`."""
    hs = sources.HubSpot()
    monkeypatch.setattr(hs, "live", lambda: True)
    monkeypatch.setattr(hs, "_ingest_lifecycle_stages", lambda: ["customer"])
    if hasattr(sources.HubSpot, "_full_roster_cache"):
        delattr(sources.HubSpot, "_full_roster_cache")

    def fake_post(url, headers, body, timeout=12):
        if "objects/companies/search" in url:
            return {"results": companies}  # one page, no paging
        return {"results": []}
    monkeypatch.setattr(config, "http_post", fake_post)
    # Don't let the deal-renewal fallback make extra calls.
    monkeypatch.setattr(hs, "_fill_roster_renewals_from_deals", lambda rows, batch=100: 0)
    rows = hs.list_all_companies(limit=100)
    if hasattr(sources.HubSpot, "_full_roster_cache"):
        delattr(sources.HubSpot, "_full_roster_cache")
    return {r["account_id"]: r for r in rows}


def test_csm_field_wins_over_record_owner(monkeypatch):
    rows = _run_roster(monkeypatch, [
        _company("1", "AU1-1", csm="555", owner="999"),   # CSM field differs from owner
    ])
    r = rows["au1-1"]
    assert r["owner_id"] == "555"            # CSM field drives ownership
    assert r["record_owner_id"] == "999"     # record owner still exposed
    assert r["csm_source"] == "csm_field"


def test_falls_back_to_record_owner_when_csm_blank(monkeypatch):
    rows = _run_roster(monkeypatch, [
        _company("2", "AU1-2", csm="", owner="999"),       # no CSM field -> owner
    ])
    r = rows["au1-2"]
    assert r["owner_id"] == "999"
    assert r["csm_source"] == "record_owner"


def test_none_when_neither_present(monkeypatch):
    rows = _run_roster(monkeypatch, [
        _company("3", "AU1-3"),                            # neither set
    ])
    r = rows["au1-3"]
    assert r["owner_id"] is None
    assert r["csm_source"] is None


def test_csm_field_is_configurable(monkeypatch):
    monkeypatch.setenv("CS_CSM_FIELD", "cs_lead_owner")
    assert sources.HubSpot._csm_field() == "cs_lead_owner"
    # When the configured field is present it wins.
    hs = sources.HubSpot()
    monkeypatch.setattr(hs, "live", lambda: True)
    monkeypatch.setattr(hs, "_ingest_lifecycle_stages", lambda: ["customer"])
    monkeypatch.setattr(hs, "_fill_roster_renewals_from_deals", lambda rows, batch=100: 0)
    if hasattr(sources.HubSpot, "_full_roster_cache"):
        delattr(sources.HubSpot, "_full_roster_cache")
    comp = {"id": "9", "properties": {"name": "C", "account_id": "AU1-9",
                                      "lifecyclestage": "customer",
                                      "cs_lead_owner": "777", "hubspot_owner_id": "111"}}
    monkeypatch.setattr(config, "http_post",
                        lambda u, h, b, timeout=12: {"results": [comp]} if "search" in u else {"results": []})
    rows = {r["account_id"]: r for r in hs.list_all_companies(limit=10)}
    if hasattr(sources.HubSpot, "_full_roster_cache"):
        delattr(sources.HubSpot, "_full_roster_cache")
    assert rows["au1-9"]["owner_id"] == "777"


def test_default_csm_field_name():
    import os
    os.environ.pop("CS_CSM_FIELD", None)
    assert sources.HubSpot._csm_field() == "customer_success_manager"
