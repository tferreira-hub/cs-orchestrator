"""Shared test isolation.

Several modules mutate process-wide globals: orchestrate._ACCOUNT_PROVIDER (the
pluggable account source) and engine's current principal. Historically some tests set
these without restoring them, so suite-order determined pass/fail (e.g. a test that
left the provider pointing at raw fixtures broke a later per-principal scoping
assertion, and vice-versa).

This autouse fixture snapshots both globals before each test and restores them after,
so a test's provider/principal mutation can never leak into the next test. It is a
no-op for tests that don't touch them.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _force_offline() -> None:
    """Hard offline guard for the whole test session.

    The adapters go live whenever a vendor token is present in the environment
    (config.live_enabled() defaults True; HubSpot.live() = live_enabled() and a token).
    If a developer has a real .env / HUBSPOT_TOKEN exported, importing config auto-loads
    it (config._load_dotenv) and tests that spawn the roster warm/deal-renewal daemon
    threads would then make REAL vendor calls (observed as HTTP 401 from a background
    thread during pytest).

    We eliminate that entire class of leak deterministically:
      * CS_USE_LIVE=0 forces config.live_enabled() False everywhere, so every adapter
        .live() is False regardless of any token that happens to be set.
      * CS_ROSTER_DEAL_RENEWAL=0 disables the background deal-renewal enrichment thread
        so no orphaned daemon can outlive a test's monkeypatch and hit the network.
      * Vendor tokens are cleared as belt-and-braces so even code paths that bypass
        live_enabled() have no credentials to use.
    Individual tests that need liveness still monkeypatch `.live()`/the adapter object
    directly (they never rely on real tokens), so this does not weaken any coverage."""
    os.environ["CS_USE_LIVE"] = "0"
    os.environ["CS_ROSTER_DEAL_RENEWAL"] = "0"
    # Disable the stale-while-revalidate report cache in tests so each test's monkeypatched
    # data is built fresh and synchronously (never served a stale/"warming" placeholder, and
    # never spawning a background build thread that would hit a live source). Set here at the
    # session guard — not only in the per-test fixture — so it holds even for the FIRST test
    # that imports engine (the fixture's engine check runs before engine is in sys.modules).
    os.environ["CS_REPORT_CACHE_TTL"] = "0"
    os.environ.pop("CS_ALLOW_WRITE", None)
    for tok in ("HUBSPOT_TOKEN", "ZENDESK_TOKEN", "ZENDESK_EMAIL", "ZENDESK_SUBDOMAIN",
                "STRIPE_API_KEY", "PENDO_API_KEY", "JIMINNY_API_KEY", "ROCKETLANE_TOKEN"):
        os.environ.pop(tok, None)


_force_offline()


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    """Re-assert the offline guard at session start, after any plugin/env loading."""
    _force_offline()


@pytest.fixture(autouse=True)
def _isolate_shared_globals():
    import orchestrate
    prev_provider = orchestrate._ACCOUNT_PROVIDER

    # engine may not be imported by every test; only snapshot its principal if present.
    engine = sys.modules.get("engine")
    prev_principal = engine.get_principal() if engine is not None else None

    # Pin a deterministic baseline for EVERY test: the raw fixture loader. Importing
    # engine has an import-time side effect (it installs engine._scoped_accounts as the
    # provider), so without this reset the provider that a test sees would depend on
    # whether some earlier test imported engine. Tests that exercise per-principal
    # scoping install engine._scoped_accounts themselves.
    orchestrate.set_account_provider(orchestrate._load)
    if engine is not None:
        engine.set_principal(None)
        # Disable the short-TTL report cache in tests so each test's monkeypatched data is
        # seen fresh (no stale cross-test reads), and clear any entries from prior tests.
        os.environ["CS_REPORT_CACHE_TTL"] = "0"
        if hasattr(engine, "_REPORT_CACHE"):
            engine._REPORT_CACHE.clear()
        # Isolate the inbound queue: start empty and mark it already-loaded so _load_inbound
        # does NOT read a stray on-disk store between tests. Tests that exercise persistence
        # set CS_INBOUND_FILE + reset the flag themselves. Mirrors the presence isolation.
        if hasattr(engine, "_INBOUND_QUEUE"):
            engine._INBOUND_QUEUE.clear()
            engine._INBOUND_QUEUE_LOADED = True
            os.environ["CS_INBOUND_FILE"] = os.path.join(
                os.environ.get("TMPDIR", "/tmp"), ".cs-inbound-test-isolated.jsonl")

    yield

    orchestrate.set_account_provider(prev_provider)
    engine = sys.modules.get("engine")
    if engine is not None:
        engine.set_principal(prev_principal)
        if hasattr(engine, "_INBOUND_QUEUE"):
            engine._INBOUND_QUEUE.clear()
            engine._INBOUND_QUEUE_LOADED = True

    # Clear the process-global HubSpot roster warm flags + cache so a background warm
    # thread that a test left in flight cannot make a later test order-dependent (the
    # _roster_warming / _roster_renewal_filling flags are set on the CLASS and were not
    # otherwise reset). CS_USE_LIVE=0 + CS_ROSTER_DEAL_RENEWAL=0 already prevent any
    # real network call; this just stops stale state leaking across tests.
    _sources = sys.modules.get("adapters.sources")
    if _sources is not None and hasattr(_sources, "HubSpot"):
        hs_cls = _sources.HubSpot
        hs_cls._roster_warming = False
        hs_cls._roster_renewal_filling = False
        if hasattr(hs_cls, "_full_roster_cache"):
            try:
                delattr(hs_cls, "_full_roster_cache")
            except AttributeError:
                pass
