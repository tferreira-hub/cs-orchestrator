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
    """Optional onboarding source with an explicit, configurable API contract.

    The endpoint and field names are configurable because Rocket Lane tenant
    deployments expose different account routes. No onboarding data is fabricated
    when the connector is not configured or the account is not mapped.
    """

    def live(self) -> bool:
        return config.live_enabled() and bool(
            config.env("ROCKET_LANE_API_URL") and config.env("ROCKET_LANE_KEY")
        )

    def status(self, account_ref: str) -> dict[str, Any]:
        import urllib.parse
        base = config.env("ROCKET_LANE_API_URL").rstrip("/")
        route = config.env("ROCKET_LANE_ACCOUNT_PATH") or "/accounts/{account_ref}"
        route = route.format(account_ref=urllib.parse.quote(identity.normalise(account_ref), safe=""))
        headers = {"Authorization": f"Bearer {config.env('ROCKET_LANE_KEY')}", "Accept": "application/json"}
        payload = config.http_get(f"{base}/{route.lstrip('/')}", headers)
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        return {
            "status": data.get("status") or data.get("onboarding_status"),
            "time_to_value_days": data.get("time_to_value_days") or data.get("timeToValueDays"),
            "target_time_to_value_days": data.get("target_time_to_value_days") or data.get("targetTimeToValueDays"),
            "health": data.get("health") or data.get("onboarding_health"),
            "_source": "rocket-lane-live",
        }


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
        inv = config.http_get(
            f"https://api.stripe.com/v1/invoices?customer={cid}&status=open&limit=100", headers
        )
        invoices = inv.get("data", [])
        now = int(time.time())
        past_due = [i for i in invoices if i.get("due_date") and i["due_date"] < now]
        amount = sum(i.get("amount_due", 0) for i in past_due) / 100.0
        days_past_due = max((now - i["due_date"]) // 86400 for i in past_due) if past_due else 0
        stage = "day_15_plus" if days_past_due >= 15 else "day_1_14" if past_due else "none"
        return {
            "past_due_invoices": len(past_due),
            "days_past_due": days_past_due or None,
            "amount_due_usd": amount,
            "dunning_stage": stage,
            "_source": "stripe-live",
        }


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

    def _find_company(self, account_ref: str) -> dict[str, Any]:
        # HubSpot stores the AUx-yyyy id in the `account_id` company property,
        # uppercase-hyphen form (e.g. AU1-3102).
        ref = identity.normalise(account_ref).upper()
        body = {
            "filterGroups": [{"filters": [{"propertyName": "account_id", "operator": "EQ", "value": ref}]}],
            "properties": ["name", "cs_segment", "icp_sales_segment", "arr", "arr__v2_", "annualrevenue",
                            "hs_active_contracts_arr", "renewal_date", "hs_next_renewal_date",
                            "contract_renewal_date", "subscription_type", "account_id",
                            "lifecyclestage", "instance", "type", "hubspot_owner_id", "industry"],
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
        # Real CS segment lives in icp_sales_segment (e.g. "Corporate", "Agency 21+ Users").
        raw_segment = _first("icp_sales_segment", "cs_segment")
        return {
            "company_id": c.get("id"),
            "name": p.get("name"),
            "segment": self._map_segment(raw_segment),  # engine model: Strategic / Scaled
            "segment_label": raw_segment,               # original HubSpot label for display
            "arr_usd": arr,
            "renewal_date": _first("hs_next_renewal_date", "renewal_date", "contract_renewal_date"),
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


# Singletons the router uses.
ZENDESK, PENDO, STRIPE, CHURN, JIMINNY, ROCKET_LANE, HUBSPOT, ENTITLEMENTS = (
    Zendesk(), Pendo(), Stripe(), Churn(), Jiminny(), RocketLane(), HubSpot(), Entitlements())
