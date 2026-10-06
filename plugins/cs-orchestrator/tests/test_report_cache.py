"""Tests for the short-TTL report cache (engine._cached_report) used by the payment-risk
and onboarding-governance pages to avoid re-fanning-out to Stripe/Rocket Lane on every
load. Verifies memoisation within TTL, per-scope keying, and the TTL=0 bypass."""

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
def _clean(monkeypatch):
    import engine
    engine._REPORT_CACHE.clear()
    yield
    engine._REPORT_CACHE.clear()
    engine.set_principal(None)


def test_cache_memoises_within_ttl(monkeypatch):
    import engine
    monkeypatch.setenv("CS_REPORT_CACHE_TTL", "60")
    engine.set_principal(None)
    calls = {"n": 0}
    def build():
        calls["n"] += 1
        return {"value": calls["n"]}
    a = engine._cached_report("t1", build)
    b = engine._cached_report("t1", build)
    assert a == b == {"value": 1}
    assert calls["n"] == 1  # build ran once; second call served from cache


def test_cache_ttl_zero_bypasses(monkeypatch):
    import engine
    monkeypatch.setenv("CS_REPORT_CACHE_TTL", "0")
    engine.set_principal(None)
    calls = {"n": 0}
    def build():
        calls["n"] += 1
        return {"value": calls["n"]}
    engine._cached_report("t2", build)
    engine._cached_report("t2", build)
    assert calls["n"] == 2  # no caching: build ran each time


def test_cache_is_scope_keyed(monkeypatch):
    """A CSM must never be served the admin's (or another CSM's) cached result."""
    import engine
    monkeypatch.setenv("CS_REPORT_CACHE_TTL", "60")
    calls = {"n": 0}
    def build():
        calls["n"] += 1
        return {"scope": engine._scope_key(), "n": calls["n"]}
    engine.set_principal({"role": "admin"})
    admin_r = engine._cached_report("t3", build)
    engine.set_principal({"role": "csm", "owner_id": "owner-7", "email": "c@x.com"})
    csm_r = engine._cached_report("t3", build)
    assert admin_r["scope"] == "admin"
    assert csm_r["scope"] == "csm:owner-7"
    assert calls["n"] == 2  # distinct scopes -> distinct cache entries, build ran twice
    engine.set_principal(None)
