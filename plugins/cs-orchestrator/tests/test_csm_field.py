"""Designated-CSM field resolution (ordered fallback).

The platform scopes/filters by the designated CSM, resolved from an ORDERED list of HubSpot
company fields (default `retention_owner` then `customer_success_manager`, both user
references), taking the first non-empty value, then falling back to the record owner
(`hubspot_owner_id`). `csm_source` records which field drove it. Configurable via
CS_CSM_FIELD (comma-separated). These tests pin that resolution in the whole-book roster.
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

from adapters import config, sources  # noqa: E402


def _company(cid, account_id, *, props_extra=None, owner=None, name="Co"):
    props = {"name": name, "account_id": account_id, "lifecyclestage": "customer"}
    if owner is not None:
        props["hubspot_owner_id"] = owner
    if props_extra:
        props.update(props_extra)
    return {"id": cid, "properties": props}


def _run_roster(monkeypatch, companies):
    hs = sources.HubSpot()
    monkeypatch.setattr(hs, "live", lambda: True)
    monkeypatch.setattr(hs, "_ingest_lifecycle_stages", lambda: ["customer"])
    if hasattr(sources.HubSpot, "_full_roster_cache"):
        delattr(sources.HubSpot, "_full_roster_cache")

    def fake_post(url, headers, body, timeout=12):
        if "objects/companies/search" in url:
            return {"results": companies}
        return {"results": []}
    monkeypatch.setattr(config, "http_post", fake_post)
    monkeypatch.setattr(hs, "_fill_roster_renewals_from_deals", lambda rows, batch=100: 0)
    rows = hs.list_all_companies(limit=100)
    if hasattr(sources.HubSpot, "_full_roster_cache"):
        delattr(sources.HubSpot, "_full_roster_cache")
    return {r["account_id"]: r for r in rows}


# --------------------------------------------------------------------------- #
# Resolver precedence
# --------------------------------------------------------------------------- #
def test_default_fields_order():
    import os
    os.environ.pop("CS_CSM_FIELD", None)
    assert sources.HubSpot._csm_fields() == ["retention_owner", "customer_success_manager"]


def test_retention_owner_wins_when_set(monkeypatch):
    # retention_owner is first in the default order -> it wins over customer_success_manager.
    rows = _run_roster(monkeypatch, [
        _company("1", "AU1-1", owner="999",
                 props_extra={"retention_owner": "111", "customer_success_manager": "222"}),
    ])
    r = rows["au1-1"]
    assert r["owner_id"] == "111"
    assert r["csm_source"] == "retention_owner"
    assert r["record_owner_id"] == "999"


def test_falls_through_to_customer_success_manager(monkeypatch):
    # retention_owner empty -> next field in the list.
    rows = _run_roster(monkeypatch, [
        _company("2", "AU1-2", owner="999",
                 props_extra={"customer_success_manager": "222"}),
    ])
    r = rows["au1-2"]
    assert r["owner_id"] == "222"
    assert r["csm_source"] == "customer_success_manager"


def test_falls_back_to_record_owner_when_all_csm_fields_blank(monkeypatch):
    rows = _run_roster(monkeypatch, [
        _company("3", "AU1-3", owner="999"),               # no CSM fields set
    ])
    r = rows["au1-3"]
    assert r["owner_id"] == "999"
    assert r["csm_source"] == "record_owner"


def test_none_when_neither_present(monkeypatch):
    rows = _run_roster(monkeypatch, [
        _company("4", "AU1-4"),                            # nothing set
    ])
    r = rows["au1-4"]
    assert r["owner_id"] is None
    assert r["csm_source"] is None


# --------------------------------------------------------------------------- #
# Configurability
# --------------------------------------------------------------------------- #
def test_csm_fields_configurable_comma_list(monkeypatch):
    monkeypatch.setenv("CS_CSM_FIELD", "cs_lead_owner, retention_owner")
    assert sources.HubSpot._csm_fields() == ["cs_lead_owner", "retention_owner"]
    rows = _run_roster(monkeypatch, [
        _company("9", "AU1-9", owner="111",
                 props_extra={"cs_lead_owner": "777", "retention_owner": "888"}),
    ])
    assert rows["au1-9"]["owner_id"] == "777"              # first configured field wins
    assert rows["au1-9"]["csm_source"] == "cs_lead_owner"


def test_revert_to_record_owner_only(monkeypatch):
    monkeypatch.setenv("CS_CSM_FIELD", "hubspot_owner_id")
    assert sources.HubSpot._csm_fields() == ["hubspot_owner_id"]
    rows = _run_roster(monkeypatch, [
        _company("5", "AU1-5", owner="999",
                 props_extra={"retention_owner": "111"}),  # ignored: field list is owner-only
    ])
    assert rows["au1-5"]["owner_id"] == "999"
    assert rows["au1-5"]["csm_source"] == "hubspot_owner_id"
