#!/usr/bin/env python3
"""Platform data-access layer (LIVE-ONLY).

Single place the engine gets account data from. The account roster and every signal
come from the LIVE vendor adapters (HubSpot, Zendesk, Pendo, Stripe). There is NO
fixture/mock fallback in this path: if a source has no key (or a call fails), that
signal is returned empty and tagged as not live, and the UI renders "not connected".

The account roster comes from HubSpot (companies carrying an `account_id`/AUx-yyyyy).
With no HubSpot key the platform simply has no accounts to show — nothing is fabricated.

The fixtures under mcp-servers/fixtures/accounts.json are retained ONLY as a
deterministic test asset for the rules engine (orchestrate.py / tests), and are never
read by this live data-access layer.

Nothing here holds a secret; adapters read keys from the environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator"
sys.path.insert(0, str(PLUGIN))

try:
    from adapters import sources as _src
    _ADAPTERS = True
except Exception:  # noqa: BLE001
    _ADAPTERS = False


def _pull(is_live, live_call, label):
    """Return (value, source). LIVE-ONLY: no fixture fallback. When the source isn't
    live (no key) or the call fails, return an empty block tagged 'not_live'."""
    if not (_ADAPTERS and is_live):
        return {}, "not_live"
    try:
        return live_call(), "live"
    except Exception as exc:
        # Missing vendor records are normal in a partially mapped portfolio and
        # should render as "not connected", not flood the server log.
        from adapters import config as _config
        if isinstance(exc, _config.SourceError):
            if _config.env("CS_LOG_SOURCE_ERRORS") == "1":
                sys.stderr.write(f"[dataaccess] {label} unavailable: {exc}\n")
            # The adapter is configured, but this account is not mapped or has no
            # record. Keep the connector visible as connected; the UI can show
            # an honest "no data" state instead of falsely saying disconnected.
            return {}, "live_no_record"
        if _config.env("CS_LOG_SOURCE_ERRORS") == "1":
            sys.stderr.write(f"[dataaccess] {label} live failed: {type(exc).__name__}: {exc}\n")
        return {}, "live_unavailable"


def account(account_id: str) -> dict:
    """Assemble one account object (same shape the rules engine expects) from LIVE
    sources only, plus a `sources` provenance map. No mock/fixture data is used."""
    sources = {}

    hubspot, sources["hubspot"] = _pull(
        _ADAPTERS and _src.HUBSPOT.live(), lambda: _src.HUBSPOT.account(account_id), "hubspot")
    zendesk, sources["zendesk"] = _pull(
        _ADAPTERS and _src.ZENDESK.live(), lambda: _src.ZENDESK.tickets(account_id), "zendesk")
    usage, sources["usage"] = _pull(
        _ADAPTERS and _src.PENDO.live(), lambda: _src.PENDO.metrics(account_id), "pendo")
    jiminny, sources["jiminny"] = _pull(
        _ADAPTERS and _src.JIMINNY.live(), lambda: _src.JIMINNY.calls(account_id), "jiminny")
    onboarding, sources["onboarding"] = _pull(
        _ADAPTERS and _src.ROCKET_LANE.live(), lambda: _src.ROCKET_LANE.status(account_id), "rocket_lane")
    stripe, sources["stripe"] = _pull(
        _ADAPTERS and _src.STRIPE.live(), lambda: _src.STRIPE.payment(account_id), "stripe")

    # Real ML churn model (Redshift), when configured. Falls back to a transparent
    # signals-based score (never presented as an ML model) when it isn't.
    churn, sources["churn"] = _pull(
        _ADAPTERS and _src.CHURN.live(), lambda: _src.CHURN.score(account_id), "churn")
    if not churn:
        churn = _computed_risk(usage, zendesk, stripe, sources)
        if churn:
            sources["churn"] = "computed"

    return {
        "hubspot": hubspot,
        "zendesk": zendesk,
        "usage": usage,
        "jiminny": jiminny,
        "churn": churn,
        "stripe": stripe,
        "onboarding": onboarding,
        "sources": sources,
    }


def _computed_risk(usage: dict, zendesk: dict, stripe: dict, sources: dict) -> dict:
    """A transparent 0-1 churn-risk score from live signals (NOT an ML model).
    Only computed when at least one live risk input exists; otherwise returns {}."""
    have_live = (sources.get("usage") == "live") or (sources.get("zendesk") == "live") \
        or (sources.get("stripe") == "live")
    if not have_live:
        return {}

    score = 0.0
    reasons = []

    # Pendo risk advisor (live): High/Medium/Low.
    prisk = str(usage.get("pendo_risk_score") or "").lower()
    if prisk == "high":
        score += 0.45; reasons.append("Pendo risk advisor: High")
    elif prisk == "medium":
        score += 0.25; reasons.append("Pendo risk advisor: Medium")

    # Usage recency (live): long absence = disengagement. Scales with severity —
    # a multi-year absence is a far stronger churn signal than a 30-day one.
    dsv = usage.get("days_since_last_visit")
    if dsv is not None:
        if dsv >= 365:
            score += 0.55; reasons.append(f"no product visit in {dsv} days (>1 year)")
        elif dsv >= 90:
            score += 0.35; reasons.append(f"no product visit in {dsv} days")
        elif dsv >= 30:
            score += 0.15; reasons.append(f"no product visit in {dsv} days")

    # Zendesk (live): low CSAT and open Sev-1.
    csat = zendesk.get("csat_30d")
    if csat is not None and csat < 70:
        score += 0.15; reasons.append(f"CSAT {csat}")
    if (zendesk.get("sev1_open") or 0) > 0:
        score += 0.15; reasons.append("open Sev-1")

    # Stripe (live): past-due payments.
    if stripe.get("dunning_stage") == "day_15_plus":
        score += 0.20; reasons.append("payment 15+ days past due")
    elif (stripe.get("past_due_invoices") or 0) > 0:
        score += 0.10; reasons.append("past-due invoice")

    score = round(min(1.0, score), 2)
    return {
        "ml_churn_score": score,     # same key the engine/health read
        "computed": True,            # NOT an ML model
        "method": "weighted live signals (Pendo risk + recency + CSAT/Sev-1 + Stripe dunning)",
        "reasons": reasons,
    }


_CACHE: dict = {"ts": 0.0, "data": None}
from threading import Lock
_CACHE_LOCK = Lock()


def all_accounts() -> dict:
    """The full roster assembled from LIVE HubSpot, keyed by AUx-yyyyy. With no
    HubSpot key the roster is empty (the live product shows no accounts, never mocks).

    Cached in-process for CS_CACHE_TTL seconds (default 120) so a single page view
    doesn't re-fan-out live API calls across every endpoint hit."""
    import os
    import time
    if not (_ADAPTERS and _src.HUBSPOT.live()):
        return {}
    try:
        ttl = float(os.environ.get("CS_CACHE_TTL", "120"))
    except ValueError:
        ttl = 120.0
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["ts"]) < ttl:
        return _CACHE["data"]
    with _CACHE_LOCK:
        # Another request may have filled the cache while this request waited.
        now = time.time()
        if _CACHE["data"] is not None and (now - _CACHE["ts"]) < ttl:
            return _CACHE["data"]
        try:
            roster = _src.HUBSPOT.roster()
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("CS_LOG_SOURCE_ERRORS") == "1":
                sys.stderr.write(f"[dataaccess] HubSpot roster failed: {type(exc).__name__}: {exc}\n")
            return _CACHE["data"] or {}
        # Keep fan-out bounded because each account makes several vendor calls.
        from concurrent.futures import ThreadPoolExecutor
        try:
            workers = int(os.environ.get("CS_FETCH_WORKERS", "2"))
        except ValueError:
            workers = 2
        data: dict = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for aid, acct in zip(roster, ex.map(account, roster)):
                data[_src.identity.normalise(aid)] = acct
        # HubSpot roster membership is the live source of truth for instance
        # hierarchy. Attach the complete sibling family before orchestration so
        # suppression can distinguish primary from test/secondary instances.
        for aid, acct in data.items():
            hs = acct.get("hubspot", {})
            family = [ref for ref in roster if _src.identity.same_account(aid, ref)]
            if hs and family:
                hs["instances"] = [
                    {"instance_id": _src.identity.normalise(ref),
                     "instance_type": _src.identity.instance_type(ref)}
                    for ref in family
                ]
        _CACHE["data"], _CACHE["ts"] = data, now
        return data


def any_live() -> bool:
    """True if at least one source has a key set (for the UI 'live data' badge)."""
    if not _ADAPTERS:
        return False
    return any([_src.HUBSPOT.live(), _src.ZENDESK.live(), _src.PENDO.live(),
                _src.JIMINNY.live(), _src.ROCKET_LANE.live(), _src.STRIPE.live(), _src.CHURN.live()])


def live_sources() -> list[str]:
    if not _ADAPTERS:
        return []
    m = {"HubSpot": _src.HUBSPOT.live(), "Zendesk": _src.ZENDESK.live(),
         "Pendo": _src.PENDO.live(), "Jiminny": _src.JIMINNY.live(),
         "Rocket Lane": _src.ROCKET_LANE.live(),
         "Stripe": _src.STRIPE.live(),
         "Churn Model": _src.CHURN.live()}
    return [k for k, v in m.items() if v]
