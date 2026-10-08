#!/usr/bin/env python3
"""CS Platform, engine layer.

Wraps the WoW orchestration (orchestrate.py) and adds the account **health score**
and portfolio roll-up the single-pane-of-glass UI needs. Kept dependency-free and
importable so both the API and the CLI/agent use the same logic (single source of
truth, WoW §1).

Module map
----------
This is a large single module BY DESIGN: the server, CLI, and agent all import it
as one `engine` namespace, and the test suite patches many of its internals by that
name (e.g. engine._src, engine._scoped_accounts, engine._INBOUND_QUEUE). Splitting it
into packages would break those import/patch call-sites for no behavioural gain, so it
stays one file. Use this map to navigate it:

  1. Identity scoping & RBAC         set/get_principal, _scoped_accounts, can_view_account,
                                     can_write_account, required_roles_missing, ForbiddenError
  2. Live-only data projection       _is_live, _live_block, _live_account, _connected
  3. Scoring (deterministic)         health_score, renewal_forecast, adoption_score,
                                     expansion_score, _roster_band
  4. Portfolio / roster / detail     portfolio, full_roster, executive_summary,
                                     account_detail, enrich_rows, daily_brief, why_not,
                                     trend_risks, _health_* trajectory helpers
  5. Write actions (two-gated)       writeback, enrol_sequence, log_note, tag_contact_role,
                                     monthly_digest/send_digest/run_monthly_digests,
                                     create_csql, move_to_pooled, dispatch_reviewed_digests
  6. Zendesk ticket writes           _ticket_account_ref, _authorise_ticket_write,
                                     zendesk_reply, zendesk_set_status
  7. Inbound / pooled / presence     record_inbound, inbound_queue, resolve_inbound,
                                     pooled_roster/cohort, set_csm_availability, _presence_for
  8. Strategic monthly review        monthly_review_queue, add_review_comment, approve_digest
  9. KPIs / retention / capacity     kpis, _retention_metrics, _task_metrics, _capacity_per_csm
 10. Lifecycle / integrations / gaps lifecycle(_state), integrations, datagaps,
                                     revenue_motion, expansion_opportunities, payment_risk_report

Shared module state (why this is one module): the request-scoped principal
(_REQUEST thread-local), the in-process inbound queue (_INBOUND_QUEUE) and CSM
presence (_CSM_PRESENCE), and the import-time installation of _scoped_accounts as
orchestrate's account provider.
"""

from __future__ import annotations

import os
import json
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator"
sys.path.insert(0, str(Path(__file__).resolve().parent))  # platform/ (for dataaccess)
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "hooks" / "scripts"))

import orchestrate  # noqa: E402  (reference rules engine)
from suppression import primary_instance_ids, suppressed_signals  # noqa: E402

# Route the rules engine and the platform through the live data-access layer
# (live per-source with fixture fallback). One source of truth for UI + agent.
import dataaccess  # noqa: E402
from adapters import sources as _src  # noqa: E402

# --------------------------------------------------------------------------- #
# Per-request identity scoping
# --------------------------------------------------------------------------- #
# A CSM sees ONLY the accounts they own; an Admin sees all. We enforce this at the
# single source of truth: the account provider the rules engine reads from. The
# server sets the authenticated principal per request (thread-local); every
# downstream computation (portfolio, KPIs, data gaps, lifecycle, tasks, and the
# agent, which all read orchestrate.load_accounts()) is then automatically scoped,
# so no endpoint can accidentally leak another CSM's book.
import threading  # noqa: E402

_REQUEST = threading.local()


class ForbiddenError(Exception):
    """Raised when the current principal may not access a specific account (-> 403)."""


def set_principal(principal: dict | None) -> None:
    """Set the authenticated principal for the current request thread.
    principal = {"email","name","role","owner_id",...} or None (unscoped/admin)."""
    _REQUEST.principal = principal


def get_principal() -> dict | None:
    return getattr(_REQUEST, "principal", None)


def _owns(account: dict, owner_id: str | None) -> bool:
    return bool(owner_id) and str(account.get("hubspot", {}).get("csm_owner_id") or "") == str(owner_id)


# --------------------------------------------------------------------------- #
# TTL memo cache with stale-while-revalidate for expensive live reports
# --------------------------------------------------------------------------- #
# Reports like the payment-risk and onboarding-governance views each fan out to a vendor
# (Stripe / Rocket Lane), which is slow. We cache the result per (report, scope) for a
# long TTL (default 600s, matching the roster cache) and serve STALE results instantly
# while refreshing in the background, so the page is NEVER slow after the initial warm.
# Keyed by scope so a CSM is never served another's data. CS_REPORT_CACHE_TTL tunes it;
# set 0 to disable for tests.
_REPORT_CACHE: dict = {}
_REPORT_REFRESHING: set = set()


def _report_cache_ttl() -> float:
    try:
        return float(os.environ.get("CS_REPORT_CACHE_TTL", "600"))
    except ValueError:
        return 600.0


# Whole-book warehouse metrics (committed/active seats, revenue, NDR inputs) from
# rpt_account_ndr_monthly, loaded in ONE batched Redshift query rather than a per-account
# fan-out. Cached module-wide with the roster TTL and refreshed in the background, so the
# hot portfolio path NEVER blocks on Redshift. {} until the first warm completes (then
# every account that has a warehouse row gets licence utilisation + NDR inputs).
_BATCH_METRICS: dict = {"at": 0.0, "data": {}}
_BATCH_METRICS_REFRESHING: bool = False


def _batch_metrics_for(account_ids: list) -> dict:
    """Return cached whole-book metrics keyed by uppercase-hyphen ref, refreshing in the
    background when stale. Non-blocking: returns whatever is cached now (possibly {}) and
    never waits on Redshift from a request path. Disabled when the metrics source is not
    live, so fixtures/tests are unaffected."""
    import time
    import threading
    global _BATCH_METRICS_REFRESHING
    try:
        if not _src.ACCOUNT_METRICS.live():
            return {}
    except Exception:  # noqa: BLE001
        return {}
    ttl = _report_cache_ttl()
    now = time.time()
    fresh = _BATCH_METRICS["data"] and (now - _BATCH_METRICS["at"]) < ttl
    if not fresh and not _BATCH_METRICS_REFRESHING:
        _BATCH_METRICS_REFRESHING = True
        ids = list(account_ids)
        def _bg():
            global _BATCH_METRICS_REFRESHING
            try:
                data = _src.ACCOUNT_METRICS.batch_metrics(ids)
                if isinstance(data, dict) and data:
                    _BATCH_METRICS["data"] = data
                    _BATCH_METRICS["at"] = time.time()
            finally:
                _BATCH_METRICS_REFRESHING = False
        threading.Thread(target=_bg, daemon=True).start()
    return _BATCH_METRICS["data"]


def warm_batch_metrics() -> int:
    """SYNCHRONOUSLY load the whole-book warehouse metrics into the cache. Called from the
    boot warm thread so portfolio NDR / licence utilisation are populated on the FIRST
    request after deploy, instead of returning 'no data' until the lazy non-blocking warm
    happens to complete. Returns the number of accounts loaded (0 when not live / on error).
    """
    import time
    try:
        if not _src.ACCOUNT_METRICS.live():
            return 0
        data = _src.ACCOUNT_METRICS.batch_metrics([])
        if isinstance(data, dict) and data:
            _BATCH_METRICS["data"] = data
            _BATCH_METRICS["at"] = time.time()
            return len(data)
    except Exception:  # noqa: BLE001 - boot warm is best-effort
        pass
    return 0


def _scope_key() -> str:
    """Cache key component for the current principal's visibility scope."""
    p = get_principal()
    if not p or p.get("role") == "admin":
        return "admin"
    return "csm:" + str(p.get("owner_id") or p.get("email") or "unknown")


def _cached_report(name: str, build):
    """Return build() memoised per (name, scope) for CS_REPORT_CACHE_TTL seconds.

    FULLY NON-BLOCKING: the request never waits for a build, even the very first time.

    - FRESH cache (age < TTL): return instantly.
    - STALE cache (data exists, age >= TTL): return stale instantly, background refresh.
    - COLD cache (no data at all): return a {"warming": true} placeholder instantly
      and kick off a background build. The frontend shows a warming state and polls
      until the cache is ready (typically 10-30s). This prevents the ALB from timing
      out on the 156s Stripe pagination cold build.

    TTL<=0 disables caching entirely (tests set this for determinism).
    """
    import time
    import threading
    ttl = _report_cache_ttl()
    if ttl <= 0:
        return build()
    key = (name, _scope_key())
    now = time.time()
    hit = _REPORT_CACHE.get(key)

    # Fresh cache: serve immediately.
    if hit and (now - hit[0]) < ttl:
        return hit[1]

    # Background refresh helper (shared by stale + cold paths).
    def _start_bg():
        if key in _REPORT_REFRESHING:
            return  # already building
        _REPORT_REFRESHING.add(key)
        principal = get_principal()
        def _bg():
            try:
                set_principal(principal)
                result = build()
                if isinstance(result, dict):
                    _REPORT_CACHE[key] = (time.time(), result)
            finally:
                _REPORT_REFRESHING.discard(key)
                set_principal(None)
        threading.Thread(target=_bg, daemon=True).start()

    # Stale cache exists: return it NOW, refresh in the background.
    if hit and hit[1] is not None:
        _start_bg()
        return hit[1]  # stale but instant

    # COLD (no cached data at all): return a warming placeholder immediately and
    # build in the background. The frontend polls until the real data appears.
    _start_bg()
    return {"warming": True, "note": f"{name} is loading for the first time after deploy. It will appear in a few seconds."}


def warm_reports():
    """Synchronously build both TTL-cached reports under the admin scope key so the
    first admin page load after deploy is instant. Called from the boot warm thread
    (not from a request handler). MUST NOT use _cached_report (which now returns a
    warming placeholder on cold miss)."""
    import time as _t
    # Load whole-book warehouse metrics (NDR inputs + licence utilisation) up front so the
    # first dashboard load shows a real Portfolio NDR instead of "no data" while the lazy
    # non-blocking cache warms. Best-effort; never crashes the boot thread.
    try:
        warm_batch_metrics()
    except Exception:  # noqa: BLE001
        pass
    for name, build in [("onboarding_governance", _onboarding_governance_build),
                        ("payment_risk_report", _payment_risk_report_build)]:
        try:
            result = build()
            if isinstance(result, dict):
                _REPORT_CACHE[(name, "admin")] = (_t.time(), result)
        except Exception:  # noqa: BLE001
            pass  # boot warm is best-effort; never crash the server


def _scoped_accounts() -> dict:
    """The account roster visible to the current principal. Admin (or no principal,
    for legacy/open mode) sees everything; a CSM sees only accounts they own.

    Attaches each account's success plans (if any) so the rules engine can fire the
    success-plan-at-risk rule. Plans are read from the shared JSONL store; accounts
    without plans are untouched (no key added), so behaviour is unchanged for them."""
    accounts = dataaccess.all_accounts()
    p = get_principal()
    if p and p.get("role") not in (None, "admin"):
        owner_id = p.get("owner_id")
        accounts = {aid: a for aid, a in accounts.items() if _owns(a, owner_id)}
    plans_by_account = _success_plans_all()
    # Whole-book warehouse metrics (seats + NDR inputs), non-blocking cached batch load.
    # This makes licence utilisation and portfolio NDR populate across the book, not just
    # the per-account enriched slice. Keyed by uppercase-hyphen ref.
    metrics_by_ref = _batch_metrics_for(list(accounts.keys()))
    if plans_by_account or metrics_by_ref:
        from adapters import identity as _id
        for aid, a in accounts.items():
            plans = plans_by_account.get(aid) if plans_by_account else None
            m = None
            if metrics_by_ref:
                m = metrics_by_ref.get(_id.normalise(aid).upper())
            if not plans and not m:
                continue
            # Shallow copy so we never mutate the dataaccess cache in place.
            a = dict(a)
            if plans:
                a["success_plans"] = plans
            if m:
                # Only fill gaps: a per-account live metrics block (enriched slice) wins.
                existing = dict(a.get("metrics") or {})
                for k, v in m.items():
                    existing.setdefault(k, v)
                a["metrics"] = existing
                # Flow licence utilisation into usage so the account view + expansion rule
                # see it (mirrors dataaccess.account()). Only when not already present.
                util = m.get("user_utilization_pct")
                if util is not None:
                    usage = dict(a.get("usage") or {})
                    usage.setdefault("license_utilization_pct", util)
                    a["usage"] = usage
                # Mark the metrics source live so _live_block keeps it (not stripped).
                src = dict(a.get("sources") or {})
                src.setdefault("metrics", "live")
                a["sources"] = src
            accounts[aid] = a
    return accounts


def _retention_accounts() -> dict:
    """The account set for portfolio RETENTION metrics (NDR/GRR), scoped to the current
    principal, spanning the WHOLE book - not just the enriched slice.

    Portfolio NDR is a book-level figure the warehouse (rpt_account_ndr_monthly) carries
    for ~4,500 accounts, including most of each CSM's book. But _scoped_accounts() only
    contains the ~50 deeply-enriched accounts, so a CSM whose book sits outside that slice
    saw "no data" even though the warehouse has their numbers. This helper assembles the
    cheap whole-book roster (one cached HubSpot scan), keeps it owner-scoped, and attaches
    the batched warehouse metrics (current + prior-year revenue) so _retention_metrics can
    compute real NDR/GRR over the CSM's actual book. HubSpot/warehouse-grounded only - no
    per-account vendor fan-out, nothing fabricated. Used ONLY by the retention computation,
    so the rules engine and task generation are untouched (no spurious roster-only tasks).
    """
    # Start from the enriched slice (already owner-scoped + batch-metric'd).
    accounts = dict(_scoped_accounts())
    p = get_principal()
    owner_id = p.get("owner_id") if (p and p.get("role") not in (None, "admin")) else None
    try:
        if not _src.HUBSPOT.live():
            return accounts
        from adapters import identity as _id
        roster = _src.HUBSPOT.list_all_companies(cached_only=True)
        metrics_by_ref = _batch_metrics_for([c.get("account_id") for c in roster if c.get("account_id")])
        for c in roster:
            aid = c.get("account_id") or ("rl-" + str(c.get("company_id")))
            if aid in accounts:
                continue  # enriched version already present (keeps its richer signals)
            if owner_id and str(c.get("owner_id") or "") != str(owner_id):
                continue  # not this CSM's account
            lc = str(c.get("lifecycle_stage") or "").lower()
            rec = {
                "account_id": aid,
                "sources": {"hubspot": "live"},
                "hubspot": {
                    "name": c.get("name"),
                    "account_id": c.get("account_id"),
                    "arr_usd": c.get("arr_usd"),
                    "segment": c.get("segment"),
                    "segment_label": c.get("segment_label") or c.get("segment"),
                    "renewal_date": c.get("renewal_date"),
                    "lifecycle_stage": c.get("lifecycle_stage"),
                    "csm_owner": c.get("csm_owner"),
                },
                "churn": {"churn_status": "churned"} if "churn" in lc else {},
                "usage": {}, "zendesk": {}, "stripe": {}, "jiminny": {}, "onboarding": {},
            }
            m = metrics_by_ref.get(_id.normalise(aid).upper()) if metrics_by_ref else None
            if m:
                rec["metrics"] = dict(m)
                rec["sources"]["metrics"] = "live"
            accounts[aid] = rec
    except Exception:  # noqa: BLE001 - retention augmentation is best-effort
        pass
    return accounts


_SUCCESS_PLANS_FILE = os.environ.get(
    "CS_SUCCESS_PLANS_FILE",
    str(Path(__file__).resolve().parents[1] / ".cs-success-plans.jsonl"))


def _success_plans_all() -> dict:
    """Latest success plan per (account_id, plan_id) from the append-only JSONL store,
    grouped by account_id. Shared file with the server's writer (CS_SUCCESS_PLANS_FILE).
    Returns {account_id: [plan, ...]}; empty when the file is absent. Never raises."""
    path = Path(os.environ.get("CS_SUCCESS_PLANS_FILE", _SUCCESS_PLANS_FILE))
    if not path.exists():
        return {}
    latest: dict = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            key = (row.get("account_id"), row.get("plan_id"))
            if row.get("account_id"):
                latest[key] = row  # later lines overwrite earlier (edits)
    except Exception:  # noqa: BLE001
        return {}
    by_account: dict = {}
    for (aid, _pid), plan in latest.items():
        by_account.setdefault(aid, []).append(plan)
    return by_account


def can_view_account(account_id: str) -> bool:
    """Whether the current principal may view a specific account (for hard 403s)."""
    p = get_principal()
    if not p or p.get("role") == "admin":
        return True
    accounts = dataaccess.all_accounts()
    a = accounts.get(account_id)
    if a is None:
        # Whole-book roster account outside the enriched slice: resolve ownership from a
        # single on-demand live lookup so a CSM can still open their own roster accounts.
        try:
            a = dataaccess.account(account_id)
        except Exception:  # noqa: BLE001
            return False
        if not (a and a.get("hubspot")):
            return False
    return _owns(a, p.get("owner_id"))


def can_write_account(account_id: str) -> bool:
    """Whether the current principal may WRITE to a specific account. Same owner-scope as
    read (admin writes any; a CSM writes only accounts they own), named separately so write
    call-sites are explicit and the policy can diverge later if needed."""
    return can_view_account(account_id)


# The three contact roles the WoW framework requires on every account.
REQUIRED_CONTACT_ROLES = ["Executive Sponsor", "Primary Champion / Admin", "Finance Contact"]


def required_roles_missing(account_id: str) -> list:
    """Return the required contact roles NOT yet tagged on an account's HubSpot contacts.
    Used by the close-gate: renewal/onboarding tasks cannot be completed until these are
    tagged (WoW §5). Resolved from the live account; an account with no HubSpot record is
    treated as missing all roles."""
    try:
        a = dataaccess.all_accounts().get(account_id) or dataaccess.account(account_id)
    except Exception:  # noqa: BLE001
        a = None
    hs = (a or {}).get("hubspot", {}) or {}
    have = {c.get("role") for c in hs.get("contacts", []) if c.get("role")}
    return [r for r in REQUIRED_CONTACT_ROLES if r not in have]


# The rules engine reads accounts through this provider, so scoping is uniform.
orchestrate.set_account_provider(_scoped_accounts)


# --------------------------------------------------------------------------- #
# Live-only data policy
# --------------------------------------------------------------------------- #
# The UI must never display a fabricated (fixture/sample) value. The rules engine
# still runs over fixtures for deterministic behaviour, but every value the API
# hands to the UI is either genuinely LIVE or explicitly marked "not connected".
#
# `sources` maps each signal block to 'live' or 'sample'. A block is only real if
# its source is 'live'. We map signal blocks to the source key that feeds them:
_SIGNAL_SOURCE = {
    "hubspot": "hubspot",
    "zendesk": "zendesk",
    "usage": "usage",     # Pendo
    "jiminny": "jiminny",
    "stripe": "stripe",
    # churn has a live adapter (Redshift) when configured; falls back to a
    # transparent computed score otherwise. onboarding has no adapter -> never 'live'.
    # Jiminny removed: no live source, was fixture-only ('remove all mock data').
    "churn": "churn",
    "onboarding": "onboarding",
    "roi_ai": "roi_ai",
}

# Pendo's live endpoint does not expose these usage metrics (the adapter hardcodes
# them to None); treat them as not-connected even when Pendo itself is live, so the
# UI never shows a fixture-derived number for them.
_PENDO_UNAVAILABLE_FIELDS = {
    "logins_last_7d", "logins_prev_7d", "active_users_pct",
    "license_utilization_pct", "key_feature_adoption_pct",
    "api_calls_last_7d", "api_calls_prev_7d",
}


def _is_live(sources: dict, block: str) -> bool:
    """True if the source feeding this block is real: either genuinely 'live', or
    'computed' (transparently derived from live signals, never fixture/sample)."""
    src_key = _SIGNAL_SOURCE.get(block, block)
    return sources.get(src_key) in ("live", "computed", "live_no_record")


def _live_block(account: dict, block: str) -> dict:
    """Return the signal block only if it is live-sourced; otherwise {} (not connected)."""
    if not _is_live(account.get("sources", {}), block):
        return {}
    data = dict(account.get(block, {}) or {})
    if block == "usage":
        # Strip only fields the live Pendo response did not supply. Configured
        # metadata mappings remain visible and can drive expansion rules.
        for f in _PENDO_UNAVAILABLE_FIELDS:
            if data.get(f) is None:
                data.pop(f, None)
    return data


def _live_account(account: dict) -> dict:
    """A copy of the account with all non-live signal blocks blanked out, so any
    downstream computation (health, KPIs, lifecycle) uses ONLY real data."""
    sources = account.get("sources", {})
    projected = {
        "hubspot": _live_block(account, "hubspot"),
        "zendesk": _live_block(account, "zendesk"),
        "usage": _live_block(account, "usage"),
        "jiminny": _live_block(account, "jiminny"),
        "churn": _live_block(account, "churn"),
        "stripe": _live_block(account, "stripe"),
        "onboarding": _live_block(account, "onboarding"),
        "metrics": _live_block(account, "metrics"),
        "roi_ai": _live_block(account, "roi_ai"),
        "entitlements": account.get("entitlements", {}) if isinstance(account.get("entitlements"), dict) else {},
        "sources": sources,
    }
    return projected


def _connected(account: dict) -> dict:
    """Per-block connection status the UI uses to render 'not connected' states."""
    sources = account.get("sources", {})
    return {b: _is_live(sources, b) for b in _SIGNAL_SOURCE}



def health_score(account: dict) -> dict:
    """Compute a 0-100 health score + RAG band from the signals.

    Deterministic and explainable: starts at 100, subtracts weighted penalties for
    churn risk, CSAT, usage drop, Sev-1, and payment status. This is the health
    score the UI shows per account and rolls up across the portfolio.
    """
    hs = account.get("hubspot", {})
    zd = account.get("zendesk", {})
    usage = account.get("usage", {})
    churn = account.get("churn", {})
    stripe = account.get("stripe", {})
    jiminny = account.get("jiminny", {})

    score = 100.0
    reasons: list[str] = []

    ml = churn.get("ml_churn_score") or 0.0
    if ml:
        pen = round(ml * 40)
        score -= pen
        if pen >= 12:
            reasons.append(f"churn risk {int(ml * 100)}% (-{pen})")

    if str(churn.get("churn_status") or "").lower() == "churned":
        score -= 40
        reasons.append("Redshift churn status: Churned (-40)")

    csat = zd.get("csat_30d")
    if csat is not None and csat < 75:
        pen = round((75 - csat) * 0.5)
        score -= pen
        reasons.append(f"CSAT {csat} (-{pen})")

    now, prev = usage.get("logins_last_7d") or 0, usage.get("logins_prev_7d") or 0
    if prev > 0 and now < prev:
        drop = (prev - now) / prev
        if drop >= 0.3:
            pen = round(drop * 25)
            score -= pen
            reasons.append(f"usage down {int(drop * 100)}% (-{pen})")

    if (zd.get("sev1_open") or 0) > 0:
        score -= 15
        reasons.append("open Sev-1 (-15)")

    if stripe.get("dunning_stage") == "day_15_plus":
        score -= 10
        reasons.append("payment 15+ days past due (-10)")

    # Live Pendo recency: no visit in a while is a real disengagement signal.
    dsv = usage.get("days_since_last_visit")
    if dsv is not None and dsv >= 14:
        pen = min(20, (dsv // 7) * 5)
        score -= pen
        reasons.append(f"no product visit in {dsv} days (-{pen})")

    # Live Pendo risk advisor.
    prisk = str(usage.get("pendo_risk_score") or "").lower()
    if prisk == "high":
        score -= 18
        reasons.append("Pendo risk advisor: High (-18)")
    elif prisk == "medium":
        score -= 8
        reasons.append("Pendo risk advisor: Medium (-8)")

    # Live Jiminny conversational intelligence: negative call sentiment is a real
    # relationship-health signal. Weighted modestly, it colours the score but does
    # not, on its own, dominate hard risk signals like churn or an open Sev-1.
    sentiment = str(jiminny.get("sentiment") or "").lower()
    if sentiment == "negative":
        score -= 10
        reasons.append("latest call sentiment: negative (-10)")
    elif sentiment == "positive":
        score = min(100.0, score + 3)
        reasons.append("latest call sentiment: positive (+3)")

    # ROI AI adoption telemetry (V5): a modest colour on health. High adoption is a
    # positive signal; very low adoption is a mild risk. Weighted small so it never
    # dominates hard signals (churn, Sev-1), matching the Jiminny treatment.
    roi_ai = account.get("roi_ai", {}) or {}
    roi_adopt = roi_ai.get("adoption_score")
    if isinstance(roi_adopt, (int, float)):
        if roi_adopt >= 75:
            score = min(100.0, score + 3)
            reasons.append(f"ROI AI adoption {int(roi_adopt)} (+3)")
        elif roi_adopt < 25:
            pen = 8 if roi_adopt < 10 else 5
            score -= pen
            reasons.append(f"ROI AI adoption {int(roi_adopt)} (-{pen})")

    if str(churn.get("churn_status") or "").lower() == "churned":
        score = min(score, 49)
    score = max(0, min(100, round(score)))
    band = "green" if score >= 75 else "amber" if score >= 50 else "red"
    # Computable only if at least one real (live) health input contributed. When no
    # live signal is present, the score is not meaningful and the UI shows "no data".
    computable = bool(
        churn.get("ml_churn_score") is not None
        or churn.get("churn_status")
        or zd.get("csat_30d") is not None
        or (zd.get("sev1_open") is not None)
        or usage.get("days_since_last_visit") is not None
        or usage.get("pendo_risk_score")
        or stripe.get("dunning_stage")
        or jiminny.get("sentiment")
        or roi_ai.get("adoption_score") is not None
    )
    return {"score": score, "band": band, "reasons": reasons, "computable": computable}


def _roster_band(company: dict) -> dict:
    """Lightweight health band for a non-enriched (Tier-1) roster row. We have no live
    signals here, so health is not computable; we surface a neutral band and let the UI
    show the account at roster level. Opening the account triggers full enrichment."""
    lc = str(company.get("lifecycle_stage") or "").lower()
    if "churn" in lc:
        return {"score": 40, "band": "red", "reasons": ["churned"], "computable": False}
    # Not computable without live signals: neutral/amber, flagged non-computable so the
    # UI renders it as "not scored yet" rather than a fabricated green.
    return {"score": 70, "band": "amber", "reasons": [], "computable": False}


def renewal_forecast(account: dict, health: dict, expansion_qualified: bool = False) -> dict:
    """Deterministic renewal-outcome forecast for an account (mirrors Planhat's AI
    Forecast column, but rules-based and fully explainable, NOT an LLM).

    Classifies an account with a renewal into one of three labels, derived ONLY from
    signals the engine already produces. Nothing is invented; every rationale line
    traces back to a health `reason` or an expansion trigger.

    Priority order (risk first, then opportunity, then the default):
      - "Churn Risk"  if health band is red, OR ml_churn_score >= CHURN_RISK (0.70),
                      OR churn_status == churned.
      - "Expansion"   if the account already qualifies for a signed expansion trigger
                      (`expansion_qualified`, computed by the caller from
                      orchestrate.evaluate() so thresholds are never duplicated here).
      - "Renewal"     otherwise (on-track renewal).

    `expansion_qualified` is passed in by the caller (the API layer) which asks the
    rules engine whether an expansion task fired for this account, reusing the signed
    UTIL_EXPANSION / API_SURGE thresholds rather than re-implementing them.

    Returns {label, rationale, evidence} where `evidence` is the list of grounding
    strings used to build the one-line rationale, or `applicable: False` when the
    account has no renewal date (nothing to forecast).
    """
    hs = account.get("hubspot", {}) or {}
    churn = account.get("churn", {}) or {}

    if not hs.get("renewal_date") or not _renewal_date_is_sane(hs.get("renewal_date")):
        return {"applicable": False, "label": None, "rationale": None, "evidence": []}

    band = health.get("band") if isinstance(health, dict) else None
    reasons = list(health.get("reasons", [])) if isinstance(health, dict) else []
    computable = bool(health.get("computable")) if isinstance(health, dict) else False

    ml = churn.get("ml_churn_score")
    churned = str(churn.get("churn_status") or "").lower() == "churned"

    # --- Churn Risk (highest priority) ---
    churn_signals = []
    if computable and band == "red":
        churn_signals.append("health red")
    if isinstance(ml, (int, float)) and ml >= orchestrate.CHURN_RISK:
        churn_signals.append(f"churn risk {int(ml * 100)}%")
    if churned:
        churn_signals.append("churned")
    if churn_signals:
        # Prefer concrete health reasons for the rationale (they carry the penalty
        # detail), falling back to the trigger summary above.
        detail = reasons[:2] if reasons else churn_signals
        return {
            "applicable": True,
            "label": "Churn Risk",
            "rationale": "Churn Risk, " + ", ".join(detail),
            "evidence": churn_signals + reasons,
        }

    # --- Expansion (opportunity) ---
    if expansion_qualified:
        detail = reasons[:1] if reasons else []
        rationale = "Expansion, qualifies for an expansion trigger"
        if detail:
            rationale += " (" + ", ".join(detail) + ")"
        return {
            "applicable": True,
            "label": "Expansion",
            "rationale": rationale,
            "evidence": ["expansion trigger fired"] + reasons,
        }

    # --- Renewal (on track) ---
    if computable:
        rationale = "Renewal, " + (reasons[0] if reasons else f"health {health.get('score')}, on track")
    else:
        rationale = "Renewal, on track (no adverse signal)"
    return {
        "applicable": True,
        "label": "Renewal",
        "rationale": rationale,
        "evidence": reasons,
    }


def _expansion_qualified(account_id: str, account: dict) -> bool:
    """Whether the signed expansion playbook fired for this account, reusing the
    rules engine's own evaluation (UTIL_EXPANSION / API_SURGE / adoption thresholds)
    so the forecast never duplicates those thresholds."""
    try:
        tasks, _ = orchestrate.evaluate(account_id, account)
    except Exception:  # noqa: BLE001
        return False
    expansion_rules = {
        orchestrate.RULE_EXPANSION_UTILIZATION,
        orchestrate.RULE_EXPANSION_API_SURGE,
        orchestrate.RULE_EXPANSION_ADOPTION,
        orchestrate.RULE_EXPANSION_ROI_AI,
    }
    return any(t.get("rule_id") in expansion_rules for t in tasks)


def adoption_score(account: dict) -> dict:
    """Multi-signal product-adoption picture (Customer 360 §3/§4). Measures whether the
    customer is actually running their recruitment business in JobAdder, not just logging
    in. Combines the live signals we have (user adoption %, feature adoption %, usage
    recency, licence utilisation, login momentum) into a 0-100 adoption score with a
    breakdown, and honestly flags the JobAdder-native workflow counters (jobs, candidates,
    submissions, placements) that the connected telemetry endpoint does not yet expose."""
    usage = account.get("usage", {}) or {}
    ent = account.get("entitlements", {}) or {}
    components: list[dict] = []
    gaps: list[str] = []

    def comp(label, value, weight):
        components.append({"label": label, "value": value, "weight": weight})

    # User adoption (active users %).
    au = usage.get("active_users_pct")
    if isinstance(au, (int, float)):
        comp("Active users", au, 0.30)
    else:
        # No mapped percentage, but the aggregation API gives a REAL distinct-active-user
        # count (30d). Without a licensed-seat denominator we cannot form a true %, but a
        # non-zero active-user count is itself strong evidence of adoption. Scale it to a
        # 0-100 engagement signal (0 users = 0, >=25 active users = full). Honest, live.
        auc = usage.get("active_users_30d")
        if isinstance(auc, (int, float)):
            engaged = min(100, round((auc / 25.0) * 100))
            comp("Active users (30d)", engaged, 0.30)
        else:
            gaps.append("active user %")
    # Feature adoption.
    fa = usage.get("key_feature_adoption_pct")
    if isinstance(fa, (int, float)):
        comp("Feature adoption", fa, 0.25)
    else:
        # No mapped percentage, but the Pendo Aggregation API gives a REAL count of
        # distinct features touched in the last 30 days. Convert feature breadth to a
        # 0-100 depth signal (0 features = 0, >=50 distinct features = full adoption).
        # This is a genuine live signal, not a fabricated percentage.
        feat = usage.get("features_used_30d")
        if isinstance(feat, (int, float)):
            breadth = min(100, round((feat / 50.0) * 100))
            comp("Feature breadth (30d)", breadth, 0.25)
        else:
            gaps.append("feature adoption %")
    # Licence utilisation (seats used vs purchased).
    util = ent.get("license_utilization_pct")
    if isinstance(util, (int, float)):
        comp("Licence utilisation", util, 0.20)
    else:
        gaps.append("licence utilisation")
    # Usage recency (recent = adopting). Convert days-since-visit to a 0-100 recency score.
    dsv = usage.get("days_since_last_visit")
    if isinstance(dsv, (int, float)):
        recency = max(0, 100 - min(100, (dsv / 30.0) * 50))  # 0d=100, 30d=50, 60d+=0
        comp("Usage recency", round(recency), 0.15)
    else:
        gaps.append("usage recency")
    # Login momentum (this week vs last).
    now, prev = usage.get("logins_last_7d"), usage.get("logins_prev_7d")
    if isinstance(now, (int, float)) and isinstance(prev, (int, float)) and prev >= 0:
        mo = 100 if now >= prev else max(0, round(100 * now / prev)) if prev else 50
        comp("Login momentum", mo, 0.10)

    # JobAdder-native core-workflow counters are the richest adoption story but are not in
    # the connected telemetry endpoint yet, name them as a data gap rather than inventing.
    workflow_gap = "core-workflow counters (jobs, candidates, submissions, placements)"

    if not components:
        return {"score": None, "computable": False, "components": [],
                "gaps": gaps + [workflow_gap],
                "method": "product adoption from live JobAdder telemetry (awaiting signals)"}
    total_w = sum(c["weight"] for c in components) or 1
    score = round(sum(c["value"] * c["weight"] for c in components) / total_w)
    return {
        "score": score,
        "band": "high" if score >= 70 else "medium" if score >= 40 else "low",
        "computable": True,
        "components": components,
        "gaps": gaps + [workflow_gap],
        "method": "weighted live adoption signals (active users, feature adoption, licence "
                  "utilisation, usage recency, login momentum). Core-workflow counters pending.",
    }


def expansion_score(account: dict, segment_median_arr: float | None = None) -> dict:
    """Computed expansion-readiness score (0-100) + drivers. The upsell mirror of the
    health/churn score: it rewards accounts that are HEALTHY and ENGAGED and show room to
    grow, so CS can prioritise proactive upsell conversations.

    Deterministic and explainable, NOT an ML model, and it never fabricates: it only
    scores from signals that are actually present. Signals (each additive):
      + strong health (a shaky account is not an expansion candidate)
      + active product engagement (recent visits / login momentum / strong adoption)
      + high license utilization (>= 85% is the signed expansion trigger; near-full = grow)
      + API usage surge (>= 1.4x, the signed expansion trigger)
      + renewal approaching within the T-120..T-30 cadence (natural commercial moment)
      + ARR headroom vs the segment median (room to expand)
      + positive call sentiment
    Suppressed entirely when the account is churned or currently at risk (red health).
    """
    hs = account.get("hubspot", {})
    usage = account.get("usage", {})
    churn = account.get("churn", {})
    ent = account.get("entitlements", {}) or {}
    jiminny = account.get("jiminny", {})

    health = health_score(account)
    # Not an expansion candidate if churned or red-health, and not scorable without health.
    if not health.get("computable"):
        return {"score": 0, "band": "none", "drivers": [], "computable": False,
                "method": "computed expansion readiness (needs live signals)"}
    churned = str(churn.get("churn_status") or "").lower() == "churned" or \
        str(hs.get("lifecycle_stage") or "").lower() in {"churned", "churned customer"}
    if churned or health.get("band") == "red":
        return {"score": 0, "band": "low", "drivers": [], "computable": True,
                "method": "computed expansion readiness (not eligible: churned or at risk)"}

    score = 0.0
    drivers: list[str] = []

    # Health foundation (0-30): expansion goes to healthy accounts.
    hscore = health.get("score", 0)
    if hscore >= 90:
        score += 30; drivers.append(f"strong health ({hscore})")
    elif hscore >= 75:
        score += 22; drivers.append(f"healthy ({hscore})")
    elif hscore >= 60:
        score += 10; drivers.append(f"stable health ({hscore})")

    # Engagement (0-25): recent product use / login momentum / adoption.
    dsv = usage.get("days_since_last_visit")
    if dsv is not None:
        if dsv <= 7:
            score += 18; drivers.append("actively using the product (visited this week)")
        elif dsv <= 30:
            score += 10; drivers.append(f"engaged (last visit {dsv} days ago)")
    now, prev = usage.get("logins_last_7d") or 0, usage.get("logins_prev_7d") or 0
    if prev > 0 and now > prev:
        score += 7; drivers.append(f"login momentum up ({prev}->{now}/wk)")
    adoption = usage.get("adoption_pct")
    if isinstance(adoption, (int, float)) and adoption >= 70:
        score += 5; drivers.append(f"high feature adoption ({int(adoption)}%)")

    # License utilization (0-25): the signed expansion trigger.
    util = ent.get("license_utilization_pct")
    if isinstance(util, (int, float)):
        if util >= 85:
            score += 25; drivers.append(f"license utilization {int(util)}% (>=85% expansion trigger)")
        elif util >= 70:
            score += 12; drivers.append(f"license utilization {int(util)}%")

    # API usage surge (0-15): the other signed expansion trigger.
    api_ratio = usage.get("api_usage_ratio")
    if isinstance(api_ratio, (int, float)) and api_ratio >= 1.4:
        score += 15; drivers.append(f"API usage surge {api_ratio:.1f}x (>=1.4x expansion trigger)")

    # Renewal proximity (0-12): T-120..T-30 is the natural commercial window.
    days_to_renewal = _days_to_renewal(hs.get("renewal_date"))
    if days_to_renewal is not None and 30 <= days_to_renewal <= 120:
        score += 12; drivers.append(f"renewal in {days_to_renewal} days (commercial window)")

    # ARR headroom (0-8): below-median ARR in the segment = room to grow.
    arr = hs.get("arr_usd")
    if isinstance(arr, (int, float)) and segment_median_arr and arr < segment_median_arr:
        score += 8; drivers.append("ARR below segment median (headroom)")

    # Positive relationship signal (0-5).
    if str(jiminny.get("sentiment") or "").lower() == "positive":
        score += 5; drivers.append("positive call sentiment")

    score = max(0, min(100, round(score)))
    band = "high" if score >= 60 else "medium" if score >= 35 else "low"
    return {"score": score, "band": band, "drivers": drivers, "computable": True,
            "method": "computed expansion readiness from live health, engagement, "
                      "utilization, API surge, renewal timing and ARR headroom (not an ML model)"}


def _renewal_date_is_sane(renewal_date) -> bool:
    """Whether a renewal date is plausibly real for an active subscription, rather than a
    HubSpot data-entry error. We regularly see corrupt values (year 1314/1700, the Unix
    epoch 1970-01-01, or dates thousands of days in the past) that are clearly not genuine
    renewals. A sane renewal sits within a sensible window: at most ~2 years overdue and at
    most ~5 years out. Anything outside that is treated as missing/garbage so it never
    pollutes the renewal views or the forecast. Honest: we drop obviously-bad data rather
    than present '260,334 days overdue' as an upcoming renewal."""
    if not renewal_date:
        return False
    from datetime import date, datetime
    try:
        rd = datetime.fromisoformat(str(renewal_date)[:10]).date()
    except (ValueError, TypeError):
        return False
    today_env = os.environ.get("CS_TODAY")
    try:
        today = datetime.fromisoformat(today_env).date() if today_env else date.today()
    except ValueError:
        today = date.today()
    delta = (rd - today).days
    # Window: up to ~2 years (730d) overdue, up to ~5 years (1825d) in the future.
    return -730 <= delta <= 1825


def _days_to_renewal(renewal_date) -> int | None:
    if not renewal_date:
        return None
    if not _renewal_date_is_sane(renewal_date):
        return None
    from datetime import date, datetime
    try:
        rd = datetime.fromisoformat(str(renewal_date)[:10]).date()
    except ValueError:
        return None
    today_env = os.environ.get("CS_TODAY")
    try:
        today = datetime.fromisoformat(today_env).date() if today_env else date.today()
    except ValueError:
        today = date.today()
    return (rd - today).days


def executive_summary() -> dict:
    """Leadership roll-up (Customer 360 §20): customer health cohorts, GRR vs target,
    renewals within 90 days and their ARR, and the expansion pipeline. Scoped to the
    caller (admin = all, CSM = own book) via the same scoped account provider. Assembles
    the existing engines rather than recomputing, so numbers are consistent."""
    rm = revenue_motion()
    accounts = orchestrate.load_accounts()
    hm = rm.get("health_mix", {})
    # Critical = red; At risk = amber; Healthy = green. Unknown = accounts with no
    # computable health.
    scored = hm.get("green", 0) + hm.get("amber", 0) + hm.get("red", 0)
    total = rm.get("total_accounts", len(accounts))
    # Renewals within 90 days + their ARR.
    renewals_90d, renewal_arr = 0, 0
    for a in accounts.values():
        la = _live_account(a)
        hs = la.get("hubspot", {})
        d = _days_to_renewal(hs.get("renewal_date"))
        if d is not None and 0 <= d <= 90:
            renewals_90d += 1
            arr = hs.get("arr_usd")
            if isinstance(arr, (int, float)):
                renewal_arr += arr
    ret = rm.get("retention", {}) or {}
    rc = rm.get("revenue_change", {}) or {}
    return {
        "customers": total,
        "paying_customers": rm.get("paying_customers"),
        "churned_customers": rm.get("churned_customers"),
        "healthy": hm.get("green", 0),
        "at_risk": hm.get("amber", 0),
        "critical": hm.get("red", 0),
        "unknown": max(0, total - scored - rm.get("churned_customers", 0)),
        "total_arr_usd": rm.get("total_arr_usd"),
        "at_risk_arr_usd": rm.get("at_risk_arr_usd"),
        "grr_pct": ret.get("grr_pct"),
        "grr_target_pct": (ret.get("target") or {}).get("grr_pct"),
        "ndr_pct": ret.get("ndr_pct"),
        "ndr_target_pct": (ret.get("target") or {}).get("ndr_pct"),
        "churned_arr_usd": ret.get("churned_arr_usd"),
        "renewals_90d": renewals_90d,
        "renewal_arr_90d_usd": renewal_arr,
        "expansion_pipeline_accounts": rc.get("expansion_pipeline_accounts"),
        "booked_upsell_count": (rc.get("upsell_count") if rc.get("upsell_computable") else None),
        "booked_upsell_arr_usd": (rc.get("upsell_arr_usd") if rc.get("upsell_computable") else None),
        "by_segment": rm.get("by_segment", []),
    }


def _primary_risk_driver(la: dict) -> tuple[str, str]:
    """Classify an at-risk account's PRIMARY churn driver into one of the matrix buckets
    the spec names (usage drop, ticket spike, billing issue), plus the other live risk
    signals the engine actually fires on. Returns (driver_key, human_label).

    Precedence reflects severity/actionability: an open Sev-1 or a ticket-spike+usage-drop
    multi-signal outranks a slow-burn usage decline, which outranks a billing issue, which
    outranks a generic high ML score with no single dominant signal. Deterministic and
    grounded only in live signals present on the account.
    """
    usage = la.get("usage", {}) or {}
    zd = la.get("zendesk", {}) or {}
    stripe = la.get("stripe", {}) or {}

    logins_now = usage.get("logins_last_7d") or 0
    logins_prev = usage.get("logins_prev_7d") or 0
    usage_drop = logins_prev > 0 and logins_now <= orchestrate.USAGE_DROP * logins_prev
    last7 = zd.get("tickets_last_7d") or 0
    prev7 = zd.get("tickets_prev_7d") or 0
    ticket_spike = prev7 > 0 and last7 >= 2 * prev7
    sev1 = (zd.get("sev1_open") or 0) > 0
    pendo_high = str(usage.get("pendo_risk_score") or "").lower() == "high"
    dsv = usage.get("days_since_last_visit")
    long_dormant = isinstance(dsv, (int, float)) and dsv >= 180
    billing = stripe.get("dunning_stage") in {"day_1_14", "day_15_plus"} or (stripe.get("past_due_invoices") or 0) > 0

    if sev1:
        return "sev1", "Critical support incident (Sev-1)"
    if ticket_spike and usage_drop:
        return "ticket_spike", "Ticket spike + usage drop"
    if ticket_spike:
        return "ticket_spike", "Support ticket spike"
    if usage_drop or long_dormant:
        return "usage_drop", "Usage drop / product disengagement"
    if billing:
        return "billing", "Billing / payment issue"
    if pendo_high:
        return "product_risk", "Pendo risk advisor: High"
    return "ml_score", "High ML churn score (no single dominant signal)"


def churn_risk_matrix(threshold: float | None = None) -> dict:
    """ML Churn Risk Matrix (UC3): the cohort of accounts at or above the ML churn
    threshold (default 70%), grouped by PRIMARY risk driver with the total ARR impact and
    account list per driver. Scoped to the caller (admins all, CSM own book).

    Only genuine ML churn scores count toward the cohort, a computed (signals-based)
    fallback score is never treated as the ML >70% threshold (same guard as the P1 rule),
    so the matrix cannot over-report. Each account's ARR is the 'impact' it contributes.
    """
    threshold = orchestrate.CHURN_RISK if threshold is None else threshold
    accounts = orchestrate.load_accounts()
    buckets: dict[str, dict] = {}
    cohort_arr = 0
    cohort_count = 0
    for aid, a in accounts.items():
        la = _live_account(a)
        churn = la.get("churn", {}) or {}
        score = churn.get("ml_churn_score")
        is_ml = churn.get("ml_churn_score") is not None and not churn.get("computed")
        churned = str(churn.get("churn_status") or "").lower() == "churned"
        # Cohort = genuine ML score >= threshold (never the computed fallback), OR an
        # explicitly churned account (ARR already lost, still a driver to attribute).
        if not ((is_ml and isinstance(score, (int, float)) and score >= threshold) or churned):
            continue
        hs = la.get("hubspot", {}) or {}
        arr = hs.get("arr_usd") if isinstance(hs.get("arr_usd"), (int, float)) else 0
        driver_key, driver_label = ("churned", "Churned account") if churned else _primary_risk_driver(la)
        b = buckets.setdefault(driver_key, {"driver": driver_key, "label": driver_label,
                                            "accounts": 0, "arr_usd": 0, "members": []})
        b["accounts"] += 1
        b["arr_usd"] += arr
        b["members"].append({
            "account_id": aid, "name": hs.get("name") or aid,
            "segment": hs.get("segment_label") or hs.get("segment") or "Unsegmented",
            "owner": hs.get("csm_owner") or "Unassigned",
            "arr_usd": arr,
            "ml_churn_score": score if is_ml else None,
            "churned": churned,
        })
        cohort_arr += arr
        cohort_count += 1
    for b in buckets.values():
        b["members"].sort(key=lambda m: -(m["arr_usd"] or 0))
    return {
        "threshold_pct": int(threshold * 100),
        "cohort_accounts": cohort_count,
        "cohort_arr_usd": cohort_arr,
        "by_driver": sorted(buckets.values(), key=lambda x: -x["arr_usd"]),
        "note": "Accounts at or above the ML churn threshold (genuine ML scores only; "
                "computed fallback excluded), grouped by primary risk driver with ARR impact.",
    }


def _onboarding_days_in(start_date: str | None) -> int | None:
    """Days a project has been in onboarding, from its start_date to today. None when
    the start date is absent/unparseable (data-gap, never fabricated)."""
    if not start_date:
        return None
    try:
        start = datetime.fromisoformat(str(start_date)[:10]).date()
    except (ValueError, TypeError):
        return None
    today_env = os.environ.get("CS_TODAY")
    try:
        today = datetime.fromisoformat(today_env).date() if today_env else date.today()
    except ValueError:
        today = date.today()
    delta = (today - start).days
    return delta if delta >= 0 else None


def _onboarding_stalled(onboarding: dict) -> tuple[bool, list[str]]:
    """Mirror the stalled logic in orchestrate.evaluate() so the governance view and the
    queue agree. Returns (stalled, reasons)."""
    status = str(onboarding.get("status") or "").lower()
    health = str(onboarding.get("health") or "").lower()
    reasons: list[str] = []
    due = onboarding.get("due_date")
    past_due_days = None
    if due:
        try:
            d = datetime.fromisoformat(str(due)[:10]).date()
            today_env = os.environ.get("CS_TODAY")
            today = (datetime.fromisoformat(today_env).date() if today_env else date.today())
            past_due_days = (today - d).days
        except (ValueError, TypeError):
            past_due_days = None
    completed_states = {"completed", "complete", "done", "live"}
    if status in {"stalled", "blocked", "at_risk", "on_hold"}:
        reasons.append(f"status={onboarding.get('status')}")
    if health in {"red", "at_risk"}:
        reasons.append(f"health={onboarding.get('health')}")
    if isinstance(past_due_days, int) and past_due_days > 0 and status not in completed_states:
        reasons.append(f"{past_due_days}d past due")
    return (bool(reasons), reasons)


def onboarding_governance() -> dict:
    """Cached wrapper (short TTL, per scope) over the live Rocket Lane governance build so
    the page doesn't re-fan-out to Rocket Lane on every load."""
    return _cached_report("onboarding_governance", _onboarding_governance_build)


def _onboarding_governance_build() -> dict:
    """Rocket Lane implementation & onboarding governance (V5 UC3): active onboarding
    projects, time-in-onboarding, stalled-before-handoff alerts, and an on-time handoff
    KPI (target >= 90%).

    SOURCE-FIRST: pulls projects directly from Rocket Lane (like the Payment Risk report
    pulls from Stripe), so the view is COMPLETE regardless of how the account roster was
    warmed and without depending on brittle per-account name matching. Each project is
    joined back to the account roster by normalised company name to attach the CSM owner;
    unmatched projects still appear (owner shown as unknown). Owner-scoped: an admin sees
    every project; a scoped CSM sees only projects for accounts they own. HONEST: when
    Rocket Lane is not connected it returns connected:false and never a fabricated zero."""
    rocket_live = "Rocket Lane" in set(dataaccess.live_sources())
    if not rocket_live:
        return {"connected": False, "active_projects": 0, "stalled_projects": 0,
                "avg_days_in_onboarding": None, "handoff_on_time_pct": None,
                "handoff_on_time_target_pct": 90, "handoff_completed": 0,
                "projects": [], "stalled": [],
                "note": "Rocket Lane is not connected; onboarding governance is unavailable."}

    try:
        all_projects = _src.ROCKET_LANE.list_active_projects()
    except Exception:  # noqa: BLE001
        all_projects = []

    # Build an owner lookup (normalised company name -> {owner, owner_id, account_id}) from
    # the FULL lightweight book (not just the enriched slice), so projects match real
    # accounts and inherit the CSM owner. Falls back to the enriched roster if the cheap
    # full-book scan is unavailable.
    from adapters import identity as _id
    p = get_principal()
    admin = (not p) or p.get("role") == "admin"
    my_owner_id = str((p or {}).get("owner_id") or "") if not admin else ""
    owner_by_name = {}
    try:
        book = _src.HUBSPOT.list_all_companies() if (dataaccess._ADAPTERS and _src.HUBSPOT.live()) else []
    except Exception:  # noqa: BLE001
        book = []
    # Resolve owner_id -> name via the adapter's cached owner lookup so rows show the CSM.
    owner_names = {}
    try:
        ids = sorted({str(c.get("owner_id")) for c in book if c.get("owner_id")})
        for oid in ids:
            nm = _src.HUBSPOT._owner_name(oid)
            if nm:
                owner_names[oid] = nm
    except Exception:  # noqa: BLE001
        owner_names = {}
    for c in book:
        nm = c.get("name")
        if not nm:
            continue
        oid = str(c.get("owner_id") or "")
        owner_by_name[_id.normalise(nm)] = {
            "owner": c.get("csm_owner") or owner_names.get(oid) or ("Unassigned" if not oid else oid),
            "owner_id": oid,
            "account_id": c.get("account_id") or ("rl-" + str(c.get("company_id"))),
        }
    # Fallback: if the full book was empty, use the enriched slice so we still match some.
    if not owner_by_name:
        for aid, a in _scoped_accounts().items():
            hs = a.get("hubspot", {}) or {}
            nm = hs.get("name")
            if nm:
                owner_by_name[_id.normalise(nm)] = {"owner": hs.get("csm_owner") or "Unassigned",
                                                     "owner_id": str(hs.get("csm_owner_id") or ""),
                                                     "account_id": aid}

    projects, stalled = [], []
    on_time = overdue = 0
    for pr in all_projects:
        cname = pr.get("company_name")
        key = _id.normalise(cname) if cname else None
        match = owner_by_name.get(key) if key else None
        # Owner scope: a scoped CSM only sees projects for accounts THEY own (matched by
        # company name, then owner_id). Admin (or open mode) sees every project.
        if not admin:
            if match is None or (my_owner_id and match.get("owner_id") != my_owner_id):
                continue
        status = str(pr.get("status") or "").lower()
        completed = status in {"completed", "complete", "done", "live"}
        days_in = _onboarding_days_in(pr.get("start_date"))
        is_stalled, reasons = _onboarding_stalled(pr)
        due = pr.get("due_date")
        if completed and due:
            try:
                dd = datetime.fromisoformat(str(due)[:10]).date()
                today_env = os.environ.get("CS_TODAY")
                today = (datetime.fromisoformat(today_env).date() if today_env else date.today())
                if today <= dd:
                    on_time += 1
                else:
                    overdue += 1
            except (ValueError, TypeError):
                pass
        row = {
            "account_id": (match or {}).get("account_id"),
            "name": cname or (pr.get("project_name") or "Unknown"),
            "owner": (match or {}).get("owner") or "-",
            "project_name": pr.get("project_name"),
            "status": pr.get("status"),
            "health": pr.get("health"),
            "start_date": pr.get("start_date"),
            "due_date": due,
            "days_in_onboarding": days_in,
            "completed": completed,
            "stalled": is_stalled,
            "stall_reasons": reasons,
            "matched_account": match is not None,
        }
        if not completed:
            projects.append(row)
        if is_stalled and not completed:
            stalled.append(row)

    handoff_denom = on_time + overdue
    handoff_on_time_pct = round(100 * on_time / handoff_denom) if handoff_denom else None
    active_days = [r["days_in_onboarding"] for r in projects if isinstance(r["days_in_onboarding"], int)]
    return {
        "connected": rocket_live,
        "active_projects": len(projects),
        "stalled_projects": len(stalled),
        "avg_days_in_onboarding": round(sum(active_days) / len(active_days)) if active_days else None,
        "handoff_on_time_pct": handoff_on_time_pct,
        "handoff_on_time_target_pct": 90,
        "handoff_completed": handoff_denom,
        "projects": sorted(projects, key=lambda r: -(r["days_in_onboarding"] or 0)),
        "stalled": sorted(stalled, key=lambda r: -(r["days_in_onboarding"] or 0)),
        "note": None,
    }


# --------------------------------------------------------------------------- #
# Executive Sponsor F2F cadence (V5 UC2)
# --------------------------------------------------------------------------- #
# Append-only JSONL log of executive face-to-face touchpoints, following the
# SUCCESS_PLANS pattern. Latest record per f2f_id wins (so a logged meeting can be
# edited). CS_F2F_LOG_FILE relocates it (e.g. an EFS mount) so it survives restarts.
F2F_LOG_FILE = Path(os.environ.get(
    "CS_F2F_LOG_FILE", str(Path(__file__).resolve().parents[1] / ".cs-f2f-log.jsonl")))
F2F_CADENCE_DAYS = int(os.environ.get("CS_F2F_CADENCE_DAYS", "90"))
HIGH_ARR_TIER1 = 100000  # ARR proxy for tier-1 strategic when no explicit customer_tier


def _f2f_today() -> date:
    env = os.environ.get("CS_TODAY")
    try:
        return date.fromisoformat(env) if env else date.today()
    except ValueError:
        return date.today()


def record_f2f(body: dict, principal: dict | None) -> dict:
    """Log an executive F2F touchpoint. Append-only; latest-wins by f2f_id."""
    account_id = str(body.get("account_id") or "").strip()
    met_on = str(body.get("met_on") or "").strip() or _f2f_today().isoformat()
    if not account_id:
        raise ValueError("account_id is required")
    try:
        date.fromisoformat(met_on[:10])
    except ValueError:
        raise ValueError("met_on must be an ISO date (YYYY-MM-DD)")
    entry = {
        "f2f_id": body.get("f2f_id") or uuid.uuid4().hex[:12],
        "account_id": account_id,
        "met_on": met_on[:10],
        "attendees": body.get("attendees") if isinstance(body.get("attendees"), list) else [],
        "cs_leadership": body.get("cs_leadership") if isinstance(body.get("cs_leadership"), list) else [],
        "notes": str(body.get("notes") or "").strip() or None,
        "outcome": str(body.get("outcome") or "").strip() or None,
        "logged_by": (principal or {}).get("email") or (principal or {}).get("name"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    F2F_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with F2F_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def f2f_log_for(account_id: str) -> list[dict]:
    """All F2F entries for an account, newest first; latest record per f2f_id wins."""
    if not F2F_LOG_FILE.exists():
        return []
    latest: dict[str, dict] = {}
    for line in F2F_LOG_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if row.get("account_id") == account_id:
            latest[row.get("f2f_id")] = row
    return sorted(latest.values(), key=lambda r: r.get("met_on", ""), reverse=True)


def last_f2f(account_id: str) -> str | None:
    """Most recent F2F met_on date (ISO) for an account, or None (data-gap)."""
    rows = f2f_log_for(account_id)
    return rows[0]["met_on"] if rows else None


def _is_tier1_strategic(hs: dict) -> bool:
    """Tier-1 strategic signal: explicit customer_tier == 'tier-1'/'tier 1', else an
    ARR >= HIGH_ARR_TIER1 proxy on a Strategic account."""
    if (hs.get("segment") or "") != "Strategic":
        return False
    tier = str(hs.get("customer_tier") or "").strip().lower()
    if tier in ("tier-1", "tier 1", "tier1", "strategic-tier-1"):
        return True
    arr = hs.get("arr_usd")
    return isinstance(arr, (int, float)) and arr >= HIGH_ARR_TIER1


def f2f_cadence() -> dict:
    """Executive F2F cadence KPI (V5 UC2): of the tier-1 strategic accounts in scope, the
    share that have an exec touchpoint within the cadence window (default 90 days).
    Target 100%. Owner-scoped. Honest: an account with no F2F logged is simply overdue,
    never credited with a fabricated meeting."""
    accounts = _scoped_accounts()
    today = _f2f_today()
    tier1 = []
    in_window = []
    overdue = []
    for aid, a in accounts.items():
        hs = a.get("hubspot", {}) or {}
        if not _is_tier1_strategic(hs):
            continue
        last = last_f2f(aid)
        row = {"account_id": aid, "name": hs.get("name") or aid,
               "owner": hs.get("csm_owner") or "Unassigned", "last_f2f": last,
               "exec_sponsor": next((c.get("name") for c in hs.get("contacts", [])
                                     if c.get("role") == "Executive Sponsor"), None)}
        tier1.append(row)
        within = False
        if last:
            try:
                within = (today - date.fromisoformat(last)).days <= F2F_CADENCE_DAYS
            except ValueError:
                within = False
        (in_window if within else overdue).append(row)
    denom = len(tier1)
    pct = round(100 * len(in_window) / denom) if denom else None
    return {
        "cadence_window_days": F2F_CADENCE_DAYS,
        "tier1_strategic_accounts": denom,
        "with_in_window_touchpoint": len(in_window),
        "overdue_accounts": sorted(overdue, key=lambda r: (r["last_f2f"] or "")),
        "tier1_exec_touchpoint_pct": pct,
        "target_pct": 100,
    }


# --------------------------------------------------------------------------- #
# ROI AI webhook telemetry (V5): inbound product-adoption signal
# --------------------------------------------------------------------------- #
# ROI AI pushes adoption telemetry via a signed webhook (HMAC). We persist each
# event append-only (CS_ROI_AI_FILE), idempotent on event_id, latest-wins per account
# by newest metric_date. Honest: no secret configured => the source is "not connected"
# and the webhook refuses; no stored data for an account => data-gap (no fabrication).
ROI_AI_FILE = Path(os.environ.get(
    "CS_ROI_AI_FILE", str(Path(__file__).resolve().parents[1] / ".cs-roi-ai.jsonl")))


def roi_ai_configured() -> bool:
    """True when the ROI AI webhook signing secret is set (source is connectable)."""
    return bool((os.environ.get("ROI_AI_WEBHOOK_SECRET") or "").strip())


def verify_roi_ai_signature(raw_body: bytes, header_sig: str | None) -> bool:
    """Constant-time HMAC-SHA256 verification of a ROI AI webhook body against the shared
    secret. Expects header value 'sha256=<hex>'. False when the secret is unset or the
    signature is missing/invalid."""
    import hmac as _hmac
    import hashlib as _hashlib
    secret = (os.environ.get("ROI_AI_WEBHOOK_SECRET") or "").strip()
    if not secret or not header_sig:
        return False
    provided = header_sig.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256="):]
    expected = _hmac.new(secret.encode("utf-8"), raw_body, _hashlib.sha256).hexdigest()
    return _hmac.compare_digest(provided, expected)


def _roi_ai_seen_event(event_id: str) -> bool:
    if not event_id or not ROI_AI_FILE.exists():
        return False
    for line in ROI_AI_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            if json.loads(line).get("event_id") == event_id:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def record_roi_ai(body: dict) -> dict:
    """Validate + persist one ROI AI telemetry event. Idempotent on event_id (returns
    {'duplicate': True} without re-writing when already seen). Raises ValueError on an
    invalid payload (caller maps to 400). Never partially writes."""
    event_id = str(body.get("event_id") or "").strip()
    account_ref = str(body.get("account_ref") or body.get("account_id") or "").strip()
    metric_date = str(body.get("metric_date") or "").strip()
    adoption = body.get("adoption_score")
    if not event_id:
        raise ValueError("event_id is required")
    if not account_ref:
        raise ValueError("account_ref is required")
    try:
        date.fromisoformat(metric_date[:10])
    except ValueError:
        raise ValueError("metric_date must be an ISO date (YYYY-MM-DD)")
    if not isinstance(adoption, (int, float)) or not (0 <= adoption <= 100):
        raise ValueError("adoption_score must be a number between 0 and 100")
    if _roi_ai_seen_event(event_id):
        return {"duplicate": True, "event_id": event_id}
    from adapters import identity as _id
    entry = {
        "event_id": event_id,
        "account_id": _id.normalise(account_ref),
        "metric_date": metric_date[:10],
        "adoption_score": adoption,
        "active_roi_users": body.get("active_roi_users"),
        "roi_realized_usd": body.get("roi_realized_usd"),
        "trend": str(body.get("trend") or "").strip().lower() or None,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    ROI_AI_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ROI_AI_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return {"duplicate": False, **entry}


def roi_ai_for(account_id: str) -> dict:
    """Latest ROI AI telemetry for an account (newest metric_date wins), or {} when none
    (data-gap). account_id is matched on the normalised id."""
    if not ROI_AI_FILE.exists():
        return {}
    from adapters import identity as _id
    want = _id.normalise(account_id)
    best = None
    for line in ROI_AI_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if row.get("account_id") == want:
            if best is None or (row.get("metric_date") or "") >= (best.get("metric_date") or ""):
                best = row
    return best or {}


def portfolio() -> dict:
    """The single-pane-of-glass payload: every account with health + segment + ARR +
    renewal, plus the prioritised task queue and suppressed signals across the book."""
    accounts = orchestrate.load_accounts()
    result = orchestrate.orchestrate()

    # Persist a daily health snapshot per account so the platform can show trajectories
    # ("health -18 over 45 days") and drive trend-based risk. Idempotent per day; guarded
    # so history never breaks the main payload.
    try:
        import history
        history.record_portfolio({aid: _live_account(a) for aid, a in accounts.items()},
                                 health_score)
    except Exception:  # noqa: BLE001
        pass

    # Join tasks to accounts by the stable account_id (a display name can collide).
    tasks_by_account: dict[str, list] = {}
    for t in result["tasks"]:
        tasks_by_account.setdefault(t.get("account_id"), []).append(t)

    rows = []
    for aid, a in accounts.items():
        live = _live_account(a)
        hsobj = live.get("hubspot", {})           # {} unless HubSpot is live
        h = health_score(live)                    # computed from live signals only
        # Deterministic renewal forecast, grounded in the health reasons + the signed
        # expansion trigger (reused from orchestrate.evaluate(), not re-thresholded).
        forecast = renewal_forecast(live, h, expansion_qualified=_expansion_qualified(aid, a))
        _usage = live.get("usage", {}) or {}
        # Pooled membership: authoritative from HubSpot cs_customer_tier when set; otherwise
        # fall back to the segment heuristic (1-20 agency + Corporate = pooled cohort).
        _seg_label = hsobj.get("segment_label") or hsobj.get("segment")
        _tier = hsobj.get("customer_tier")
        if hsobj.get("pooled") is not None:
            _pooled = bool(hsobj.get("pooled"))
        else:
            _pooled = _seg_label in ("Agency 1-2 Users", "Agency 3-20 Users", "Corporate")
        rows.append({
            "account_id": aid,
            "name": hsobj.get("name"),
            "segment": _seg_label,
            "customer_tier": _tier,          # authoritative HubSpot tier when present (e.g. "Pooled")
            "pooled": _pooled,               # True when pooled (tier-authoritative, else segment fallback)
            "pooled_source": ("tier" if hsobj.get("pooled") is not None else "segment"),
            "arr_usd": hsobj.get("arr_usd"),
            "renewal_date": hsobj.get("renewal_date"),
            "subscription_type": hsobj.get("subscription_type"),
            "csm_owner": hsobj.get("csm_owner"),
            "lifecycle_stage": hsobj.get("lifecycle_stage"),
            "health": h,
            "renewal_forecast": forecast,
            "connected": _connected(a),
            # Live usage-recency signal (Pendo). Powers the "Usage Trend" column; None when
            # the usage source is not connected, so the UI shows "no data" (never fabricated).
            "usage_days_since_visit": _usage.get("days_since_last_visit") if _connected(a).get("usage") else None,
            # Live CSAT (Zendesk) carried onto the row from the already-fetched signal, so
            # the dashboard CSAT distribution works without a second fan-out. None when the
            # Zendesk source has no CSAT for this account (honest no-data).
            "csat_30d": (live.get("zendesk", {}) or {}).get("csat_30d") if _connected(a).get("zendesk") else None,
            "open_task_count": len(tasks_by_account.get(aid, [])),
        })
    rows.sort(key=lambda r: r["health"]["score"])  # worst health first

    # --- Whole-book merge (Tier-1 lightweight) --------------------------------
    # The enriched rows above are the deeply-signalled slice. Merge in the REST of the
    # customer book from the cheap full-roster scan so every page sees the whole book
    # and the global filter has a real population to work on. These rows carry roster-
    # level fields only (name, segment, owner, ARR, renewal, cohort) with a lightweight
    # health band; full per-account signals load on demand when an account is opened.
    enriched_ids = {r["account_id"] for r in rows}
    include_churned = str(os.environ.get("CS_INCLUDE_CHURNED", "")).lower() in ("1", "true", "yes")
    book_total = managed_count = pooled_count = 0
    try:
        if _src.HUBSPOT.live():
            _roster = _src.HUBSPOT.list_all_companies(cached_only=True)
            # Resolve the distinct HubSpot owner IDs on the whole-book roster to CSM NAMES
            # once (there are only ~15-20 CSMs; _owner_name is cached, so this is a handful
            # of calls). Without this, roster rows carry only the numeric owner_id and the
            # Owner filter shows IDs ("715230") instead of names for most of the book.
            _owner_names: dict = {}
            try:
                for _oid in {str(c.get("owner_id")) for c in _roster if c.get("owner_id")}:
                    _owner_names[_oid] = _src.HUBSPOT._owner_name(_oid)
            except Exception:  # noqa: BLE001
                _owner_names = {}
            for c in _roster:
                lc = str(c.get("lifecycle_stage") or "").lower()
                is_churned = "churn" in lc
                book_total += 1
                if c.get("cohort") == "managed":
                    managed_count += 1
                else:
                    pooled_count += 1
                aid = c.get("account_id") or ("rl-" + str(c.get("company_id")))
                if aid in enriched_ids:
                    continue  # already have the deeply-enriched version
                if is_churned and not include_churned:
                    continue  # active book by default; churned available via filter/env
                band = _roster_band(c)
                _rd = c.get("renewal_date") or None
                rows.append({
                    "account_id": aid,
                    "name": c.get("name"),
                    "segment": c.get("segment_label") or c.get("segment"),
                    "customer_tier": c.get("customer_tier"),
                    "pooled": (c.get("cohort") == "pooled"),
                    "pooled_source": "roster",
                    "cohort": c.get("cohort"),
                    "arr_usd": c.get("arr_usd"),
                    "renewal_date": _rd,
                    "subscription_type": c.get("subscription_type"),
                    "csm_owner": _owner_names.get(str(c.get("owner_id"))) if c.get("owner_id") else None,
                    "csm_owner_id": c.get("owner_id"),
                    "lifecycle_stage": c.get("lifecycle_stage"),
                    "churned": is_churned,
                    "health": band,
                    "renewal_forecast": (renewal_forecast({"hubspot": {"renewal_date": _rd}, "arr_usd": c.get("arr_usd")}, band)
                                         if _rd else
                                         {"applicable": False, "label": None, "rationale": None, "evidence": []}),
                    "connected": {},
                    "usage_days_since_visit": None,
                    "open_task_count": 0,
                    "enriched": False,
                })
    except Exception:  # noqa: BLE001
        pass
    # Tag the enriched rows as fully enriched + give them a cohort for the filter.
    for r in rows:
        if "enriched" not in r:
            r["enriched"] = True
            r.setdefault("cohort", "pooled" if r.get("pooled") else "managed")
    rows.sort(key=lambda r: (r["health"]["score"], not r.get("enriched")))

    # Only sum ARR that is genuinely live-sourced (HubSpot live). No fixture ARR.
    total_arr = sum(r["arr_usd"] or 0 for r in rows)
    at_risk_arr = sum(r["arr_usd"] or 0 for r in rows
                      if r["health"]["computable"] and r["health"]["band"] == "red")

    return {
        "summary": {
            "accounts": len(rows),
            "priority1": sum(1 for t in result["tasks"] if t["priority"] == 1),
            "suppressed": len(result["suppressed"]),
            "total_arr_usd": total_arr,
            "at_risk_arr_usd": at_risk_arr,
            "live_sources": dataaccess.live_sources(),
            "data_mode": "live" if dataaccess.any_live() else "sample",
            "account_scope": ("all" if (not get_principal() or get_principal().get("role") == "admin")
                              else "csm"),
            "scope_owner": (get_principal() or {}).get("name") or (get_principal() or {}).get("email"),
            "principal": ({"name": get_principal().get("name"), "email": get_principal().get("email"),
                           "role": get_principal().get("role")} if get_principal() else None),
            "judge": result.get("judge", {"verdict": "UNKNOWN", "violations": []}),
            # Whole-book counts (all customers, not just the enriched slice) so the UI
            # can show "showing N of M" and drive the global cohort filter.
            "book_total": book_total,
            "book_managed": managed_count,
            "book_pooled": pooled_count,
            "enriched_count": len(enriched_ids),
            "churned_included": include_churned,
        },
        "accounts": rows,
        "tasks": result["tasks"],
        "suppressed": result["suppressed"],
            "automations": result.get("automations", []),
        # Portfolio-wide health trajectory (avg computed-health per day) from the real
        # snapshot history, so the dashboard 'Portfolio Development' widget is live, not
        # a fabricated line. Empty points until enough daily snapshots accumulate.
        "health_history": _portfolio_trajectory(),
    }


def full_roster(cohort: str | None = None, limit: int | None = None) -> dict:
    """Tier-1 whole-book roster: ALL customer companies (managed + pooled/long-tail),
    classified, with portfolio-wide counts and filter facets.

    Lightweight companion to portfolio(): does NOT enrich each company with the other
    vendors (that stays on-demand when an account is opened). Answers "show the entire
    book and let me filter Managed / Pooled / All", which the account_id-tagged roster
    alone cannot. `cohort` optionally filters to 'managed' or 'pooled' (None = all).
    Rows are scoped: an admin sees the whole book; a scoped CSM sees only what they own."""
    rows = _src.HUBSPOT.list_all_companies(limit=limit) if _src.HUBSPOT.live() else []

    principal = get_principal()
    role = (principal or {}).get("role")
    owner_id = (principal or {}).get("owner_id")
    scoped = bool(principal and role != "admin")
    if scoped and owner_id:
        rows = [r for r in rows if str(r.get("owner_id") or "") == str(owner_id)]
    elif scoped and not owner_id:
        rows = []  # a CSM with no resolved owner id sees an empty book, never the whole base

    managed = [r for r in rows if r.get("cohort") == "managed"]
    pooled = [r for r in rows if r.get("cohort") == "pooled"]

    def _arr(rs):
        return sum(r.get("arr_usd") or 0 for r in rs)

    def _facet(key):
        out: dict[str, int] = {}
        for r in rows:
            v = r.get(key) or "Unknown"
            out[v] = out.get(v, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    selected = rows
    if cohort in ("managed", "pooled"):
        selected = [r for r in rows if r.get("cohort") == cohort]

    return {
        "summary": {
            "total": len(rows),
            "managed": len(managed),
            "pooled": len(pooled),
            "total_arr_usd": _arr(rows),
            "managed_arr_usd": _arr(managed),
            "pooled_arr_usd": _arr(pooled),
            "live": _src.HUBSPOT.live(),
            "capped": len(rows) >= int(os.environ.get("CS_FULL_ROSTER_LIMIT", "5000")),
            "account_scope": ("all" if not scoped else "csm"),
            "cohort": cohort or "all",
        },
        "facets": {
            "segment": _facet("segment_label"),
            "lifecycle": _facet("lifecycle_stage"),
        },
        "companies": selected,
    }


def _confidence(account: dict, evidence: dict) -> str:
    sources = account.get("sources", {})
    live_count = sum(1 for value in sources.values() if value == "live")
    if live_count >= 3 and evidence:
        return "high"
    if live_count >= 1 and evidence:
        return "medium"
    return "low"


def daily_brief() -> dict:
    """Human-ready daily brief derived only from the deterministic queue."""
    accounts = orchestrate.load_accounts()
    result = orchestrate.orchestrate()
    by_id = dict(accounts)
    actions = []
    for task in result["tasks"][:3]:
        account = by_id.get(task.get("account_id"), {})
        hs = account.get("hubspot", {})
        actions.append({
            "account": task.get("account"), "priority": task.get("priority"),
            "mandate": task.get("mandate"), "trigger": task.get("trigger"),
            "why": task.get("evidence", {}),
            "recommended_action": task.get("recommended_action"),
            "owner": hs.get("csm_owner") or "Unassigned",
            "sla": {1: "within 24 hours", 2: "today", 3: "this week"}.get(task.get("priority"), "review"),
            "confidence": _confidence(account, task.get("evidence", {})),
            "sources": [key for key, value in account.get("sources", {}).items() if value == "live"],
        })
    gaps = datagaps()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accounts_reviewed": len(accounts),
        "top_actions": actions,
        "priority1_count": sum(1 for task in result["tasks"] if task.get("priority") == 1),
        "at_risk_arr_usd": portfolio()["summary"]["at_risk_arr_usd"],
        "data_gap_accounts": gaps["summary"]["accounts_missing_roles"],
        "judge": result.get("judge", {}),
    }


def why_not(account_id: str) -> dict:
    if not can_view_account(account_id):
        raise ForbiddenError(account_id)
    accounts = orchestrate.load_accounts()
    account = accounts.get(account_id)
    if not account:
        raise KeyError(account_id)
    tasks, _ = orchestrate.evaluate(account_id, account)
    if tasks:
        return {"account_id": account_id, "has_tasks": True, "tasks": tasks}
    live = _live_account(account)
    health = health_score(live)
    usage = live.get("usage", {})
    churn = live.get("churn", {})
    reasons = []
    if health["computable"] and health["band"] == "green":
        reasons.append("health is currently green")
    if usage.get("days_since_last_visit") is not None and usage["days_since_last_visit"] <= 14:
        reasons.append("recent product activity")
    if not churn.get("ml_churn_score") and str(churn.get("churn_status", "")).lower() != "churned":
        reasons.append("no qualifying churn signal")
    if (live.get("zendesk", {}).get("sev1_open") or 0) == 0:
        reasons.append("no open Sev-1")
    return {"account_id": account_id, "has_tasks": False, "reasons": reasons or ["no deterministic rule fired"],
            "data_gaps": [key for key, value in account.get("sources", {}).items() if value == "not_live"]}


def _health_trend_for(account_id: str):
    try:
        import history
        return history.health_trend(account_id, window_days=45)
    except Exception:  # noqa: BLE001
        return None


def _portfolio_trajectory():
    """Portfolio-wide health trajectory for the dashboard; guarded so a history read
    failure never breaks the main payload. Returns {"points": [...]}, possibly empty."""
    try:
        import history
        return history.portfolio_trajectory()
    except Exception:  # noqa: BLE001
        return {"points": []}


def _health_history_for(account_id: str):
    try:
        import history
        return history.history_for(account_id)[-30:]  # last 30 points for a sparkline
    except Exception:  # noqa: BLE001
        return []


def trend_risks(account_id: str) -> list[dict]:
    """Trend-based risks (Customer 360 §5): declines that only history reveals, e.g.
    'health declined >15 points in 45 days'. Returns evidence-backed risk dicts, or []
    when there isn't enough history. Complements the point-in-time playbook risks."""
    risks = []
    try:
        import history
        tr = history.health_trend(account_id, window_days=45)
        if tr and tr["direction"] == "down" and abs(tr["delta"]) >= 15:
            sev = "high" if abs(tr["delta"]) >= 25 else "medium"
            risks.append({
                "type": "health_decline",
                "severity": sev,
                "title": f"Health declining: {tr['delta']} points over {tr['days']} days",
                "evidence": f"Health moved from {tr['past']} to {tr['current']} in {tr['days']} days.",
            })
    except Exception:  # noqa: BLE001
        pass
    return risks


def _timeline_for(account_id: str, tasks: list):
    try:
        import history
        from datetime import datetime, timezone
        today = os.environ.get("CS_TODAY") or datetime.now(timezone.utc).date().isoformat()
        extra = []
        for t in (tasks or []):
            extra.append({
                "date": t.get("due_on") or today,
                "type": "task",
                "title": (t.get("trigger") or t.get("recommended_action") or "CS task"),
                "severity": "risk" if t.get("priority") == 1 else "watch" if t.get("priority", 9) <= 2 else "info",
            })
        return history.timeline_for(account_id, extra_events=extra)
    except Exception:  # noqa: BLE001
        return []


def account_detail(account_id: str) -> dict:
    if not can_view_account(account_id):
        raise ForbiddenError(account_id)
    accounts = orchestrate.load_accounts()
    if account_id in accounts:
        a = accounts[account_id]
    else:
        # Whole-book roster account (Tier-1, not in the deeply-enriched slice). Enrich it
        # live ON DEMAND so the detail page works for EVERY account, not just the enriched
        # ~slice. This is the single-account fan-out (HubSpot + Zendesk + Pendo + Jiminny +
        # Stripe + churn + onboarding), the same assembler the roster uses per account.
        try:
            a = dataaccess.account(account_id)
        except Exception as exc:  # noqa: BLE001
            raise KeyError(account_id) from exc
        # No live HubSpot record for this id -> genuinely unknown account (honest 404).
        if not (a and a.get("hubspot")):
            raise KeyError(account_id)
    tasks, suppressed = orchestrate.evaluate(account_id, a)
    live = _live_account(a)
    h = health_score(live)
    # Expansion qualification reuses the tasks the rules engine just produced, so the
    # forecast keys off the signed expansion triggers without re-thresholding here.
    _expansion_rules = {
        orchestrate.RULE_EXPANSION_UTILIZATION,
        orchestrate.RULE_EXPANSION_API_SURGE,
        orchestrate.RULE_EXPANSION_ADOPTION,
        orchestrate.RULE_EXPANSION_ROI_AI,
    }
    expansion_qualified = any(t.get("rule_id") in _expansion_rules for t in tasks)
    return {
        "account_id": account_id,
        "hubspot": live.get("hubspot", {}),
        "signals": {
            "zendesk": live.get("zendesk", {}),
            "usage": live.get("usage", {}),
            "jiminny": live.get("jiminny", {}),
            "churn": live.get("churn", {}),
            "stripe": live.get("stripe", {}),
            "metrics": live.get("metrics", {}),
            "roi_ai": live.get("roi_ai", {}),
        },
        "onboarding": live.get("onboarding", {}),
        "f2f_log": f2f_log_for(account_id),
        "f2f_last": last_f2f(account_id),
        "health": h,
        "renewal_forecast": renewal_forecast(live, h, expansion_qualified=expansion_qualified),
        "health_trend": _health_trend_for(account_id),
        "trend_risks": trend_risks(account_id),
        "health_history": _health_history_for(account_id),
        "timeline": _timeline_for(account_id, tasks),
        "expansion": expansion_score(live),
        "adoption": adoption_score(live),
        "lifecycle_state": lifecycle_state(live),
        "sources": a.get("sources", {}),
        "connected": _connected(a),
        "primary_instances": sorted(primary_instance_ids(live.get("hubspot", {}))),
        "tasks": tasks,
        "suppressed": suppressed,
        "hubspot_writeback": _writeback_payload(live, h, tasks) if _is_live(a.get("sources", {}), "hubspot") else None,
    }


# Per-CSM whole-book enrichment cache. Keyed by owner_id (or "admin"), holds the enriched
# row fields for that book plus progress, so a CSM's My Companies view can fill in health/
# usage/forecast across their ENTIRE book (not just the enriched-50 slice) without a
# synchronous fan-out that would hang the request. Populated by a bounded background
# thread; the UI polls get_book_enrichment() and re-renders as rows land.
_BOOK_ENRICHMENT: dict = {}          # owner_key -> {"rows": {aid: row}, "done": int, "total": int, "running": bool, "at": float}
_BOOK_ENRICHMENT_LOCK = __import__("threading").Lock()


def _book_owner_key() -> str:
    p = get_principal()
    if not p or p.get("role") == "admin":
        return "admin"
    return "owner:" + str(p.get("owner_id") or "none")


def enrich_my_book(max_accounts: int | None = None) -> dict:
    """Kick off (or report on) background enrichment of the CURRENT principal's whole book.

    The enriched slice only covers ~50 accounts globally, so a CSM's My Companies view
    shows "roster / no data" for most of their book even though each account CAN be
    enriched on demand. This enriches every account the CSM owns - resolved from the cheap
    whole-book roster - in a bounded background thread (CS_FETCH_WORKERS concurrency),
    caching the row-level fields per owner. Non-blocking: returns the current progress
    immediately; the UI polls and re-renders as rows arrive. Honest: an account with no
    live record is skipped (stays a roster row). Owner-scoped; never fabricates.
    """
    import os, time, threading
    key = _book_owner_key()
    p = get_principal()
    owner_id = p.get("owner_id") if (p and p.get("role") not in (None, "admin")) else None
    try:
        cap = max_accounts if max_accounts is not None else int(os.environ.get("CS_BOOK_ENRICH_MAX", "400"))
    except (ValueError, TypeError):
        cap = 400

    with _BOOK_ENRICHMENT_LOCK:
        state = _BOOK_ENRICHMENT.get(key)
        # Fresh completed result (within TTL) or an in-flight run: just report progress.
        ttl = _report_cache_ttl()
        if state and (state.get("running") or (time.time() - state.get("at", 0)) < ttl):
            return _book_progress(key)
        _BOOK_ENRICHMENT[key] = {"rows": {}, "done": 0, "total": 0, "running": True, "at": time.time()}

    # Resolve the owner's book from the cheap roster (real account_id refs only - a
    # synthetic rl-<id> row has no vendor-resolvable ref, so it stays a roster row).
    try:
        roster = _src.HUBSPOT.list_all_companies(cached_only=True) if _src.HUBSPOT.live() else []
    except Exception:  # noqa: BLE001
        roster = []
    ids = []
    for c in roster:
        aid = c.get("account_id")
        if not aid:
            continue
        if owner_id and str(c.get("owner_id") or "") != str(owner_id):
            continue
        ids.append(_src.identity.normalise(aid))
    ids = ids[:cap]

    with _BOOK_ENRICHMENT_LOCK:
        _BOOK_ENRICHMENT[key]["total"] = len(ids)

    principal_snapshot = dict(p) if p else None

    def _worker():
        from concurrent.futures import ThreadPoolExecutor
        try:
            workers = int(os.environ.get("CS_FETCH_WORKERS", "6"))
        except ValueError:
            workers = 6
        # The background threads do not inherit the request's principal context, so each
        # enrich_one re-asserts it (ownership is re-checked from the enriched record).
        def _enrich_one(aid):
            try:
                set_principal(principal_snapshot)
                rows = enrich_rows([aid])
                return rows.get("accounts", {}).get(aid)
            except Exception:  # noqa: BLE001
                return None
        try:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                for aid, row in zip(ids, ex.map(_enrich_one, ids)):
                    with _BOOK_ENRICHMENT_LOCK:
                        st = _BOOK_ENRICHMENT.get(key)
                        if st is None:
                            return
                        st["done"] += 1
                        if row:
                            st["rows"][aid] = row
                        st["at"] = time.time()
        finally:
            with _BOOK_ENRICHMENT_LOCK:
                st = _BOOK_ENRICHMENT.get(key)
                if st is not None:
                    st["running"] = False
                    st["at"] = time.time()

    threading.Thread(target=_worker, daemon=True).start()
    return _book_progress(key)


def _book_progress(key: str) -> dict:
    with _BOOK_ENRICHMENT_LOCK:
        st = _BOOK_ENRICHMENT.get(key) or {"rows": {}, "done": 0, "total": 0, "running": False, "at": 0.0}
        return {
            "accounts": dict(st["rows"]),
            "done": st["done"],
            "total": st["total"],
            "running": st["running"],
            "complete": (not st["running"]) and st["total"] > 0 and st["done"] >= st["total"],
        }


def get_book_enrichment() -> dict:
    """Report current progress of the principal's book enrichment (poll endpoint)."""
    return _book_progress(_book_owner_key())


def enrich_rows(account_ids: list) -> dict:
    """Progressive enrichment for list views: given specific account ids (typically the
    rows currently visible), live-enrich each ON DEMAND and return only the row-level
    fields the tables/widgets read. This lets a list fill in health/usage/owner for the
    whole book a screen at a time, without the one-shot full-book fan-out. Honest: an id
    with no live HubSpot record is skipped (stays a roster row). Owner-scoped.
    """
    out: dict = {}
    enriched_slice = orchestrate.load_accounts()
    for aid in (account_ids or [])[:60]:  # cap per call to keep each request snappy
        if not aid or not can_view_account(aid):
            continue
        a = enriched_slice.get(aid)
        if a is None:
            try:
                a = dataaccess.account(aid)
            except Exception:  # noqa: BLE001
                continue
        if not (a and a.get("hubspot")):
            continue
        live = _live_account(a)
        hs = live.get("hubspot", {})
        h = health_score(live)
        tasks, _sup = orchestrate.evaluate(aid, a)
        forecast = renewal_forecast(live, h, expansion_qualified=_expansion_qualified(aid, a))
        usage = live.get("usage", {}) or {}
        out[aid] = {
            "account_id": aid,
            "name": hs.get("name"),
            "segment": hs.get("segment_label") or hs.get("segment"),
            "arr_usd": hs.get("arr_usd"),
            "renewal_date": hs.get("renewal_date"),
            "subscription_type": hs.get("subscription_type"),
            "csm_owner": hs.get("csm_owner"),
            "lifecycle_stage": hs.get("lifecycle_stage"),
            "health": h,
            "renewal_forecast": forecast,
            "connected": _connected(a),
            "usage_days_since_visit": usage.get("days_since_last_visit") if _connected(a).get("usage") else None,
            "csat_30d": (live.get("zendesk", {}) or {}).get("csat_30d") if _connected(a).get("zendesk") else None,
            "open_task_count": len(tasks),
            "enriched": True,
        }
    return {"accounts": out}


def _writeback_payload(account: dict, health: dict, tasks: list) -> dict:
    """The CS data pushed BACK to HubSpot for Sales visibility (req §1 bi-directional).

    In fixture mode this is the payload that WOULD be written; swap for a live
    hubspot.crm.companies PATCH."""
    if not health["computable"]:
        return {
            "target": "hubspot.crm.companies",
            "cs_health_score": None,
            "cs_risk_status": "no_data",
            "cs_active_playbook": None,
            "note": "No live health signal is available; no health write-back is recommended.",
        }
    churned = str(account.get("churn", {}).get("churn_status") or "").lower() == "churned"
    risk = "at_risk" if churned or health["band"] == "red" else "watch" if health["band"] == "amber" else "healthy"
    playbook = next((t["trigger"] for t in tasks if t["priority"] == 1), None) \
        or next((t["trigger"] for t in tasks), None)
    return {
        "target": "hubspot.crm.companies",
        "cs_health_score": health["score"],
        "cs_risk_status": risk,
        "cs_active_playbook": playbook,
    }


def writeback(account_id: str, apply: bool = False) -> dict:
    """Prepare or apply the HubSpot CS write-back with an auditable result.

    Dry-run is the default. Applying requires both `apply=True` and
    `CS_ALLOW_WRITE=1`; the adapter enforces the same guard at the HTTP boundary.
    """
    account = orchestrate.load_accounts().get(account_id)
    if not account:
        raise KeyError(account_id)
    live = _live_account(account)
    health = health_score(live)
    tasks, _ = orchestrate.evaluate(account_id, account)
    payload = _writeback_payload(live, health, tasks)
    if not (dataaccess._ADAPTERS and _src.HUBSPOT.live()):
        result = {"synced": False, "mode": "not-connected", "would_write": payload}
    else:
        result = _src.HUBSPOT.push_cs_data(
            account_id,
            health_score=payload["cs_health_score"],
            risk_status=payload["cs_risk_status"],
            active_playbook=payload["cs_active_playbook"],
            apply=apply,
        )
    return {
        "audit_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "apply_requested": bool(apply),
        "write_enabled": bool(os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on")),
        "result": result,
    }


# Re-engagement sequences the platform can enrol pooled accounts into. Deterministic
# catalogue (the actual send is done by the connected sequencing tool on apply).
SEQUENCES = {
    "low_usage_reengage": {
        "label": "Low-usage re-engagement",
        "steps": ["Value check-in email", "Feature nudge (idle feature)", "Book a 15-min reset call"],
        "trigger": "No product visit in 28+ days (live Pendo)",
    },
    "renewal_180": {
        "label": "Renewal runway (T-180)",
        "steps": ["Renewal heads-up", "Success recap", "Renewal confirmation"],
        "trigger": "Renewal within 180 days",
    },
    "onboarding_welcome": {
        "label": "Automated onboarding (Tech Touch Pillar 1)",
        "steps": ["Welcome email", "Feature training drip", "Milestone engagement survey",
                  "Community workspace invitation"],
        "trigger": "New customer in onboarding / early lifecycle (scaled tech-touch)",
    },
}


def enrol_sequence(account_id: str, sequence: str = "low_usage_reengage", apply: bool = False) -> dict:
    """Prepare or apply a one-to-many re-engagement sequence enrolment.

    Same two-gate safety as the HubSpot write-back: dry-run by default; a real
    enrolment requires BOTH apply=True AND CS_ALLOW_WRITE=1. Nothing is sent otherwise.
    Grounded: only enrols accounts that genuinely qualify (e.g. idle 28+ days) and
    never fabricates a send result.
    """
    seq = SEQUENCES.get(sequence)
    if not seq:
        raise ValueError(f"unknown sequence {sequence}")
    account = orchestrate.load_accounts().get(account_id)
    if not account:
        raise KeyError(account_id)
    live = _live_account(account)
    hs = live.get("hubspot", {}) or {}
    usage = live.get("usage", {}) or {}
    dsv = usage.get("days_since_last_visit")
    # Qualification is deterministic and source-backed.
    if sequence == "low_usage_reengage":
        qualifies = dsv is not None and dsv >= 28
        reason = (f"idle {dsv} days" if qualifies else
                  (f"active ({dsv} days since visit)" if dsv is not None else "no usage signal"))
    elif sequence == "onboarding_welcome":
        ob = live.get("onboarding", {}) or {}
        lc = str(hs.get("lifecycle_stage") or "").lower()
        is_new = lc in ("customer",) and (ob.get("status") or "").lower() in (
            "new", "onboarding", "in_progress", "kickoff", "implementation", "implementing", "training", "")
        qualifies = is_new and not str(ob.get("status") or "").lower() in ("completed", "complete", "done", "live")
        reason = ("new customer in onboarding" if qualifies else
                  "not in early onboarding phase")
    else:
        qualifies = bool(hs.get("renewal_date"))
        reason = "renewal date set" if qualifies else "no renewal date"
    write_enabled = os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on")
    can_apply = bool(apply) and write_enabled and qualifies
    prepared = {
        "sequence": sequence,
        "label": seq["label"],
        "steps": seq["steps"],
        "trigger": seq["trigger"],
        "account": hs.get("name") or account_id,
        "qualifies": qualifies,
        "qualification_reason": reason,
    }
    if can_apply:
        # Live enrolment requires a connected sequencing tool. When none is wired we
        # do NOT claim a send; we surface an honest "no sequencing tool connected".
        result = {"enrolled": False, "mode": "no-sequencing-tool",
                  "note": "CS_ALLOW_WRITE=1 and account qualifies, but no sequencing tool "
                          "is connected. Connect an email/sequencing source to send."}
    elif not qualifies:
        result = {"enrolled": False, "mode": "not-qualified", "note": reason}
    else:
        result = {"enrolled": False, "mode": "dry-run",
                  "note": "Prepared only. Set CS_ALLOW_WRITE=1 and request apply=true "
                          "with a connected sequencing tool to enrol."}
    return {
        "audit_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "apply_requested": bool(apply),
        "write_enabled": write_enabled,
        "prepared": prepared,
        "result": result,
    }


def log_note(account_id: str, note: str, apply: bool = False) -> dict:
    """Log a call/meeting note to the account's HubSpot timeline. Reversible CRM write;
    two-gate (apply + CS_ALLOW_WRITE), dry-run by default."""
    if not (dataaccess._ADAPTERS and _src.HUBSPOT.live()):
        result = {"logged": False, "mode": "not-connected"}
    else:
        result = _src.HUBSPOT.log_note(account_id, note, apply=apply)
    return {
        "audit_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "apply_requested": bool(apply),
        "write_enabled": bool(os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on")),
        "result": result,
    }


def tag_contact_role(account_id: str, contact_email: str, role: str, apply: bool = False) -> dict:
    """Tag a contact's CS role (Exec Sponsor / Primary Champion / Finance Contact) in
    HubSpot. Reversible; two-gate; dry-run by default. Satisfies the WoW contact-role
    architecture requirement from inside the platform."""
    if not (dataaccess._ADAPTERS and _src.HUBSPOT.live()):
        result = {"tagged": False, "mode": "not-connected"}
    else:
        result = _src.HUBSPOT.tag_contact_role(account_id, contact_email, role, apply=apply)
    return {
        "audit_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "apply_requested": bool(apply),
        "write_enabled": bool(os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on")),
        "result": result,
    }


def monthly_digest(account_id: str) -> dict:
    """Compile the monthly account performance digest (Tech-Touch engine / Scenario E) from
    LIVE signals: licence/seat utilisation, active logins, top feature adoption, Zendesk
    tickets resolved, and CSAT. Flags an expansion CTA when licence utilisation >= 85%.
    Identifies the Primary Admin recipient; when none is tagged, flags a data-cleanup need
    (edge case from the spec). Every field is honest 'no data' when its source is absent,
    nothing is fabricated. Read-only compile; the send is a separate gated action."""
    detail = account_detail(account_id)  # owner-scope enforced inside (raises ForbiddenError)
    hs = detail.get("hubspot", {}) or {}
    sig = detail.get("signals", {}) or {}
    usage = sig.get("usage", {}) or {}
    zd = sig.get("zendesk", {}) or {}
    metrics = sig.get("metrics", {}) or {}

    util = usage.get("license_utilization_pct")
    if util is None:
        util = metrics.get("user_utilization_pct")
    contacts = hs.get("contacts", []) or []
    admin = next((c for c in contacts
                  if c.get("role") == "Primary Champion / Admin" and c.get("email")), None)
    # The spec's 1st-of-month dispatch goes to the Primary Admin AND the Executive Sponsor.
    exec_sponsor = next((c for c in contacts
                         if c.get("role") == "Executive Sponsor" and c.get("email")), None)

    metrics_block = {
        "licence_utilization_pct": util,
        "active_logins_7d": usage.get("logins_last_7d"),
        "top_feature_adoption_pct": usage.get("key_feature_adoption_pct"),
        "days_since_last_visit": usage.get("days_since_last_visit"),
        "tickets_resolved_30d": zd.get("tickets_resolved_30d"),
        "csat_30d": zd.get("csat_30d"),
        # ROI AI adoption (V5): None when no telemetry for this account (data-gap).
        "roi_ai_adoption_score": (sig.get("roi_ai", {}) or {}).get("adoption_score"),
    }
    expansion_cta = isinstance(util, (int, float)) and util >= 85
    # Combined, de-duplicated recipient list (Primary Admin + Executive Sponsor), each
    # with the role it was reached by so the dispatch log is auditable.
    recipients = []
    seen_emails = set()
    for contact, role in ((admin, "Primary Champion / Admin"), (exec_sponsor, "Executive Sponsor")):
        email = (contact or {}).get("email")
        if email and email.lower() not in seen_emails:
            seen_emails.add(email.lower())
            recipients.append({"email": email, "name": contact.get("name"), "role": role})
    return {
        "account_id": account_id,
        "name": hs.get("name") or account_id,
        # Primary recipient kept for backward compatibility (Primary Admin).
        "recipient": (admin or {}).get("email"),
        "recipient_name": (admin or {}).get("name"),
        # Executive Sponsor as a named second recipient (spec: dispatch to both).
        "exec_sponsor": (exec_sponsor or {}).get("email"),
        "exec_sponsor_name": (exec_sponsor or {}).get("name"),
        # Full de-duplicated recipient set the send iterates over.
        "recipients": recipients,
        "missing_primary_admin": admin is None,  # -> data-cleanup task (spec edge case)
        "missing_exec_sponsor": exec_sponsor is None,
        "metrics": metrics_block,
        "expansion_cta": expansion_cta,  # inline 'Add Seats / Upgrade' when utilisation >= 85%
        "period": datetime.now(timezone.utc).strftime("%B %Y"),
    }


def send_digest(account_id: str, apply: bool = False) -> dict:
    """Send the monthly digest to the account's Primary Admin AND Executive Sponsor, and
    log a copy to the HubSpot record timeline (spec UC2: 1st-of-month dispatch to both,
    copy on the timeline). Two-gate (apply + CS_ALLOW_WRITE), dry-run by default.
    Customer-facing send, so even with both gates it only sends when an outbound email
    capability is connected (CS_EMAIL_PROVIDER); otherwise it honestly reports 'prepared,
    no email provider connected', never a fake send. When no Primary Admin is tagged it
    refuses and flags the data-cleanup need. The timeline copy is logged only after a
    successful send and never fails the send if the note-log itself fails."""
    digest = monthly_digest(account_id)
    if digest["missing_primary_admin"]:
        result = {"sent": False, "mode": "no-recipient",
                  "note": "No Primary Admin tagged; a data-cleanup task is required before "
                          "the monthly digest can be sent."}
    elif not (apply and _write_enabled()):
        result = {"sent": False, "mode": "dry-run", "recipient": digest["recipient"],
                  "note": "Prepared only. Set CS_ALLOW_WRITE=1 and request apply=true to send."}
    elif not (os.environ.get("CS_EMAIL_PROVIDER") or "").strip():
        result = {"sent": False, "mode": "no-email-provider", "recipient": digest["recipient"],
                  "note": "Writes enabled and a recipient exists, but no outbound email "
                          "provider is connected (set CS_EMAIL_PROVIDER / provision HubSpot "
                          "marketing send). No email was sent."}
    else:
        # Real send path. Option A: HubSpot transactional single-send. The concrete send is
        # performed by the connected provider and we surface its honest result verbatim
        # (template-not-configured / send-failed / applied); we never fabricate a success.
        provider = (os.environ.get("CS_EMAIL_PROVIDER") or "").strip().lower()
        if provider == "hubspot" and dataaccess._ADAPTERS and _src.HUBSPOT.live():
            tokens = {
                "account_name": digest["name"],
                "period": digest["period"],
                "licence_utilization_pct": digest["metrics"].get("licence_utilization_pct"),
                "active_logins_7d": digest["metrics"].get("active_logins_7d"),
                "top_feature_adoption_pct": digest["metrics"].get("top_feature_adoption_pct"),
                "tickets_resolved_30d": digest["metrics"].get("tickets_resolved_30d"),
                "csat_30d": digest["metrics"].get("csat_30d"),
                "expansion_cta": "yes" if digest["expansion_cta"] else "no",
                "recipient_name": digest.get("recipient_name"),
            }
            # Dispatch to EVERY resolved recipient (Primary Admin AND Executive Sponsor,
            # per the spec), surfacing each send's honest result. Falls back to the single
            # primary recipient if the recipients list is somehow empty.
            recipient_list = digest.get("recipients") or (
                [{"email": digest["recipient"], "name": digest.get("recipient_name"),
                  "role": "Primary Champion / Admin"}] if digest.get("recipient") else [])
            sends = []
            for r in recipient_list:
                send = _src.HUBSPOT.send_transactional_email(
                    r["email"],
                    subject=f"{digest['name']} - your {digest['period']} JobAdder summary",
                    custom_properties={**tokens, "recipient_name": r.get("name")}, apply=True)
                sends.append({"email": r["email"], "role": r.get("role"),
                              "sent": bool(send.get("sent")), "mode": send.get("mode"),
                              "note": send.get("note"), "send_result": send.get("send_result")})
            all_sent = bool(sends) and all(s["sent"] for s in sends)
            # Log a copy to the HubSpot record timeline (spec: "a copy logged to the HubSpot
            # record timeline"). Non-fatal: a timeline-log failure never fails the send.
            timeline = {"logged": False, "mode": "not-attempted"}
            if all_sent:
                try:
                    recips = ", ".join(f"{s['role']} <{s['email']}>" for s in sends)
                    note = (f"Monthly performance report ({digest['period']}) dispatched to {recips}. "
                            f"Licence utilisation {digest['metrics'].get('licence_utilization_pct')}%, "
                            f"CSAT {digest['metrics'].get('csat_30d')}, "
                            f"expansion CTA {'included' if digest['expansion_cta'] else 'not included'}.")
                    tl = _src.HUBSPOT.log_note(account_id, note, apply=True)
                    timeline = {"logged": bool(tl.get("logged")), "mode": tl.get("mode"),
                                "note_preview": note[:160]}
                except Exception as exc:  # noqa: BLE001
                    timeline = {"logged": False, "mode": "log-failed", "error": f"{type(exc).__name__}: {exc}"}
            # `mode` mirrors the provider's own send mode for the primary recipient
            # (back-compat: callers/tests read 'applied'); 'recipients' carries the full
            # per-recipient detail for the Admin + Exec Sponsor dispatch.
            primary_mode = sends[0]["mode"] if sends else "no-recipient"
            result = {"sent": all_sent,
                      "mode": primary_mode if all_sent else "partial-or-failed",
                      "recipient": digest["recipient"],      # primary (back-compat)
                      "recipients": sends,                    # per-recipient results (Admin + Exec Sponsor)
                      "provider": provider,
                      "timeline_logged": timeline,
                      "note": f"Dispatched to {len(sends)} recipient(s): "
                              + ", ".join(s["role"] for s in sends) + "."}
        else:
            # Provider named but not usable (unknown provider, or HubSpot not live).
            result = {"sent": False, "mode": "provider-unavailable",
                      "recipient": digest["recipient"], "provider": provider,
                      "note": "The configured CS_EMAIL_PROVIDER is not usable "
                              "(unknown provider or HubSpot not connected). No email sent."}
    return {
        "audit_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "apply_requested": bool(apply),
        "write_enabled": _write_enabled(),
        "digest": digest,
        "result": result,
    }


def _write_enabled() -> bool:
    return bool(os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on"))


def run_monthly_digests(apply: bool = False) -> dict:
    """Batch monthly-digest run across the current principal's named-account book (the
    1st-of-month scheduler calls this). Compiles each account's digest and performs the
    gated send through send_digest, so every honesty gate is preserved: writes-off/no
    recipient/no email provider never send. Returns a summary only (counts + per-account
    outcome), never customer prose. Owner-scoped: iterates only accounts the principal can
    write, so a scheduled run on a service principal (admin) covers the whole book while a
    CSM-scoped run covers theirs. Idempotent to re-run: a second run with no provider just
    re-reports 'prepared'."""
    period = datetime.now(timezone.utc).strftime("%B %Y")
    accounts = orchestrate.load_accounts()
    rows: list[dict] = []
    compiled = sent = missing_admin = no_provider = dry_run = errors = failed = 0
    for aid in accounts:
        # Owner-scope gate: skip accounts outside the principal's write scope silently so a
        # CSM-scoped scheduled run only touches their book (never raises mid-batch).
        if not can_write_account(aid):
            continue
        try:
            res = send_digest(aid, apply=apply)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            rows.append({"account_id": aid, "mode": "error", "error": str(exc)})
            continue
        mode = (res.get("result") or {}).get("mode")
        compiled += 1
        if mode == "applied":
            sent += 1
        elif mode == "no-recipient":
            missing_admin += 1
        elif mode in ("no-email-provider", "template-not-configured", "provider-unavailable"):
            no_provider += 1   # recipient exists but the send path is not usable yet
        elif mode == "dry-run":
            dry_run += 1
        elif mode == "send-failed":
            failed += 1
        rows.append({
            "account_id": aid,
            "name": (res.get("digest") or {}).get("name"),
            "mode": mode,
            "recipient": (res.get("digest") or {}).get("recipient"),
            "expansion_cta": (res.get("digest") or {}).get("expansion_cta"),
        })
    return {
        "run_id": uuid.uuid4().hex,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "period": period,
        "apply_requested": bool(apply),
        "write_enabled": _write_enabled(),
        "email_provider": (os.environ.get("CS_EMAIL_PROVIDER") or "").strip() or None,
        "summary": {
            "accounts_in_scope": len(rows),
            "compiled": compiled,
            "sent": sent,
            "dry_run": dry_run,
            "missing_primary_admin": missing_admin,
            "no_email_provider": no_provider,
            "send_failed": failed,
            "errors": errors,
        },
        "accounts": rows,
    }

# --- Strategic monthly review / approve window (Tech-Touch §3, UC2) ----------
# The high-touch monthly rhythm: on/after the 28th the platform pre-populates a DRAFT
# monthly report per strategic (named) account; 28th-31st the CSM reviews, adds executive
# comments, and approves; on the 1st approved reports dispatch, and any still-unreviewed
# auto-send the baseline (edge case: "unreviewed drafts auto-send baseline on the 1st").
# Review state is keyed by (account_id, period) and PERSISTED to an append-only JSONL
# store (latest-wins), so CSM comments/approvals survive restarts AND are visible to the
# separate scheduler ECS process that runs the 1st-of-month dispatch. Mirrors the F2F /
# success-plan / ROI-AI stores. Env CS_DIGEST_REVIEWS_FILE relocates it (EFS in prod).
_DIGEST_REVIEWS_FILE = os.environ.get(
    "CS_DIGEST_REVIEWS_FILE",
    str(Path(__file__).resolve().parents[1] / ".cs-digest-reviews.jsonl"))


def _review_key(account_id: str, period: str) -> str:
    return f"{account_id}::{period}"


def _review_get(account_id: str, period: str) -> dict | None:
    """Latest review entry for (account_id, period) from the JSONL store, or None."""
    path = Path(os.environ.get("CS_DIGEST_REVIEWS_FILE", _DIGEST_REVIEWS_FILE))
    if not path.exists():
        return None
    key = _review_key(account_id, period)
    found = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if _review_key(row.get("account_id", ""), row.get("period", "")) == key:
                found = row  # later lines win
    except Exception:  # noqa: BLE001
        return None
    return found


def _review_put(account_id: str, period: str, entry: dict) -> None:
    """Append a review entry (latest-wins on read)."""
    path = Path(os.environ.get("CS_DIGEST_REVIEWS_FILE", _DIGEST_REVIEWS_FILE))
    row = {"account_id": account_id, "period": period, **entry}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _is_named_account(a: dict) -> bool:
    """Strategic/named = not pooled. Mirrors the portfolio pooled/managed split."""
    hs = (a.get("hubspot") or a) if isinstance(a, dict) else {}
    seg = hs.get("segment_label") or hs.get("segment")
    if hs.get("pooled") is not None:
        return not bool(hs.get("pooled"))
    return seg not in POOLED_SEGMENTS


def monthly_review_queue(period: str | None = None) -> dict:
    """The strategic monthly-report review queue: a draft digest + review status for every
    owner-scoped NAMED account. Statuses: draft (pre-populated) | commented | approved.
    Reusable 28th-31st; owner-scoped (a CSM sees their named book, admin sees all)."""
    period = period or datetime.now(timezone.utc).strftime("%B %Y")
    accounts = orchestrate.load_accounts()
    rows = []
    counts = {"draft": 0, "commented": 0, "approved": 0}
    for aid, a in accounts.items():
        if not can_view_account(aid):
            continue
        if not _is_named_account(a):
            continue
        try:
            digest = monthly_digest(aid)
        except Exception:  # noqa: BLE001
            continue
        rv = _review_get(aid, period)
        status = (rv or {}).get("status", "draft")
        counts[status] = counts.get(status, 0) + 1
        rows.append({
            "account_id": aid,
            "name": digest["name"],
            "period": period,
            "recipient": digest["recipient"],
            "missing_primary_admin": digest["missing_primary_admin"],
            "expansion_cta": digest["expansion_cta"],
            "metrics": digest["metrics"],
            "status": status,
            "comment": (rv or {}).get("comment"),
            "reviewer": (rv or {}).get("reviewer"),
            "updated_at": (rv or {}).get("updated_at"),
        })
    rows.sort(key=lambda r: (r["status"] != "draft", r["name"] or ""))
    return {"period": period, "count": len(rows), "status_counts": counts, "accounts": rows}


def add_review_comment(account_id: str, comment: str, period: str | None = None) -> dict:
    """Add/replace the CSM's executive comment on a strategic account's monthly draft.
    Moves status draft -> commented. Owner-scope enforced at the endpoint."""
    period = period or datetime.now(timezone.utc).strftime("%B %Y")
    comment = (comment or "").strip()
    if not comment:
        raise ValueError("comment text is required")
    p = get_principal() or {}
    entry = _review_get(account_id, period) or {}
    entry.update({"status": "commented" if entry.get("status") != "approved" else "approved",
                  "comment": comment,
                  "reviewer": p.get("name") or p.get("email"),
                  "updated_at": datetime.now(timezone.utc).isoformat()})
    _review_put(account_id, period, entry)
    return {"account_id": account_id, "period": period, **entry}


def approve_digest(account_id: str, period: str | None = None) -> dict:
    """Approve a strategic account's monthly report for dispatch on the 1st."""
    period = period or datetime.now(timezone.utc).strftime("%B %Y")
    p = get_principal() or {}
    entry = _review_get(account_id, period) or {}
    entry.update({"status": "approved",
                  "reviewer": p.get("name") or p.get("email"),
                  "approved_at": datetime.now(timezone.utc).isoformat(),
                  "updated_at": datetime.now(timezone.utc).isoformat()})
    _review_put(account_id, period, entry)
    return {"account_id": account_id, "period": period, **entry}


def dispatch_reviewed_digests(period: str | None = None, apply: bool = False) -> dict:
    """The 1st-of-month dispatch for strategic accounts: send APPROVED reports, and
    auto-send the BASELINE for any still-unreviewed (draft) account so the monthly cadence
    is never missed (spec edge case). Commented-but-not-approved are held (the CSM intended
    to finish). Honest + gated: all honesty gates in send_digest are preserved. Returns a
    summary. Owner-scoped."""
    period = period or datetime.now(timezone.utc).strftime("%B %Y")
    q = monthly_review_queue(period)
    sent = baseline = held = errors = 0
    rows = []
    for acc in q["accounts"]:
        aid = acc["account_id"]
        status = acc["status"]
        if status == "commented":
            held += 1
            rows.append({"account_id": aid, "name": acc["name"], "action": "held",
                         "note": "Commented but not approved; not dispatched."})
            continue
        # approved -> dispatch; draft -> auto baseline.
        try:
            res = send_digest(aid, apply=apply)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            rows.append({"account_id": aid, "name": acc["name"], "action": "error", "error": str(exc)})
            continue
        mode = (res.get("result") or {}).get("mode")
        did_send = mode == "applied"
        if status == "approved":
            sent += 1 if did_send else 0
        else:
            baseline += 1 if did_send else 0
        rows.append({"account_id": aid, "name": acc["name"],
                     "action": "approved-dispatch" if status == "approved" else "auto-baseline",
                     "mode": mode})
    return {
        "period": period,
        "apply_requested": bool(apply),
        "summary": {
            "named_accounts": q["count"],
            "approved_dispatched": sent,
            "auto_baseline": baseline,
            "held_commented": held,
            "errors": errors,
        },
        "accounts": rows,
    }



def create_csql(account_id: str, name: str, amount_usd=None, note: str | None = None,
                apply: bool = False) -> dict:
    """Create an expansion deal (CSQL) in HubSpot for an account. Two-gate; dry-run by
    default; owner-scope enforced at the endpoint. Expansion routing from in-platform."""
    if not (dataaccess._ADAPTERS and _src.HUBSPOT.live()):
        result = {"created": False, "mode": "not-connected"}
    else:
        result = _src.HUBSPOT.create_csql(account_id, name, amount_usd=amount_usd, note=note, apply=apply)
    return {
        "audit_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "apply_requested": bool(apply),
        "write_enabled": bool(os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on")),
        "result": result,
    }


# The pooled (Scaled) cohort = sub-$10k structure: 1-20 user Agency tiers + Corporate.
# Authoritative when HubSpot sets cs_customer_tier="Pooled"; otherwise this segment set is
# the heuristic (same definition the portfolio uses for the pooled/managed split).
POOLED_SEGMENTS = ("Agency 1-2 Users", "Agency 3-20 Users", "Corporate")


def pooled_cohort(account_ids: list[str] | None = None) -> dict:
    """Resolve the 1-20 Agency + Corporate cohort for the current principal (owner-scoped),
    with count + ARR + current owners, so the move-to-pooled action can preview exactly what
    it will touch. When account_ids is given, restrict to those; otherwise scan the book."""
    accounts = orchestrate.load_accounts()
    rows = []
    total_arr = 0
    for aid, a in accounts.items():
        if account_ids is not None and aid not in account_ids:
            continue
        if not can_view_account(aid):
            continue
        live = _live_account(a)
        hs = live.get("hubspot", {}) or {}
        tier = hs.get("customer_tier")
        seg = hs.get("segment_label") or hs.get("segment")
        already_pooled = (str(tier or "").lower() == "pooled") or bool(hs.get("pooled"))
        in_cohort = already_pooled or (seg in POOLED_SEGMENTS)
        if not in_cohort:
            continue
        arr = hs.get("arr_usd") or 0
        total_arr += arr
        rows.append({"account_id": aid, "name": hs.get("name"), "segment": seg,
                     "customer_tier": tier, "already_pooled": already_pooled,
                     "csm_owner": hs.get("csm_owner"), "arr_usd": arr,
                     "writable": can_write_account(aid)})
    return {"cohort": "agency_1_20_plus_corporate", "count": len(rows),
            "total_arr_usd": total_arr, "accounts": rows}


def move_to_pooled(account_ids: list[str] | None = None, clear_owner: bool = True,
                   pooled_team: str | None = None, apply: bool = False) -> dict:
    """Move the 1-20 Agency + Corporate cohort (or an explicit account_ids list) to the
    pooled (Scaled) structure by setting cs_customer_tier='Pooled' in HubSpot, optionally
    clearing the named owner so the accounts leave individual books and are served by the
    pooled round-robin queue. When pooled_team is given, each account is also assigned to
    that pooled team (cs_pooled_team). Owner-scoped (a CSM moves only accounts they own;
    admin moves any), two-gated (apply + CS_ALLOW_WRITE), dry-run by default, audited at the
    endpoint, and fully reversible. Honest batch summary; never fabricates a write.

    An account already pooled is skipped as a no-op (counted 'already_pooled')."""
    cohort = pooled_cohort(account_ids)
    rows = []
    moved = dry_run = skipped_pooled = forbidden = not_connected = errors = 0
    connected = bool(dataaccess._ADAPTERS and _src.HUBSPOT.live())
    for acc in cohort["accounts"]:
        aid = acc["account_id"]
        if acc["already_pooled"]:
            skipped_pooled += 1
            rows.append({"account_id": aid, "name": acc["name"], "mode": "already-pooled"})
            continue
        if not can_write_account(aid):
            forbidden += 1
            rows.append({"account_id": aid, "name": acc["name"], "mode": "forbidden"})
            continue
        if not connected:
            not_connected += 1
            rows.append({"account_id": aid, "name": acc["name"], "mode": "not-connected"})
            continue
        try:
            res = _src.HUBSPOT.set_customer_tier(aid, tier="Pooled",
                                                 clear_owner=clear_owner,
                                                 pooled_team=pooled_team, apply=apply)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            rows.append({"account_id": aid, "name": acc["name"], "mode": "error", "error": str(exc)})
            continue
        mode = res.get("mode")
        if mode == "applied":
            moved += 1
        elif mode == "dry-run":
            dry_run += 1
        rows.append({"account_id": aid, "name": acc["name"], "mode": mode,
                     "cleared_owner": res.get("cleared_owner")})
    return {
        "batch_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "cohort": cohort["cohort"],
        "apply_requested": bool(apply),
        "clear_owner": bool(clear_owner),
        "pooled_team": (pooled_team or None),
        "write_enabled": bool(os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on")),
        "summary": {
            "in_cohort": cohort["count"],
            "total_arr_usd": cohort["total_arr_usd"],
            "moved": moved,
            "dry_run": dry_run,
            "already_pooled": skipped_pooled,
            "forbidden": forbidden,
            "not_connected": not_connected,
            "errors": errors,
        },
        "accounts": rows,
    }


# --- Weekly time-blocked operating rhythm (WoW §3, UC2) ----------------------
# Groups the live prioritised task queue into the WoW operating blocks so a CSM sees the
# week the way the Ways-of-Working framework prescribes, instead of one flat list:
#   Monday Analytics & Portfolio Review , the week's review (worst-health + all open tasks count)
#   Daily P1 Defensive Risk (Must Protect), ML churn / usage drop / Sev-1 (priority 1-2 PROTECT)
#   Weekly Renewals & Expansion (Must Expand), T-cadence + expansion triggers
#   Weekly Adoption & QBR (Must Use), adoption / onboarding
# Built from engine.portfolio() tasks (rule_id/mandate/priority), owner-scoped already.
def operating_rhythm() -> dict:
    p = portfolio()
    tasks = p.get("tasks", [])
    blocks = {
        "monday_review": {"title": "Monday · Analytics & Portfolio Review",
                          "focus": "Review health shifts, open risk playbooks, and the week's priorities.",
                          "cadence": "Monday morning", "tasks": []},
        "daily_p1_risk": {"title": "Daily · Defensive Risk (Must Protect)",
                          "focus": "ML churn >70%, sudden usage drops, Sev-1 incidents. Acknowledge within the 24h SLA.",
                          "cadence": "Daily (Priority 1)", "tasks": []},
        "weekly_renewals_expansion": {"title": "Weekly · Renewals & Expansion (Must Expand)",
                          "focus": "T-120/90/60/30 renewal cadence and expansion triggers.",
                          "cadence": "Dedicated weekly blocks", "tasks": []},
        "weekly_adoption_qbr": {"title": "Weekly · Adoption & QBR (Must Use)",
                          "focus": "Drive sticky adoption, unblock onboarding, prepare QBRs.",
                          "cadence": "Dedicated weekly blocks", "tasks": []},
    }
    protect_rules = {orchestrate.RULE_PREDICTIVE_RISK, orchestrate.RULE_CHURNED_RECOVERY,
                     orchestrate.RULE_SCALED_EXCEPTION, orchestrate.RULE_DAY15_PAYMENT,
                     orchestrate.RULE_OVERDUE_RENEWAL}
    expand_rules = {orchestrate.RULE_EXPANSION_UTILIZATION, orchestrate.RULE_EXPANSION_API_SURGE,
                    orchestrate.RULE_EXPANSION_ADOPTION, orchestrate.RULE_RENEWAL_CADENCE}
    use_rules = {orchestrate.RULE_ADOPTION_INTERVENTION, orchestrate.RULE_ONBOARDING_STAGNATION,
                 orchestrate.RULE_CONTACT_HYGIENE}
    for t in tasks:
        rid = t.get("rule_id")
        mandate = t.get("mandate")
        pri = t.get("priority")
        row = {"account_id": t.get("account_id"), "account": t.get("account"),
               "priority": pri, "mandate": mandate, "rule_id": rid,
               "trigger": t.get("trigger"), "due_on": t.get("due_on"), "segment": t.get("segment")}
        if rid in protect_rules or mandate == "MUST_PROTECT" or pri == 1:
            blocks["daily_p1_risk"]["tasks"].append(row)
        elif rid in expand_rules or mandate == "MUST_EXPAND":
            blocks["weekly_renewals_expansion"]["tasks"].append(row)
        elif rid in use_rules or mandate == "MUST_USE":
            blocks["weekly_adoption_qbr"]["tasks"].append(row)
        else:
            blocks["weekly_adoption_qbr"]["tasks"].append(row)
    # Monday review is a summary block (not a 4th copy of tasks): the week at a glance.
    accts = p.get("accounts", [])
    worst = sorted((a for a in accts if a.get("health", {}).get("computable")),
                   key=lambda a: a["health"]["score"])[:10]
    blocks["monday_review"]["summary"] = {
        "open_tasks": len(tasks),
        "p1_count": sum(1 for t in tasks if t.get("priority") == 1),
        "worst_health": [{"account_id": a["account_id"], "name": a.get("name"),
                          "score": a["health"]["score"], "band": a["health"]["band"]} for a in worst],
    }
    for b in blocks.values():
        if "tasks" in b:
            b["tasks"].sort(key=lambda r: (r["priority"] or 9))
            b["count"] = len(b["tasks"])
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "blocks": blocks}


# --- CSM availability / presence feed (Tech-Touch round-robin, WoW §3) -------
# Real-time Available/OOO status for pooled CSMs. Backed by an in-process store that the
# platform updates via POST /api/csm/availability (the "Help Desk presence" source the
# spec calls for). Honest fallback: a CSM with no explicit status is treated as available,
# so round-robin still distributes before anyone sets presence. A status can carry an
# optional 'until' epoch (auto-expire back to available) and a short note (e.g. "OOO").
_CSM_PRESENCE: dict[str, dict] = {}


def set_csm_availability(name: str, available: bool, until: int | None = None,
                         note: str | None = None) -> dict:
    """Set a pooled CSM's presence. Returns the updated entry. Owner/admin gated at the
    endpoint. available=False marks OOO so round-robin skips them and assigned-but-now-
    unavailable items flag for reassignment."""
    name = (name or "").strip()
    if not name:
        raise ValueError("csm name is required")
    entry = {"name": name, "available": bool(available),
             "until": int(until) if until else None,
             "note": (note or "").strip() or None,
             "updated_at": datetime.now(timezone.utc).isoformat()}
    _CSM_PRESENCE[name] = entry
    return entry


def _presence_for(name: str) -> bool:
    """Resolve current availability for a CSM, honouring an expired 'until' (back to
    available) and defaulting to available when no status has ever been set."""
    e = _CSM_PRESENCE.get(name)
    if not e:
        return True
    if not e["available"] and e.get("until"):
        try:
            if datetime.now(timezone.utc).timestamp() >= float(e["until"]):
                return True  # OOO window elapsed
        except (TypeError, ValueError):
            pass
    return bool(e["available"])


def pooled_roster(with_availability: bool = True) -> dict:
    """The pooled CSM roster (distinct owners of pooled accounts) with live availability,
    so the inbound round-robin and the governance view share one source of truth."""
    try:
        owners = sorted({
            (a.get("csm_owner") or "").strip()
            for a in portfolio().get("accounts", [])
            if (a.get("pooled") or a.get("cohort") == "pooled") and a.get("csm_owner")
        })
    except Exception:  # noqa: BLE001
        owners = []
    roster = []
    for o in owners:
        if not o:
            continue
        row = {"name": o, "available": _presence_for(o) if with_availability else True}
        e = _CSM_PRESENCE.get(o)
        if e:
            row["note"] = e.get("note")
            row["until"] = e.get("until")
        roster.append(row)
    return {"roster": roster,
            "available": [r["name"] for r in roster if r["available"]],
            "unavailable": [r["name"] for r in roster if not r["available"]],
            "presence_source": "live" if _CSM_PRESENCE else "default-all-available"}


# --- Inbound queue store (Option A, Tech-Touch UC1) --------------------------
# Triaged inbound tickets from /api/inbound/hubspot are persisted here so the pooled
# team has a live, actionable queue (not just a one-shot triage response). In-process
# store keyed by ticket id; newest first. A durable store (DynamoDB/DB) can replace this
# without changing the intake/triage contract. Resolving a ticket (reply/close via the
# Zendesk writes, or marking handled) sets its status.
_INBOUND_QUEUE: dict[str, dict] = {}


def record_inbound(tickets: list[dict]) -> int:
    """Persist triaged tickets into the inbound queue (upsert by id). Returns how many
    were stored/updated. Status defaults to 'open'; existing status is preserved."""
    n = 0
    for t in tickets or []:
        tid = t.get("id")
        if not tid:
            continue
        prev = _INBOUND_QUEUE.get(tid, {})
        row = dict(t)
        row["status"] = prev.get("status", "open")
        row["recorded_at"] = prev.get("recorded_at") or datetime.now(timezone.utc).isoformat()
        _INBOUND_QUEUE[tid] = row
        n += 1
    return n


def inbound_queue(status: str | None = None) -> dict:
    """The persisted inbound queue (newest first), optionally filtered by status, with a
    summary by destination/intent/SLA so the pooled team can work it like an inbox.

    AGEING ON READ: an open ticket's sla_breached and needs_reassign flags are
    recomputed each read against the CURRENT time and live CSM presence, not frozen at
    intake. This satisfies UC1's '20h -> auto-reassign' and the 24h SLA: a ticket that
    crosses the thresholds while sitting in the queue, or whose owner later goes OOO,
    now surfaces correctly without needing a fresh intake batch."""
    import time as _t
    now = int(_t.time())
    SLA_H, REASSIGN_H = 24, 20  # mirror inbound.SLA_HOURS / REASSIGN_AFTER_HOURS
    for r in _INBOUND_QUEUE.values():
        if r.get("status") != "open":
            continue
        received = r.get("received_at")
        if not isinstance(received, (int, float)):
            continue
        age_h = max(0, (now - received) / 3600.0)
        sla_due = r.get("sla_due") or (received + SLA_H * 3600)
        r["sla_breached"] = now > sla_due
        owner = r.get("assigned_to")
        owner_ooo = bool(owner) and not _presence_for(owner)
        r["needs_reassign"] = (owner is not None) and (not r["sla_breached"]) and \
            (age_h >= REASSIGN_H or owner_ooo)
    rows = sorted(_INBOUND_QUEUE.values(),
                  key=lambda r: r.get("recorded_at") or "", reverse=True)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    by_dest: dict[str, int] = {}
    by_intent: dict[str, int] = {}
    breached = open_count = reassign = 0
    for r in rows:
        by_dest[r.get("destination")] = by_dest.get(r.get("destination"), 0) + 1
        by_intent[r.get("intent")] = by_intent.get(r.get("intent"), 0) + 1
        if r.get("status") == "open":
            open_count += 1
        if r.get("sla_breached"):
            breached += 1
        if r.get("needs_reassign"):
            reassign += 1
    return {"count": len(rows), "open": open_count, "sla_breached": breached,
            "needs_reassign": reassign,
            "by_destination": by_dest, "by_intent": by_intent, "tickets": rows}


def resolve_inbound(ticket_id: str, status: str = "resolved") -> dict:
    """Mark an inbound ticket resolved/handled. Returns the updated row or raises KeyError."""
    status = (status or "resolved").strip().lower()
    if status not in ("open", "resolved", "handed_off", "closed"):
        raise ValueError(f"invalid status {status!r}")
    row = _INBOUND_QUEUE.get(ticket_id)
    if not row:
        raise KeyError(ticket_id)
    row["status"] = status
    row["resolved_at"] = datetime.now(timezone.utc).isoformat()
    return row



def _ticket_account_ref(ticket_id: str) -> str | None:
    """Resolve the account a Zendesk ticket belongs to, via the inbound queue row the
    pooled team works from. Returns the normalised account id/ref, or None when the
    ticket is not in the queue (no known account linkage)."""
    row = _INBOUND_QUEUE.get(ticket_id) or {}
    ref = row.get("account_ref")
    return str(ref).strip() if ref else None


def _authorise_ticket_write(ticket_id: str, action: str) -> None:
    """Owner-scope a Zendesk write to the account the ticket belongs to.

    Admins (or open/legacy mode with no principal) may act on any ticket. A scoped CSM
    may only reply to / close tickets on accounts they own. A ticket with no resolvable
    account (not in the inbound queue) is denied for a scoped CSM, fail closed, so a
    CSM cannot act on an arbitrary ticket_id outside their book. Raises ForbiddenError
    (-> 403) when not permitted.
    """
    p = get_principal()
    if not p or p.get("role") == "admin":
        return
    account_ref = _ticket_account_ref(ticket_id)
    if account_ref and can_write_account(account_ref):
        return
    raise ForbiddenError(
        f"You can only {action} tickets on accounts you own"
        + ("" if account_ref else " (this ticket is not linked to an account in your queue)")
    )


def zendesk_reply(ticket_id: str, body: str, public: bool = True, apply: bool = False) -> dict:
    """Reply to a Zendesk ticket (public or internal) from the inbound queue. Two-gate;
    dry-run by default. Owner-scoped: a CSM may only reply to tickets on accounts they
    own (admins any). Resolving inbound in-platform (increment 3)."""
    _authorise_ticket_write(ticket_id, "reply to")
    if not (dataaccess._ADAPTERS and _src.ZENDESK.live()):
        result = {"replied": False, "mode": "not-connected"}
    else:
        result = _src.ZENDESK.reply_ticket(ticket_id, body, public=public, apply=apply)
    return {
        "audit_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "ticket_id": ticket_id,
        "apply_requested": bool(apply),
        "write_enabled": bool(os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on")),
        "result": result,
    }


def zendesk_set_status(ticket_id: str, status: str = "solved", comment: str | None = None,
                       apply: bool = False) -> dict:
    """Set a Zendesk ticket's status (close = 'solved') from the inbound queue. Two-gate;
    dry-run by default; reversible (solved can be reopened). Owner-scoped: a CSM may only
    change the status of tickets on accounts they own (admins any)."""
    _authorise_ticket_write(ticket_id, "change the status of")
    if not (dataaccess._ADAPTERS and _src.ZENDESK.live()):
        result = {"updated": False, "mode": "not-connected"}
    else:
        result = _src.ZENDESK.set_ticket_status(ticket_id, status=status, comment=comment, apply=apply)
    return {
        "audit_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "ticket_id": ticket_id,
        "apply_requested": bool(apply),
        "write_enabled": bool(os.environ.get("CS_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on")),
        "result": result,
    }


def _retention_metrics(accounts: dict, tasks_by_account: dict) -> dict:
    """Portfolio revenue retention, computed transparently from LIVE signals only.

    GRR (Gross Revenue Retention) is the headline retention metric and is computed
    from real churn: (base_arr - churned_arr) / base_arr, capped at 100% (no upside
    counted). It tracks the CS brief's GRR >= 92% goal.

    NDR (Net Revenue Retention) is REALISED net revenue retention computed from the
    warehouse monthly ARR series (`rpt_account_ndr_monthly`): dollar-weighted
    current-period revenue (`mrr_usd`) over the same accounts' prior-year revenue
    (`revenue_prev_year_usd`). This captures expansion, contraction, and churn on the
    existing base, the board metric, NOT unrealised pipeline. Only accounts that have
    BOTH a current and a prior figure contribute, so the number is never inflated by
    opportunity. `ndr_pct` is an honest None (and `ndr_computable` False) when the
    warehouse has no prior-period revenue for any in-scope account, so the UI shows
    "no data" rather than a misleading percentage. It tracks the CS brief's NDR > 100% goal.

    Expansion PIPELINE is reported SEPARATELY from retention (never folded into NDR):
    the ARR of healthy accounts carrying an active expansion trigger (license
    utilization / API surge / strong adoption). This is opportunity, not revenue,
    labelled as such.

    Definitions (annualised, book-level):
      base_arr          = sum of live contract ARR across the book
      churned_arr       = ARR of accounts flagged churned (Redshift status or HubSpot
                          lifecycle), revenue lost
      grr_pct           = (base_arr - churned_arr) / base_arr, capped at 100%
      ndr_pct           = sum(current revenue) / sum(prior-year revenue) over accounts
                          with both figures, realised net retention, or None if none
      expansion_pipeline_arr = ARR of healthy accounts with an active expansion trigger
                          (pipeline/opportunity, NOT booked expansion)

    `computable` is False when no live ARR is available, so the UI shows "no data"
    rather than a misleading 0%.
    """
    base_arr = 0
    churned_arr = 0
    expansion_pipeline_arr = 0
    expansion_accounts = 0
    # NDR from the warehouse monthly ARR series (rpt_account_ndr_monthly): dollar-weighted
    # current-period revenue vs the same accounts' prior-year revenue. This is REALISED
    # net revenue retention (expansion - contraction - churn on the existing base), the
    # board metric, not pipeline. Only accounts with BOTH figures contribute.
    ndr_current = 0.0
    ndr_prior = 0.0
    ndr_accounts = 0
    for aid, a in accounts.items():
        live = _live_account(a)
        hs = live.get("hubspot", {})
        m = live.get("metrics", {}) or {}
        try:
            cur, prior = m.get("revenue_prev_year_usd"), None
            # metrics() exposes current revenue as mrr_usd and prior as revenue_prev_year_usd.
            cur = m.get("mrr_usd"); prior = m.get("revenue_prev_year_usd")
            if cur not in (None, "") and prior not in (None, "") and float(prior) > 0:
                ndr_current += float(cur)
                ndr_prior += float(prior)
                ndr_accounts += 1
        except (TypeError, ValueError):
            pass
        arr = hs.get("arr_usd") or 0
        if not arr:
            continue
        base_arr += arr
        churn = live.get("churn", {})
        churned = str(churn.get("churn_status") or "").lower() == "churned" \
            or str(hs.get("lifecycle_stage") or "").lower() in {"churned", "churned customer"}
        if churned:
            churned_arr += arr
            continue  # a churned account is not an expansion opportunity
        acct_tasks = tasks_by_account.get(aid, [])
        if any(t.get("rule_id") in {
            orchestrate.RULE_EXPANSION_UTILIZATION,
            orchestrate.RULE_EXPANSION_API_SURGE,
            orchestrate.RULE_EXPANSION_ADOPTION,
        } for t in acct_tasks):
            expansion_pipeline_arr += arr
            expansion_accounts += 1

    ndr_pct = round(100.0 * ndr_current / ndr_prior, 1) if ndr_prior > 0 else None

    if not base_arr:
        return {"computable": False, "grr_pct": None,
                "ndr_pct": None, "ndr_computable": False, "ndr_accounts": 0,
                "base_arr_usd": 0, "churned_arr_usd": 0,
                "expansion_pipeline_arr_usd": 0, "expansion_pipeline_accounts": 0,
                "target": {"grr_pct": 92, "ndr_pct": 100},
                "note": "No live contract ARR available; retention is not computable."}

    grr = round(100 * (base_arr - churned_arr) / base_arr, 1)
    return {
        "computable": True,
        "grr_pct": grr,
        # NDR (realised net revenue retention) from the warehouse monthly ARR series:
        # current-period revenue vs the same accounts' prior-year revenue, dollar-weighted.
        # Honest None when the warehouse has no prior-period data for any account.
        "ndr_pct": ndr_pct,
        "ndr_computable": ndr_pct is not None,
        "ndr_accounts": ndr_accounts,
        "base_arr_usd": base_arr,
        "churned_arr_usd": churned_arr,
        # Expansion PIPELINE (opportunity), reported separately from retention.
        "expansion_pipeline_arr_usd": expansion_pipeline_arr,
        "expansion_pipeline_accounts": expansion_accounts,
        "target": {"grr_pct": 92, "ndr_pct": 100},
        "method": "live ARR; GRR from Redshift/HubSpot churned status. NDR from the "
                  "rpt_account_ndr_monthly warehouse series (current vs prior-year revenue, "
                  "dollar-weighted). Expansion pipeline = active expansion-trigger accounts "
                  "(opportunity, separate from retention).",
    }


def kpis() -> dict:
    """Leadership KPI & capacity tracking (req §3): per-CSM portfolio allocation,
    task load, at-risk ARR, and health mix, to inform headcount/resourcing."""
    accounts = orchestrate.load_accounts()
    result = orchestrate.orchestrate()
    task_metrics = _task_metrics(result["tasks"])
    tasks_by_account = {}
    for t in result["tasks"]:
        tasks_by_account.setdefault(t.get("account_id"), []).append(t)
    status_by_id = task_metrics["status_by_id"]
    active_tasks = [t for t in result["tasks"]
                    if status_by_id.get(t.get("task_id"), "open") != "completed"]

    by_csm: dict[str, dict] = {}
    mandate_counts = {"MUST_PROTECT": 0, "MUST_EXPAND": 0, "MUST_USE": 0}
    for t in active_tasks:
        mandate_counts[t["mandate"]] = mandate_counts.get(t["mandate"], 0) + 1

    for aid, a in accounts.items():
        live = _live_account(a)
        hs = live.get("hubspot", {})              # {} unless HubSpot live
        csm = hs.get("csm_owner") or "Unassigned"
        h = health_score(live)
        tasks = tasks_by_account.get(aid, [])      # join by stable account_id
        active_account_tasks = [task for task in tasks
                    if status_by_id.get(task.get("task_id"), "open") != "completed"]
        rec = by_csm.setdefault(csm, {
            "csm": csm, "accounts": 0, "arr_usd": 0, "open_tasks": 0,
            "priority1_tasks": 0, "at_risk_accounts": 0, "completed_tasks": 0,
            "overdue_tasks": 0,
        })
        rec["accounts"] += 1
        rec["arr_usd"] += hs.get("arr_usd", 0) or 0   # only live ARR contributes
        rec["open_tasks"] += len(active_account_tasks)
        rec["priority1_tasks"] += sum(1 for t in active_account_tasks if t["priority"] == 1)
        rec["completed_tasks"] += sum(1 for t in tasks if task_metrics["status_by_id"].get(t.get("task_id")) == "completed")
        rec["overdue_tasks"] += sum(1 for t in active_account_tasks if t.get("task_id") in task_metrics["overdue_task_ids"])
        if h["computable"] and h["band"] == "red":
            rec["at_risk_accounts"] += 1

    capacity = _capacity_per_csm()
    for rec in by_csm.values():
        rec["capacity_utilization_pct"] = round(100 * rec["open_tasks"] / capacity, 1) if capacity else 0

    return {
        "by_csm": sorted(by_csm.values(), key=lambda r: -r["open_tasks"]),
        "mandate_load": mandate_counts,
        "retention": _retention_metrics(_retention_accounts(), tasks_by_account),
        "task_metrics": {key: (sorted(value) if key == "overdue_task_ids" else value)
                 for key, value in task_metrics.items() if key != "status_by_id"},
        "totals": {
            "csms": len([c for c in by_csm if c not in ("Pooled", "Unassigned")]),
            "open_tasks": task_metrics["open_tasks"],
            "priority1_tasks": sum(1 for t in active_tasks if t["priority"] == 1),
        },
    }


def _csm_targets() -> dict:
    """Per-CSM weekly targets for the leaderboard, as a {csm_name: {outreach, completion_pct}}
    map. Sourced from the CS_CSM_TARGETS env JSON (operator-configured), e.g.
    '{"Jane Doe": {"outreach": 15, "completion_pct": 90}}'. When unset or a CSM has no
    target, that CSM's target is a data-gap (None), never a fabricated number."""
    raw = os.environ.get("CS_CSM_TARGETS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def leaderboard() -> dict:
    """Gamified team-performance leaderboard (V5 UC1/UC3). Ranks pooled/named CSMs on
    real, already-computed KPI data (reuses kpis().by_csm, single source, no drift):
    week-ending completion rate, open load, overdue, priority-1 load, and proactive
    outreach vs an operator-set target. Surfaces a weekly target-compliance KPI
    (target >= 90%).

    Owner-scoped and privacy-aware: an admin sees every CSM named; a scoped CSM sees
    their own row named with real numbers and peers ANONYMISED ("CSM 2", ...) so the
    ranking is visible without exposing a colleague's book. Honest: a CSM with no target
    shows target/compliance as a data-gap, not 0.
    """
    k = kpis()
    rows = [r for r in k.get("by_csm", []) if r.get("csm") not in ("Unassigned",)]
    targets = _csm_targets()

    p = get_principal()
    is_admin = (not p) or p.get("role") == "admin"
    me = (p or {}).get("name") or (p or {}).get("email")

    board = []
    compliant = measurable = 0
    for r in rows:
        csm = r.get("csm")
        completed = r.get("completed_tasks", 0)
        open_t = r.get("open_tasks", 0)
        denom = completed + open_t
        completion_pct = round(100 * completed / denom) if denom else None
        tgt = targets.get(csm) or {}
        outreach_target = tgt.get("outreach")
        completion_target = tgt.get("completion_pct", 90)  # default weekly target
        # Compliance measurable only where we have a completion rate.
        meets = None
        if completion_pct is not None:
            measurable += 1
            meets = completion_pct >= (completion_target or 90)
            if meets:
                compliant += 1
        board.append({
            "csm": csm,
            "accounts": r.get("accounts", 0),
            "arr_usd": r.get("arr_usd", 0),
            "open_tasks": open_t,
            "completed_tasks": completed,
            "overdue_tasks": r.get("overdue_tasks", 0),
            "priority1_tasks": r.get("priority1_tasks", 0),
            "capacity_utilization_pct": r.get("capacity_utilization_pct", 0),
            "completion_rate_pct": completion_pct,          # week-ending completion
            "outreach_target": outreach_target,              # None => data-gap (target not set)
            "completion_target_pct": completion_target,
            "meets_target": meets,                           # None when not measurable
        })

    # Rank by completion rate desc (None last), then fewest overdue, then most completed.
    # Ranking basis: completion ranking is only meaningful once CSMs have actually
    # COMPLETED tasks in-platform. A 0% rate derived purely from open tasks is not
    # completion data. So fall back to a WORKLOAD ranking (most open+overdue first) until
    # at least one task has been completed anywhere, so the board is useful day one.
    any_completion = any((b["completed_tasks"] or 0) > 0 for b in board)
    if any_completion:
        ranking_basis = "completion"
        board.sort(key=lambda b: (
            -(b["completion_rate_pct"] if b["completion_rate_pct"] is not None else -1),
            b["overdue_tasks"],
            -b["completed_tasks"],
        ))
    else:
        ranking_basis = "workload"
        board.sort(key=lambda b: (
            -(b["open_tasks"] + b["overdue_tasks"]),
            -b["overdue_tasks"],
            -(b["accounts"] or 0),
        ))
    for i, b in enumerate(board, 1):
        b["rank"] = i

    # Privacy projection for a scoped CSM: keep own row named, anonymise peers.
    if not is_admin:
        anon = 0
        for b in board:
            if b["csm"] != me:
                anon += 1
                b["csm"] = f"CSM {b['rank']}"
                # Hide peer book size/ARR; keep rank + completion for the ladder.
                b["accounts"] = None
                b["arr_usd"] = None

    weekly_target_compliance_pct = round(100 * compliant / measurable) if measurable else None
    return {
        "leaderboard": board,
        "ranking_basis": ranking_basis,
        "weekly_target_compliance_pct": weekly_target_compliance_pct,
        "weekly_target_compliance_target_pct": 90,
        "measurable_csms": measurable,
        "targets_configured": bool(targets),
        "note": (None if targets else
                 "Per-CSM outreach targets are not configured (set CS_CSM_TARGETS); "
                 "completion-rate ranking is shown and compliance uses the default 90% target."),
    }


def _task_events_path() -> Path:
    return Path(os.environ.get("CS_TASK_EVENTS_FILE", str(Path(__file__).resolve().parents[1] / ".cs-task-events.jsonl")))


def _load_task_events() -> dict[str, dict]:
    latest: dict[str, dict] = {}
    path = _task_events_path()
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("task_id"):
            latest[event["task_id"]] = event
    return latest


def _capacity_per_csm() -> int:
    try:
        return max(1, int(os.environ.get("CS_TASK_CAPACITY_PER_CSM", "20")))
    except ValueError:
        return 20


def _task_metrics(tasks: list[dict]) -> dict:
    events = _load_task_events()
    # Use the SAME business-date anchor (CS_TODAY) the tasks' created_on/due_on were
    # stamped with, so overdue counts, SLA adherence and task age never skew when the
    # platform runs on an anchored date. (_f2f_today honours CS_TODAY, else date.today.)
    today = _f2f_today()
    completed = in_progress = overdue = 0
    ages = []
    overdue_ids = set()
    on_time = late = 0
    for task in tasks:
        task_id = task.get("task_id")
        event = events.get(task_id, {})
        status = event.get("status", "open")
        if status == "completed":
            completed += 1
            completed_at = str(event.get("completed_at") or event.get("updated_at") or today.isoformat())[:10]
            if task.get("due_on") and completed_at <= task["due_on"]:
                on_time += 1
            else:
                late += 1
        elif status == "in_progress":
            in_progress += 1
        created = task.get("created_on")
        if created:
            try:
                ages.append(max(0, (today - date.fromisoformat(created)).days))
            except ValueError:
                pass
        if status != "completed" and task.get("due_on") and today.isoformat() > task["due_on"]:
            overdue += 1
            if task_id:
                overdue_ids.add(task_id)
    return {
        "completed_tasks": completed,
        "in_progress_tasks": in_progress,
        "open_tasks": len(tasks) - completed,
        "overdue_tasks": overdue,
        "average_task_age_days": round(sum(ages) / len(ages), 1) if ages else 0,
        "sla_adherence_pct": round(100 * on_time / (on_time + late), 1) if on_time + late else None,
        "status_by_id": {task_id: event.get("status", "open") for task_id, event in events.items()},
        "overdue_task_ids": overdue_ids,
    }


_AU_STATES = {
    "NSW": "NSW", "NEW SOUTH WALES": "NSW",
    "VIC": "VIC", "VICTORIA": "VIC",
    "QLD": "QLD", "QUEENSLAND": "QLD",
    "WA": "WA", "WESTERN AUSTRALIA": "WA",
    "SA": "SA", "SOUTH AUSTRALIA": "SA",
    "TAS": "TAS", "TASMANIA": "TAS",
    "ACT": "ACT", "AUSTRALIAN CAPITAL TERRITORY": "ACT",
    "NT": "NT", "NORTHERN TERRITORY": "NT",
}


def _normalise_state(raw):
    """Map a HubSpot state value to a canonical AU state code. Non-AU (or blank) values
    are grouped as 'International' / 'Unknown' so the ARR-by-state view stays clean."""
    if not raw or not str(raw).strip():
        return "Unknown"
    key = str(raw).strip().upper()
    if key in _AU_STATES:
        return _AU_STATES[key]
    return "International"


def revenue_motion() -> dict:
    """Executive revenue-motion snapshot for the CS dashboard (Chartio CS-dashboard model:
    paying customers, churn, at-risk, retention, ARR distribution). Live data only; every
    figure traces to HubSpot ARR / lifecycle and Redshift churn status. Nothing invented."""
    accounts = orchestrate.load_accounts()
    scoped = [_live_account(a) for a in accounts.values()]
    tasks_by_account: dict = {}
    for t in orchestrate.orchestrate().get("tasks", []):
        tasks_by_account.setdefault(t.get("account_id"), []).append(t)
    # Retention (NDR/GRR) is a whole-book figure: compute it over the owner-scoped whole
    # book + warehouse metrics, not just the enriched slice, so a CSM sees real NDR for
    # their actual book (the warehouse carries it) instead of "no data".
    retention_accounts = _retention_accounts()
    _retention = _retention_metrics(retention_accounts, tasks_by_account)

    def _is_churned(la):
        stage = str(la.get("hubspot", {}).get("lifecycle_stage") or "").lower()
        status = str(la.get("churn", {}).get("churn_status") or "").lower()
        return stage in {"churned", "churned customer"} or status == "churned"

    def _arr(la):
        v = la.get("hubspot", {}).get("arr_usd")
        return v if isinstance(v, (int, float)) else 0

    paying = [la for la in scoped if not _is_churned(la) and la.get("hubspot")]
    churned = [la for la in scoped if _is_churned(la)]
    churned_detail = sorted(
        [{"name": la.get("hubspot", {}).get("name") or "Unnamed",
          "account_id": la.get("account_id") or la.get("hubspot", {}).get("account_id"),
          "owner": la.get("hubspot", {}).get("csm_owner") or "Unassigned",
          "segment": la.get("hubspot", {}).get("segment_label") or la.get("hubspot", {}).get("segment") or "Unsegmented",
          "arr_usd": _arr(la),
          "lifecycle_stage": la.get("hubspot", {}).get("lifecycle_stage") or "unknown",
          "health": health_score(la).get("score"),
          "renewal_date": la.get("hubspot", {}).get("renewal_date"),
          "days_since_visit": (la.get("usage") or {}).get("days_since_last_visit")}
         for la in churned],
        key=lambda r: -r["arr_usd"])
    bands = {"green": 0, "amber": 0, "red": 0}
    at_risk_arr = 0
    for la in paying:
        b = health_score(la).get("band")
        if b in bands:
            bands[b] += 1
        if b in ("red", "amber"):
            at_risk_arr += _arr(la)

    by_segment: dict[str, dict] = {}
    by_state: dict[str, dict] = {}
    for la in scoped:
        hs = la.get("hubspot", {})
        seg = hs.get("segment_label") or hs.get("segment") or "Unsegmented"
        row = by_segment.setdefault(seg, {"segment": seg, "accounts": 0, "arr_usd": 0})
        row["accounts"] += 1
        row["arr_usd"] += _arr(la)
        state = _normalise_state(hs.get("state"))
        srow = by_state.setdefault(state, {"state": state, "accounts": 0, "arr_usd": 0})
        srow["accounts"] += 1
        srow["arr_usd"] += _arr(la)

    # Booked revenue motion from HubSpot deals (real upsell + churn events). Falls back
    # to an honest empty state if deals are unreadable. Guarded so a deals failure never
    # breaks the dashboard.
    booked = {}
    try:
        from adapters.sources import HubSpot as _HS
        hs_adapter = _HS()
        if hs_adapter.live():
            booked = hs_adapter.revenue_motion_deals() or {}
    except Exception:  # noqa: BLE001
        booked = {}

    return {
        "paying_customers": len(paying),
        "churned_customers": len(churned),
        "total_accounts": len(scoped),
        "total_arr_usd": sum(_arr(la) for la in scoped),
        "paying_arr_usd": sum(_arr(la) for la in paying),
        "churned_arr_usd": sum(_arr(la) for la in churned),
        "at_risk_arr_usd": at_risk_arr,
        "health_mix": bands,
        "retention": _retention,
        "by_segment": sorted(by_segment.values(), key=lambda r: -r["arr_usd"]),
        "by_state": sorted([s for s in by_state.values() if s["arr_usd"] > 0], key=lambda r: -r["arr_usd"]),
        "churned_detail": churned_detail,
        "booked": booked,
        # Expansion / upsell / downgrade motion. Only the expansion PIPELINE is currently
        # computable (accounts hitting an expansion trigger, opportunity not booked). Booked
        # upsell/downgrade requires ARR-change history (prior-period ARR or HubSpot deal /
        # Stripe subscription-change events), which is not connected. We never invent these.
        "revenue_change": {
            "expansion_pipeline_accounts": (_retention or {}).get("expansion_pipeline_accounts", 0),
            "expansion_pipeline_arr_usd": (_retention or {}).get("expansion_pipeline_arr_usd", 0),
            "upsell_computable": bool(booked.get("upsell")),
            "upsell_count": (booked.get("upsell") or {}).get("count"),
            "upsell_arr_usd": (booked.get("upsell") or {}).get("arr_usd"),
            "upsell_partial": (booked.get("upsell") or {}).get("arr_is_partial"),
            "downgrade_computable": bool(booked.get("churn")),
            "churn_count": (booked.get("churn") or {}).get("count"),
            "churn_deal_arr_usd": (booked.get("churn") or {}).get("arr_usd"),
            "churn_partial": (booked.get("churn") or {}).get("arr_is_partial"),
            "window_days": booked.get("window_days"),
            "needs": "booked ARR-change history (prior-period ARR, HubSpot deal stages, or Stripe subscription changes)",
        },
    }


def expansion_opportunities() -> dict:
    """Ranked expansion candidates using the computed expansion-readiness score, scoped to
    the caller (admins see all; CSMs see their own book via the scoped account provider).
    Evidence-grounded: only accounts with a computable score and real drivers appear."""
    accounts = orchestrate.load_accounts()
    scoped = [_live_account(a) for a in accounts.values()]
    # Segment median ARR for the headroom driver.
    from statistics import median
    seg_arr: dict[str, list] = {}
    for la in scoped:
        hs = la.get("hubspot", {})
        seg = hs.get("segment_label") or hs.get("segment") or "Unsegmented"
        v = hs.get("arr_usd")
        if isinstance(v, (int, float)):
            seg_arr.setdefault(seg, []).append(v)
    seg_median = {s: median(v) for s, v in seg_arr.items() if v}

    candidates = []
    for la in scoped:
        hs = la.get("hubspot", {})
        seg = hs.get("segment_label") or hs.get("segment") or "Unsegmented"
        es = expansion_score(la, segment_median_arr=seg_median.get(seg))
        if es.get("computable") and es.get("score", 0) >= 35 and es.get("drivers"):
            candidates.append({
                "account_id": la.get("account_id") or hs.get("account_id"),
                "name": hs.get("name") or "Unnamed",
                "segment": seg,
                "owner": hs.get("csm_owner") or "Unassigned",
                "arr_usd": hs.get("arr_usd"),
                "score": es["score"],
                "band": es["band"],
                "drivers": es["drivers"],
            })
    candidates.sort(key=lambda c: -c["score"])
    return {
        "candidates": candidates,
        "count": len(candidates),
        "method": "Expansion-readiness scoring across the live book, weighing account health, "
                  "product engagement, licence utilisation, API growth, renewal timing and "
                  "revenue headroom to surface the strongest upsell opportunities first.",
    }


def lifecycle_state(account: dict) -> dict:
    """Derive the customer lifecycle stage as a state machine (Customer 360 §6) from live
    signals, rather than a manual field. Stages: Implementation, Onboarding, Adoption,
    Value Realisation, Mature, plus the reverse path At Risk / Recovery. Returns the stage
    and a short reason so it is explainable and evidence-driven."""
    hs = account.get("hubspot", {}) or {}
    ob = account.get("onboarding", {}) or {}
    churn = account.get("churn", {}) or {}
    stage_raw = str(hs.get("lifecycle_stage") or "").lower()
    health = health_score(account)
    band = health.get("band")

    # Terminal / reverse states first.
    if stage_raw in {"churned", "churned customer"} or str(churn.get("churn_status") or "").lower() == "churned":
        return {"stage": "Churned", "reason": "HubSpot lifecycle marks this account churned.", "flow": "exited"}
    if band == "red":
        return {"stage": "At Risk", "reason": "Health is red; the account has moved backwards and needs recovery.", "flow": "reverse"}

    # Onboarding / implementation when explicit onboarding signal says so.
    ob_status = str(ob.get("status") or "").lower()
    if ob_status in {"implementation", "implementing", "kickoff"}:
        return {"stage": "Implementation", "reason": "Onboarding status is implementation.", "flow": "forward"}
    if ob_status in {"onboarding", "in_progress", "training"} or (ob.get("time_to_value_days") is None and ob_status):
        return {"stage": "Onboarding", "reason": "Onboarding is in progress.", "flow": "forward"}

    # Forward maturity from adoption + health.
    ad = adoption_score(account)
    ascore = ad.get("score") if ad.get("computable") else None
    if ascore is not None:
        if ascore >= 70 and band == "green":
            return {"stage": "Value Realisation", "reason": f"Strong adoption ({ascore}) and healthy relationship.", "flow": "forward"}
        if ascore >= 40:
            return {"stage": "Adoption", "reason": f"Building adoption ({ascore}); engagement is growing.", "flow": "forward"}
        return {"stage": "Onboarding", "reason": f"Low adoption ({ascore}); still ramping.", "flow": "forward"}
    # No adoption signal: infer from health.
    if band == "green":
        return {"stage": "Mature", "reason": "Healthy, established customer.", "flow": "forward"}
    return {"stage": "Adoption", "reason": "Established but still building steady usage.", "flow": "forward"}


def lifecycle() -> dict:
    """Unified lifecycle view (req §3): onboarding velocity/health alongside adoption."""
    accounts = orchestrate.load_accounts()
    rows = []
    for aid, a in accounts.items():
        live = _live_account(a)
        hs = live.get("hubspot", {})
        ob = live.get("onboarding", {})   # {} always: no live onboarding adapter
        usage = live.get("usage", {})     # adoption fields stripped: Pendo can't supply them
        rows.append({
            "account_id": aid,
            "name": hs.get("name"),
            "segment": hs.get("segment_label") or hs.get("segment"),
            "onboarding_status": ob.get("status"),               # None -> not connected
            "time_to_value_days": ob.get("time_to_value_days"),  # None -> not connected
            "onboarding_health": ob.get("health"),
            "key_feature_adoption_pct": usage.get("key_feature_adoption_pct"),  # None
            "active_users_pct": usage.get("active_users_pct"),                   # None
            "days_since_last_visit": usage.get("days_since_last_visit"),         # real Pendo signal
            "connected": _connected(a),
        })
    return {"accounts": rows}


def integrations() -> dict:
    """Integration status map (req §1) for the single pane of glass. Reflects which
    connectors feed the platform, their sync direction, and what they contribute.
    Fixture mode reports 'connected (sample)'; swap adapters for live to flip to 'live'."""
    accounts = orchestrate.load_accounts()
    liveset = set(dataaccess.live_sources())
    # Tableau is not a per-account data source; it is "connected (live)" when the
    # embedding connected app is configured (server url + client id + secret id/value).
    try:
        import tableau as _tableau
        if _tableau.is_configured():
            liveset.add("Tableau")
    except Exception:  # noqa: BLE001
        pass
    # ROI AI is "live" once the inbound webhook signing secret is configured (the vendor
    # can then post signed telemetry). Mirrors the Tableau config-driven liveness check.
    if roi_ai_configured():
        liveset.add("ROI AI")
    # Precise, honest status. Three distinct states instead of a vague "not connected":
    #   connected (live)     , the source is authenticated and returning data
    #   configuration required, we have access but a specific env value is missing
    #   access pending       , JobAdder does not have API access to this vendor yet
    import os as _os
    def _missing(*names):
        return [n for n in names if not (_os.environ.get(n) or "").strip()]

    # Jiminny and Rocket Lane access IS granted and their keys are live (verified against
    # both vendor APIs: Rocket Lane returns projects, Jiminny returns call activities on the
    # EU host). They are reported via live_sources() like every other keyed source, so they
    # resolve to "connected (live)" when live and "configured, awaiting data" otherwise -
    # no vendor access is pending. ACCESS_PENDING is kept only for genuinely-pending vendors.
    ACCESS_PENDING: set = set()
    CONFIG_REQS = {
        "Zendesk": ("ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_TOKEN"),
        "Churn Model": ("REDSHIFT_DATABASE", "REDSHIFT_CHURN_TABLE"),
        "Entitlements": ("ENTITLEMENTS_API_URL", "ENTITLEMENTS_KEY"),
        "ROI AI": ("ROI_AI_WEBHOOK_SECRET",),
        "Jiminny": ("JIMINNY_KEY",),
        "Rocket Lane": ("ROCKET_LANE_KEY",),
    }
    def status_for(name):
        if name in liveset:
            return {"status": "connected (live)", "state": "live", "detail": None}
        if name in ACCESS_PENDING:
            return {"status": "access pending", "state": "pending",
                    "detail": "JobAdder does not have API access to this vendor yet. "
                              "The connector is built and will light up once access is granted."}
        if name in CONFIG_REQS:
            missing = _missing(*CONFIG_REQS[name])
            if missing:
                return {"status": "configuration required", "state": "config",
                        "detail": "Set " + ", ".join(missing) + " to activate this live source."}
            # Configured but not returning data: credentials present, call failing/empty.
            return {"status": "configured, awaiting data", "state": "config",
                    "detail": "Credentials are set but no live records returned yet."}
        return {"status": "not connected", "state": "off", "detail": None}
    def st(name):
        return status_for(name)["status"]
    hs_live = "HubSpot" in liveset
    writes_on = str(_os.environ.get("CS_ALLOW_WRITE", "")).lower() in ("1", "true", "yes", "on")
    hs_pushes = (["health score", "risk status", "active playbook",
                  "call/meeting notes", "contact roles (Sponsor/Champion/Finance)"]
                 if hs_live else [])
    return {
        "writes_enabled": writes_on,
        "connectors": [
            {"system": "HubSpot", "category": "CRM",
             "direction": "bi-directional (write)" if (hs_live and writes_on) else "bi-directional",
             **status_for("HubSpot"),
             "pulls": ["contract value", "renewal date", "account hierarchy", "contacts"],
             "pushes": hs_pushes,
             "writes": (["CS write-back", "sequence enrolment", "notes", "contact roles", "expansion deals (CSQL)"]
                        if (hs_live and writes_on) else [])},
            {"system": "Stripe", "category": "Billing / Finance", "direction": "read-only",
             **status_for("Stripe"),
             "pulls": ["invoice status", "days past due", "ARR"], "pushes": []},
            {"system": "Zendesk", "category": "Support",
             "direction": "read + write" if (("Zendesk" in liveset) and writes_on) else "read-only",
             **status_for("Zendesk"),
             "pulls": ["ticket volume", "CSAT", "Sev-1 flags"],
             "pushes": [],
             "writes": (["reply to ticket", "close/transfer ticket"]
                        if (("Zendesk" in liveset) and writes_on) else [])},
            {"system": "Product Telemetry", "category": "Usage (Pendo)", "direction": "read-only",
             **status_for("Pendo"),
             "pulls": ["risk advisor", "adoption", "days since last visit", "plan tier"], "pushes": []},
            {"system": "Jiminny", "category": "Conversational Intelligence", "direction": "read-only",
             **status_for("Jiminny"),
             "pulls": ["last call", "sentiment", "summary", "customer talk ratio"], "pushes": []},
            {"system": "Rocket Lane", "category": "Onboarding", "direction": "read-only",
             **status_for("Rocket Lane"),
             "pulls": ["onboarding status", "time to value", "onboarding health"], "pushes": []},
            {"system": "Churn Model (Redshift)", "category": "Data Platform · Redshift Data API",
             "direction": "read-only", "access": "read-only",
             **status_for("Churn Model"),
             "pulls": ["ML churn score", "model version", "top risk drivers"], "pushes": []},
            {"system": "Entitlements", "category": "Licensing / Billing",
             "direction": "read-only", "access": "read-only",
             **status_for("Entitlements"),
             "pulls": ["licensed seats", "active seats", "license utilization %"], "pushes": []},
            {"system": "ROI AI", "category": "Product Telemetry · webhook",
             "direction": "inbound webhook (signed)", "access": "read-only",
             **status_for("ROI AI"),
             "pulls": ["ROI AI adoption score", "active ROI users", "ROI realized", "trend"],
             "pushes": []},
            {"system": "Tableau", "category": "Analytics / Reporting", "direction": "embed (SSO)",
             "access": "read-only", **status_for("Tableau"),
             "pulls": ["embedded dashboards (Revenue, NDR, Billing, Stripe)"],
             "pushes": ["signed-in CSM identity (Connected App JWT)"]},
        ]
    }


def ingestion_status() -> dict:
    """Data-ingestion health for the whole platform: which sources are live, how fresh the
    warehouse metrics are, and how much of the book has been deeply enriched vs is still
    the lightweight roster. Powers the dashboard freshness banner so the team can see at a
    glance what's ready to use now vs still filling in (answers 'when is it fully
    ingested'). Live/warehouse-grounded only; never fabricated."""
    import time
    try:
        live = set(dataaccess.live_sources())
    except Exception:  # noqa: BLE001
        live = set()
    core = ["HubSpot", "Stripe", "Zendesk", "Pendo", "Churn Model"]
    sources = [{"system": s, "live": s in live} for s in core]

    # Warehouse metrics freshness + coverage (NDR / licence utilisation).
    bm = _BATCH_METRICS.get("data") or {}
    bm_at = _BATCH_METRICS.get("at") or 0
    metrics_age_s = (time.time() - bm_at) if bm_at else None
    metrics_fresh = bool(bm) and metrics_age_s is not None and metrics_age_s < _report_cache_ttl()

    # Enrichment coverage: deeply-enriched slice vs the whole active book.
    try:
        enriched = len(dataaccess.all_accounts())
    except Exception:  # noqa: BLE001
        enriched = 0
    book_total = 0
    try:
        if _src.HUBSPOT.live():
            for c in _src.HUBSPOT.list_all_companies(cached_only=True):
                if "churn" not in str(c.get("lifecycle_stage") or "").lower():
                    book_total += 1
    except Exception:  # noqa: BLE001
        book_total = 0

    core_live = all(s["live"] for s in sources if s["system"] in ("HubSpot",))
    degraded = [s["system"] for s in sources if not s["live"]]
    # Overall state: ready (core live + metrics fresh), warming (metrics loading), or
    # degraded (a core source is down).
    if not core_live:
        state = "degraded"
    elif not metrics_fresh:
        state = "warming"
    else:
        state = "ready"

    return {
        "state": state,
        "sources": sources,
        "degraded": degraded,
        "warehouse_metrics": {
            "accounts": len(bm),
            "fresh": metrics_fresh,
            "age_seconds": int(metrics_age_s) if metrics_age_s is not None else None,
        },
        "enrichment": {
            "enriched_accounts": enriched,
            "book_total_accounts": book_total,
            "pct": (round(100 * enriched / book_total) if book_total else None),
        },
    }


def book_readiness(full: bool = False) -> dict:
    """Per-CSM 'your book readiness' summary: for the CURRENT principal's owned accounts,
    how complete is the data that drives the platform? Reports the count + ARR of accounts
    missing each required HubSpot field (renewal date, owner, segment, subscription), an
    overall readiness %, and the top accounts to fix first (highest ARR with the most
    gaps). Owner-scoped and HubSpot-grounded (cheap roster scan, no per-account fan-out);
    honest - it reflects exactly what is set in HubSpot, nothing inferred.

    This gives each CSM a personal, actionable cleanup list and makes the 'shared effort'
    on data hygiene concrete rather than abstract.
    """
    p = get_principal()
    owner_id = p.get("owner_id") if (p and p.get("role") not in (None, "admin")) else None
    fields = [
        ("renewal_date", "Renewal date"),
        ("owner_id", "CSM owner"),
        ("segment", "Segment (ICP)"),
        ("subscription_type", "Subscription type"),
    ]
    rows = []
    try:
        if _src.HUBSPOT.live():
            for c in _src.HUBSPOT.list_all_companies(cached_only=True):
                if "churn" in str(c.get("lifecycle_stage") or "").lower():
                    continue  # active book only
                if owner_id and str(c.get("owner_id") or "") != str(owner_id):
                    continue  # this CSM's book only (admin sees all)
                rows.append(c)
    except Exception:  # noqa: BLE001
        rows = []

    total = len(rows)
    field_missing = {label: {"count": 0, "arr_usd": 0.0} for _, label in fields}
    per_account = []
    def _hs_url(cid):
        try:
            return _src.HUBSPOT.company_url(cid) if cid else None
        except Exception:  # noqa: BLE001
            return None
    for c in rows:
        arr = c.get("arr_usd") or 0
        missing = []
        for key, label in fields:
            if c.get(key) in (None, ""):
                missing.append(label)
                field_missing[label]["count"] += 1
                field_missing[label]["arr_usd"] += arr if isinstance(arr, (int, float)) else 0
        if missing:
            per_account.append({
                "account_id": c.get("account_id") or ("rl-" + str(c.get("company_id"))),
                "name": c.get("name"),
                "arr_usd": arr if isinstance(arr, (int, float)) else 0,
                "missing": missing,
                "missing_count": len(missing),
                "hubspot_url": _hs_url(c.get("company_id")),
            })

    # Overall readiness = fraction of (account x required-field) cells that are populated.
    cells = total * len(fields)
    filled = cells - sum(v["count"] for v in field_missing.values())
    readiness_pct = round(100 * filled / cells) if cells else None

    # Top accounts to fix first: most gaps, then highest ARR.
    per_account.sort(key=lambda r: (-r["missing_count"], -(r["arr_usd"] or 0)))
    fully_complete = total - len(per_account)

    return {
        "scope": ("csm" if owner_id else "all"),
        "total_accounts": total,
        "fully_complete": fully_complete,
        "accounts_with_gaps": len(per_account),
        "readiness_pct": readiness_pct,
        "field_gaps": [
            {"field": label, "missing": field_missing[label]["count"],
             "arr_at_risk_usd": round(field_missing[label]["arr_usd"])}
            for _, label in fields
        ],
        "fix_first": per_account if full else per_account[:15],
    }


def readiness_leaderboard() -> dict:
    """Team-wide data-readiness roll-up for leadership: for EVERY CSM, how complete is the
    required HubSpot data on their book. Admin-only in effect (a CSM would only see their
    own owner id resolve); groups the whole active book by owner, resolves owner ids to CSM
    names, and reports per-CSM readiness % + the biggest gap and ARR at risk. Lets
    leadership target the data-hygiene effort where it moves the needle most. HubSpot-
    grounded (cheap roster scan); honest - reflects exactly what is set, nothing inferred.
    """
    fields = [
        ("renewal_date", "Renewal date"),
        ("owner_id", "CSM owner"),
        ("segment", "Segment (ICP)"),
        ("subscription_type", "Subscription type"),
    ]
    try:
        roster = _src.HUBSPOT.list_all_companies(cached_only=True) if _src.HUBSPOT.live() else []
    except Exception:  # noqa: BLE001
        roster = []

    # Resolve the distinct owner ids to CSM names once (cached, ~15-20 CSMs).
    owner_names: dict = {}
    try:
        for oid in {str(c.get("owner_id")) for c in roster if c.get("owner_id")}:
            owner_names[oid] = _src.HUBSPOT._owner_name(oid)
    except Exception:  # noqa: BLE001
        owner_names = {}

    by_owner: dict = {}
    for c in roster:
        if "churn" in str(c.get("lifecycle_stage") or "").lower():
            continue
        oid = str(c.get("owner_id") or "")
        name = owner_names.get(oid) or ("Unassigned" if not oid else oid)
        rec = by_owner.setdefault(name, {
            "csm": name, "accounts": 0, "fully_complete": 0, "cells_filled": 0,
            "cells_total": 0, "field_missing": {label: 0 for _, label in fields},
            "arr_at_risk_usd": 0.0,
        })
        rec["accounts"] += 1
        arr = c.get("arr_usd") or 0
        arr = arr if isinstance(arr, (int, float)) else 0
        missing_here = 0
        for key, label in fields:
            rec["cells_total"] += 1
            if c.get(key) in (None, ""):
                rec["field_missing"][label] += 1
                missing_here += 1
            else:
                rec["cells_filled"] += 1
        if missing_here == 0:
            rec["fully_complete"] += 1
        else:
            rec["arr_at_risk_usd"] += arr

    out = []
    for rec in by_owner.values():
        pct = round(100 * rec["cells_filled"] / rec["cells_total"]) if rec["cells_total"] else None
        # Biggest single gap (field with the most missing) to name the top action.
        top_field, top_missing = None, 0
        for label, cnt in rec["field_missing"].items():
            if cnt > top_missing:
                top_field, top_missing = label, cnt
        out.append({
            "csm": rec["csm"],
            "accounts": rec["accounts"],
            "fully_complete": rec["fully_complete"],
            "readiness_pct": pct,
            "accounts_with_gaps": rec["accounts"] - rec["fully_complete"],
            "top_gap_field": top_field,
            "top_gap_missing": top_missing,
            "arr_at_risk_usd": round(rec["arr_at_risk_usd"]),
        })
    # Worst readiness first (most to gain), then by account volume.
    out.sort(key=lambda r: (r["readiness_pct"] if r["readiness_pct"] is not None else 101,
                            -r["accounts"]))

    total_accts = sum(r["accounts"] for r in out)
    total_complete = sum(r["fully_complete"] for r in out)
    return {
        "csms": out,
        "summary": {
            "csm_count": len([r for r in out if r["csm"] not in ("Unassigned",)]),
            "total_accounts": total_accts,
            "fully_complete": total_complete,
            "overall_readiness_pct": (round(100 * total_complete / total_accts)
                                      if total_accts else None),
        },
    }


def datagaps() -> dict:
    """Data Gap Analysis (Requirements §4). For each account, report which source
    systems have it (coverage) and which required CS fields are empty in HubSpot,
    so RevOps can see exactly what cleanup is needed. Uses only live data."""
    accounts = orchestrate.load_accounts()
    # Required CS metadata per WoW §3/§5.
    HS_FIELDS = [
        ("segment_label", "Segment (ICP)"),
        ("arr_usd", "ARR"),
        ("renewal_date", "Renewal date"),
        ("csm_owner", "CSM owner"),
        ("subscription_type", "Subscription type"),
    ]
    ROLES = ["Executive Sponsor", "Primary Champion / Admin", "Finance Contact"]

    rows = []
    totals = {"zendesk": 0, "stripe": 0, "usage": 0, "hubspot": 0}
    field_missing = {label: 0 for _, label in HS_FIELDS}
    roles_missing_accounts = 0

    # --- Enriched slice: full per-account source coverage + role tagging. -------------
    # These accounts have been through deep enrichment so we genuinely know whether each
    # vendor returned a record and which contact roles are tagged.
    enriched_ids = set()
    for aid, a in accounts.items():
        enriched_ids.add(aid)
        src = a.get("sources", {})
        hs = a.get("hubspot", {}) if _is_live(src, "hubspot") else {}
        coverage = {
            "hubspot": _is_live(src, "hubspot"),
            "zendesk": _is_live(src, "zendesk"),
            "stripe": _is_live(src, "stripe"),
            "usage": _is_live(src, "usage"),
        }
        coverage_status = {}
        for key in coverage:
            source_state = src.get(key, "not_live")
            coverage_status[key] = (
                "live" if source_state in ("live", "computed")
                else "no_record" if source_state == "live_no_record"
                else "not_connected"
            )
        for k, v in coverage.items():
            if v and src.get(k) not in ("live_no_record", "live_unavailable"):
                totals[k] += 1
        missing_fields = []
        for key, label in HS_FIELDS:
            if hs.get(key) in (None, ""):
                missing_fields.append(label)
                field_missing[label] += 1
        have_roles = {c.get("role") for c in hs.get("contacts", [])}
        missing_roles = [r for r in ROLES if r not in have_roles]
        if missing_roles:
            roles_missing_accounts += 1
        # Missing source systems (account not found in that vendor).
        missing_systems = [s.title() if s != "usage" else "Pendo"
                   for s in ("zendesk", "stripe", "usage")
                   if src.get(s) not in ("live", "computed")]
        rows.append({
            "account_id": aid,
            "name": hs.get("name") or aid,
            "hubspot_url": hs.get("hubspot_url"),
            "coverage": coverage,
            "coverage_status": coverage_status,
            "missing_systems": missing_systems,
            "missing_fields": missing_fields,
            "missing_roles": missing_roles,
            "gap_count": len(missing_systems) + len(missing_fields) + (1 if missing_roles else 0),
            "enriched": True,
        })

    # --- Whole-book roster rows: HubSpot required-field gaps for EVERY active customer. -
    # The five required HubSpot fields come straight from the cheap whole-book roster scan,
    # so we report them honestly for all 4,300+ active customers (not just the enriched 50).
    # Source-system coverage and contact roles are marked "not_checked" for these rows -
    # the connectors are live but we have NOT pulled a per-account record, which is distinct
    # from "no record". We never claim a vendor has/lacks an account we did not check.
    book_roster_field_map = [
        ("segment", "Segment (ICP)"),
        ("arr_usd", "ARR"),
        ("renewal_date", "Renewal date"),
        ("owner_id", "CSM owner"),
        ("subscription_type", "Subscription type"),
    ]
    book_total = 0
    def _hs_company_url(cid):
        try:
            return _src.HUBSPOT.company_url(cid) if cid else None
        except Exception:  # noqa: BLE001
            return None
    try:
        if _src.HUBSPOT.live():
            _roster = _src.HUBSPOT.list_all_companies(cached_only=True)
            for c in _roster:
                lc = str(c.get("lifecycle_stage") or "").lower()
                if "churn" in lc:
                    continue  # active book only, consistent with the coverage matrix
                aid = c.get("account_id") or ("rl-" + str(c.get("company_id")))
                if aid in enriched_ids:
                    continue  # already represented with full coverage above
                book_total += 1
                roster_missing = []
                for key, label in book_roster_field_map:
                    if c.get(key) in (None, ""):
                        roster_missing.append(label)
                        field_missing[label] += 1
                rows.append({
                    "account_id": aid,
                    "name": c.get("name") or aid,
                    "hubspot_url": _hs_company_url(c.get("company_id")),
                    "coverage": {"hubspot": True, "zendesk": False, "stripe": False, "usage": False},
                    # not_checked: connector live but no per-account lookup done for this row.
                    "coverage_status": {"hubspot": "live", "zendesk": "not_checked",
                                        "stripe": "not_checked", "usage": "not_checked"},
                    "missing_systems": [],      # unknown, not fabricated
                    "missing_fields": roster_missing,
                    "missing_roles": [],        # unknown until enriched
                    "gap_count": len(roster_missing),
                    "enriched": False,
                })
    except Exception:  # noqa: BLE001
        book_total = book_total

    rows.sort(key=lambda r: -r["gap_count"])  # worst-coverage first
    n = len(rows) or 1
    book_total_all = len(enriched_ids) + book_total

    return {
        "accounts": rows,
        "summary": {
            "total_accounts": len(rows),
            # coverage_pct is over the enriched slice only (the only accounts for which we
            # genuinely checked each vendor); the UI labels it accordingly.
            "coverage_pct": {k: round(100 * v / (len(enriched_ids) or 1)) for k, v in totals.items()},
            "not_in_zendesk": len(enriched_ids) - totals["zendesk"],
            "not_in_stripe": len(enriched_ids) - totals["stripe"],
            "not_in_pendo": len(enriched_ids) - totals["usage"],
            "hubspot_field_gaps": field_missing,     # now whole-book
            "accounts_missing_roles": roles_missing_accounts,
            "enriched_accounts": len(enriched_ids),
            "book_total_accounts": book_total_all,
        },
    }


def admin_coverage_matrix() -> dict:
    """Admin Coverage Matrix (UC3 governance): the book-level roll-up of required contact
    role coverage. For each of the three WoW roles (Executive Sponsor, Primary Champion /
    Admin, Finance Contact) report how many active accounts have it tagged, the coverage %,
    and the ARR sitting at risk because the role is missing. Owner-scoped. Live HubSpot only.
    Complements datagaps() (which is per-account) with the leadership-level matrix the spec
    names."""
    accounts = _scoped_accounts()
    roles = list(REQUIRED_CONTACT_ROLES)
    per_role = {r: {"role": r, "covered": 0, "missing": 0, "arr_at_risk_usd": 0.0} for r in roles}
    total = 0
    fully_covered = 0
    arr_total = 0.0
    for aid, a in accounts.items():
        if not _is_live(a.get("sources", {}), "hubspot"):
            continue
        hs = a.get("hubspot", {}) or {}
        # Active book only: a churned account's missing roles are not actionable coverage.
        lc = str(hs.get("lifecycle_stage") or "").lower()
        if lc in ("churned", "churned customer"):
            continue
        total += 1
        arr = hs.get("arr_usd") or 0
        arr_total += arr
        have = {c.get("role") for c in hs.get("contacts", []) if c.get("role")}
        missing_here = 0
        for r in roles:
            if r in have:
                per_role[r]["covered"] += 1
            else:
                per_role[r]["missing"] += 1
                per_role[r]["arr_at_risk_usd"] += arr
                missing_here += 1
        if missing_here == 0:
            fully_covered += 1
    n = total or 1
    matrix = []
    for r in roles:
        pr = per_role[r]
        pr["coverage_pct"] = round(100 * pr["covered"] / n)
        pr["arr_at_risk_usd"] = round(pr["arr_at_risk_usd"])
        matrix.append(pr)
    return {
        "roles": matrix,
        "summary": {
            "active_accounts": total,
            "fully_covered": fully_covered,
            "fully_covered_pct": round(100 * fully_covered / n),
            "total_arr_usd": round(arr_total),
        },
    }


def _admin_url(account_ref: str) -> str | None:
    """Derive the JobAdder admin account URL from the account ref, e.g.
    au5-402271 -> https://au5admin.jobadder.com/accounts/402271. Returns None when the
    ref does not parse to a shard+tenant."""
    from adapters import identity as _identity
    p = _identity.parse(account_ref or "")
    if not p:
        return None
    return f"https://{p['shard']}admin.jobadder.com/accounts/{p['tenant']}"


def _billing_contact(hs: dict, stripe: dict) -> str | None:
    """Billing contact for the Payment Risk Report: the HubSpot Finance Contact if tagged,
    otherwise the Stripe customer email, otherwise any first contact's email. Live only."""
    contacts = hs.get("contacts", []) or []
    for c in contacts:
        if c.get("role") == "Finance Contact":
            return c.get("email") or c.get("name")
    if stripe.get("customer_email"):
        return stripe["customer_email"]
    for c in contacts:
        if c.get("email"):
            return c.get("email")
    return None


def _payment_thresholds() -> dict:
    """Access-suspension and cancellation day thresholds (days past due). Configurable so
    the report matches JobAdder's real dunning schedule without a code change. Defaults are
    conservative placeholders; set CS_ACCESS_SUSPEND_DAYS / CS_CANCEL_DAYS to the real
    values."""
    def _int(name, default):
        try:
            return int(os.environ.get(name, "").strip() or default)
        except ValueError:
            return default
    return {
        "access_suspend_days": _int("CS_ACCESS_SUSPEND_DAYS", 21),
        "cancel_days": _int("CS_CANCEL_DAYS", 30),
    }


def payment_risk_report() -> dict:
    """Cached wrapper (short TTL, per scope) over the live Payment Risk build so the page
    doesn't re-query Stripe on every load."""
    return _cached_report("payment_risk_report", _payment_risk_report_build)


def _payment_risk_report_build() -> dict:
    """Payment Risk Report (live). Buckets the owner-scoped book into pages driven by live
    Stripe dunning (days past due) plus configurable access/cancellation thresholds:

      - payment_failed:    an open invoice is past due now (dunning_stage != none).
      - access_risk_7d:    days past due is within 7 days of the access-suspension threshold.
      - access_risk_14d:   within 14 days (and not already in the 7d bucket).
      - cancellation_risk: days past due is within 14 days of the cancellation threshold.

    Each row carries the fields the manual report tracks, all from live sources: customer
    name, JobAdder id, billing contact (Finance Contact or Stripe email), CSM owner, the
    Stripe customer dashboard link, and the derived JobAdder admin link. Honest empty when
    Stripe is not connected, no data is fabricated."""
    th = _payment_thresholds()
    suspend_at, cancel_at = th["access_suspend_days"], th["cancel_days"]
    stripe_live = "Stripe" in set(dataaccess.live_sources())

    # Source failed/past-due payments DIRECTLY from Stripe account-wide, so the report is
    # complete regardless of how the whole-book roster was warmed. Then join to the account
    # roster (HubSpot) for the billing contact + CSM, and respect the principal's scope.
    scoped = _scoped_accounts()
    p = get_principal()
    admin_scope = (not p) or p.get("role") == "admin"
    owner_id = (p or {}).get("owner_id")

    problems = []
    source_error = None
    owner_map = {}
    if stripe_live:
        try:
            problems = _src.STRIPE.list_payment_problems()
        except Exception as exc:  # noqa: BLE001
            source_error = f"{type(exc).__name__}: {exc}"
            import sys as _sys
            print(f"[payment-risk] list_payment_problems failed: {source_error}", file=_sys.stderr)
            problems = []
    # CSM owner map for exactly the payment-problem accounts (account_ref -> owner name),
    # resolved directly from HubSpot by account id so it works for accounts in any lifecycle
    # stage (not just the customer roster). Batched + cached, so it is a handful of calls.
    if problems:
        try:
            owner_map = _src.HUBSPOT.owners_for_refs([p.get("account_ref") for p in problems])
        except Exception:  # noqa: BLE001
            owner_map = {}

    def _norm(ref):
        from adapters import identity as _id
        return _id.normalise(ref)

    def _row(prob: dict) -> dict:
        ref = _norm(prob.get("account_ref"))
        a = scoped.get(ref) or {}
        hs = a.get("hubspot", {}) or {}
        stripe_for_contact = {"customer_email": prob.get("customer_email")}
        return {
            "account_id": ref,
            "name": hs.get("name") or prob.get("account_ref"),
            "billing_contact": _billing_contact(hs, stripe_for_contact),
            "csm_owner": hs.get("csm_owner") or owner_map.get(ref) or None,
            "days_past_due": prob.get("days_past_due"),
            "amount_due_usd": prob.get("amount_due_usd"),
            "dunning_stage": prob.get("dunning_stage"),
            "failed_attempts": prob.get("failed_attempts"),
            "past_due_invoices": prob.get("past_due_invoices"),
            "stripe_url": (f"https://dashboard.stripe.com/customers/{prob['customer_id']}"
                           if prob.get("customer_id") else None),
            "admin_url": _admin_url(prob.get("account_ref")),
        }

    pages = {"payment_failed": [], "access_risk_7d": [], "access_risk_14d": [],
             "cancellation_risk_7d": [], "cancellation_risk_14d": []}
    for prob in problems:
        ref = _norm(prob.get("account_ref"))
        # Owner scope: a CSM only sees payment problems on accounts they own. Admin sees all.
        if not admin_scope:
            a = scoped.get(ref)
            if a is None or not _owns(a, owner_id):
                continue
        dd = prob.get("days_past_due") or 0
        row = _row(prob)
        pages["payment_failed"].append(row)
        days_to_suspend = suspend_at - dd
        if 0 <= days_to_suspend <= 7:
            pages["access_risk_7d"].append(row)
        elif 7 < days_to_suspend <= 14:
            pages["access_risk_14d"].append(row)
        days_to_cancel = cancel_at - dd
        if 0 <= days_to_cancel <= 7:
            pages["cancellation_risk_7d"].append(row)
        elif 7 < days_to_cancel <= 14:
            pages["cancellation_risk_14d"].append(row)

    for key in pages:
        pages[key].sort(key=lambda r: -(r.get("days_past_due") or 0))

    return {
        "stripe_connected": stripe_live,
        "thresholds": th,
        "pages": pages,
        "counts": {k: len(v) for k, v in pages.items()},
        "source_error": source_error,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(portfolio()["summary"], indent=2))
