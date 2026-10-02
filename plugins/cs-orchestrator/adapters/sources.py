#!/usr/bin/env python3
"""Live source adapters.

Each adapter:
  - `live()` returns True only when its credentials are present in the environment
    AND the master CS_USE_LIVE switch is on. Otherwise the caller uses fixtures.
  - its fetch method returns data in the SAME shape as the fixtures, so the rules
    engine, hooks, API, and UI need zero changes.
  - is keyed on the canonical AUx-yyyyy account ref (stored as an external id /
    metadata in each vendor).

Read-only everywhere except HubSpot, which supports the bi-directional write-back
(health score / risk status / active playbook) the requirements call for.

Nothing here contains a secret; all credentials come from config.env().
"""

from __future__ import annotations

import re
from typing import Any

from . import config, identity


# ------------------------------------------------------------------ Zendesk ---
class Zendesk:
    """Support signals -> {open_tickets, tickets_last_7d, tickets_prev_7d, sev1_open,
    csat_30d, by_instance}. Read-only (subdomain + email + token)."""

    def live(self) -> bool:
        return config.live_enabled() and bool(
            config.env("ZENDESK_SUBDOMAIN") and config.env("ZENDESK_EMAIL") and config.env("ZENDESK_TOKEN")
        )

    def _find_org(self, base: str, headers: dict, account_ref: str):
        """Resolve a Zendesk organization id for an AUx-yyyyy account.

        Order of attempts (JobAdder's Zendesk stores the id in a few ways):
          1) organization external_id (hyphen-uppercase, e.g. AU1-10094)
          2) the `workato_unique_id` custom field (confirmed live), trying both the
             hyphen-uppercase (AU1-10094) and underscore-lower (au1_10094) forms.
        Returns (org_id, org_name) or raises SourceError if none match.
        """
        import urllib.parse
        norm = identity.normalise(account_ref)
        hyphen_upper = norm.upper()
        underscore_lower = norm.replace("-", "_").lower()

        # 1) external_id
        orgs = config.http_get(
            f"{base}/organizations/search.json?external_id={urllib.parse.quote(hyphen_upper)}", headers)
        org_list = orgs.get("organizations", [])
        if org_list:
            return org_list[0]["id"], org_list[0].get("name")

        # 2) workato_unique_id custom field (working syntax: workato_unique_id:"VALUE")
        for val in (hyphen_upper, underscore_lower):
            q = f'type:organization workato_unique_id:"{val}"'
            res = config.http_get(f"{base}/search.json?query={urllib.parse.quote(q)}", headers)
            results = res.get("results", [])
            if results:
                return results[0]["id"], results[0].get("name")

        raise config.SourceError(
            f"Zendesk org not found for {hyphen_upper} (tried external_id and workato_unique_id)")

    def tickets(self, account_ref: str) -> dict[str, Any]:
        import urllib.parse
        from datetime import datetime, timedelta, timezone
        sub = config.env("ZENDESK_SUBDOMAIN")
        auth = config.basic_auth_header(f"{config.env('ZENDESK_EMAIL')}/token", config.env("ZENDESK_TOKEN"))
        headers = {"Authorization": auth, "Accept": "application/json"}
        base = f"https://{sub}.zendesk.com/api/v2"
        org_id, org_name = self._find_org(base, headers, account_ref)

        now = datetime.now(timezone.utc)
        d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        d14 = (now - timedelta(days=14)).strftime("%Y-%m-%d")
        d30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")

        def _count(query: str) -> int:
            r = config.http_get(f"{base}/search.json?query={urllib.parse.quote(query)}", headers)
            return int(r.get("count", len(r.get("results", []))))

        base_q = f"type:ticket organization:{org_id}"
        last7 = _count(f"{base_q} created>={d7}")
        prev7 = _count(f"{base_q} created>={d14} created<{d7}")
        open_tickets = _count(f"{base_q} status<solved")

        # CSAT over the trailing 30 days only (the field is `csat_30d`), from the
        # org's rated tickets: good / (good + bad). Bounding the window keeps the
        # score current instead of drifting toward an all-time average.
        rated = _count(f"{base_q} satisfaction:good created>={d30}")
        bad = _count(f"{base_q} satisfaction:bad created>={d30}")
        csat = round(100 * rated / (rated + bad)) if (rated + bad) else None

        return {
            "open_tickets": open_tickets,
            "tickets_last_7d": last7,
            "tickets_prev_7d": prev7,
            "sev1_open": _count(f"{base_q} status<solved tags:sev1"),
            "csat_30d": csat,
            "by_instance": {identity.normalise(account_ref): last7},
            "_source": "zendesk-live",
            "_org_name": org_name,
        }

    # ---- Writes (increment 3): reply to and close/transfer tickets so a CSM can resolve
    # inbound items in-platform. Two-gate: dry-run unless apply=true AND CS_ALLOW_WRITE=1.
    # Uses the same email/token auth; the token must have ticket write scope (otherwise
    # the live call 403s and we surface it honestly). Nothing is sent in dry-run.
    def _base_headers(self):
        auth = config.basic_auth_header(f"{config.env('ZENDESK_EMAIL')}/token", config.env("ZENDESK_TOKEN"))
        return f"https://{config.env('ZENDESK_SUBDOMAIN')}.zendesk.com/api/v2", \
               {"Authorization": auth, "Accept": "application/json", "Content-Type": "application/json"}

    def reply_ticket(self, ticket_id, body: str, public: bool = True,
                     apply: bool = False) -> dict[str, Any]:
        """Add a comment (public reply or internal note) to a Zendesk ticket."""
        body = (body or "").strip()
        if not ticket_id:
            raise ValueError("ticket_id is required")
        if not body:
            raise ValueError("reply body is required")
        if apply and config.writes_allowed():
            base, headers = self._base_headers()
            payload = {"ticket": {"comment": {"body": body, "public": bool(public)}}}
            config.http_patch(f"{base}/tickets/{ticket_id}.json", headers, payload)
            return {"replied": True, "mode": "applied", "target": "zendesk.tickets",
                    "ticket_id": ticket_id, "public": bool(public), "_source": "zendesk-live-write"}
        return {"replied": False, "mode": "dry-run", "target": "zendesk.tickets",
                "ticket_id": ticket_id, "would_write": {"comment": body, "public": bool(public)},
                "note": "No Zendesk mutation sent. Set CS_ALLOW_WRITE=1, request apply=true, and "
                        "use a token with ticket write scope.",
                "_source": "zendesk-live-readonly"}

    def set_ticket_status(self, ticket_id, status: str = "solved", comment: str | None = None,
                          apply: bool = False) -> dict[str, Any]:
        """Set a ticket's status (e.g. 'solved' to close, 'open' to reopen), optionally with
        a closing comment. Reversible (a solved ticket can be reopened)."""
        if not ticket_id:
            raise ValueError("ticket_id is required")
        status = (status or "").strip().lower()
        if status not in ("new", "open", "pending", "hold", "solved", "closed"):
            raise ValueError(f"invalid status {status!r}")
        if apply and config.writes_allowed():
            base, headers = self._base_headers()
            tk = {"status": status}
            if comment:
                tk["comment"] = {"body": comment, "public": False}
            config.http_patch(f"{base}/tickets/{ticket_id}.json", headers, {"ticket": tk})
            return {"updated": True, "mode": "applied", "target": "zendesk.tickets",
                    "ticket_id": ticket_id, "status": status, "_source": "zendesk-live-write"}
        return {"updated": False, "mode": "dry-run", "target": "zendesk.tickets",
                "ticket_id": ticket_id, "would_write": {"status": status},
                "note": "No Zendesk mutation sent. Set CS_ALLOW_WRITE=1, request apply=true, and "
                        "use a token with ticket write scope.",
                "_source": "zendesk-live-readonly"}


# -------------------------------------------------------------------- Pendo ---
class Pendo:
    """Product telemetry -> {logins_last_7d, logins_prev_7d, active_users_pct,
    license_utilization_pct, key_feature_adoption_pct, api_calls_*}. Read-only.

    Expansion metrics (utilization / API velocity / active users / feature adoption)
    are only returned when EXPLICITLY mapped to a Pendo metadata field via the
    corresponding PENDO_<METRIC>_KEY environment variable (see adapters/config.py).
    We deliberately do NOT guess field names: JobAdder's Pendo tenant exposes these
    under install-specific metadata keys, and inventing default paths risks either
    silently returning nothing or, worse, reading the wrong field. An unmapped metric
    stays None (a data gap), never fabricated. Days-since-last-visit and the Pendo
    Predict risk-advisor signals are read directly because their locations are known."""

    def live(self) -> bool:
        return config.live_enabled() and bool(config.env("PENDO_KEY"))

    def metrics(self, account_ref: str) -> dict[str, Any]:
        import time
        headers = {"x-pendo-integration-key": config.env("PENDO_KEY"), "content-type": "application/json"}
        # Pendo stores the account id in UNDERSCORE form (au1_12345), unlike Zendesk's
        # uppercase-hyphen external_id. Normalise then convert '-' -> '_'.
        ref = identity.normalise(account_ref).replace("-", "_")
        agg = config.http_get(f"https://app.pendo.io/api/v1/account/{ref}", headers)
        md = agg.get("metadata", {}) if isinstance(agg, dict) else {}
        agent = md.get("agent", {})
        auto = md.get("auto", {})
        predict = md.get("pendo_predict", {})

        def _find_value(value, key):
            if isinstance(value, dict):
                if key in value:
                    return value[key]
                for child in value.values():
                    found = _find_value(child, key)
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = _find_value(child, key)
                    if found is not None:
                        return found
            return None

        def configured_metric(name):
            configured_key = config.env(f"PENDO_{name.upper()}_KEY")
            if not configured_key:
                # No explicit mapping -> data gap. We do NOT guess vendor field names
                # or present absent telemetry as zero.
                return None
            # Explicit mapping: resolve the configured dotted path, falling back to a
            # nested key search. The mapping is authoritative and install-specific.
            current = md
            for part in configured_key.split("."):
                if not isinstance(current, dict) or part not in current:
                    current = None
                    break
                current = current[part]
            return current if current is not None else _find_value(md, configured_key)

        # Recency: days since last visit (from epoch-ms lastvisit).
        last_visit_ms = auto.get("lastvisit")
        days_since_visit = None
        if last_visit_ms:
            days_since_visit = int((time.time() * 1000 - last_visit_ms) / 86400000)

        # Behavioural activity velocity from the Pendo Aggregation API (read-only).
        # The account-metadata endpoint carries no usage counters, but the aggregation
        # API can count product events per account over a window. We use event volume
        # as a real ACTIVITY proxy for the API/usage-velocity expansion signal, split
        # into last-7d vs prior-7d. Explicit PENDO_*_KEY mappings (below) still win if
        # an install exposes true counters as metadata; otherwise these derived counts
        # feed api_calls_last_7d/prev_7d. On any failure they stay None (data gap).
        #
        # Opt-in: each account costs two extra aggregation calls, so this is gated by
        # CS_PENDO_ACTIVITY=1 to keep the default portfolio fan-out bounded. When off,
        # velocity relies solely on explicit metadata mappings (data gap if unmapped).
        if config.env("CS_PENDO_ACTIVITY") == "1":
            activity_last7, activity_prev7 = self._activity_velocity(ref, headers)
        else:
            activity_last7, activity_prev7 = None, None

        mapped_api_last = configured_metric("api_calls_last_7d")
        mapped_api_prev = configured_metric("api_calls_prev_7d")

        return {
            # Real usage recency (Pendo doesn't expose 7d login counts on this endpoint;
            # days-since-last-visit is the available real signal).
            "days_since_last_visit": days_since_visit,
            "logins_last_7d": configured_metric("logins_last_7d"),
            "logins_prev_7d": configured_metric("logins_prev_7d"),
            "active_users_pct": configured_metric("active_users_pct"),
            "license_utilization_pct": configured_metric("license_utilization_pct"),
            "key_feature_adoption_pct": configured_metric("key_feature_adoption_pct"),
            # Explicit metadata mapping wins; else the derived aggregation activity count.
            "api_calls_last_7d": mapped_api_last if mapped_api_last is not None else activity_last7,
            "api_calls_prev_7d": mapped_api_prev if mapped_api_prev is not None else activity_prev7,
            # Provenance: mark when the velocity numbers are the derived activity proxy
            # rather than a true API-call counter, so nothing is over-claimed.
            "api_velocity_source": ("pendo_metadata" if mapped_api_last is not None
                                    else "pendo_activity_events" if activity_last7 is not None
                                    else None),
            # Real Pendo Predict "JobAdder risk advisor" signals.
            "pendo_risk_score": predict.get("jobadder_risk_advisor___score"),
            "pendo_adoption": predict.get("jobadder_risk_advisor___adoption"),
            "pendo_adoption_engagement": predict.get("jobadder_risk_advisor___adoption_engagement"),
            "pendo_trend": predict.get("jobadder_risk_advisor___trend"),
            # Plan context.
            "plan_tier": agent.get("tier"),
            "plan_price": agent.get("planprice"),
            "site": agent.get("sitename"),
            "by_instance": {ref: {"days_since_last_visit": days_since_visit}},
            "_source": "pendo-live",
        }

    def _activity_velocity(self, ref: str, headers: dict) -> tuple[int | None, int | None]:
        """Per-account product-event counts for the last 7 days and the prior 7 days,
        via the Pendo Aggregation API. Returns (last7, prev7), or (None, None) if the
        aggregation is unavailable — never fabricated. This is an ACTIVITY proxy (event
        volume), not a literal API-call meter; provenance is recorded by the caller."""
        import time as _time
        day = 86400 * 1000
        now = int(_time.time() * 1000)

        def _count(first_ms: int) -> int | None:
            pipeline = [
                {"source": {"events": None,
                            "timeSeries": {"period": "dayRange", "first": first_ms, "count": 7}}},
                {"filter": f'accountId == "{ref}"'},
                {"group": {"group": [], "fields": [{"count": {"count": None}}]}},
            ]
            try:
                res = self._aggregation(pipeline, headers)
            except Exception:  # noqa: BLE001 - activity is best-effort; absence = data gap
                return None
            rows = res.get("results") if isinstance(res, dict) else None
            if not rows:
                return 0  # account resolved, simply no events in the window
            return int(rows[0].get("count", 0))

        last7 = _count(now - 7 * day)
        prev7 = _count(now - 14 * day)
        if last7 is None and prev7 is None:
            return None, None
        return last7, prev7

    @staticmethod
    def _aggregation(pipeline: list, headers: dict) -> dict:
        """Read-only POST to the Pendo Aggregation API. Routed through
        config.http_post_readonly so it shares the same 429 rate-limit retry/backoff
        as every other adapter call (important under the per-account fan-out when
        CS_PENDO_ACTIVITY is on). The endpoint is analytics-only — it never mutates."""
        body = {"response": {"mimeType": "application/json"},
                "request": {"pipeline": pipeline}}
        return config.http_post_readonly("https://app.pendo.io/api/v1/aggregation",
                                         headers, body, timeout=20)


# ----------------------------------------------------------- Rocket Lane ---
class RocketLane:
    """Onboarding source, verified against the Rocket Lane public API
    (developer.rocketlane.com). Base https://api.rocketlane.com/api, auth header
    `api-key: <key>`. Onboarding lives in PROJECTS; we match a JobAdder account to a
    Rocket Lane company by name, then read its latest project's status/health/dates.
    Only ROCKET_LANE_KEY is required (base URL defaults to the real host); nothing is
    fabricated when the key is absent or the account cannot be matched."""

    def _base(self) -> str:
        return (config.env("ROCKET_LANE_API_URL") or "https://api.rocketlane.com/api").rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"api-key": config.env("ROCKET_LANE_KEY"), "Accept": "application/json"}

    def live(self) -> bool:
        return config.live_enabled() and bool(config.env("ROCKET_LANE_KEY"))

    def status(self, account_ref: str, company_name: str | None = None) -> dict[str, Any]:
        """Onboarding status for an account. Matches a Rocket Lane company by name
        (company_name preferred; else the AUx-yyyy ref), then reads its newest project."""
        import urllib.parse
        base = self._base()
        headers = self._headers()
        name = company_name or identity.normalise(account_ref)
        try:
            # Find the company by name (native companyName filter).
            cq = urllib.parse.urlencode({"companyName.eq": name, "pageSize": 1})
            cres = config.http_get(f"{base}/1.0/companies?{cq}", headers)
            companies = cres.get("data") or [] if isinstance(cres, dict) else []
            if not companies:
                return {"status": None, "_source": "rocket-lane-live", "_matched": False}
            company_id = companies[0].get("companyId")
            # Newest project for that company.
            pq = urllib.parse.urlencode({"companyId.eq": company_id, "pageSize": 1, "sortBy": "createdAt", "sortOrder": "DESC"})
            pres = config.http_get(f"{base}/1.0/projects?{pq}", headers)
            projects = pres.get("data") or [] if isinstance(pres, dict) else []
            if not projects:
                return {"status": None, "_source": "rocket-lane-live", "_matched": True}
            p = projects[0]
            # Rocket Lane exposes status/health via the project's status object + fields.
            status_obj = p.get("status") or {}
            status_label = status_obj.get("label") if isinstance(status_obj, dict) else status_obj
            return {
                "status": status_label,
                "project_name": p.get("projectName"),
                "start_date": p.get("startDate"),
                "due_date": p.get("dueDate"),
                "archived": p.get("archived"),
                "health": self._field(p, ("health", "onboarding health", "status health")),
                "_source": "rocket-lane-live",
                "_matched": True,
            }
        except Exception as exc:  # noqa: BLE001
            if config.env("CS_LOG_SOURCE_ERRORS"):
                import sys as _s; print(f"[rocket-lane] {type(exc).__name__}: {exc}", file=_s.stderr)
            return {"status": None, "_source": "rocket-lane-live", "_error": True}

    @staticmethod
    def _field(project: dict, label_contains: tuple) -> Any:
        """Pull a custom field value from a Rocket Lane project by fuzzy label match."""
        for f in project.get("fields", []) or []:
            lbl = str(f.get("fieldLabel") or "").lower()
            if any(k in lbl for k in label_contains):
                return f.get("fieldValueLabel") or f.get("fieldValue")
        return None



# ------------------------------------------------------------- Entitlements ---
class Entitlements:
    """Licensed-seat / entitlement source for TRUE license utilization.

    License utilization (% of purchased seats actually in use) is a contract-vs-usage
    metric whose authoritative source is the billing / entitlement system, NOT product
    telemetry — Pendo can show activity, but only entitlements know how many seats were
    sold. This adapter is an explicit, env-gated seam: point it at whatever internal
    endpoint owns seat counts. It is READ-ONLY and fabricates nothing — when the
    connector is not configured, or an account has no record, license utilization
    remains a data gap (None) and the UI shows "not connected".

    Configuration (all optional; unset => connector reports not-live):
      ENTITLEMENTS_API_URL      base URL of the entitlements service
      ENTITLEMENTS_KEY          bearer token
      ENTITLEMENTS_ACCOUNT_PATH account route template, default /accounts/{account_ref}
    The response is read flexibly: an explicit `license_utilization_pct`, or computed
    from `active_seats`/`seats_used` over `licensed_seats`/`seats_purchased`.
    """

    def live(self) -> bool:
        return config.live_enabled() and bool(
            config.env("ENTITLEMENTS_API_URL") and config.env("ENTITLEMENTS_KEY")
        )

    def utilization(self, account_ref: str) -> dict[str, Any]:
        import urllib.parse
        base = config.env("ENTITLEMENTS_API_URL").rstrip("/")
        route = config.env("ENTITLEMENTS_ACCOUNT_PATH") or "/accounts/{account_ref}"
        route = route.format(account_ref=urllib.parse.quote(identity.normalise(account_ref).upper(), safe=""))
        headers = {"Authorization": f"Bearer {config.env('ENTITLEMENTS_KEY')}", "Accept": "application/json"}
        payload = config.http_get(f"{base}/{route.lstrip('/')}", headers)
        data = payload.get("data", payload) if isinstance(payload, dict) else {}

        pct = data.get("license_utilization_pct")
        licensed = data.get("licensed_seats") or data.get("seats_purchased")
        used = data.get("active_seats") or data.get("seats_used")
        if pct is None and licensed:
            try:
                pct = round(100 * float(used or 0) / float(licensed))
            except (ValueError, TypeError, ZeroDivisionError):
                pct = None
        return {
            "license_utilization_pct": pct,
            "licensed_seats": licensed,
            "active_seats": used,
            "_source": "entitlements-live",
        }


# ------------------------------------------------------------------- Stripe ---
class Stripe:
    """Billing -> {past_due_invoices, days_past_due, amount_due_usd, dunning_stage}.
    Read-only. Refuses a full secret key (sk_live_) unless CS_ALLOW_STRIPE_SECRET_KEY=1;
    prefer a restricted key (rk_...)."""

    # Search API requires 2020-08-27+; pin a known-good version regardless of the
    # account's dashboard-configured default (older accounts default to 2014-06-17,
    # which 400s on /v1/customers/search).
    API_VERSION = "2023-10-16"

    def live(self) -> bool:
        key = config.env("STRIPE_KEY")
        return config.live_enabled() and bool(key)

    def _guard_key(self) -> str:
        key = config.env("STRIPE_KEY")
        if key and key.startswith("sk_live_") and not config.stripe_secret_key_allowed():
            raise config.SourceError(
                "Refusing to use a Stripe LIVE SECRET key (sk_live_). Use a restricted "
                "read-only key (rk_...), or set CS_ALLOW_STRIPE_SECRET_KEY=1 to override "
                "(the key can still do far more than this adapter needs; prefer rotating "
                "to a restricted key)."
            )
        return key

    def payment(self, account_ref: str) -> dict[str, Any]:
        import time
        key = self._guard_key()
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Stripe-Version": self.API_VERSION,
        }
        # JobAdder's Stripe customers store the id uppercase-hyphen (e.g. AU5-402364)
        # under metadata['ja_account_id'], not 'account_ref'.
        ref = identity.normalise(account_ref).upper()
        cust = config.http_get(
            f"https://api.stripe.com/v1/customers/search?query=metadata['ja_account_id']:'{ref}'",
            headers,
        )
        data = cust.get("data", [])
        if not data:
            raise config.SourceError(f"Stripe customer not found for {ref}")
        cid = data[0]["id"]
        cust_email = data[0].get("email") or None
        # All non-draft open invoices (JobAdder bills by charge attempt; many invoices have
        # NO due_date, so a due-date-only filter misses genuine failed payments). We treat an
        # invoice as a FAILED PAYMENT when it is open/past_due, has been attempted, and still
        # has an amount remaining — i.e. a collection attempt that bounced.
        inv = config.http_get(
            f"https://api.stripe.com/v1/invoices?customer={cid}&status=open&limit=100", headers
        )
        invoices = inv.get("data", [])
        now = int(time.time())

        def _unpaid(i):
            return (i.get("amount_remaining", i.get("amount_due", 0)) or 0) > 0

        failed = [i for i in invoices
                  if _unpaid(i) and i.get("attempted") and (i.get("attempt_count") or 0) >= 1]
        due_past = [i for i in invoices if i.get("due_date") and i["due_date"] < now and _unpaid(i)]
        # Union of the two signals = the account's open payment problems.
        problem = list({(i.get("id") or idx): i
                        for idx, i in enumerate(failed + due_past)}.values())

        amount = sum((i.get("amount_remaining") or i.get("amount_due", 0)) for i in problem) / 100.0

        # Days past due: prefer the real due_date; otherwise age from the invoice's
        # created/period_end so attempt-based failures still get a meaningful age.
        def _age_days(i):
            anchor = i.get("due_date") or i.get("period_end") or i.get("created")
            return ((now - anchor) // 86400) if anchor and anchor < now else 0
        days_past_due = max((_age_days(i) for i in problem), default=0)

        # Subscription status is the authoritative access signal: past_due / unpaid mean the
        # customer is in dunning / has lost (or is about to lose) access.
        sub_status = None
        try:
            subs = config.http_get(
                f"https://api.stripe.com/v1/subscriptions?customer={cid}&status=all&limit=10", headers
            ).get("data", [])
            bad = [s for s in subs if s.get("status") in ("past_due", "unpaid")]
            canceled = [s for s in subs if s.get("status") == "canceled"]
            if bad:
                sub_status = bad[0]["status"]
            elif canceled and not any(s.get("status") == "active" for s in subs):
                sub_status = "canceled"
        except Exception:  # noqa: BLE001
            sub_status = None

        has_problem = bool(problem) or sub_status in ("past_due", "unpaid")
        if not has_problem:
            stage = "none"
        elif days_past_due >= 15 or sub_status == "unpaid":
            stage = "day_15_plus"
        else:
            stage = "day_1_14"

        max_attempts = max((i.get("attempt_count") or 0 for i in problem), default=0)
        return {
            "past_due_invoices": len(problem),
            "payment_failed": bool(failed) or sub_status in ("past_due", "unpaid"),
            "failed_attempts": max_attempts,
            "subscription_status": sub_status,
            "days_past_due": days_past_due or None,
            "amount_due_usd": amount,
            "dunning_stage": stage,
            # Identity for the Payment Risk Report: the Stripe customer id builds the
            # dashboard link, and the customer email is a billing-contact fallback.
            "customer_id": cid,
            "customer_email": cust_email,
            "_source": "stripe-live",
        }

    def list_payment_problems(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Account-wide list of customers with a live payment problem, sourced DIRECTLY
        from Stripe (not from the per-account enrichment cache) so the Payment Risk Report
        is complete regardless of how the whole-book roster was warmed.

        Paginates OPEN invoices, keeps those that are failed (attempted + unpaid) or past
        the due date, groups by customer, and resolves each customer's JobAdder account id
        (metadata['ja_account_id']) and email. Returns one row per customer:
          {account_ref, customer_id, customer_email, dunning_stage, days_past_due,
           amount_due_usd, failed_attempts, past_due_invoices, payment_failed}
        """
        import time
        key = self._guard_key()
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
                   "Stripe-Version": self.API_VERSION}
        now = int(time.time())

        def _unpaid(i):
            return (i.get("amount_remaining", i.get("amount_due", 0)) or 0) > 0

        # Collect problem invoices across all pages (bounded), expanding the customer so we
        # get ja_account_id + email without a second call per invoice. A single failing page
        # must NOT discard everything already collected, so each page is guarded.
        by_cust: dict[str, dict] = {}
        url = ("https://api.stripe.com/v1/invoices?status=open&limit=100"
               "&expand[]=data.customer")
        fetched = 0
        while url and fetched < limit:
            try:
                page = config.http_get(url, headers, timeout=20)
            except Exception as exc:  # noqa: BLE001
                import sys as _sys
                print(f"[stripe] list_payment_problems page failed, returning partial: "
                      f"{type(exc).__name__}: {exc}", file=_sys.stderr)
                break
            rows = page.get("data", [])
            fetched += len(rows)
            for i in rows:
                failed = _unpaid(i) and i.get("attempted") and (i.get("attempt_count") or 0) >= 1
                past_due = i.get("due_date") and i["due_date"] < now and _unpaid(i)
                if not (failed or past_due):
                    continue
                cust = i.get("customer")
                cust = cust if isinstance(cust, dict) else {"id": cust}
                ja = (cust.get("metadata") or {}).get("ja_account_id")
                if not ja:
                    continue  # cannot map to a JobAdder account -> skip (honest)
                anchor = i.get("due_date") or i.get("period_end") or i.get("created")
                age = ((now - anchor) // 86400) if anchor and anchor < now else 0
                rec = by_cust.setdefault(ja, {
                    "account_ref": ja, "customer_id": cust.get("id"),
                    "customer_email": cust.get("email"),
                    "amount_due_usd": 0.0, "days_past_due": 0,
                    "failed_attempts": 0, "past_due_invoices": 0, "payment_failed": False,
                })
                rec["amount_due_usd"] += (i.get("amount_remaining") or i.get("amount_due", 0)) / 100.0
                rec["days_past_due"] = max(rec["days_past_due"], age)
                rec["failed_attempts"] = max(rec["failed_attempts"], i.get("attempt_count") or 0)
                rec["past_due_invoices"] += 1
                rec["payment_failed"] = rec["payment_failed"] or bool(failed)
            if page.get("has_more") and rows:
                last = rows[-1].get("id")
                url = ("https://api.stripe.com/v1/invoices?status=open&limit=100"
                       f"&starting_after={last}&expand[]=data.customer")
            else:
                url = None
        # Finalise dunning stage per customer.
        out = []
        for rec in by_cust.values():
            dd = rec["days_past_due"]
            rec["dunning_stage"] = "day_15_plus" if dd >= 15 else "day_1_14"
            rec["days_past_due"] = dd or None
            rec["_source"] = "stripe-live"
            out.append(rec)
        return out


# -------------------------------------------------------------- Churn (ML) ---
class Churn:
    """ML churn score -> {ml_churn_score, model_version, top_drivers}. Read-only,
    via the Redshift Data API (no persistent DB connection / VPC access needed;
    auth is whatever AWS credentials boto3 resolves - profile, role, etc.)."""

    POLL_INTERVAL_S = 0.5
    # A cold Redshift Serverless workgroup can take ~20-30s to resume on the FIRST
    # query before it warms up (measured ~24s live); subsequent queries are ~1-2s.
    # 20s was too tight and failed every churn call until the workgroup was warm, so
    # the default covers cold start. Overridable via CS_REDSHIFT_POLL_TIMEOUT_S.
    POLL_TIMEOUT_S = int(config.env("CS_REDSHIFT_POLL_TIMEOUT_S") or 45)

    def live(self) -> bool:
        if not config.live_enabled() or not config.env("REDSHIFT_DATABASE"):
            return False
        if not (config.env("REDSHIFT_WORKGROUP") or config.env("REDSHIFT_CLUSTER_ID")):
            return False
        # Do not report a live ML source until the data platform has published
        # an explicit business-facing score view (the existing training view is
        # not a supported CS input).
        if not config.env("REDSHIFT_CHURN_TABLE"):
            return False
        try:
            import boto3  # noqa: F401
        except ImportError:
            return False
        return True

    def _client(self):
        import boto3
        region = config.env("AWS_REGION") or config.env("AWS_DEFAULT_REGION") or "ap-southeast-2"
        # Cross-account access: the churn model lives in the Data Platform account,
        # while the CS Platform typically runs elsewhere. When REDSHIFT_ASSUME_ROLE_ARN
        # is set, assume that role (in the Data Platform account) and build the Data API
        # client with the returned temporary credentials. When unset, use the ambient
        # credentials (local AWS_PROFILE, or a same-account task role) unchanged.
        role_arn = config.env("REDSHIFT_ASSUME_ROLE_ARN")
        if role_arn:
            sts = boto3.client("sts", region_name=region)
            session_name = config.env("REDSHIFT_ASSUME_ROLE_SESSION") or "cs-platform-churn"
            creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)["Credentials"]
            return boto3.client(
                "redshift-data", region_name=region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
        return boto3.client("redshift-data", region_name=region)

    def _target_kwargs(self) -> dict[str, str]:
        kwargs = {"Database": config.env("REDSHIFT_DATABASE")}
        if config.env("REDSHIFT_WORKGROUP"):
            kwargs["WorkgroupName"] = config.env("REDSHIFT_WORKGROUP")
        else:
            kwargs["ClusterIdentifier"] = config.env("REDSHIFT_CLUSTER_ID")
            if config.env("REDSHIFT_SECRET_ARN"):
                kwargs["SecretArn"] = config.env("REDSHIFT_SECRET_ARN")
            else:
                kwargs["DbUser"] = config.env("REDSHIFT_DB_USER")
        return kwargs

    @staticmethod
    def _cell(field: dict) -> Any:
        if field.get("isNull"):
            return None
        for key in ("stringValue", "doubleValue", "longValue", "booleanValue"):
            if key in field:
                return field[key]
        return None

    @staticmethod
    def _identifier(value: str, qualified: bool = False) -> str:
        pattern = r"[A-Za-z_][A-Za-z0-9_]*"
        valid = (all(re.fullmatch(pattern, part) for part in value.split("."))
                 if qualified else bool(re.fullmatch(pattern, value)))
        if not valid:
            raise config.SourceError(f"Invalid Redshift churn identifier: {value}")
        return value

    def score(self, account_ref: str) -> dict[str, Any]:
        import time
        client = self._client()
        table = self._identifier(config.env("REDSHIFT_CHURN_TABLE") or "", qualified=True)
        id_col = self._identifier(config.env("REDSHIFT_CHURN_ID_COLUMN") or "nk_ja_account")
        mode = (config.env("REDSHIFT_CHURN_MODE") or "score").lower()
        if mode not in ("score", "status"):
            raise config.SourceError(f"Unsupported Redshift churn mode: {mode}")
        if mode == "status":
            status_col = self._identifier(
                config.env("REDSHIFT_CHURN_STATUS_COLUMN") or "calculated_churn_status")
            select_sql = f"{status_col} AS churn_status"
            order_sql = ""
        else:
            score_col = self._identifier(config.env("REDSHIFT_CHURN_SCORE_COLUMN") or "churn_probability")
            version_col = self._identifier(config.env("REDSHIFT_CHURN_VERSION_COLUMN") or "model_version")
            driver_1_col = self._identifier(config.env("REDSHIFT_CHURN_DRIVER_1_COLUMN") or "top_driver_1")
            driver_2_col = self._identifier(config.env("REDSHIFT_CHURN_DRIVER_2_COLUMN") or "top_driver_2")
            scored_at_col = self._identifier(config.env("REDSHIFT_CHURN_SCORED_AT_COLUMN") or "scored_at")
            select_sql = (
                f"{score_col} AS ml_churn_score, {version_col} AS model_version, "
                f"{driver_1_col} AS top_driver_1, {driver_2_col} AS top_driver_2")
            order_sql = f" ORDER BY {scored_at_col} DESC"
        # Redshift's nk_ja_account values use uppercase-hyphen form (AU1-5005).
        ref = identity.normalise(account_ref).upper()
        sql = (
            f"SELECT {select_sql} "
            f"FROM {table} WHERE {id_col} = :account_ref "
            f"{order_sql} LIMIT 1"
        )
        exec_resp = client.execute_statement(
            Sql=sql, Parameters=[{"name": "account_ref", "value": ref}], **self._target_kwargs()
        )
        statement_id = exec_resp["Id"]

        deadline = time.monotonic() + self.POLL_TIMEOUT_S
        status = "SUBMITTED"
        while status not in ("FINISHED", "FAILED", "ABORTED"):
            if time.monotonic() > deadline:
                raise config.SourceError(f"Redshift churn query timed out for {ref}")
            time.sleep(self.POLL_INTERVAL_S)
            desc = client.describe_statement(Id=statement_id)
            status = desc["Status"]
        if status != "FINISHED":
            raise config.SourceError(f"Redshift churn query failed for {ref}: {desc.get('Error')}")

        result = client.get_statement_result(Id=statement_id)
        records = result.get("Records", [])
        if not records:
            raise config.SourceError(f"No churn score found for {ref} in {table}")
        row = records[0]
        if mode == "status":
            return {
                "churn_status": self._cell(row[0]),
                "computed": False,
                "_source": "redshift-live",
            }
        drivers = [d for d in (self._cell(row[2]), self._cell(row[3])) if d]
        return {
            "ml_churn_score": self._cell(row[0]),
            "model_version": self._cell(row[1]),
            "top_drivers": drivers,
            "computed": False,  # real ML model output, not the transparent fallback
            "_source": "redshift-live",
        }


# ------------------------------------------------- Account metrics (Redshift) ---
class AccountMetrics:
    """Real per-account business metrics from the Data Platform warehouse
    (rpt.rpt_account_ndr_monthly): revenue/NDR, user adoption (active vs committed seats),
    first-login (time-to-value) and tenure. Uses the same cross-account Redshift Data API
    path as the churn adapter. Env:
      REDSHIFT_METRICS_TABLE   (default rpt.rpt_account_ndr_monthly)
      REDSHIFT_METRICS_ID_COLUMN (default ja_account, uppercase-hyphen AUx-yyyy)
    Returns {} when unconfigured or no row, never fabricates."""

    POLL_INTERVAL_S = 1.0
    POLL_TIMEOUT_S = int(config.env("CS_REDSHIFT_POLL_TIMEOUT_S") or 45)

    def live(self) -> bool:
        return (config.live_enabled() and bool(config.env("REDSHIFT_DATABASE"))
                and bool(config.env("REDSHIFT_WORKGROUP") or config.env("REDSHIFT_CLUSTER_ID"))
                and ((config.env("REDSHIFT_METRICS_ENABLED") or "1") not in ("0", "false")))

    # Reuse the churn adapter's connection helpers to avoid duplicating cross-account logic.
    _churn = None

    def _c(self):
        if AccountMetrics._churn is None:
            AccountMetrics._churn = Churn()
        return AccountMetrics._churn

    def metrics(self, account_ref: str) -> dict[str, Any]:
        if not self.live():
            return {}
        import time
        ch = self._c()
        try:
            table = ch._identifier(config.env("REDSHIFT_METRICS_TABLE") or "rpt.rpt_account_ndr_monthly", qualified=True)
            id_col = ch._identifier(config.env("REDSHIFT_METRICS_ID_COLUMN") or "ja_account")
        except config.SourceError:
            return {}
        ref = identity.normalise(account_ref).upper()
        client = ch._client()
        sql = (
            "SELECT revenue, revenue_for_the_previous_year, max_daily_users_over_month, "
            "deal_committed_users, first_user_login_date, initial_subscription_start_date, "
            "tenure_months, user_change "
            f"FROM {table} WHERE {id_col} = :ref "
            "ORDER BY date_reporting_month DESC LIMIT 1"
        )
        try:
            resp = client.execute_statement(Sql=sql, Parameters=[{"name": "ref", "value": ref}], **ch._target_kwargs())
            sid = resp["Id"]
            deadline = time.monotonic() + self.POLL_TIMEOUT_S
            status = "SUBMITTED"
            while status not in ("FINISHED", "FAILED", "ABORTED"):
                if time.monotonic() > deadline:
                    return {}
                time.sleep(self.POLL_INTERVAL_S)
                status = client.describe_statement(Id=sid)["Status"]
            if status != "FINISHED":
                return {}
            recs = client.get_statement_result(Id=sid).get("Records", [])
        except Exception:  # noqa: BLE001
            return {}
        if not recs:
            return {}
        row = recs[0]
        cell = ch._cell
        rev = cell(row[0]); rev_py = cell(row[1])
        active_users = cell(row[2]); committed = cell(row[3])
        ndr_pct = None
        try:
            if rev_py:
                ndr_pct = round(100.0 * float(rev) / float(rev_py))
        except (TypeError, ValueError, ZeroDivisionError):
            ndr_pct = None
        user_util = None
        try:
            if committed:
                user_util = round(100.0 * float(active_users or 0) / float(committed))
        except (TypeError, ValueError, ZeroDivisionError):
            user_util = None
        return {
            "mrr_usd": rev,
            "revenue_prev_year_usd": rev_py,
            "ndr_pct": ndr_pct,
            "active_users": active_users,
            "committed_users": committed,
            "user_utilization_pct": user_util,
            "first_login_date": cell(row[4]),
            "subscription_start_date": cell(row[5]),
            "tenure_months": cell(row[6]),
            "user_change": cell(row[7]),
            "_source": "redshift-live",
        }


# ------------------------------------------------------------------ Jiminny ---
class Jiminny:
    """Jiminny Customer API (verified against the official OpenAPI spec at
    jiminny.github.io/customer-api-docs). Base is app.jiminny.com (US) or
    app.jiminny.eu (EU) + /customer/api/v1; auth is Bearer <80-char token>.
    Calls are modelled as "activities"; getActivities filters by `accountId` =
    the CRM (HubSpot) external account id, so we pass the HubSpot company id when
    we have it, falling back to the AUx-yyyy ref."""

    def _base(self) -> str:
        # Region: US by default; set JIMINNY_REGION=eu or JIMINNY_API_URL to override.
        explicit = config.env("JIMINNY_API_URL")
        if explicit:
            return explicit.rstrip("/")
        region = (config.env("JIMINNY_REGION") or "us").strip().lower()
        host = "app.jiminny.eu" if region == "eu" else "app.jiminny.com"
        return f"https://{host}/customer/api/v1"

    def live(self) -> bool:
        return config.live_enabled() and bool(config.env("JIMINNY_KEY"))

    def calls(self, account_ref: str, crm_account_id: str | None = None) -> dict[str, Any]:
        """Latest call activity for an account. `crm_account_id` is the HubSpot company
        id (preferred Jiminny accountId); falls back to the AUx-yyyy external ref."""
        import urllib.parse, datetime as _dt
        headers = {"Authorization": f"Bearer {config.env('JIMINNY_KEY')}", "Accept": "application/json"}
        base = self._base()
        acct = crm_account_id or identity.normalise(account_ref).upper()
        # Required: a <6-month window. Use the last ~180 days up to now (UTC).
        to = _dt.datetime.utcnow()
        frm = to - _dt.timedelta(days=180)
        q = urllib.parse.urlencode({
            "accountId": acct,
            "fromDate": frm.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": to.strftime("%Y-%m-%d %H:%M:%S"),
            "pageSize": 1,
        })
        data = config.http_get(f"{base}/getActivities?{q}", headers)
        # Response is a paged list; accept common shapes.
        items = (data.get("data") or data.get("activities") or data.get("results") or []) if isinstance(data, dict) else []
        last = items[0] if items else {}
        return {
            "last_call_date": last.get("actualStartTime") or last.get("createdAt") or last.get("scheduledStartTime"),
            "title": last.get("title"),
            "activity_type": last.get("activityType") or last.get("type"),
            "duration_for_humans": last.get("durationForHumans"),
            "average_score": last.get("averageScore"),
            "_source": "jiminny-live",
        }


# ------------------------------------------------------------------ HubSpot ---
class HubSpot:
    """CRM -> company object (segment, ARR, renewal, contacts) keyed on AUx-yyyyy
    external id. Bi-directional: pushes CS data back for Sales visibility."""

    @staticmethod
    def _ingest_lifecycle_stages() -> list[str]:
        """Which HubSpot lifecyclestage values to ingest for the whole-book scan.

        Defaults to the existing customer book ('customer' + the churned-customer stage
        id '20251280'), so behaviour is unchanged unless configured. To bring ONBOARDING
        (or lead/opportunity) accounts into the platform, set CS_HUBSPOT_LIFECYCLE_STAGES
        to a comma-separated list of lifecyclestage values/ids, e.g.
            CS_HUBSPOT_LIFECYCLE_STAGES=customer,20251280,onboarding
        The exact onboarding value is site-specific (often a custom numeric stage id),
        so it is configuration rather than a guessed hardcode."""
        import os
        raw = os.environ.get("CS_HUBSPOT_LIFECYCLE_STAGES", "").strip()
        if not raw:
            return ["customer", "20251280"]
        return [s.strip() for s in raw.split(",") if s.strip()]

    def live(self) -> bool:
        return config.live_enabled() and bool(config.env("HUBSPOT_TOKEN"))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {config.env('HUBSPOT_TOKEN')}", "Content-Type": "application/json"}

    def revenue_motion_deals(self, window_days: int | None = None) -> dict:
        """Aggregate booked revenue motion from HubSpot deals over a rolling window.

        Upsell/expansion = closed-won deals in the Price Rise pipeline.
        Churn            = deals in the Renewals 'Churned' stage.
        These are REAL booked events (deal amount = ARR change), not inferred. Pipeline
        and stage ids are configurable so this is not hard-coded to one portal:
          HUBSPOT_UPSELL_PIPELINE_ID   (default 10754643 - Project Price Rise V1)
          HUBSPOT_RENEWALS_PIPELINE_ID (default 89993136 - Renewals)
          HUBSPOT_CHURNED_STAGE_ID     (default 166792613 - Renewals/Churned)
        Returns {} when deals are unreadable so the caller shows an honest empty state."""
        if not self.live():
            return {}
        from datetime import datetime, timezone, timedelta
        import time as _t
        # 10-minute cache: deals move slowly and this keeps the dashboard fast.
        cache = getattr(HubSpot, "_rm_cache", None)
        if cache and (_t.time() - cache[0] < 600):
            return cache[1]
        # Anchor the window to the platform's business date (CS_TODAY) when set, so the
        # motion reflects the period the rest of the platform reasons about. Default to a
        # wide 36-month window so real booked deals surface rather than a false zero.
        window_days = int(config.env("CS_REVENUE_WINDOW_DAYS") or window_days or 1095)
        today_env = config.env("CS_TODAY")
        try:
            anchor = datetime.fromisoformat(today_env).replace(tzinfo=timezone.utc) if today_env else datetime.now(timezone.utc)
        except ValueError:
            anchor = datetime.now(timezone.utc)
        since = int((anchor - timedelta(days=window_days)).timestamp() * 1000)
        upsell_pipe = config.env("HUBSPOT_UPSELL_PIPELINE_ID") or "10754643"
        churn_stage = config.env("HUBSPOT_CHURNED_STAGE_ID") or "166792613"

        def _search(filters):
            # Fetch a single page: HubSpot returns the exact `total`, and one page of
            # 100 (sorted by amount desc via closedate) is plenty for the top-deals list
            # and a representative ARR sum. Paginating every page made this a 45s call.
            body = {"filterGroups": [{"filters": filters}],
                    "properties": ["dealname", "amount", "closedate", "createdate",
                                    "deal_signed_date", "contract_length__months_",
                                    "dealtype", "customer_type", "billing_type",
                                    "hs_arr", "hs_mrr"],
                    "limit": 100, "sorts": [{"propertyName": "amount", "direction": "DESCENDING"}]}
            try:
                res = config.http_post_readonly(
                    "https://api.hubapi.com/crm/v3/objects/deals/search", self._headers(), body)
            except Exception:  # noqa: BLE001
                return None, 0, 0.0
            total = res.get("total", 0)
            deals, page_sum = [], 0.0
            for r in res.get("results", []):
                p = r.get("properties", {})
                def _f(key):
                    try:
                        return float(p.get(key) or 0)
                    except (TypeError, ValueError):
                        return 0.0
                amt = _f("amount")
                page_sum += amt
                deals.append({
                    "name": p.get("dealname"),
                    "amount_usd": amt,
                    "arr_usd": _f("hs_arr") or amt,
                    "mrr_usd": _f("hs_mrr"),
                    "closed": p.get("closedate"),
                    "created": p.get("createdate"),
                    "start": p.get("deal_signed_date"),
                    "term_months": p.get("contract_length__months_"),
                    "signup_type": p.get("dealtype") or p.get("customer_type"),
                    "billing": p.get("billing_type"),
                })
            return deals, total, page_sum

        upsell, up_total, up_sum = _search([
            {"propertyName": "pipeline", "operator": "EQ", "value": upsell_pipe},
            {"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "true"},
            {"propertyName": "closedate", "operator": "GTE", "value": since}])
        churn, ch_total, ch_sum = _search([
            {"propertyName": "dealstage", "operator": "EQ", "value": churn_stage},
            {"propertyName": "closedate", "operator": "GTE", "value": since}])
        if upsell is None and churn is None:
            return {}
        upsell = upsell or []
        churn = churn or []
        result = {
            "window_days": window_days,
            "upsell": {
                "count": up_total,
                "arr_usd": round(up_sum),
                "arr_is_partial": up_total > len(upsell),
                "deals": upsell[:10],
            },
            "churn": {
                "count": ch_total,
                "arr_usd": round(ch_sum),
                "arr_is_partial": ch_total > len(churn),
                "deals": churn[:10],
            },
        }
        import time as _t2
        HubSpot._rm_cache = (_t2.time(), result)
        return result

    def roster(self, limit: int | None = None) -> list[str]:
        """Live account roster: companies that carry an `account_id` (AUx-yyyyy).

        BOUNDED by `limit` (default from CS_ROSTER_LIMIT, else 25) because a full book
        can be many thousands of companies; assembling every signal for all of them on
        one request would fan out to tens of thousands of live API calls. The platform
        shows a bounded, real slice; raise CS_ROSTER_LIMIT to widen it."""
        import os
        if limit is None:
            try:
                limit = int(os.environ.get("CS_ROSTER_LIMIT", "25"))
            except ValueError:
                limit = 25
        ids: list[str] = []
        after = None
        scope = os.environ.get("CS_ACCOUNT_SCOPE", "all").strip().lower()
        owner_id = os.environ.get("CS_CSM_OWNER_ID", "").strip() if scope == "csm" else ""
        filters = [{"propertyName": "account_id", "operator": "HAS_PROPERTY"}]
        if owner_id:
            filters.append({"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id})
        while len(ids) < limit:
            page = min(100, limit - len(ids))
            body = {
                "filterGroups": [{"filters": filters}],
                "properties": ["account_id"],
                "limit": page,
            }
            if after:
                body["after"] = after
            res = config.http_post("https://api.hubapi.com/crm/v3/objects/companies/search",
                                   self._headers(), body)
            for r in res.get("results", []):
                aid = r.get("properties", {}).get("account_id")
                if aid:
                    ids.append(identity.normalise(aid))
            after = (res.get("paging", {}) or {}).get("next", {}).get("after")
            if not after:
                break
        return ids[:limit]

    def list_all_companies(self, limit: int | None = None, cached_only: bool = False) -> list[dict[str, Any]]:
        """Tier-1 lightweight full-book roster: ALL customer companies, cheap fields only.

        Unlike roster()/account() this does NOT fan out to the other vendors or fetch
        deals/contacts, so it can cover the whole book (thousands of companies) with a
        handful of paginated HubSpot search calls. It powers the portfolio-wide list and
        the Managed / Pooled / All filter. Full multi-vendor enrichment stays on demand
        (when a CSM opens an account).

        Base set = companies with lifecyclestage=customer (the real customer book, not
        prospects). Each row is classified:
          managed = has a CS account_id tag (onboarded into CS tracking)
          pooled  = cs_customer_tier == 'pooled', OR (untagged long-tail customer)
        Cached for CS_FULL_ROSTER_TTL seconds (default 600) because the whole-book scan
        is heavier than a single account. Returns [] when not live."""
        if not self.live():
            return []
        import os, time as _t
        if limit is None:
            try:
                limit = int(os.environ.get("CS_FULL_ROSTER_LIMIT", "5000"))
            except ValueError:
                limit = 5000
        try:
            ttl = int(os.environ.get("CS_FULL_ROSTER_TTL", "600"))
        except ValueError:
            ttl = 600
        cache = getattr(HubSpot, "_full_roster_cache", None)
        if cache and (_t.time() - cache[0] < ttl) and cache[2] >= limit:
            return cache[1][:limit]

        # Non-blocking mode: if the cache is cold, return nothing NOW and warm it in a
        # background thread, so a page load that calls this never pays the ~40s scan.
        # The next load (after warm) gets the full book. Used by portfolio().
        if cached_only:
            if not getattr(HubSpot, "_roster_warming", False):
                HubSpot._roster_warming = True
                import threading
                def _warm():
                    try:
                        self.list_all_companies(limit=limit)
                    finally:
                        HubSpot._roster_warming = False
                threading.Thread(target=_warm, daemon=True).start()
            return cache[1][:limit] if cache else []

        props = ["name", "account_id", "arr", "arr__v2_", "hs_active_contracts_arr",
                 "icp_sales_segment", "cs_segment", "lifecyclestage", "hubspot_owner_id",
                 "cs_customer_tier", "industry", "country"]
        rows: list[dict[str, Any]] = []
        after = None
        while len(rows) < limit:
            page = min(100, limit - len(rows))
            body = {
                "filterGroups": [{"filters": [
                    {"propertyName": "lifecyclestage", "operator": "IN",
                     "values": self._ingest_lifecycle_stages()}  # customer + churned by default; configurable to add onboarding
                ]}],
                "properties": props,
                "limit": page,
            }
            if after:
                body["after"] = after
            res = config.http_post(
                "https://api.hubapi.com/crm/v3/objects/companies/search", self._headers(), body)
            for r in res.get("results", []):
                p = r.get("properties", {})
                acc = p.get("account_id")
                tier = (p.get("cs_customer_tier") or "").strip()
                managed = bool(acc)
                pooled = (tier.lower() == "pooled") or (not managed)

                def _n(*keys):
                    for k in keys:
                        v = p.get(k)
                        if v not in (None, ""):
                            try:
                                return int(float(v))
                            except (ValueError, TypeError):
                                pass
                    return None

                raw_segment = p.get("icp_sales_segment") or p.get("cs_segment")
                rows.append({
                    "company_id": r.get("id"),
                    "account_id": (identity.normalise(acc) if acc else None),
                    "name": p.get("name"),
                    "arr_usd": _n("arr__v2_", "arr", "hs_active_contracts_arr"),
                    "segment": self._map_segment(raw_segment),
                    "segment_label": raw_segment,
                    "lifecycle_stage": {"20251280": "Churned Customer", "customer": "Customer"}
                                        .get(p.get("lifecyclestage"), p.get("lifecyclestage")),
                    "owner_id": (str(p.get("hubspot_owner_id")) if p.get("hubspot_owner_id") else None),
                    "customer_tier": tier or None,
                    "managed": managed,
                    "pooled": pooled,
                    "cohort": ("managed" if managed and not (tier.lower() == "pooled") else "pooled"),
                    "country": p.get("country"),
                    "industry": (p.get("industry") or "").replace("_", " ").title() or None,
                    "_source": "hubspot-live",
                })
            after = (res.get("paging", {}) or {}).get("next", {}).get("after")
            if not after:
                break
        HubSpot._full_roster_cache = (_t.time(), rows, limit)
        return rows[:limit]

    def _find_company(self, account_ref: str) -> dict[str, Any]:
        # HubSpot stores the AUx-yyyy id in the `account_id` company property,
        # uppercase-hyphen form (e.g. AU1-3102).
        ref = identity.normalise(account_ref).upper()
        body = {
            "filterGroups": [{"filters": [{"propertyName": "account_id", "operator": "EQ", "value": ref}]}],
            "properties": ["name", "cs_segment", "icp_sales_segment", "arr", "arr__v2_", "annualrevenue",
                            "hs_active_contracts_arr", "renewal_date", "hs_next_renewal_date",
                            "contract_renewal_date", "subscription_type", "account_id",
                            "lifecyclestage", "instance", "type", "hubspot_owner_id", "industry",
                            "state", "hs_state_code", "country", "cs_customer_tier"],
            "limit": 1,
        }
        res = config.http_post("https://api.hubapi.com/crm/v3/objects/companies/search", self._headers(), body)
        results = res.get("results", [])
        if not results:
            raise config.SourceError(f"HubSpot company not found for account_id={ref}")
        return results[0]

    def account(self, account_ref: str) -> dict[str, Any]:
        c = self._find_company(account_ref)
        p = c.get("properties", {})

        def _num(*keys):
            for k in keys:
                v = p.get(k)
                if v not in (None, ""):
                    try:
                        return int(float(v))
                    except (ValueError, TypeError):
                        pass
            return None

        def _first(*keys):
            for k in keys:
                v = p.get(k)
                if v not in (None, ""):
                    return v
            return None

        # annualrevenue is the company's total revenue, not contract ARR; do not
        # use it as a fallback because it would corrupt CS revenue-risk reporting.
        arr = _num("arr__v2_", "arr", "hs_active_contracts_arr")
        company_renewal = _first("hs_next_renewal_date", "renewal_date", "contract_renewal_date")
        # Many companies have blank ARR / renewal on the COMPANY object; the real signal
        # lives on the closed-won DEAL. Derive from the deal when the company is blank:
        #   arr          <- deal hs_arr or amount
        #   renewal_date <- deal (signed or close date) + contract_length__months_
        # Live, evidence-grounded (real deal values), never invented.
        deal_arr, deal_renewal = None, None
        if arr is None or not company_renewal:
            d = self._account_deal(c.get("id"))
            if d:
                deal_arr = d.get("arr_usd")
                deal_renewal = d.get("renewal_date")
        if arr is None and deal_arr:
            arr = int(deal_arr)
        renewal_date = company_renewal or deal_renewal
        # Real CS segment lives in icp_sales_segment (e.g. "Corporate", "Agency 21+ Users").
        raw_segment = _first("icp_sales_segment", "cs_segment")
        # Authoritative pooled/tier flag from HubSpot. When cs_customer_tier is set it is
        # the source of truth for pooled membership; segment is only a fallback heuristic.
        customer_tier = _first("cs_customer_tier")
        pooled = (str(customer_tier or "").strip().lower() == "pooled") if customer_tier else None
        return {
            "company_id": c.get("id"),
            "name": p.get("name"),
            "segment": self._map_segment(raw_segment),  # engine model: Strategic / Scaled
            "segment_label": raw_segment,               # original HubSpot label for display
            "customer_tier": customer_tier,             # authoritative tier (e.g. "Pooled") when set in HubSpot
            "pooled": pooled,                           # True/False when tier known; None when unset
            "arr_usd": arr,
            "arr_source": ("company" if _num("arr__v2_", "arr", "hs_active_contracts_arr") is not None
                           else ("deal" if deal_arr else None)),
            "state": _first("hs_state_code", "state"),
            "country": p.get("country"),
            "renewal_date": renewal_date,
            "renewal_source": ("company" if company_renewal else ("deal" if deal_renewal else None)),
            "subscription_type": p.get("subscription_type"),
            "csm_owner": self._owner_name(p.get("hubspot_owner_id")),
            "csm_owner_id": (str(p.get("hubspot_owner_id")) if p.get("hubspot_owner_id") else None),
            "industry": (p.get("industry") or "").replace("_", " ").title() or None,
            "lifecycle_stage": {
                "20251280": "Churned Customer",
                "customer": "Customer",
            }.get(p.get("lifecyclestage"), p.get("lifecyclestage")),
            "contacts": self._contacts(c.get("id")),  # real associated contacts (live)
            "instances": [{"instance_id": identity.normalise(account_ref),
                            "instance_type": identity.instance_type(account_ref), "arr_share": 1.0}],
            "_source": "hubspot-live",
        }

    # Map HubSpot buying-role / job-title text to the WoW required roles.
    _ROLE_MAP = {
        "decision maker": "Executive Sponsor", "budget holder": "Executive Sponsor",
        "executive sponsor": "Executive Sponsor", "champion": "Primary Champion / Admin",
        "influencer": "Primary Champion / Admin", "end user": "Primary Champion / Admin",
        "billing": "Finance Contact", "finance": "Finance Contact",
    }

    def _role_from(self, buying_role, jobtitle):
        for src in (buying_role, jobtitle):
            if not src:
                continue
            s = str(src).lower()
            for kw, role in self._ROLE_MAP.items():
                if kw in s:
                    return role
        return None

    def _contacts(self, company_id, limit: int = 10):
        """Real associated contacts for a company (names + emails + inferred role).
        Bounded to `limit`. Returns [] on any error so the account still renders."""
        if not company_id:
            return []
        try:
            assoc = config.http_get(
                f"https://api.hubapi.com/crm/v4/objects/companies/{company_id}/associations/contacts?limit={limit}",
                self._headers())
            ids = [a.get("toObjectId") for a in assoc.get("results", []) if a.get("toObjectId")][:limit]
            if not ids:
                return []
            body = {"inputs": [{"id": str(i)} for i in ids],
                    "properties": ["firstname", "lastname", "email", "jobtitle", "hs_buying_role"]}
            res = config.http_post(
                "https://api.hubapi.com/crm/v3/objects/contacts/batch/read", self._headers(), body)
            out = []
            for c in res.get("results", []):
                p = c.get("properties", {})
                name = " ".join(x for x in [p.get("firstname"), p.get("lastname")] if x).strip()
                out.append({
                    "name": name or p.get("email") or "(unnamed)",
                    "email": p.get("email"),
                    "title": p.get("jobtitle"),
                    "role": self._role_from(p.get("hs_buying_role"), p.get("jobtitle")),
                })
            return out
        except Exception:  # noqa: BLE001
            return []

    def _account_deal(self, company_id):
        """Most relevant closed-won deal for a company, with a derived renewal date.

        Many companies carry no ARR / renewal on the COMPANY object; the real contract
        signal is on the deal. Reads the company's associated deals, picks the latest
        closed-won one, and derives:
          arr_usd      <- hs_arr or amount (real booked value)
          renewal_date <- (deal_signed_date or closedate) + contract_length__months_
        Returns None on any error so the account still renders. Live, not invented."""
        if not company_id:
            return None
        try:
            assoc = config.http_get(
                f"https://api.hubapi.com/crm/v4/objects/companies/{company_id}/associations/deals?limit=25",
                self._headers())
            ids = [a.get("toObjectId") for a in assoc.get("results", []) if a.get("toObjectId")]
            if not ids:
                return None
            body = {"inputs": [{"id": str(i)} for i in ids],
                    "properties": ["amount", "hs_arr", "closedate", "deal_signed_date",
                                   "contract_length__months_", "hs_is_closed_won", "dealstage"]}
            res = config.http_post(
                "https://api.hubapi.com/crm/v3/objects/deals/batch/read", self._headers(), body)
            won = [r.get("properties", {}) for r in res.get("results", [])
                   if str(r.get("properties", {}).get("hs_is_closed_won")).lower() == "true"]
            pool = won or [r.get("properties", {}) for r in res.get("results", [])]
            if not pool:
                return None
            pool.sort(key=lambda pr: pr.get("closedate") or "", reverse=True)
            p = pool[0]

            def _f(k):
                try:
                    return float(p.get(k) or 0) or None
                except (TypeError, ValueError):
                    return None

            arr = _f("hs_arr") or _f("amount")
            renewal = None
            start = p.get("deal_signed_date") or p.get("closedate")
            term = p.get("contract_length__months_")
            if start and term:
                try:
                    from datetime import datetime
                    base = datetime.fromisoformat(str(start)[:10])
                    months = int(float(term))
                    yy = base.year + (base.month - 1 + months) // 12
                    mm = (base.month - 1 + months) % 12 + 1
                    dd = min(base.day, 28)
                    renewal = f"{yy:04d}-{mm:02d}-{dd:02d}"
                except (ValueError, TypeError):
                    renewal = None
            return {"arr_usd": arr, "renewal_date": renewal}
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _map_segment(label):
        """Map HubSpot ICP sales segment to the WoW engine's Strategic/Scaled model.
        'Corporate' and large agencies (21+ users) are High-Touch (Strategic); smaller
        agencies are Tech-Touch (Scaled). Falls back to Scaled when unknown."""
        if not label:
            return None
        s = str(label).lower()
        if "corporate" in s or "21+" in s or "enterprise" in s:
            return "Strategic"
        return "Scaled"

    # Owner id -> display name, cached across accounts to avoid repeat calls.
    _OWNER_CACHE: dict[str, str] = {}

    def _owner_name(self, owner_id):
        if not owner_id:
            return None
        oid = str(owner_id)
        if oid in self._OWNER_CACHE:
            return self._OWNER_CACHE[oid]
        try:
            o = config.http_get(f"https://api.hubapi.com/crm/v3/owners/{oid}", self._headers())
            name = " ".join(x for x in [o.get("firstName"), o.get("lastName")] if x).strip() or o.get("email")
        except Exception:  # noqa: BLE001
            name = None
        self._OWNER_CACHE[oid] = name
        return name

    # email (lowercased) -> owner id, cached. Used to scope a logged-in CSM to the
    # accounts they own. The SSO email is the join to the HubSpot owner record.
    _OWNER_EMAIL_CACHE: dict[str, str | None] = {}

    def owners_for_refs(self, refs: list[str]) -> dict[str, str]:
        """Resolve {normalised account_ref -> CSM owner name} for a SPECIFIC set of account
        refs, directly from the company search (filtered by account_id IN batches of 100).
        Unlike owner_map(), this does NOT depend on the lifecyclestage-filtered roster, so it
        covers payment-problem accounts in any lifecycle stage. Owner ids resolve via the
        cached _owner_name. Returns {} when not live."""
        if not self.live() or not refs:
            return {}
        up = sorted({identity.normalise(r).upper() for r in refs if r})
        out: dict[str, str] = {}
        for i in range(0, len(up), 100):
            batch = up[i:i + 100]
            body = {
                "filterGroups": [{"filters": [
                    {"propertyName": "account_id", "operator": "IN", "values": batch}]}],
                "properties": ["account_id", "hubspot_owner_id"],
                "limit": 100,
            }
            try:
                res = config.http_post(
                    "https://api.hubapi.com/crm/v3/objects/companies/search",
                    self._headers(), body)
            except Exception:  # noqa: BLE001
                continue
            for r in res.get("results", []):
                p = r.get("properties", {}) or {}
                aid = p.get("account_id")
                oid = p.get("hubspot_owner_id")
                if aid and oid:
                    name = self._owner_name(oid)
                    if name:
                        out[identity.normalise(aid)] = name
        return out

    def owner_map(self, limit: int = 5000) -> dict[str, str]:
        """Whole-book {normalised account_ref -> CSM owner name}, built from the cheap
        company roster (list_all_companies gives account_id + owner_id) with owner ids
        resolved to names (cached, so only ~one call per distinct CSM). Used by reports
        that need the CSM for accounts outside the small enriched slice (e.g. the Payment
        Risk Report). Returns {} when HubSpot is not live."""
        if not self.live():
            return {}
        out: dict[str, str] = {}
        for row in self.list_all_companies(limit=limit):
            aid = row.get("account_id")
            oid = row.get("owner_id")
            if not aid or not oid:
                continue
            name = self._owner_name(oid)
            if name:
                out[identity.normalise(aid)] = name
        return out

    def owner_id_for_email(self, email: str) -> str | None:
        """Resolve a HubSpot owner id from an email address (the SSO identity join).

        Owners are paged from /crm/v3/owners and matched on email, case-insensitively.
        Result cached across the process. Returns None if no owner matches that email
        (a CSM with no HubSpot ownership sees an empty book, never everyone's)."""
        if not email:
            return None
        key = email.strip().lower()
        if key in self._OWNER_EMAIL_CACHE:
            return self._OWNER_EMAIL_CACHE[key]
        found: str | None = None
        after = None
        try:
            while True:
                url = "https://api.hubapi.com/crm/v3/owners?limit=100"
                if after:
                    url += f"&after={after}"
                page = config.http_get(url, self._headers())
                for o in page.get("results", []):
                    if str(o.get("email") or "").strip().lower() == key:
                        found = str(o.get("id"))
                        break
                if found:
                    break
                after = (page.get("paging", {}) or {}).get("next", {}).get("after")
                if not after:
                    break
        except Exception:  # noqa: BLE001
            found = None
        self._OWNER_EMAIL_CACHE[key] = found
        return found

    WRITEBACK_PROPERTIES = {
        "cs_health_score": {"label": "CS Health Score", "type": "number", "fieldType": "number", "groupName": "companyinformation"},
        "cs_risk_status": {"label": "CS Risk Status", "type": "enumeration", "fieldType": "select", "groupName": "companyinformation",
                           "options": [{"label": "Healthy", "value": "healthy"}, {"label": "Watch", "value": "watch"}, {"label": "At risk", "value": "at_risk"}]},
        "cs_active_playbook": {"label": "CS Active Playbook", "type": "string", "fieldType": "text", "groupName": "companyinformation"},
    }

    def _writeback_property_status(self) -> dict[str, Any]:
        data = config.http_get("https://api.hubapi.com/crm/v3/properties/companies", self._headers())
        existing = {p.get("name") for p in data.get("results", [])}
        return {name: {"exists": name in existing, "definition": definition}
                for name, definition in self.WRITEBACK_PROPERTIES.items()}

    def push_cs_data(self, account_ref: str, health_score=None, risk_status=None,
                     active_playbook=None, apply: bool = False) -> dict[str, Any]:
        """Verify and optionally write CS fields with two explicit write gates.

        `CS_ALLOW_WRITE=1` enables the capability; `apply=true` on the request
        enables this specific operation. The default always remains dry-run.
        """
        c = self._find_company(account_ref)  # read-only lookup
        props = {}
        if health_score is not None:
            props["cs_health_score"] = health_score
        if risk_status is not None:
            props["cs_risk_status"] = risk_status
        if active_playbook is not None:
            props["cs_active_playbook"] = active_playbook

        property_status = self._writeback_property_status()
        missing_properties = [name for name, status in property_status.items() if not status["exists"]]
        can_apply = apply and config.writes_allowed()
        if can_apply and missing_properties:
            for name in missing_properties:
                definition = dict(property_status[name]["definition"])
                definition["name"] = name
                config.http_post("https://api.hubapi.com/crm/v3/properties/companies",
                                 self._headers(), definition)
        if can_apply:
            config.http_patch(
                f"https://api.hubapi.com/crm/v3/objects/companies/{c['id']}",
                self._headers(), {"properties": props},
            )
            return {"synced": True, "mode": "applied", "target": "hubspot.crm.companies",
                    "company_id": c["id"], "written_fields": props,
                    "created_properties": missing_properties, "property_status": property_status,
                    "_source": "hubspot-live-write"}

        return {"synced": False, "mode": "dry-run", "target": "hubspot.crm.companies",
                "company_id": c["id"], "would_write": props,
                "missing_properties": missing_properties, "property_status": property_status,
                "note": "No HubSpot mutation sent. Set CS_ALLOW_WRITE=1 and request apply=true to enable.",
                "_source": "hubspot-live-readonly"}

    # Buying-role label -> HubSpot hs_buying_role internal value. HubSpot's default enum
    # uses these tokens; a portal with custom values can override via CS_HS_ROLE_MAP later.
    _ROLE_TO_HS = {
        "Executive Sponsor": "DECISION_MAKER",
        "Primary Champion / Admin": "CHAMPION",
        "Finance Contact": "BUDGET_HOLDER",
    }

    def log_note(self, account_ref: str, note: str, apply: bool = False) -> dict[str, Any]:
        """Log a call/meeting NOTE to the company's HubSpot timeline (a note engagement).
        Reversible. Two-gate: dry-run unless apply=true AND CS_ALLOW_WRITE=1."""
        note = (note or "").strip()
        if not note:
            raise ValueError("note text is required")
        c = self._find_company(account_ref)  # read-only lookup
        if apply and config.writes_allowed():
            import time as _t
            body = {
                "properties": {
                    "hs_note_body": note,
                    "hs_timestamp": int(_t.time() * 1000),
                },
                "associations": [{
                    "to": {"id": c["id"]},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED",
                               "associationTypeId": 190}],  # note -> company
                }],
            }
            res = config.http_post("https://api.hubapi.com/crm/v3/objects/notes",
                                   self._headers(), body)
            return {"logged": True, "mode": "applied", "target": "hubspot.crm.notes",
                    "company_id": c["id"], "note_id": res.get("id"),
                    "_source": "hubspot-live-write"}
        return {"logged": False, "mode": "dry-run", "target": "hubspot.crm.notes",
                "company_id": c["id"], "would_write": {"hs_note_body": note},
                "note": "No HubSpot mutation sent. Set CS_ALLOW_WRITE=1 and request apply=true.",
                "_source": "hubspot-live-readonly"}

    def tag_contact_role(self, account_ref: str, contact_email: str, role: str,
                         apply: bool = False) -> dict[str, Any]:
        """Tag a contact's CS role (Executive Sponsor / Primary Champion / Finance Contact)
        by setting hs_buying_role on the contact. Reversible. Two-gate as above. Resolves
        the contact by email among the company's associated contacts (never writes a contact
        that is not already on the account)."""
        role = (role or "").strip()
        contact_email = (contact_email or "").strip().lower()
        hs_role = self._ROLE_TO_HS.get(role)
        if not hs_role:
            raise ValueError(f"unknown role {role!r}; expected one of {list(self._ROLE_TO_HS)}")
        if not contact_email:
            raise ValueError("contact_email is required")
        c = self._find_company(account_ref)
        contacts = self._contacts(c["id"], limit=50)
        match = next((x for x in contacts if (x.get("email") or "").lower() == contact_email), None)
        if not match:
            raise ValueError(f"no associated contact with email {contact_email} on this account")
        # Resolve the contact id (the _contacts helper does not return it; search by email).
        found = config.http_post(
            "https://api.hubapi.com/crm/v3/objects/contacts/search", self._headers(),
            {"filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": contact_email}]}],
             "properties": ["email", "hs_buying_role"], "limit": 1})
        hits = found.get("results", [])
        if not hits:
            raise ValueError(f"contact {contact_email} not found in HubSpot")
        contact_id = hits[0]["id"]
        if apply and config.writes_allowed():
            config.http_patch(
                f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
                self._headers(), {"properties": {"hs_buying_role": hs_role}})
            return {"tagged": True, "mode": "applied", "target": "hubspot.crm.contacts",
                    "contact_id": contact_id, "email": contact_email,
                    "role": role, "hs_buying_role": hs_role, "_source": "hubspot-live-write"}
        return {"tagged": False, "mode": "dry-run", "target": "hubspot.crm.contacts",
                "contact_id": contact_id, "email": contact_email, "role": role,
                "would_write": {"hs_buying_role": hs_role},
                "note": "No HubSpot mutation sent. Set CS_ALLOW_WRITE=1 and request apply=true.",
                "_source": "hubspot-live-readonly"}

    def create_csql(self, account_ref: str, name: str, amount_usd=None,
                    note: str | None = None, apply: bool = False) -> dict[str, Any]:
        """Create an expansion deal (CSQL) in HubSpot and associate it to the company, so a
        CSM can route an expansion opportunity without leaving the platform. Pipeline/stage
        are portal-specific and configurable via CS_HS_EXPANSION_PIPELINE / _STAGE (default
        to HubSpot's standard sales pipeline + appointmentscheduled). Two-gate; dry-run
        default. Reversible (a deal can be deleted/closed-lost)."""
        import os as _os
        name = (name or "").strip()
        if not name:
            raise ValueError("deal name is required")
        c = self._find_company(account_ref)  # read-only lookup
        pipeline = _os.environ.get("CS_HS_EXPANSION_PIPELINE", "default").strip() or "default"
        stage = _os.environ.get("CS_HS_EXPANSION_STAGE", "appointmentscheduled").strip() or "appointmentscheduled"
        props = {"dealname": name, "pipeline": pipeline, "dealstage": stage,
                 "deal_source_type": "CS_PLATFORM_CSQL"}
        if amount_usd not in (None, ""):
            try:
                props["amount"] = str(int(float(amount_usd)))
            except (TypeError, ValueError):
                pass
        if apply and config.writes_allowed():
            body = {"properties": props, "associations": [{
                "to": {"id": c["id"]},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5}],  # deal -> company
            }]}
            res = config.http_post("https://api.hubapi.com/crm/v3/objects/deals",
                                   self._headers(), body)
            deal_id = res.get("id")
            if note and deal_id:
                try:
                    import time as _t
                    config.http_post("https://api.hubapi.com/crm/v3/objects/notes", self._headers(), {
                        "properties": {"hs_note_body": note, "hs_timestamp": int(_t.time() * 1000)},
                        "associations": [{"to": {"id": deal_id},
                                          "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                                     "associationTypeId": 214}]}]})  # note -> deal
                except Exception:  # noqa: BLE001
                    pass
            return {"created": True, "mode": "applied", "target": "hubspot.crm.deals",
                    "company_id": c["id"], "deal_id": deal_id, "pipeline": pipeline,
                    "stage": stage, "_source": "hubspot-live-write"}
        return {"created": False, "mode": "dry-run", "target": "hubspot.crm.deals",
                "company_id": c["id"], "would_write": props,
                "note": "No HubSpot mutation sent. Set CS_ALLOW_WRITE=1 and request apply=true.",
                "_source": "hubspot-live-readonly"}

    def send_transactional_email(self, to_email: str, subject: str | None = None,
                                 custom_properties: dict | None = None,
                                 apply: bool = False) -> dict[str, Any]:
        """Send a transactional email via HubSpot's single-send API (Option A: HubSpot as
        the digest sender). Uses a transactional email TEMPLATE created in the portal whose
        id is CS_HS_TRANSACTIONAL_EMAIL_ID; the template renders from the custom_properties
        tokens we pass. Two-gate (apply + CS_ALLOW_WRITE) and honest at every step:
          * no recipient            -> refused
          * writes off / dry-run    -> prepared only, nothing sent
          * no template configured  -> 'template-not-configured', nothing sent
        Never fabricates a send: it returns applied only on a real 2xx from HubSpot."""
        import os as _os
        to_email = (to_email or "").strip()
        if not to_email:
            raise ValueError("recipient email is required")
        email_id = _os.environ.get("CS_HS_TRANSACTIONAL_EMAIL_ID", "").strip()
        if not (apply and config.writes_allowed()):
            return {"sent": False, "mode": "dry-run", "target": "hubspot.transactional.single-send",
                    "recipient": to_email, "email_id": email_id or None,
                    "note": "No email sent. Set CS_ALLOW_WRITE=1 and request apply=true.",
                    "_source": "hubspot-live-readonly"}
        if not email_id:
            return {"sent": False, "mode": "template-not-configured",
                    "target": "hubspot.transactional.single-send", "recipient": to_email,
                    "note": "No transactional email template configured. Create a transactional "
                            "email in HubSpot and set CS_HS_TRANSACTIONAL_EMAIL_ID. No email sent.",
                    "_source": "hubspot-live-readonly"}
        # Single-send expects the template emailId plus a message envelope. customProperties
        # are the merge tokens the template renders (we pass the digest fields as strings).
        props = [{"name": k, "value": "" if v is None else str(v)}
                 for k, v in (custom_properties or {}).items()]
        message: dict[str, Any] = {"to": to_email}
        if subject:
            message["subject"] = subject
        body = {"emailId": int(email_id) if email_id.isdigit() else email_id,
                "message": message, "customProperties": props}
        res = config.http_post("https://api.hubapi.com/marketing/v3/transactional/single-email/send",
                               self._headers(), body)
        # HubSpot returns a sendResult (e.g. SENT/QUEUED) + statusId. Treat only an explicit
        # success/queued as sent; anything else is reported honestly as not sent.
        send_result = (res.get("sendResult") or res.get("status") or "").upper()
        ok = send_result in ("SENT", "QUEUED", "PROCESSING", "")  # "" when 2xx w/ async body
        return {"sent": bool(ok), "mode": "applied" if ok else "send-failed",
                "target": "hubspot.transactional.single-send", "recipient": to_email,
                "email_id": email_id, "send_result": send_result or None,
                "status_id": res.get("statusId"), "_source": "hubspot-live-write"}

    def set_customer_tier(self, account_ref: str, tier: str = "Pooled",
                          clear_owner: bool = False, apply: bool = False) -> dict[str, Any]:
        """Set a company's cs_customer_tier (e.g. 'Pooled') in HubSpot, so CS can move an
        account between the Managed and Scaled/Pooled structures from the platform. When
        clear_owner is True (moving INTO pooled), the named HubSpot owner is also cleared so
        the account leaves individual books and is served by the pooled round-robin queue.
        Two-gate (apply + CS_ALLOW_WRITE); dry-run by default. Fully reversible (set the tier
        back / reassign an owner). Owner-scope is enforced at the engine/endpoint, not here."""
        tier = (tier or "").strip()
        if not tier:
            raise ValueError("tier is required")
        c = self._find_company(account_ref)  # read-only lookup
        props: dict[str, Any] = {"cs_customer_tier": tier}
        if clear_owner:
            # Clearing hubspot_owner_id removes named ownership -> pooled/unassigned.
            props["hubspot_owner_id"] = ""
        if apply and config.writes_allowed():
            config.http_patch(
                f"https://api.hubapi.com/crm/v3/objects/companies/{c['id']}",
                self._headers(), {"properties": props})
            return {"updated": True, "mode": "applied", "target": "hubspot.crm.companies",
                    "company_id": c["id"], "written_fields": props, "tier": tier,
                    "cleared_owner": bool(clear_owner), "_source": "hubspot-live-write"}
        return {"updated": False, "mode": "dry-run", "target": "hubspot.crm.companies",
                "company_id": c["id"], "would_write": props, "tier": tier,
                "cleared_owner": bool(clear_owner),
                "note": "No HubSpot mutation sent. Set CS_ALLOW_WRITE=1 and request apply=true.",
                "_source": "hubspot-live-readonly"}


# Singletons the router uses.
ZENDESK, PENDO, STRIPE, CHURN, JIMINNY, ROCKET_LANE, HUBSPOT, ENTITLEMENTS, ACCOUNT_METRICS = (
    Zendesk(), Pendo(), Stripe(), Churn(), Jiminny(), RocketLane(), HubSpot(), Entitlements(), AccountMetrics())
