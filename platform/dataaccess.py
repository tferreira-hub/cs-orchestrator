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
        churn = _computed_risk(usage, zendesk, stripe, jiminny, sources)
        if churn:
            sources["churn"] = "computed"

    # True license utilization comes from the entitlement/billing system, not Pendo.
    # When configured, merge it into the usage block so the expansion rule (>= 85%)
    # can fire on real seats-used data. Unconfigured / no record => data gap (None).
    entitlements, sources["entitlements"] = _pull(
        _ADAPTERS and _src.ENTITLEMENTS.live(),
        lambda: _src.ENTITLEMENTS.utilization(account_id), "entitlements")
    if entitlements.get("license_utilization_pct") is not None:
        usage = dict(usage)
        usage["license_utilization_pct"] = entitlements["license_utilization_pct"]
        usage["licensed_seats"] = entitlements.get("licensed_seats")
        usage["active_seats"] = entitlements.get("active_seats")

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


def _computed_risk(usage: dict, zendesk: dict, stripe: dict, jiminny: dict, sources: dict) -> dict:
    """A transparent 0-1 churn-risk score from live signals (NOT an ML model).
    Only computed when at least one live risk input exists; otherwise returns {}."""
    have_live = (sources.get("usage") == "live") or (sources.get("zendesk") == "live") \
        or (sources.get("stripe") == "live") or (sources.get("jiminny") == "live")
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

    # Jiminny (live): negative call sentiment is a leading relationship-risk signal.
    if str(jiminny.get("sentiment") or "").lower() == "negative":
        score += 0.15; reasons.append("negative call sentiment")

    score = round(min(1.0, score), 2)
    return {
        "ml_churn_score": score,     # same key the engine/health read
        "computed": True,            # NOT an ML model
        "method": "weighted live signals (Pendo risk + recency + CSAT/Sev-1 + Stripe dunning + Jiminny sentiment)",
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
        _merge_instance_families(data, roster)
        _CACHE["data"], _CACHE["ts"] = data, now
        return data


def _is_zendesk_live(acct: dict) -> bool:
    """True when this assembled account's Zendesk block came from the live adapter."""
    return acct.get("sources", {}).get("zendesk") == "live"


def _merge_instance_families(data: dict, roster: list[str]) -> dict:
    """Attach the sibling instance family to each account and fold live per-instance
    Zendesk volume into a single `by_instance` map, so multi-instance suppression
    (WoW §5) works on real Zendesk data — not just fixtures.

    Each sibling instance is fetched as its own roster entry with its own Zendesk
    org, so its ticket count already lives in data[sibling]["zendesk"]. We collect
    every live sibling's last-7d volume, keyed by normalised instance id, onto the
    account's Zendesk block. `suppression.ticket_spike_on_primary()` then compares
    primary vs test/secondary counts instead of a single self-referential entry.

    Mutates and returns `data`.
    """
    norm = _src.identity.normalise if _ADAPTERS else (lambda x: str(x).strip().lower().replace("_", "-"))
    inst_type = _src.identity.instance_type if _ADAPTERS else (lambda x: "primary")
    same = _src.identity.same_account if _ADAPTERS else (lambda a, b: norm(a) == norm(b))

    for aid, acct in data.items():
        hs = acct.get("hubspot", {})
        family = [ref for ref in roster if same(aid, ref)]
        if not (hs and family):
            continue
        hs["instances"] = [
            {"instance_id": norm(ref), "instance_type": inst_type(ref)}
            for ref in family
        ]
        if not _is_zendesk_live(acct):
            continue
        by_instance: dict[str, int] = {}
        total_last7 = 0
        total_prev7 = 0
        for ref in family:
            sib = data.get(norm(ref))
            sib_zd = (sib or {}).get("zendesk") or {}
            # Only count siblings whose Zendesk record is genuinely live.
            if not _is_zendesk_live(sib or {}):
                continue
            inst_id = norm(ref)
            # Prefer the sibling's own by_instance entry; fall back to its
            # tickets_last_7d total (self-attributed to that instance).
            sib_counts = sib_zd.get("by_instance") or {}
            count = sib_counts.get(inst_id)
            if count is None:
                count = sib_zd.get("tickets_last_7d") or 0
            by_instance[inst_id] = count
            total_last7 += sib_zd.get("tickets_last_7d") or 0
            total_prev7 += sib_zd.get("tickets_prev_7d") or 0
        if by_instance:
            zd = acct.setdefault("zendesk", {})
            zd["by_instance"] = by_instance
            # Use family-wide totals so a spike concentrated on a test sibling is
            # DETECTED at the account level and then correctly SUPPRESSED as
            # non-primary-driven — producing the visible "Suppressed" entry rather
            # than silently never firing.
            if len(by_instance) > 1:
                zd["tickets_last_7d"] = total_last7
                zd["tickets_prev_7d"] = total_prev7
    return data


def any_live() -> bool:
    """True if at least one source has a key set (for the UI 'live data' badge)."""
    if not _ADAPTERS:
        return False
    return any([_src.HUBSPOT.live(), _src.ZENDESK.live(), _src.PENDO.live(),
                _src.JIMINNY.live(), _src.ROCKET_LANE.live(), _src.STRIPE.live(),
                _src.CHURN.live(), _src.ENTITLEMENTS.live()])


def live_sources() -> list[str]:
    if not _ADAPTERS:
        return []
    m = {"HubSpot": _src.HUBSPOT.live(), "Zendesk": _src.ZENDESK.live(),
         "Pendo": _src.PENDO.live(), "Jiminny": _src.JIMINNY.live(),
         "Rocket Lane": _src.ROCKET_LANE.live(),
         "Stripe": _src.STRIPE.live(),
         "Churn Model": _src.CHURN.live(),
         "Entitlements": _src.ENTITLEMENTS.live()}
    return [k for k, v in m.items() if v]
