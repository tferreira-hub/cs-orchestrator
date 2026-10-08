"""Contract tests for the unified whole-book portfolio merge.

These cover the `feat/unified-filter-whole-book` work:
  * engine.portfolio() merges the cheap full-roster scan (Tier-1 roster rows) into the
    deeply-enriched slice so every view/filter sees the whole customer book;
  * enriched rows are tagged enriched=True with a cohort, roster rows enriched=False;
  * churned customers are excluded by default and included via CS_INCLUDE_CHURNED;
  * the summary carries book_total / book_managed / book_pooled / enriched_count;
  * HubSpot.list_all_companies(cached_only=True) is non-blocking on a cold cache
    (returns immediately, warms in the background) and serves a warm cache verbatim.

All HubSpot/vendor access is monkeypatched; no network. Mirrors the mocking style in
test_auth_scoping.py / test_integration_contracts.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLATFORM))


@pytest.fixture(autouse=True)
def _restore_state():
    """Clear the principal after each test. Provider/principal isolation is handled by
    the shared conftest; this just guarantees these whole-book tests start/end clean."""
    yield
    import engine
    engine.set_principal(None)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _one_enriched_account():
    """A single deeply-enriched account (the signalled slice)."""
    return {
        "au1-enriched": {
            "hubspot": {"name": "Enriched Co", "segment": "Strategic", "arr_usd": 120000,
                        "csm_owner": "Owner One", "csm_owner_id": "owner-1", "contacts": [],
                        "account_id": "au1-enriched",
                        "instances": [{"instance_id": "au1-enriched", "instance_type": "primary"}]},
            "sources": {"hubspot": "live"}, "zendesk": {}, "usage": {}, "churn": {},
            "stripe": {}, "jiminny": {}, "onboarding": {},
        }
    }


def _roster_book():
    """The cheap whole-book roster: the enriched one PLUS extra managed/pooled/churned."""
    return [
        # Same id as the enriched slice -> should be de-duped (not double-counted in rows).
        {"account_id": "au1-enriched", "name": "Enriched Co", "cohort": "managed",
         "segment_label": "Strategic", "arr_usd": 120000, "lifecycle_stage": "customer",
         "owner_id": "owner-1"},
        {"account_id": "au1-managed", "name": "Managed Co", "cohort": "managed",
         "segment_label": "Mid-Market", "arr_usd": 40000, "lifecycle_stage": "customer",
         "owner_id": "owner-2"},
        {"account_id": "au1-pooled", "name": "Pooled Co", "cohort": "pooled",
         "segment_label": "Agency 1-2 Users", "arr_usd": 3000, "lifecycle_stage": "customer",
         "owner_id": None},
        {"account_id": "au1-churned", "name": "Churned Co", "cohort": "pooled",
         "segment_label": "Corporate", "arr_usd": 0, "lifecycle_stage": "Churned Customer",
         "owner_id": None},
    ]


def _patch_portfolio(monkeypatch, roster, *, live=True):
    """Wire engine.portfolio() to a fixed enriched slice + roster, no network.

    The enriched slice is injected through orchestrate.set_account_provider (the same
    seam the other contract tests use). The _restore_state autouse fixture puts the
    default provider and principal back after each test, so these tests stay
    order-independent within the full suite.
    """
    import engine

    engine.orchestrate.set_account_provider(_one_enriched_account)

    class _FakeHS:
        def live(self):
            return live

        def list_all_companies(self, limit=None, cached_only=False):
            return list(roster)

        def _owner_name(self, oid):
            # Mirror the live adapter: resolve a HubSpot owner id to a CSM name.
            return {"owner-1": "Owner One", "owner-2": "Owner Two"}.get(str(oid))

    monkeypatch.setattr(engine._src, "HUBSPOT", _FakeHS())
    engine.set_principal(None)  # open mode -> no owner scoping narrows the enriched slice
    return engine


# --------------------------------------------------------------------------- #
# Whole-book merge
# --------------------------------------------------------------------------- #
def test_merge_adds_roster_rows_and_dedupes_enriched(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch_portfolio(monkeypatch, _roster_book())
    accts = engine.portfolio()["accounts"]
    ids = {a["account_id"] for a in accts}

    # Enriched account appears exactly once (roster duplicate de-duped).
    assert sum(1 for a in accts if a["account_id"] == "au1-enriched") == 1
    # Active managed + pooled roster rows merged in.
    assert "au1-managed" in ids
    assert "au1-pooled" in ids
    # Churned excluded by default.
    assert "au1-churned" not in ids

    by_id = {a["account_id"]: a for a in accts}
    # Enriched slice is tagged enriched=True; roster rows enriched=False.
    assert by_id["au1-enriched"]["enriched"] is True
    assert by_id["au1-managed"]["enriched"] is False
    assert by_id["au1-pooled"]["enriched"] is False
    # Roster rows resolve the HubSpot owner id to a CSM NAME (not the numeric id), so the
    # Owner filter shows names. owner-2 -> "Owner Two"; a null owner stays None.
    assert by_id["au1-managed"]["csm_owner"] == "Owner Two"
    assert by_id["au1-managed"]["csm_owner_id"] == "owner-2"
    assert by_id["au1-pooled"]["csm_owner"] is None
    # Roster rows carry a cohort for the global filter.
    assert by_id["au1-managed"]["cohort"] == "managed"
    assert by_id["au1-pooled"]["cohort"] == "pooled"
    assert by_id["au1-pooled"]["pooled"] is True
    # Roster rows are honestly non-computable (no fabricated green health).
    assert by_id["au1-managed"]["health"]["computable"] is False


def test_churned_included_when_env_set(monkeypatch):
    monkeypatch.setenv("CS_INCLUDE_CHURNED", "true")
    engine = _patch_portfolio(monkeypatch, _roster_book())
    out = engine.portfolio()
    ids = {a["account_id"] for a in out["accounts"]}
    assert "au1-churned" in ids
    churned = next(a for a in out["accounts"] if a["account_id"] == "au1-churned")
    assert churned["churned"] is True
    assert out["summary"]["churned_included"] is True


def test_summary_book_counts(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch_portfolio(monkeypatch, _roster_book())
    summary = engine.portfolio()["summary"]
    # book_total counts the entire roster scan (all 4), regardless of churn filtering.
    assert summary["book_total"] == 4
    assert summary["book_managed"] == 2          # au1-enriched + au1-managed
    assert summary["book_pooled"] == 2           # au1-pooled + au1-churned
    assert summary["enriched_count"] == 1        # the deeply-signalled slice
    assert summary["churned_included"] is False


def test_merge_is_noop_when_hubspot_not_live(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch_portfolio(monkeypatch, _roster_book(), live=False)
    out = engine.portfolio()
    ids = {a["account_id"] for a in out["accounts"]}
    # Only the enriched slice remains; no roster rows merged.
    assert ids == {"au1-enriched"}
    assert out["summary"]["book_total"] == 0


# --------------------------------------------------------------------------- #
# HubSpot.list_all_companies(cached_only=...) non-blocking behaviour
# --------------------------------------------------------------------------- #
def test_cached_only_cold_cache_is_nonblocking(monkeypatch):
    """Cold cache + cached_only=True must return immediately ([]) WITHOUT waiting for
    the expensive paginated scan. We prove non-blocking by making the mocked HTTP scan
    block on a gate: the caller must return before the gate is released."""
    import threading
    import time
    from adapters import sources

    hs = sources.HubSpot()
    monkeypatch.setattr(hs, "live", lambda: True)
    # Ensure cache is cold and no warm is in flight.
    if hasattr(sources.HubSpot, "_full_roster_cache"):
        delattr(sources.HubSpot, "_full_roster_cache")
    sources.HubSpot._roster_warming = False

    gate = threading.Event()          # held closed so the background scan cannot finish
    scan_started = threading.Event()

    def _blocking_post(*_a, **_k):
        scan_started.set()
        gate.wait(timeout=5)          # background thread parks here until released
        return {"results": []}

    monkeypatch.setattr(sources.config, "http_post", _blocking_post)

    t0 = time.time()
    out = hs.list_all_companies(cached_only=True)
    elapsed = time.time() - t0

    assert out == []                  # returns immediately on cold cache
    assert elapsed < 1.0              # caller did NOT wait on the blocking scan
    # The warm work was handed to a background thread (it may or may not have reached
    # the HTTP call yet). Release the gate and let it unwind cleanly.
    gate.set()
    for _ in range(100):
        if not getattr(sources.HubSpot, "_roster_warming", False):
            break
        time.sleep(0.05)
    assert getattr(sources.HubSpot, "_roster_warming", False) is False
    # Clean up any cache the warm thread populated.
    if hasattr(sources.HubSpot, "_full_roster_cache"):
        delattr(sources.HubSpot, "_full_roster_cache")


def test_cached_only_serves_warm_cache(monkeypatch):
    """A warm, unexpired cache is served verbatim without any HTTP calls."""
    import time
    from adapters import sources

    hs = sources.HubSpot()
    monkeypatch.setattr(hs, "live", lambda: True)
    warm_rows = [{"account_id": "warm-1", "name": "Warm Co", "cohort": "managed"}]
    # (timestamp, rows, limit_covered) — fresh now, covers the default 5000 limit.
    sources.HubSpot._full_roster_cache = (time.time(), warm_rows, 5000)

    calls = {"http": 0}
    monkeypatch.setattr(sources.config, "http_post",
                        lambda *a, **k: calls.__setitem__("http", calls["http"] + 1) or {"results": []})

    out = hs.list_all_companies(cached_only=True)
    assert out == warm_rows
    assert calls["http"] == 0
    delattr(sources.HubSpot, "_full_roster_cache")


def test_scan_fetches_active_stage_before_churned(monkeypatch):
    """The Tier-1 scan must page the ACTIVE 'customer' stage fully BEFORE the churned
    '20251280' stage, so active accounts are never crowded out of a bounded/partial
    roster by the much larger churned bucket. We mock http_post per stage and assert
    (a) each stage is queried with an EQ filter in order, and (b) under a tight limit
    the active rows win and churned is truncated."""
    from adapters import sources

    hs = sources.HubSpot()
    monkeypatch.setattr(hs, "live", lambda: True)
    monkeypatch.delenv("CS_HUBSPOT_LIFECYCLE_STAGES", raising=False)
    if hasattr(sources.HubSpot, "_full_roster_cache"):
        delattr(sources.HubSpot, "_full_roster_cache")

    stage_order = []

    def _post(url, headers, body, *a, **k):
        f = body["filterGroups"][0]["filters"][0]
        assert f["operator"] == "EQ"          # one stage at a time, never IN
        stage = f["value"]
        if not body.get("after"):
            stage_order.append(stage)
        # 3 'customer' rows, then (would-be) 3 churned rows, single page each.
        if stage == "customer":
            return {"results": [{"id": f"c{i}", "properties": {"lifecyclestage": "customer",
                     "account_id": f"AU1-{i}", "name": f"Active {i}"}} for i in range(3)]}
        return {"results": [{"id": f"x{i}", "properties": {"lifecyclestage": "20251280",
                 "name": f"Churned {i}"}} for i in range(3)]}

    monkeypatch.setattr(sources.config, "http_post", _post)

    # Full scan: active first, then churned.
    rows = hs.list_all_companies(limit=5000)
    assert stage_order == ["customer", "20251280"]       # active queried first
    assert [r["lifecycle_stage"] for r in rows[:3]] == ["Customer", "Customer", "Customer"]
    delattr(sources.HubSpot, "_full_roster_cache")

    # Tight limit: active fills it, churned is truncated out entirely.
    stage_order.clear()
    rows = hs.list_all_companies(limit=3)
    assert len(rows) == 3
    assert all(r["lifecycle_stage"] == "Customer" for r in rows)   # churned crowded OUT, not active
    delattr(sources.HubSpot, "_full_roster_cache")
