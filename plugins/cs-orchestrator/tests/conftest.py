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

import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)


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

    yield

    orchestrate.set_account_provider(prev_provider)
    engine = sys.modules.get("engine")
    if engine is not None:
        engine.set_principal(prev_principal)
