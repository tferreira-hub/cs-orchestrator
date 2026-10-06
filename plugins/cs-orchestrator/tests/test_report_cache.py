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
    import engine, time
    monkeypatch.setenv("CS_REPORT_CACHE_TTL", "60")
    engine.set_principal(None)
    calls = {"n": 0}
    def build():
        calls["n"] += 1
        return {"value": calls["n"]}
    # Cold miss returns a warming placeholder immediately and builds in the background.
    first = engine._cached_report("t1", build)
    assert first.get("warming") is True
    # Wait for the background build to populate the cache.
    for _ in range(50):
        if ("t1", "admin") in engine._REPORT_CACHE:
            break
        time.sleep(0.02)
    # Now subsequent calls are served fresh from cache (no further builds).
    a = engine._cached_report("t1", build)
    b = engine._cached_report("t1", build)
    assert a == b == {"value": 1}
    assert calls["n"] == 1  # build ran exactly once (in the background)


def test_cold_miss_returns_warming_not_blocking(monkeypatch):
    """A cold miss must return a {warming:true} placeholder IMMEDIATELY (not block on
    the build) and populate the cache in the background."""
    import engine, time
    monkeypatch.setenv("CS_REPORT_CACHE_TTL", "60")
    engine.set_principal(None)
    def slow_build():
        time.sleep(0.3)  # simulate a slow fan-out
        return {"value": "real"}
    t0 = time.time()
    r = engine._cached_report("slowrep", slow_build)
    elapsed = time.time() - t0
    assert r.get("warming") is True, "cold miss must return a warming placeholder"
    assert elapsed < 0.2, f"cold miss must not block on the build (took {elapsed:.2f}s)"
    # Background build eventually populates the cache.
    for _ in range(60):
        if ("slowrep", "admin") in engine._REPORT_CACHE:
            break
        time.sleep(0.02)
    real = engine._cached_report("slowrep", slow_build)
    assert real == {"value": "real"}


def test_warm_reports_populates_admin_cache(monkeypatch):
    """warm_reports() (boot warm) must build synchronously and populate the admin-scope
    cache directly, so the first admin page load is a fresh hit, not a warming placeholder."""
    import engine
    monkeypatch.setenv("CS_REPORT_CACHE_TTL", "60")
    engine.set_principal(None)
    # Patch the heavy builds so the test is hermetic and fast (no live fan-out).
    monkeypatch.setattr(engine, "_onboarding_governance_build", lambda: {"connected": True, "projects": []})
    monkeypatch.setattr(engine, "_payment_risk_report_build", lambda: {"stripe_connected": True, "pages": {}})
    engine.warm_reports()
    # Both reports should now be cached under the admin key.
    assert ("onboarding_governance", "admin") in engine._REPORT_CACHE
    assert ("payment_risk_report", "admin") in engine._REPORT_CACHE
    # And reading them returns the real dict, not a warming placeholder.
    r = engine._cached_report("onboarding_governance", engine._onboarding_governance_build)
    assert not r.get("warming"), "admin read after warm_reports must be a fresh hit"


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
    import engine, time
    monkeypatch.setenv("CS_REPORT_CACHE_TTL", "60")
    calls = {"n": 0}
    def build():
        calls["n"] += 1
        return {"scope": engine._scope_key(), "n": calls["n"]}
    # Admin cold miss -> warming; wait for bg build.
    engine.set_principal({"role": "admin"})
    first = engine._cached_report("t3", build)
    assert first.get("warming") is True
    for _ in range(50):
        if ("t3", "admin") in engine._REPORT_CACHE:
            break
        time.sleep(0.02)
    admin_r = engine._cached_report("t3", build)
    # CSM cold miss -> warming; wait for bg build.
    engine.set_principal({"role": "csm", "owner_id": "owner-7", "email": "c@x.com"})
    first2 = engine._cached_report("t3", build)
    assert first2.get("warming") is True
    for _ in range(50):
        if ("t3", "csm:owner-7") in engine._REPORT_CACHE:
            break
        time.sleep(0.02)
    csm_r = engine._cached_report("t3", build)
    assert admin_r["scope"] == "admin"
    assert csm_r["scope"] == "csm:owner-7"
    assert calls["n"] == 2  # distinct scopes -> distinct cache entries, build ran twice
    engine.set_principal(None)
