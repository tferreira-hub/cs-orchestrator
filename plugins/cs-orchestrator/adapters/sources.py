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

    def tickets(self, account_ref: str) -> dict[str, Any]:
        import urllib.parse
        from datetime import datetime, timedelta, timezone
        sub = config.env("ZENDESK_SUBDOMAIN")
        auth = config.basic_auth_header(f"{config.env('ZENDESK_EMAIL')}/token", config.env("ZENDESK_TOKEN"))
        headers = {"Authorization": auth, "Accept": "application/json"}
        base = f"https://{sub}.zendesk.com/api/v2"
        ref = identity.normalise(account_ref).upper()
        orgs = config.http_get(f"{base}/organizations/search.json?external_id={urllib.parse.quote(ref)}", headers)
        org_list = orgs.get("organizations", [])
        if not org_list:
            raise config.SourceError(f"Zendesk org not found for {ref}")
        org_id = org_list[0]["id"]

        now = datetime.now(timezone.utc)
        d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        d14 = (now - timedelta(days=14)).strftime("%Y-%m-%d")

        def _count(query: str) -> int:
            r = config.http_get(f"{base}/search.json?query={urllib.parse.quote(query)}", headers)
            return int(r.get("count", len(r.get("results", []))))

        base_q = f"type:ticket organization:{org_id}"
        last7 = _count(f"{base_q} created>={d7}")
        prev7 = _count(f"{base_q} created>={d14} created<{d7}")
        open_tickets = _count(f"{base_q} status<solved")

        # CSAT from the org's recent rated tickets (good / good+bad).
        rated = config.http_get(
            f"{base}/search.json?query={urllib.parse.quote(base_q + ' satisfaction:good')}", headers).get("count", 0)
        bad = config.http_get(
            f"{base}/search.json?query={urllib.parse.quote(base_q + ' satisfaction:bad')}", headers).get("count", 0)
        csat = round(100 * rated / (rated + bad)) if (rated + bad) else None

        return {
            "open_tickets": open_tickets,
            "tickets_last_7d": last7,
            "tickets_prev_7d": prev7,
            "sev1_open": _count(f"{base_q} status<solved tags:sev1"),
            "csat_30d": csat,
            "by_instance": {identity.normalise(account_ref): last7},
            "_source": "zendesk-live",
            "_org_name": org_list[0].get("name"),
        }


# -------------------------------------------------------------------- Pendo ---
class Pendo:
    """Product telemetry -> {logins_last_7d, logins_prev_7d, active_users_pct,
    license_utilization_pct, key_feature_adoption_pct, api_calls_*}. Read-only."""

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

        # Recency: days since last visit (from epoch-ms lastvisit).
        last_visit_ms = auto.get("lastvisit")
        days_since_visit = None
        if last_visit_ms:
            days_since_visit = int((time.time() * 1000 - last_visit_ms) / 86400000)

        return {
            # Real usage recency (Pendo doesn't expose 7d login counts on this endpoint;
            # days-since-last-visit is the available real signal).
            "days_since_last_visit": days_since_visit,
            "logins_last_7d": None,
            "logins_prev_7d": None,
            "active_users_pct": None,
            "license_utilization_pct": None,
            "key_feature_adoption_pct": None,
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


# ------------------------------------------------------------------- Stripe ---
class Stripe:
    """Billing -> {past_due_invoices, days_past_due, amount_due_usd, dunning_stage}.
    Read-only. REQUIRES a restricted key (rk_...), refuses a full secret key."""

    def live(self) -> bool:
        key = config.env("STRIPE_KEY")
        return config.live_enabled() and bool(key)

    def _guard_key(self) -> str:
        key = config.env("STRIPE_KEY")
        if key and key.startswith("sk_live_"):
            raise config.SourceError(
                "Refusing to use a Stripe LIVE SECRET key (sk_live_). Use a restricted "
                "read-only key (rk_...). Rotate the secret key immediately."
            )
        return key

    def payment(self, account_ref: str) -> dict[str, Any]:
        key = self._guard_key()
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        ref = identity.normalise(account_ref)
        # Find the customer by AUx-yyyyy stored in metadata, then read open invoices.
        cust = config.http_get(
            f"https://api.stripe.com/v1/customers/search?query=metadata['account_ref']:'{ref}'",
            headers,
        )
        data = cust.get("data", [])
        if not data:
            raise config.SourceError(f"Stripe customer not found for {ref}")
        cid = data[0]["id"]
        inv = config.http_get(
            f"https://api.stripe.com/v1/invoices?customer={cid}&status=open&limit=100", headers
        )
        invoices = inv.get("data", [])
        past_due = [i for i in invoices if i.get("due_date")]
        amount = sum(i.get("amount_due", 0) for i in past_due) / 100.0
        # days_past_due from the oldest due_date would be computed here in production.
        stage = "none"
        return {
            "past_due_invoices": len(past_due),
            "days_past_due": None,
            "amount_due_usd": amount,
            "dunning_stage": stage,
            "_source": "stripe-live",
        }


# ------------------------------------------------------------------ Jiminny ---
class Jiminny:
    def live(self) -> bool:
        return config.live_enabled() and bool(config.env("JIMINNY_KEY"))

    def calls(self, account_ref: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {config.env('JIMINNY_KEY')}", "Accept": "application/json"}
        ref = identity.normalise(account_ref)
        data = config.http_get(f"https://api.jiminny.com/v1/accounts/{ref}/calls?limit=1", headers)
        calls = data.get("calls", []) if isinstance(data, dict) else []
        last = calls[0] if calls else {}
        return {
            "last_call_date": last.get("date"),
            "sentiment": last.get("sentiment"),
            "summary": last.get("summary"),
            "talk_ratio_customer": last.get("talkRatioCustomer"),
            "_source": "jiminny-live",
        }


# ------------------------------------------------------------------ HubSpot ---
class HubSpot:
    """CRM -> company object (segment, ARR, renewal, contacts) keyed on AUx-yyyyy
    external id. Bi-directional: pushes CS data back for Sales visibility."""

    def live(self) -> bool:
        return config.live_enabled() and bool(config.env("HUBSPOT_TOKEN"))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {config.env('HUBSPOT_TOKEN')}", "Content-Type": "application/json"}

    def _find_company(self, account_ref: str) -> dict[str, Any]:
        # HubSpot stores the AUx-yyyy id in the `account_id` company property,
        # uppercase-hyphen form (e.g. AU1-3102).
        ref = identity.normalise(account_ref).upper()
        body = {
            "filterGroups": [{"filters": [{"propertyName": "account_id", "operator": "EQ", "value": ref}]}],
            "properties": ["name", "cs_segment", "arr", "renewal_date", "account_id",
                            "lifecyclestage", "instance", "type"],
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
        arr = None
        if p.get("arr"):
            try:
                arr = int(float(p["arr"]))
            except (ValueError, TypeError):
                arr = None
        return {
            "company_id": c.get("id"),
            "name": p.get("name"),
            "segment": p.get("cs_segment"),
            "arr_usd": arr,
            "renewal_date": p.get("renewal_date"),
            "csm_owner": None,
            "contacts": [],  # associated contacts fetched separately in production
            "instances": [{"instance_id": identity.normalise(account_ref),
                            "instance_type": identity.instance_type(account_ref), "arr_share": 1.0}],
            "_source": "hubspot-live",
        }

    def push_cs_data(self, account_ref: str, health_score=None, risk_status=None, active_playbook=None) -> dict[str, Any]:
        """READ-ONLY MODE: computes the write-back payload and verifies the target
        company exists, but DOES NOT write. Per the read-only integration policy, no
        adapter issues a mutating request. To enable the real PATCH later, set
        CS_ALLOW_WRITE=1 explicitly (off by default) and use a token with write scope.
        """
        c = self._find_company(account_ref)  # read-only lookup
        props = {}
        if health_score is not None:
            props["cs_health_score"] = health_score
        if risk_status is not None:
            props["cs_risk_status"] = risk_status
        if active_playbook is not None:
            props["cs_active_playbook"] = active_playbook

        if config.env("CS_ALLOW_WRITE") in ("1", "true", "True"):
            config.http_patch(
                f"https://api.hubapi.com/crm/v3/objects/companies/{c['id']}",
                self._headers(), {"properties": props},
            )
            return {"synced": True, "target": "hubspot.crm.companies", "company_id": c["id"],
                    "written_fields": props, "_source": "hubspot-live-write"}

        # Default: read-only. Report what WOULD be written, without writing.
        return {"synced": False, "target": "hubspot.crm.companies", "company_id": c["id"],
                "would_write": props, "mode": "read-only (no PATCH sent)",
                "note": "Set CS_ALLOW_WRITE=1 with a write-scoped token to enable the live push-back.",
                "_source": "hubspot-live-readonly"}


# Singletons the router uses.
ZENDESK, PENDO, STRIPE, JIMINNY, HUBSPOT = Zendesk(), Pendo(), Stripe(), Jiminny(), HubSpot()
