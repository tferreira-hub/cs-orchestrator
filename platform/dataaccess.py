#!/usr/bin/env python3
"""Platform data-access layer.

Single place the engine gets account data from. For each account it pulls every
signal through the live adapters (HubSpot, Zendesk, Pendo, Stripe, Jiminny) when a
key is present, and falls back to the fixture for that signal otherwise. Every signal
is tagged with its source ('live' or 'sample') so the UI can show provenance.

The account list itself comes from the fixtures (the AUx-yyyyy roster). In production
the roster would come from HubSpot; here the fixture ids ARE the AUx-yyyyy keys used
to look each account up in every system.

Nothing here holds a secret; adapters read keys from the environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator"
sys.path.insert(0, str(PLUGIN))

import orchestrate  # noqa: E402  (fixture roster + rules)
try:
    from adapters import with_fallback
    from adapters import sources as _src
    _ADAPTERS = True
except Exception:  # noqa: BLE001
    _ADAPTERS = False


def _pull(is_live, live_call, fixture_val, label):
    """Return (value, source) where source is 'live' or 'sample'."""
    if not (_ADAPTERS and is_live):
        return fixture_val, "sample"
    try:
        return live_call(), "live"
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[dataaccess] {label} live failed, using sample: {type(exc).__name__}: {exc}\n")
        return fixture_val, "sample"


def account(account_id: str) -> dict:
    """Assemble one account object (same shape the rules engine expects) from live
    sources with per-signal fixture fallback, plus a `sources` provenance map."""
    fx = orchestrate._load().get(account_id, {})
    sources = {}

    hubspot, sources["hubspot"] = _pull(
        _ADAPTERS and _src.HUBSPOT.live(), lambda: _src.HUBSPOT.account(account_id),
        fx.get("hubspot", {}), "hubspot")
    # Merge: keep fixture values where the live source returns null, so routing fields
    # (segment, renewal, owner, contacts, instances) never regress to None on live data.
    if sources["hubspot"] == "live":
        fxh = fx.get("hubspot", {})
        merged = dict(fxh)
        for k, v in hubspot.items():
            if v is not None and v != [] and v != "":
                merged[k] = v
        for k in ("segment", "renewal_date", "csm_owner", "contacts", "instances"):
            if not merged.get(k) and fxh.get(k):
                merged[k] = fxh[k]
        hubspot = merged
    zendesk, sources["zendesk"] = _pull(
        _ADAPTERS and _src.ZENDESK.live(), lambda: _src.ZENDESK.tickets(account_id),
        fx.get("zendesk", {}), "zendesk")
    usage, sources["usage"] = _pull(
        _ADAPTERS and _src.PENDO.live(), lambda: _src.PENDO.metrics(account_id),
        fx.get("usage", {}), "pendo")
    stripe, sources["stripe"] = _pull(
        _ADAPTERS and _src.STRIPE.live(), lambda: _src.STRIPE.payment(account_id),
        fx.get("stripe", {}), "stripe")
    jiminny, sources["jiminny"] = _pull(
        _ADAPTERS and _src.JIMINNY.live(), lambda: _src.JIMINNY.calls(account_id),
        fx.get("jiminny", {}), "jiminny")

    return {
        "hubspot": hubspot,
        "zendesk": zendesk,
        "usage": usage,
        "churn": fx.get("churn", {}),          # churn model: fixture unless CHURN_API wired
        "stripe": stripe,
        "jiminny": jiminny,
        "onboarding": fx.get("onboarding", {}),
        "sources": sources,
    }


def all_accounts() -> dict:
    """The full roster assembled from live sources (with fallback), keyed by AUx-yyyyy."""
    return {aid: account(aid) for aid in orchestrate._load()}


def any_live() -> bool:
    """True if at least one source has a key set (for the UI 'live data' badge)."""
    if not _ADAPTERS:
        return False
    return any([_src.HUBSPOT.live(), _src.ZENDESK.live(), _src.PENDO.live(),
                _src.STRIPE.live(), _src.JIMINNY.live()])


def live_sources() -> list[str]:
    if not _ADAPTERS:
        return []
    m = {"HubSpot": _src.HUBSPOT.live(), "Zendesk": _src.ZENDESK.live(),
         "Pendo": _src.PENDO.live(), "Stripe": _src.STRIPE.live(), "Jiminny": _src.JIMINNY.live()}
    return [k for k, v in m.items() if v]
