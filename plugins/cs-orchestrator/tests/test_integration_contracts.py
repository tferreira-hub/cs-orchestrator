"""Contract tests for live adapters, MCP provenance, agent enforcement, and platform HTTP routes."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLATFORM))


def _load_mcp():
    path = PLUGIN / "mcp-servers" / "cs_stack_server.py"
    spec = importlib.util.spec_from_file_location("cs_stack_server_contracts", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_redshift_data_api_score_mode(monkeypatch):
    from adapters import config, sources

    class FakeClient:
        def __init__(self):
            self.sql = None

        def execute_statement(self, **kwargs):
            self.sql = kwargs
            return {"Id": "statement-1"}

        def describe_statement(self, Id):
            return {"Status": "FINISHED"}

        def get_statement_result(self, Id):
            return {"Records": [[
                {"doubleValue": 0.72},
                {"stringValue": "churn-v3"},
                {"stringValue": "usage decline"},
                {"stringValue": "support escalation"},
            ]]}

    fake = FakeClient()
    monkeypatch.setattr(sources.CHURN, "_client", lambda: fake)
    monkeypatch.setattr(sources.CHURN, "POLL_INTERVAL_S", 0)
    monkeypatch.setenv("REDSHIFT_DATABASE", "dwh")
    monkeypatch.setenv("REDSHIFT_WORKGROUP", "warehouse")
    monkeypatch.setenv("REDSHIFT_CHURN_TABLE", "marts.cs_account_churn_scores")
    monkeypatch.delenv("REDSHIFT_CHURN_MODE", raising=False)

    result = sources.CHURN.score("au1_5005")

    assert result == {
        "ml_churn_score": 0.72,
        "model_version": "churn-v3",
        "top_drivers": ["usage decline", "support escalation"],
        "computed": False,
        "_source": "redshift-live",
    }
    assert fake.sql["Parameters"] == [{"name": "account_ref", "value": "AU1-5005"}]
    assert "marts.cs_account_churn_scores" in fake.sql["Sql"]
    assert "ORDER BY scored_at DESC" in fake.sql["Sql"]


def test_redshift_cold_start_is_not_prematurely_timed_out(monkeypatch):
    """Regression: a cold Redshift Serverless workgroup takes ~20-30s to resume on
    the first query (measured ~24s live). The poll loop must wait through several
    SUBMITTED/PICKED/STARTED polls and still succeed, rather than timing out at 20s
    as it did before. We verify a statement that only reaches FINISHED after several
    polls is read successfully."""
    from adapters import sources

    class SlowClient:
        def __init__(self):
            self.polls = 0

        def execute_statement(self, **kwargs):
            return {"Id": "cold-1"}

        def describe_statement(self, Id):
            self.polls += 1
            # Warm up only after several polls (simulates cold-start resume).
            return {"Status": "FINISHED" if self.polls >= 6 else "STARTED"}

        def get_statement_result(self, Id):
            return {"Records": [[{"stringValue": "Not churned"}]]}

    fake = SlowClient()
    monkeypatch.setattr(sources.CHURN, "_client", lambda: fake)
    monkeypatch.setattr(sources.CHURN, "POLL_INTERVAL_S", 0)   # no real sleep in test
    monkeypatch.setattr(sources.CHURN, "POLL_TIMEOUT_S", 45)   # the fixed default
    monkeypatch.setenv("REDSHIFT_DATABASE", "dwh")
    monkeypatch.setenv("REDSHIFT_WORKGROUP", "warehouse")
    monkeypatch.setenv("REDSHIFT_CHURN_TABLE", "marts.cs_account_churn_scores")
    monkeypatch.setenv("REDSHIFT_CHURN_MODE", "status")

    result = sources.CHURN.score("au6-2733")
    assert result["churn_status"] == "Not churned"
    assert result["computed"] is False
    assert fake.polls >= 6   # proves we waited through the cold-start polls


def test_redshift_poll_timeout_is_configurable(monkeypatch):
    """The cold-start poll ceiling is overridable via CS_REDSHIFT_POLL_TIMEOUT_S so
    an operator can widen it for an especially slow workgroup, and defaults to 45s."""
    import importlib
    from adapters import sources as _s
    # Default (no override) is the cold-start-safe 45s.
    monkeypatch.delenv("CS_REDSHIFT_POLL_TIMEOUT_S", raising=False)
    importlib.reload(_s)
    assert _s.Churn.POLL_TIMEOUT_S == 45
    # Explicit override is honoured.
    monkeypatch.setenv("CS_REDSHIFT_POLL_TIMEOUT_S", "90")
    importlib.reload(_s)
    assert _s.Churn.POLL_TIMEOUT_S == 90
    # Restore module to default for the rest of the suite.
    monkeypatch.delenv("CS_REDSHIFT_POLL_TIMEOUT_S", raising=False)
    importlib.reload(_s)


def test_jiminny_adapter_maps_latest_call(monkeypatch):
    from adapters import config, sources

    monkeypatch.setenv("JIMINNY_KEY", "test-key")
    monkeypatch.setenv("JIMINNY_REGION", "eu")
    monkeypatch.delenv("JIMINNY_API_URL", raising=False)
    captured = {}

    def fake_get(url, headers, timeout=12):
        captured["url"] = url
        captured["headers"] = headers
        # Real Jiminny getActivities response shape (key: results).
        return {"results": [{
            "id": "abc",
            "title": "Catchup call with Procurement",
            "activityType": "Web Demo",
            "actualStartTime": "2026-09-22T10:00:00+00:00",
            "durationForHumans": "13m 50s",
            "averageScore": 3.5,
        }], "metadata": {}}

    monkeypatch.setattr(config, "http_get", fake_get)
    result = sources.JIMINNY.calls("AU1_5005", crm_account_id="12345")

    assert result["last_call_date"] == "2026-09-22T10:00:00+00:00"
    assert result["title"] == "Catchup call with Procurement"
    assert result["activity_type"] == "Web Demo"
    assert result["average_score"] == 3.5
    # Hits the EU customer API getActivities with Bearer auth and the CRM accountId.
    assert "app.jiminny.eu/customer/api/v1/getActivities" in captured["url"]
    assert "accountId=12345" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer test-key"


def test_pendo_configured_expansion_metrics_are_live(monkeypatch):
    from adapters import config, sources

    monkeypatch.setenv("PENDO_KEY", "test-key")
    monkeypatch.setenv("PENDO_LICENSE_UTILIZATION_PCT_KEY", "license_usage")
    monkeypatch.setenv("PENDO_API_CALLS_LAST_7D_KEY", "api_7d")
    monkeypatch.setattr(config, "http_get", lambda *args, **kwargs: {
        "metadata": {"agent": {"lastvisit": None}, "custom": {
            "license_usage": 91, "api_7d": 1200,
        }}
    })

    result = sources.PENDO.metrics("AU1_5005")
    assert result["license_utilization_pct"] == 91
    assert result["api_calls_last_7d"] == 1200
    assert result["active_users_pct"] is None


def test_pendo_configured_nested_metric_is_live(monkeypatch):
    from adapters import config, sources

    monkeypatch.setenv("PENDO_KEY", "test-key")
    monkeypatch.setenv("PENDO_ACTIVE_USERS_PCT_KEY", "custom.active_users")
    monkeypatch.setattr(config, "http_get", lambda *args, **kwargs: {
        "metadata": {"custom": {"active_users": 72}}
    })
    result = sources.PENDO.metrics("AU1_5005")
    assert result["active_users_pct"] == 72


def test_pendo_unmapped_expansion_metrics_are_data_gaps(monkeypatch):
    """Without an explicit PENDO_<METRIC>_KEY mapping, expansion metrics are a data
    gap (None) — the adapter never guesses vendor field names or returns 0."""
    from adapters import config, sources

    monkeypatch.setenv("PENDO_KEY", "test-key")
    monkeypatch.delenv("CS_PENDO_ACTIVITY", raising=False)   # activity off
    # No PENDO_*_KEY overrides configured.
    monkeypatch.setattr(config, "http_get", lambda *args, **kwargs: {
        "metadata": {"custom": {"license_utilization_pct": 88}, "auto": {"lastvisit": None}}
    })
    result = sources.PENDO.metrics("AU1_5005")
    assert result["license_utilization_pct"] is None
    assert result["api_calls_last_7d"] is None
    assert result["active_users_pct"] is None
    assert result["key_feature_adoption_pct"] is None
    assert result["api_velocity_source"] is None


def test_pendo_activity_velocity_feeds_api_calls_when_enabled(monkeypatch):
    """With CS_PENDO_ACTIVITY=1, the Pendo Aggregation API supplies per-account event
    counts (last-7d vs prior-7d) into api_calls_*, tagged as an activity proxy."""
    from adapters import config, sources

    monkeypatch.setenv("PENDO_KEY", "test-key")
    monkeypatch.setenv("CS_PENDO_ACTIVITY", "1")
    monkeypatch.setattr(config, "http_get", lambda *a, **k: {"metadata": {"auto": {"lastvisit": None}}})

    calls = {"n": 0}
    def fake_agg(pipeline, headers):
        calls["n"] += 1
        # first call = last7 window, second = prev7 window
        return {"results": [{"count": 12 if calls["n"] == 1 else 2}]}
    # Patch on the class so `self._aggregation` resolves the staticmethod correctly.
    monkeypatch.setattr(sources.Pendo, "_aggregation", staticmethod(fake_agg))

    result = sources.PENDO.metrics("au-uat_1353")
    assert result["api_calls_last_7d"] == 12
    assert result["api_calls_prev_7d"] == 2
    assert result["api_velocity_source"] == "pendo_activity_events"


def test_pendo_activity_off_by_default(monkeypatch):
    """Activity aggregation is opt-in; off by default it makes no aggregation calls
    and velocity stays a data gap unless explicitly mapped."""
    from adapters import config, sources

    monkeypatch.setenv("PENDO_KEY", "test-key")
    monkeypatch.delenv("CS_PENDO_ACTIVITY", raising=False)
    monkeypatch.setattr(config, "http_get", lambda *a, **k: {"metadata": {"auto": {"lastvisit": None}}})

    def boom(*a, **k):
        raise AssertionError("aggregation must not be called when CS_PENDO_ACTIVITY is off")
    monkeypatch.setattr(sources.Pendo, "_aggregation", staticmethod(boom))

    result = sources.PENDO.metrics("au-uat_1353")
    assert result["api_calls_last_7d"] is None
    assert result["api_velocity_source"] is None


def test_entitlements_reports_explicit_utilization(monkeypatch):
    """The env-gated Entitlements connector returns an explicit license_utilization_pct."""
    from adapters import config, sources

    monkeypatch.setenv("ENTITLEMENTS_API_URL", "https://ent.example")
    monkeypatch.setenv("ENTITLEMENTS_KEY", "test-key")
    captured = {}
    def fake_get(url, headers, timeout=12):
        captured["url"] = url
        return {"data": {"license_utilization_pct": 91}}
    monkeypatch.setattr(config, "http_get", fake_get)

    assert sources.ENTITLEMENTS.live() is True
    r = sources.ENTITLEMENTS.utilization("au1-5005")
    assert r["license_utilization_pct"] == 91
    assert captured["url"].endswith("/accounts/AU1-5005")


def test_entitlements_computes_utilization_from_seats(monkeypatch):
    """When no explicit pct is given, utilization is computed from active/licensed seats."""
    from adapters import config, sources

    monkeypatch.setenv("ENTITLEMENTS_API_URL", "https://ent.example")
    monkeypatch.setenv("ENTITLEMENTS_KEY", "test-key")
    monkeypatch.setattr(config, "http_get", lambda *a, **k: {
        "licensed_seats": 200, "active_seats": 184})
    r = sources.ENTITLEMENTS.utilization("au1-5005")
    assert r["license_utilization_pct"] == 92   # round(100*184/200)


def test_entitlements_not_configured_is_data_gap(monkeypatch):
    """Unconfigured entitlements => not live; the platform reports a data gap, never
    a fabricated utilization value."""
    from adapters import sources

    monkeypatch.delenv("ENTITLEMENTS_API_URL", raising=False)
    monkeypatch.delenv("ENTITLEMENTS_KEY", raising=False)
    assert sources.ENTITLEMENTS.live() is False


def test_entitlements_utilization_fires_expansion_trigger():
    """End-to-end: an entitlements-sourced license utilization >= 85% on a healthy
    Strategic account fires the expansion trigger through the rules engine."""
    import orchestrate
    account = {
        "hubspot": {"name": "GrowCo", "segment": "Strategic", "arr_usd": 300000, "contacts": [],
                    "instances": [{"instance_id": "au1-grow", "instance_type": "primary"}]},
        "usage": {"license_utilization_pct": 88, "licensed_seats": 200, "active_seats": 176,
                  "days_since_last_visit": 3},
        "churn": {}, "zendesk": {}, "stripe": {}, "jiminny": {}, "onboarding": {},
    }
    tasks, _ = orchestrate.evaluate("au1-grow", account)
    exp = [t for t in tasks if t["rule_id"] == orchestrate.RULE_EXPANSION_UTILIZATION]
    assert exp, "license utilization 88% on a healthy Strategic account should fire expansion"
    assert exp[0]["evidence"]["license_utilization_pct"] == 88


def test_pendo_aggregation_uses_retry_backed_readonly_post(monkeypatch):
    """The Pendo aggregation call must go through config.http_post_readonly so it
    inherits the shared 429 retry/backoff (not a bare urllib POST) under fan-out."""
    from adapters import config, sources

    seen = {}
    def fake_readonly(url, headers, body, timeout=12):
        seen["url"] = url
        seen["pipeline"] = body["request"]["pipeline"]
        return {"results": [{"count": 5}]}
    monkeypatch.setattr(config, "http_post_readonly", fake_readonly)

    out = sources.Pendo._aggregation([{"source": {"events": None}}], {"x": "y"})
    assert out == {"results": [{"count": 5}]}
    assert seen["url"] == "https://app.pendo.io/api/v1/aggregation"


def test_zendesk_csat_is_bounded_to_30_day_window(monkeypatch):
    """CSAT (field `csat_30d`) must query only the trailing 30 days of rated
    tickets, so the satisfaction queries carry a `created>=` bound."""
    from adapters import config, sources

    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "jobadder")
    monkeypatch.setenv("ZENDESK_EMAIL", "ops@example.com")
    monkeypatch.setenv("ZENDESK_TOKEN", "tok")
    queries = []

    def fake_get(url, headers, timeout=12):
        queries.append(url)
        if "external_id" in url or "workato" in url:
            return {"organizations": [{"id": 7, "name": "Org"}],
                    "results": [{"id": 7, "name": "Org"}]}
        if "satisfaction%3Agood" in url or "satisfaction:good" in url:
            return {"count": 8}
        if "satisfaction%3Abad" in url or "satisfaction:bad" in url:
            return {"count": 2}
        return {"count": 1}

    monkeypatch.setattr(config, "http_get", fake_get)
    result = sources.ZENDESK.tickets("AU1-10094")
    assert result["csat_30d"] == 80  # 8 / (8+2)
    sat_queries = [q for q in queries if "satisfaction" in q]
    assert sat_queries, "expected satisfaction queries"
    # Every satisfaction query is date-bounded (created>= appears url-encoded).
    assert all("created%3E%3D" in q for q in sat_queries), sat_queries


def test_redshift_cross_account_assume_role(monkeypatch):
    """When REDSHIFT_ASSUME_ROLE_ARN is set, the churn client is built from assumed-role
    temp credentials (cross-account Data Platform access); unset = ambient creds."""
    import sys as _sys, types
    from adapters import sources, config

    calls = {"assumed": False, "client_kwargs": None}

    class FakeSTS:
        def assume_role(self, RoleArn, RoleSessionName):
            calls["assumed"] = (RoleArn, RoleSessionName)
            return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK",
                                     "SessionToken": "TOK"}}

    def fake_client(service, **kwargs):
        if service == "sts":
            return FakeSTS()
        calls["client_kwargs"] = kwargs
        return object()

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = fake_client
    monkeypatch.setitem(_sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("REDSHIFT_ASSUME_ROLE_ARN", "arn:aws:iam::503561421603:role/cs-platform-churn-reader")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")

    sources.CHURN._client()
    assert calls["assumed"][0].endswith(":role/cs-platform-churn-reader")
    assert calls["assumed"][1] == "cs-platform-churn"
    # The redshift-data client was built with the assumed temp credentials.
    assert calls["client_kwargs"]["aws_access_key_id"] == "AK"
    assert calls["client_kwargs"]["aws_session_token"] == "TOK"


def test_redshift_cross_account_assume_role_default_ambient(monkeypatch):
    """Without REDSHIFT_ASSUME_ROLE_ARN, no assume_role happens (ambient creds)."""
    import sys as _sys, types
    from adapters import sources

    calls = {"assumed": False}

    def fake_client(service, **kwargs):
        if service == "sts":
            calls["assumed"] = True
        return object()

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = fake_client
    monkeypatch.setitem(_sys.modules, "boto3", fake_boto3)
    monkeypatch.delenv("REDSHIFT_ASSUME_ROLE_ARN", raising=False)

    sources.CHURN._client()
    assert calls["assumed"] is False


def test_jiminny_negative_sentiment_lowers_health():
    """Live Jiminny negative call sentiment must reduce the health score and appear
    as an explainable driver."""
    import engine
    base = {"hubspot": {}, "zendesk": {"csat_30d": 90}, "usage": {}, "churn": {},
            "stripe": {}, "jiminny": {}}
    neg = {"hubspot": {}, "zendesk": {"csat_30d": 90}, "usage": {}, "churn": {},
           "stripe": {}, "jiminny": {"sentiment": "negative"}}
    h_base = engine.health_score(base)
    h_neg = engine.health_score(neg)
    assert h_neg["score"] < h_base["score"]
    assert any("sentiment: negative" in r for r in h_neg["reasons"])
    assert h_neg["computable"] is True


def test_jiminny_negative_sentiment_raises_computed_risk():
    """Negative Jiminny sentiment must contribute to the transparent computed risk
    score when Jiminny is a live source."""
    import dataaccess
    risk = dataaccess._computed_risk(
        usage={}, zendesk={}, stripe={}, jiminny={"sentiment": "negative"},
        sources={"jiminny": "live"})
    assert risk, "a live jiminny signal should make risk computable"
    assert risk["ml_churn_score"] >= 0.15
    assert any("negative call sentiment" in r for r in risk["reasons"])
    assert risk["computed"] is True


def test_retention_metrics_compute_grr_and_expansion_pipeline():
    """GRR must compute from real churn; expansion is reported as a SEPARATE pipeline
    figure (opportunity), never folded into an inflated NDR percentage."""
    import engine
    accounts = {
        "au1-1": {"hubspot": {"name": "A", "segment": "Strategic", "arr_usd": 400000},
                  "sources": {"hubspot": "live"}, "churn": {}, "usage": {}, "zendesk": {},
                  "stripe": {}, "jiminny": {}, "onboarding": {}},
        "au1-2": {"hubspot": {"name": "B", "segment": "Strategic", "arr_usd": 100000,
                              "lifecycle_stage": "Churned Customer"},
                  "sources": {"hubspot": "live"}, "churn": {"churn_status": "churned"},
                  "usage": {}, "zendesk": {}, "stripe": {}, "jiminny": {}, "onboarding": {}},
    }
    tasks_by_account = {"au1-1": [{"rule_id": "expansion_license_utilization"}]}
    r = engine._retention_metrics(accounts, tasks_by_account)
    assert r["computable"] is True
    assert r["grr_pct"] == 80.0            # (500k - 100k) / 500k
    assert "ndr_pct" not in r              # no inflated NDR published
    assert r["base_arr_usd"] == 500000
    assert r["churned_arr_usd"] == 100000
    # Expansion is a separate PIPELINE figure (opportunity), not retention.
    assert r["expansion_pipeline_arr_usd"] == 400000
    assert r["expansion_pipeline_accounts"] == 1
    assert r["target"] == {"grr_pct": 92}


def test_retention_not_computable_without_live_arr():
    """No live ARR -> retention is explicitly not computable (no fabricated 0%)."""
    import engine
    accounts = {"au1-1": {"hubspot": {}, "sources": {}, "churn": {}, "usage": {},
                          "zendesk": {}, "stripe": {}, "jiminny": {}, "onboarding": {}}}
    r = engine._retention_metrics(accounts, {})
    assert r["computable"] is False
    assert r["grr_pct"] is None


def test_sequence_enrolment_defaults_to_dry_run(monkeypatch):
    """One-to-many sequence enrolment never sends by default (dry-run), even with apply=true,
    unless CS_ALLOW_WRITE=1. Same two-gate safety as the HubSpot write-back."""
    import engine, orchestrate
    monkeypatch.delenv("CS_ALLOW_WRITE", raising=False)
    # Pick a real account from the loaded book that is idle 28+ days (qualifies).
    accounts = orchestrate.load_accounts()
    aid = next(iter(accounts))
    r = engine.enrol_sequence(aid, sequence="low_usage_reengage", apply=True)
    assert r["apply_requested"] is True
    assert r["write_enabled"] is False
    # With writes disabled, the result must NOT be an applied enrolment.
    assert r["result"]["mode"] in ("dry-run", "not-qualified")
    assert r["result"].get("enrolled") in (False, None)
    assert r["prepared"]["sequence"] == "low_usage_reengage"
    assert isinstance(r["prepared"]["steps"], list) and r["prepared"]["steps"]


def test_sequence_enrolment_rejects_unknown_sequence():
    import engine
    # Sequence name is validated before any account/live lookup, so this is hermetic.
    try:
        engine.enrol_sequence("au1-anything", sequence="does_not_exist", apply=False)
        assert False, "expected ValueError for unknown sequence"
    except ValueError:
        pass


def test_rocket_lane_adapter_maps_onboarding(monkeypatch):
    from adapters import config, sources

    monkeypatch.setenv("ROCKET_LANE_KEY", "test-key")
    monkeypatch.delenv("ROCKET_LANE_API_URL", raising=False)
    calls = []

    def fake_get(url, headers, timeout=12):
        calls.append((url, headers))
        # Verify the real auth header and host are used.
        assert headers.get("api-key") == "test-key"
        if "/companies" in url:
            return {"data": [{"companyId": 121094, "companyName": "Acme"}]}
        if "/projects" in url:
            return {"data": [{"projectId": 9, "projectName": "Acme Onboarding",
                              "status": {"label": "At Risk"}, "startDate": "2026-01-01",
                              "dueDate": "2026-03-01",
                              "fields": [{"fieldLabel": "Onboarding Health", "fieldValueLabel": "red"}]}]}
        return {"data": []}

    monkeypatch.setattr(config, "http_get", fake_get)
    result = sources.ROCKET_LANE.status("AU1_5005", company_name="Acme")
    assert result["status"] == "At Risk"
    assert result["project_name"] == "Acme Onboarding"
    assert result["health"] == "red"
    assert result["_matched"] is True
    # Base host defaults to the real Rocket Lane API, companies looked up first.
    assert any("api.rocketlane.com/api/1.0/companies" in u for u, _ in calls)
    assert any("/1.0/projects" in u for u, _ in calls)


def test_live_roster_enriches_instance_family(monkeypatch):
    import dataaccess

    monkeypatch.setattr(dataaccess._src.HUBSPOT, "live", lambda: True)
    monkeypatch.setattr(dataaccess._src.HUBSPOT, "roster",
                        lambda: ["AU1-12345", "AU1-12345-dev", "AU2-12345"])
    monkeypatch.setattr(dataaccess, "account", lambda aid: {
        "hubspot": {"name": aid}, "sources": {"hubspot": "live"},
        "zendesk": {}, "usage": {}, "jiminny": {}, "churn": {}, "stripe": {}, "onboarding": {},
    })
    monkeypatch.setenv("CS_CACHE_TTL", "0")
    accounts = dataaccess.all_accounts()
    instances = accounts["au1-12345"]["hubspot"]["instances"]
    assert {item["instance_id"] for item in instances} == {"au1-12345", "au1-12345-dev", "au2-12345"}
    assert any(item["instance_type"] == "test" for item in instances)


def test_live_zendesk_per_instance_suppression(monkeypatch):
    """WoW §5 multi-instance suppression must work on LIVE Zendesk data, not just
    fixtures. Each sibling instance is fetched as its own roster entry with its own
    Zendesk org; the family merge must fold those per-instance counts into one
    `by_instance` map so a spike concentrated on a test instance is detected and
    then suppressed as non-primary-driven."""
    import dataaccess
    import orchestrate
    from suppression import ticket_spike_on_primary, primary_instance_ids

    # Primary sees a normal ticket volume; the -dev test instance carries a spike.
    per_instance_zd = {
        "au1-12345":     {"tickets_last_7d": 2,  "tickets_prev_7d": 3,
                          "by_instance": {"au1-12345": 2}},
        "au1-12345-dev": {"tickets_last_7d": 16, "tickets_prev_7d": 4,
                          "by_instance": {"au1-12345-dev": 16}},
    }

    def fake_account(aid):
        norm = dataaccess._src.identity.normalise(aid)
        return {
            "hubspot": {"name": aid, "segment": "Strategic", "arr_usd": 200000, "contacts": []},
            "sources": {"hubspot": "live", "zendesk": "live"},
            "zendesk": dict(per_instance_zd.get(norm, {"tickets_last_7d": 0, "tickets_prev_7d": 0})),
            "usage": {}, "jiminny": {}, "churn": {}, "stripe": {}, "onboarding": {},
        }

    monkeypatch.setattr(dataaccess._src.HUBSPOT, "live", lambda: True)
    monkeypatch.setattr(dataaccess._src.HUBSPOT, "roster",
                        lambda: ["AU1-12345", "AU1-12345-dev"])
    monkeypatch.setattr(dataaccess, "account", fake_account)
    monkeypatch.setenv("CS_CACHE_TTL", "0")

    accounts = dataaccess.all_accounts()
    primary = accounts["au1-12345"]
    zd = primary["zendesk"]

    # The merge folded both instances' volume into one by_instance map...
    assert zd["by_instance"] == {"au1-12345": 2, "au1-12345-dev": 16}
    # ...and used family-wide totals so the spike is detectable at account level.
    assert zd["tickets_last_7d"] == 18
    assert zd["tickets_prev_7d"] == 7

    # The spike is real (18 >= 2*7) but concentrated on the non-primary instance,
    # so ticket_spike_on_primary must NOT fire it as primary-driven risk.
    primary_ids = primary_instance_ids(primary["hubspot"])
    assert primary_ids == {"au1-12345"}
    fired, evidence = ticket_spike_on_primary(zd, primary_ids)
    assert evidence["raw_spike"] is True
    assert fired is False
    assert evidence["primary_tickets"] == 2
    assert evidence["non_primary_tickets"] == 16

    # End-to-end: the account produces a suppressed-signal entry, and no P1
    # predictive-risk task is created from the test-instance noise.
    tasks, suppressed = orchestrate.evaluate("au1-12345", primary)
    assert any(s["signal"] == "support_ticket_spike" for s in suppressed)
    assert not any(t["rule_id"] == "predictive_risk_playbook" for t in tasks)


def test_hubspot_roster_scopes_to_csm_owner(monkeypatch):
    from adapters import config, sources

    monkeypatch.setenv("HUBSPOT_TOKEN", "test-token")
    monkeypatch.setenv("CS_ACCOUNT_SCOPE", "csm")
    monkeypatch.setenv("CS_CSM_OWNER_ID", "owner-123")
    captured = {}

    def fake_post(url, headers, body, timeout=12):
        captured["body"] = body
        return {"results": [{"properties": {"account_id": "AU1-5005"}}]}

    monkeypatch.setattr(config, "http_post", fake_post)
    assert sources.HUBSPOT.roster(limit=1) == ["au1-5005"]
    filters = captured["body"]["filterGroups"][0]["filters"]
    assert {item["propertyName"] for item in filters} == {"account_id", "hubspot_owner_id"}


def test_mcp_live_success_and_fixture_opt_in(monkeypatch):
    module = _load_mcp()
    monkeypatch.setattr(module, "_ADAPTERS", True)
    monkeypatch.setattr(module, "MCP_MODE", "live")
    monkeypatch.setattr(module._src.CHURN, "live", lambda: True)
    monkeypatch.setattr(module._src.CHURN, "score", lambda aid: {"churn_status": "Churned", "_source": "redshift-live"})

    live = module.tool_churn_get_score({"account_id": "AU1-5005"})
    assert live["_source"] == "redshift-live"

    monkeypatch.setattr(module._src.CHURN, "live", lambda: False)
    with pytest.raises(RuntimeError, match="fixture fallback"):
        module.tool_churn_get_score({"account_id": "acct_northwind"})

    monkeypatch.setattr(module, "MCP_MODE", "fixture")
    fixture = module.tool_churn_get_score({"account_id": "acct_northwind"})
    assert fixture["_source"] == "fixture"


def test_portfolio_snapshot_preserves_accounts_when_stripe_mapping_is_missing(monkeypatch):
    module = _load_mcp()
    monkeypatch.setattr(module, "_platform_account_list", lambda: None)
    monkeypatch.setattr(module, "_ADAPTERS", True)
    monkeypatch.setattr(module._src.HUBSPOT, "live", lambda: True)
    monkeypatch.setattr(module._src.HUBSPOT, "roster", lambda: ["AU1-10094"])
    monkeypatch.setattr(module._src.HUBSPOT, "account", lambda aid: {"name": "Account", "_source": "hubspot-live"})
    monkeypatch.setattr(module._src.ZENDESK, "live", lambda: False)
    monkeypatch.setattr(module._src.PENDO, "live", lambda: False)
    monkeypatch.setattr(module._src.CHURN, "live", lambda: False)
    monkeypatch.setattr(module._src.JIMINNY, "live", lambda: False)
    monkeypatch.setattr(module._src.STRIPE, "live", lambda: True)
    monkeypatch.setattr(module._src.STRIPE, "payment", lambda aid: (_ for _ in ()).throw(RuntimeError("Stripe customer not found")))
    monkeypatch.setattr(module, "MCP_MODE", "live")

    snapshot = module.tool_get_portfolio_snapshot({})
    row = snapshot["accounts"][0]
    assert row["account_id"] == "AU1-10094"
    assert row["hubspot"]["name"] == "Account"
    assert row["stripe"]["_source"] == "not_connected"
    assert row["sources"]["stripe"] == "not_connected"


def test_mcp_task_queue_uses_portfolio_snapshot(monkeypatch):
    module = _load_mcp()
    account = {
        "account_id": "au1-1",
        "hubspot": {"name": "At Risk", "segment": "Strategic", "contacts": [],
                    "instances": [{"instance_id": "au1-1", "instance_type": "primary"}]},
        "zendesk": {"sev1_open": 1, "by_instance": {"au1-1": 1}},
        "usage": {}, "churn": {}, "stripe": {}, "jiminny": {}, "sources": {},
    }
    monkeypatch.setattr(module, "_platform_task_queue", lambda: None)
    monkeypatch.setattr(module, "tool_get_portfolio_snapshot", lambda _: {"accounts": [account]})

    result = module.tool_get_task_queue({})

    assert result["reviewed"] == 1
    assert result["tasks"][0]["account"] == "At Risk"
    assert result["judge"]["verdict"] == "PASS"


def test_mcp_task_queue_prefers_compact_platform_queue(monkeypatch):
    module = _load_mcp()
    expected = {"reviewed": 25, "tasks": [{"account": "First"}],
                "suppressed": [], "automations": [],
                "judge": {"verdict": "PASS"}, "_source": "cs-platform-live-queue"}
    monkeypatch.setattr(module, "_platform_task_queue", lambda: expected)
    monkeypatch.setattr(module, "tool_get_portfolio_snapshot",
                        lambda _: (_ for _ in ()).throw(AssertionError("fallback called")))

    assert module.tool_get_task_queue({}) == expected


def test_mcp_exposes_compact_read_only_data_gaps_tool():
    module = _load_mcp()

    listed = module.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool = next(item for item in listed["result"]["tools"] if item["name"] == "get_data_gaps")

    assert tool["annotations"]["readOnlyHint"] is True
    assert "source coverage" in tool["description"]


def test_mcp_exposes_read_only_csm_brief_tool():
    module = _load_mcp()

    listed = module.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool = next(item for item in listed["result"]["tools"] if item["name"] == "get_csm_brief")

    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["inputSchema"]["properties"]["csm_owner"]["type"] == "string"
    assert "next governed task" in tool["description"]


def test_list_accounts_prefers_enriched_platform_rows(monkeypatch):
    module = _load_mcp()
    rows = [{"account_id": "au1-1", "name": "Account", "csm_owner": "Chris",
             "health": {"score": 90}, "lifecycle_stage": "Customer"}]
    monkeypatch.setattr(module, "_platform_account_list", lambda: rows)

    assert module.tool_list_accounts({}) == rows


def test_agent_validator_requires_queue_and_passing_judge():
    sys.path.insert(0, str(PLUGIN))
    from agent_runner import validate_answer

    accounts = {"au1-1": {"hubspot": {"name": "Northwind Traders"}, "churn": {"ml_churn_score": 0.72}}}
    queue = {"tasks": [{"account": "Northwind Traders", "priority": 1}], "judge": {"verdict": "PASS"}}
    assert any("not called" in finding for finding in validate_answer(
        "Northwind Traders is the priority action.", queue, accounts, False))

    failing_queue = {"tasks": [{"account": "Northwind Traders"}], "judge": {"verdict": "NEEDS_CHANGES"}}
    assert any("NEEDS_CHANGES" in finding for finding in validate_answer(
        "Northwind Traders needs review.", failing_queue, accounts, True))


def test_agent_social_reply_is_exempt_from_harness_validation():
    sys.path.insert(0, str(PLUGIN))
    from agent_runner import validate_answer

    accounts = {"au1-1": {"hubspot": {"name": "Northwind Traders", "arr_usd": 12345}}}
    queue = {"tasks": [], "judge": {"verdict": "UNKNOWN"}}
    greeting = "Good morning, Tiago. What would you like to look at, your priorities today or a specific account?"
    # Without the social flag a numberless greeting is fine, but a greeting that happened to
    # contain figures (or an unknown judge on a non-account-focused turn) would be blocked.
    numeric_greeting = "Good morning! Across 25 accounts, retention is 98% and 350 things need attention."
    assert validate_answer(numeric_greeting, queue, accounts, False, "hi jane", social=True) == []
    assert validate_answer(greeting, queue, accounts, False, "hi jane", social=True) == []


def test_agent_meeting_intent_detection():
    sys.path.insert(0, str(PLUGIN))
    import agent_runner

    for q in ("Prepare my meeting with Northwind",
              "prep me for the call with Globex",
              "get me ready for my QBR",
              "meeting brief for Umbrella",
              "help me prepare for the check-in"):
        assert agent_runner._is_meeting_intent(q), q

    for q in ("What are my top actions today?",
              "How is Northwind's health?",
              "Show me the expansion pipeline"):
        assert not agent_runner._is_meeting_intent(q), q


def test_agent_feedback_learning_guidance_is_safe_and_metadata_only(tmp_path, monkeypatch):
    sys.path.insert(0, str(PLUGIN))
    import agent_runner

    monkeypatch.setattr(agent_runner, "FEEDBACK_LOG", tmp_path / "missing.jsonl")
    assert agent_runner._feedback_learning_guidance() == ""

    log = tmp_path / "fb.jsonl"
    log.write_text(
        json.dumps({"rating": "needs_correction", "reason": "wrong_priority"}) + "\n" +
        json.dumps({"rating": "needs_correction", "reason": "wrong_priority"}) + "\n" +
        json.dumps({"rating": "helpful"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(agent_runner, "FEEDBACK_LOG", log)
    guidance = agent_runner._feedback_learning_guidance()
    assert "wrong priority" in guidance
    assert "Learning from recent CSM feedback" in guidance
    # Still metadata only: no customer/answer content leaks through.
    assert "subscription" not in guidance

    # Recency weighting: a reason that only appears in the OLDEST entries should rank
    # below one that dominates the most RECENT entries, even if raw counts are equal.
    log2 = tmp_path / "fb2.jsonl"
    lines = (
        [json.dumps({"rating": "needs_correction", "reason": "source_gap"})] * 3 +
        [json.dumps({"rating": "needs_correction", "reason": "missing_evidence"})] * 3
    )
    log2.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(agent_runner, "FEEDBACK_LOG", log2)
    g2 = agent_runner._feedback_learning_guidance()
    # missing_evidence is newest -> it should be listed first in the ranked reasons.
    assert g2.index("missing evidence") < g2.index("source gap")


def test_agent_tools_include_portfolio_snapshot_and_parse_frontmatter():
    import agent_runner

    agent = agent_runner.load_agent("cs-orchestrator")
    assert agent["frontmatter"]["name"] == "cs-orchestrator"
    assert agent["frontmatter"]["target"] == "github-copilot"
    assert "mcp-servers" in agent["frontmatter"]
    assert agent["frontmatter"]["disable-model-invocation"] == "true"
    assert "cs-stack/*" in agent["frontmatter"]["tools"]
    assert agent["frontmatter"]["agents"] == "[]"
    assert "execute" not in agent["frontmatter"]["tools"]
    assert "read" not in agent["frontmatter"]["tools"]
    assert "search" not in agent["frontmatter"]["tools"]
    assert "CS Agent**, an AI agent" in agent["system"]
    assert "general conversational AI agent" in agent["system"]
    assert "Do not refuse a question merely because it is outside" in agent["system"]
    assert "Use the governed workflow below only when" in agent["system"]
    assert "These are valid governed" in agent["system"]
    assert "search the repository for tools" in agent["system"]
    assert "provide the queue while either source is available" in agent["system"]
    assert "read-only MCP tools rather than shell" in agent["system"]
    assert "strict one-call" in agent["system"]
    assert "do not emit a progress preamble" in agent["system"]
    assert "answer in your own words" in agent["system"]
    assert "without using a prewritten response" in agent["system"]
    assert 'Treat "who am I?" as a question about the user' in agent["system"]
    assert "Never infer personal identity from workspace names" in agent["system"]
    assert "running in the CS Platform through Amazon Bedrock" in agent["system"]
    assert agent_runner.MODEL in agent["system"]
    for specialist in ("risk-analyst", "renewal-planner", "outreach-drafter",
                       "cs-playbook-judge"):
        specialist_agent = agent_runner.load_agent(specialist)
        assert "read" not in specialist_agent["frontmatter"]["tools"]
        assert "search" not in specialist_agent["frontmatter"]["tools"]
    accounts, tools, _state, _queue = agent_runner._tools_impl()
    assert "get_portfolio_snapshot" in tools
    snapshot = tools["get_portfolio_snapshot"][0]({})
    assert snapshot["reviewed"] == len(accounts)


def test_plugin_exposes_cs_stack_from_root_mcp_manifest():
    plugin = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))

    assert plugin["mcpServers"] == "./mcp.json"
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert mcp["mcpServers"]["cs-stack"]["args"] == [
        "${PLUGIN_ROOT}/mcp-servers/cs_stack_server.py"
    ]


def test_mcp_tools_are_read_only_and_do_not_expose_writeback():
    module = _load_mcp()

    listed = module.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = listed["result"]["tools"]

    assert "hubspot_push_cs_data" not in {tool["name"] for tool in tools}
    assert all(tool["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    } for tool in tools)


def test_agent_validator_scopes_numbers_to_mentioned_account():
    from agent_runner import validate_answer

    accounts = {
        "au1-1": {"hubspot": {"name": "Northwind Traders", "arr_usd": 480000}},
        "au1-2": {"hubspot": {"name": "Initech", "arr_usd": 350000}},
    }
    queue = {"tasks": [{"account": "Northwind Traders", "priority": 1,
                         "evidence": {"churn_score": 0.72}}],
             "judge": {"verdict": "PASS"}}
    findings = validate_answer("Northwind Traders is 72% risk and ARR is 350000.",
                              queue, accounts, True)
    assert any("unsupported numeric claims" in finding for finding in findings)


def test_agent_validator_allows_portfolio_aggregates():
    from agent_runner import validate_answer

    accounts = {
        f"au1-{index}": {"hubspot": {"name": name}}
        for index, name in enumerate(("Northwind Traders", "Initech", "Umbrella Ltd"), 1)
    }
    queue = {"reviewed": 25, "tasks": [{"account": "Northwind Traders", "priority": 1,
                                         "evidence": {"churn_score": 0.72}}],
             "suppressed": [], "judge": {"verdict": "PASS"}}
    findings = validate_answer("Northwind Traders is first; 25 accounts were reviewed.",
                              queue, accounts, True)
    assert not any("unsupported numeric claims" in finding for finding in findings)


def test_agent_validator_allows_policy_thresholds_not_customer_facts():
    from agent_runner import validate_answer

    accounts = {"au1-1": {"hubspot": {"name": "Northwind Traders"}}}
    queue = {"reviewed": 1, "tasks": [{"account": "Northwind Traders", "priority": 1,
                                         "evidence": {"days_since_last_visit": 180}}],
             "suppressed": [], "judge": {"verdict": "PASS"}}
    findings = validate_answer(
        "Northwind Traders needs review within 24 hours because the playbook uses a 180-day disengagement threshold.",
        queue, accounts, True)
    assert not any("unsupported numeric claims" in finding for finding in findings)


def test_agent_validator_allows_api_surge_policy_threshold():
    from agent_runner import validate_answer

    accounts = {"au1-1": {"hubspot": {"name": "Northwind Traders"}}}
    queue = {"reviewed": 1, "tasks": [], "judge": {"verdict": "PASS"}}
    findings = validate_answer(
        "No expansion signal is active; the playbook checks API velocity at 1.4x.",
        queue, accounts, True)
    assert not any("unsupported numeric claims" in finding for finding in findings)


def test_agent_validator_allows_rounded_usage_and_derived_health():
    from agent_runner import validate_answer

    accounts = {"au1-1": {"hubspot": {"name": "Ignite"},
                           "usage": {"days_since_last_visit": 3003},
                           "churn": {"churn_status": "Churned"}, "zendesk": {}, "stripe": {}}}
    queue = {"tasks": [{"account": "Ignite", "priority": 1,
                         "evidence": {"days_since_last_visit": 3003}}],
             "judge": {"verdict": "PASS"}}
    findings = validate_answer("Ignite has been inactive for about 3000 days and has health 40.",
                              queue, accounts, True)
    assert not any("unsupported numeric claims" in finding for finding in findings)


def test_agent_validator_rejects_wrong_account_churn_status():
    from agent_runner import validate_answer

    accounts = {
        "au1-1": {"hubspot": {"name": "Naval Group Australia"}, "churn": {"churn_status": "Churned"}},
        "au1-2": {"hubspot": {"name": "Ignite"}, "churn": {"ml_churn_score": 0.55, "computed": True}},
    }
    queue = {"reviewed": 2, "tasks": [{"account": "Naval Group Australia", "priority": 1,
                                         "evidence": {"churn_status": "Churned"}}],
             "suppressed": [], "judge": {"verdict": "PASS"}}
    findings = validate_answer(
        "Naval Group Australia is churned. Ignite is also churned and needs protection.",
        queue, accounts, True)
    assert any("unsupported churn status" in finding for finding in findings)


def test_agent_validator_accepts_hubspot_churned_lifecycle():
    from agent_runner import validate_answer

    accounts = {
        "au1-1": {
            "hubspot": {"name": "Naval Group Australia", "lifecycle_stage": "Churned Customer"},
            "churn": {"_source": "not_connected"},
        },
    }
    queue = {"reviewed": 1, "tasks": [{"account": "Naval Group Australia", "priority": 2,
                                         "evidence": {"lifecycle_stage": "Churned Customer"}}],
             "suppressed": [], "judge": {"verdict": "PASS"}}

    findings = validate_answer(
        "Naval Group Australia is churned and needs lifecycle recovery.",
        queue, accounts, True)

    assert not any("unsupported churn status" in finding for finding in findings)


def test_agent_validator_does_not_block_expansion_on_unrelated_status():
    from agent_runner import validate_answer

    accounts = {
        "au1-1": {"hubspot": {"name": "Ignite"}, "churn": {"ml_churn_score": 0.55}},
    }
    queue = {"tasks": [], "judge": {"verdict": "PASS"}}
    findings = validate_answer("No expansion signal is active; Ignite is not eligible.",
                              queue, accounts, True, "Where are the expansion signals?")
    assert not any("unsupported churn status" in finding for finding in findings)


def test_owner_directory_and_named_owner_brief_are_grounded():
    from agent_runner import _owner_account_answer

    accounts = {
        "au1-1": {"hubspot": {"name": "Account A", "csm_owner": "Chris Coombs",
                                  "lifecycle_stage": "Customer", "arr_usd": 100},
                   "usage": {}, "zendesk": {}, "stripe": {}, "churn": {}},
        "au1-2": {"hubspot": {"name": "Account B", "csm_owner": "Other Owner"},
                   "usage": {}, "zendesk": {}, "stripe": {}, "churn": {}},
    }
    queue = {"tasks": [{"account": "Account A", "priority": 5,
                         "recommended_action": "Update contacts", "due_on": "2026-10-02"}]}

    directory = _owner_account_answer("Who are the portfolio owners?", accounts, queue)
    brief = _owner_account_answer("I am Chris Coombs. Show my accounts and next task.", accounts, queue)
    follow_up = _owner_account_answer("What should I work on next?", accounts, queue,
                                      csm_owner="Chris Coombs")
    ownership = _owner_account_answer("Do I own Account B?", accounts, queue,
                                      csm_owner="Chris Coombs")
    all_owners = _owner_account_answer("Who are all portfolio owners?", accounts, queue,
                                       csm_owner="Chris Coombs")

    assert "Chris Coombs" in directory and "Account A" in directory
    assert "Other Owner" in directory and "Account B" in directory
    assert "Account A" in brief
    assert "Update contacts" in brief
    assert "Account B" not in brief
    assert "Account A" in follow_up
    assert "Account B" not in follow_up
    assert ownership == "No. **Account B** is owned by **Other Owner**, not **Chris Coombs**."
    assert "Chris Coombs" in all_owners and "Account A" in all_owners
    assert "Other Owner" in all_owners and "Account B" in all_owners
    # New conversational format: no em dashes, ends with a follow-up question.
    assert "—" not in all_owners and "–" not in all_owners
    assert "?" in all_owners


def test_identity_conversation_does_not_load_actions():
    from agent_runner import _conversation_answer

    answer = _conversation_answer("Who are you?", csm_owner="Chris Coombs")

    assert "Jane" in answer
    assert "Customer Success specialist" in answer
    assert "Chris Coombs" in answer
    assert "Priority" not in answer


def test_structured_actions_include_owner_sla_source_and_gaps():
    from agent_runner import _structured_actions

    queue = {"tasks": [{"account": "Northwind Traders", "segment": "Strategic",
                         "priority": 1, "mandate": "MUST_PROTECT",
                         "trigger": "Risk", "evidence": {"churn_score": 0.72},
                         "recommended_action": "Review account"}]}
    accounts = {"au1-1": {"hubspot": {"name": "Northwind Traders", "csm_owner": "Chris",
                                        "contacts": []},
                           "sources": {"hubspot": "live", "zendesk": "not_live"}}}
    action = _structured_actions(queue, accounts)[0]
    assert action["owner"] == "Chris"
    assert action["sla"] == "within 24 hours"
    assert action["source"]["zendesk"] == "not_live"
    assert "missing contact roles" in action["data_gaps"][-1]


def test_no_live_health_does_not_prepare_healthy_writeback():
    import engine

    payload = engine._writeback_payload({}, {"score": 100, "band": "green", "computable": False}, [])
    assert payload["cs_health_score"] is None
    assert payload["cs_risk_status"] == "no_data"
    assert payload["cs_active_playbook"] is None


def test_kpis_include_task_lifecycle_metrics(monkeypatch, tmp_path):
    import orchestrate
    import engine

    previous_provider = orchestrate._ACCOUNT_PROVIDER
    monkeypatch.setenv("CS_TASK_EVENTS_FILE", str(tmp_path / "task-events.jsonl"))
    task = {"task_id": "task-1", "created_on": "2026-09-23", "due_on": "2026-09-24"}
    (tmp_path / "task-events.jsonl").write_text(json.dumps({
        "task_id": "task-1", "status": "completed", "completed_at": "2026-09-24"
    }) + "\n")
    metrics = engine._task_metrics([task])
    assert metrics["completed_tasks"] == 1
    assert metrics["sla_adherence_pct"] == 100.0
    assert metrics["average_task_age_days"] >= 0


def test_kpis_exclude_completed_tasks_from_open_capacity(monkeypatch, tmp_path):
    import engine

    monkeypatch.setenv("CS_TASK_EVENTS_FILE", str(tmp_path / "task-events.jsonl"))
    task = {"task_id": "task-1", "created_on": "2026-09-23", "due_on": "2026-09-24"}
    (tmp_path / "task-events.jsonl").write_text(json.dumps({
        "task_id": "task-1", "status": "completed", "completed_at": "2026-09-24"
    }) + "\n")
    metrics = engine._task_metrics([task])
    assert metrics["completed_tasks"] == 1
    assert metrics["open_tasks"] == 0


def test_datagaps_keeps_zero_arr_as_present(monkeypatch):
    import engine

    account = {"hubspot": {"name": "Zero ARR", "arr_usd": 0, "segment_label": "Scaled",
                            "renewal_date": "2026-12-01", "csm_owner": "Owner",
                            "subscription_type": "Month to Month", "contacts": []},
               "sources": {"hubspot": "live", "zendesk": "live", "stripe": "live", "usage": "live"}}
    engine.orchestrate.set_account_provider(lambda: {"au1-zero": account})
    try:
        row = engine.datagaps()["accounts"][0]
        assert "ARR" not in row["missing_fields"]
    finally:
        engine.orchestrate.set_account_provider(engine.orchestrate._load)


def test_playbook_approval_requires_quality_gates(monkeypatch, tmp_path):
    import server

    proposal_path = tmp_path / "proposals.jsonl"
    monkeypatch.setattr(server, "PLAYBOOK_PROPOSALS", proposal_path)
    proposal = server._record_playbook_proposal({
        "title": "Approval gate", "rationale": "Must prove quality before release.",
    })
    with pytest.raises(ValueError, match="quality gates"):
        server._record_playbook_review(proposal["proposal_id"], {
            "decision": "approve_for_implementation",
            "reviewed_by": "CS Leadership", "review_note": "Reviewed",
        })


def test_judge_rejects_suppressed_risk_and_wrong_priority():
    from playbook_judge import judge

    accounts = {"au1-1": {
        "hubspot": {"name": "Umbrella", "segment": "Strategic",
                    "instances": [{"instance_id": "p", "instance_type": "primary"},
                                   {"instance_id": "d", "instance_type": "test"}]},
        "zendesk": {"tickets_last_7d": 18, "tickets_prev_7d": 4,
                    "by_instance": {"p": 2, "d": 16}},
        "stripe": {}, "usage": {}, "churn": {},
    }}
    tasks = [{"account": "Umbrella", "segment": "Strategic", "priority": 3,
              "mandate": "MUST_PROTECT", "rule_id": "predictive_risk_playbook",
              "trigger": "Predictive Risk Playbook (24h SLA)",
              "evidence": {"drivers": ["ticket_spike(4->18)"]},
              "draft_message": "Review the account."}]
    result = judge(tasks, accounts)
    rules = {item["rule"] for item in result["violations"]}
    assert result["verdict"] == "NEEDS_CHANGES"
    assert "suppression" in rules
    assert "priority_mapping" in rules


def test_judge_rejects_missing_expected_task():
    from playbook_judge import judge
    import orchestrate

    accounts = orchestrate.load_accounts()
    expected = orchestrate.orchestrate()["tasks"]
    assert expected
    result = judge(expected[1:], accounts)
    assert result["verdict"] == "NEEDS_CHANGES"
    assert any(item["rule"] == "task_completeness" for item in result["violations"])


def test_grounding_hook_uses_structured_account_evidence():
    hook = PLUGIN / "hooks" / "scripts" / "grounding-gate.py"
    packet = {
        "answer": "Northwind Traders is 72% risk and ARR is 350000.",
        "actions": [{"account": "Northwind Traders", "evidence": {"churn_score": 0.72, "arr_usd": 480000}}],
    }
    proc = subprocess.run([sys.executable, str(hook)], input=json.dumps(packet),
                          capture_output=True, text=True)
    assert "350000" in proc.stderr
    assert "WARNING" in proc.stderr


def test_grounding_gate_is_not_registered_as_a_stop_hook():
    manifest = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    package = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    openplugin = json.loads((PLUGIN / ".plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert openplugin["name"] == package["name"] == "cs-orchestrator"
    assert openplugin["version"] == package["version"]
    assert manifest["hooks"] == {}


def test_bedrock_runner_delegation_and_correction(monkeypatch):
    import agent_runner

    state = {"queue_called": False}
    queue = {"tasks": [{"account": "Northwind Traders", "priority": 1,
                         "mandate": "MUST_PROTECT", "evidence": {"churn_score": 0.72}}],
             "judge": {"verdict": "PASS"}}
    accounts = {"au1-1": {"hubspot": {"name": "Northwind Traders", "csm_owner": "Chris",
                                        "contacts": []}, "sources": {"hubspot": "live"},
                           "churn": {"ml_churn_score": 0.72}}}

    def queue_tool(_):
        state["queue_called"] = True
        return queue

    tools = (accounts, {
        "get_task_queue": (queue_tool, "queue"),
    }, state, queue)
    monkeypatch.setattr(agent_runner, "_tools_impl", lambda: tools)
    monkeypatch.setattr(agent_runner, "_client", lambda: type("Client", (), {"meta": type("Meta", (), {"region_name": "us-east-1"})()})())
    monkeypatch.setattr(agent_runner, "MODEL", "test-model")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return "Northwind Traders has 99% churn risk."
        state["queue_called"] = True
        return "Northwind Traders is the Priority 1 action."

    monkeypatch.setattr(agent_runner, "_run_agent", fake_run)
    result = agent_runner.run("What should I do today?")

    assert result["ok"] is True
    assert len(calls) == 2
    assert result["actions"][0]["account"] == "Northwind Traders"
    assert result["actions"][0]["sla"] == "within 24 hours"


def test_bedrock_runner_records_real_delegation(monkeypatch):
    import agent_runner

    class FakeClient:
        def __init__(self):
            self.calls = 0

    client = FakeClient()
    tools = ({}, {}, {})
    transcript = []

    def fake_converse(_client, system, messages, specs):
        client.calls += 1
        if client.calls == 1:
            return {"stopReason": "tool_use", "output": {"message": {"role": "assistant", "content": [{
                "toolUse": {"toolUseId": "sub-1", "name": "run_subagent", "input": {"agent": "risk-analyst", "task": "Assess Northwind."}}}]}}}
        return {"stopReason": "end_turn", "output": {"message": {"role": "assistant", "content": [{"text": "Northwind Traders is the priority action."}]}}}

    monkeypatch.setattr(agent_runner, "_converse", fake_converse)
    result = agent_runner._run_agent(client, {"name": "cs-orchestrator", "system": "test"},
                                     "Assess the portfolio.", tools, [], transcript)
    assert result == "Northwind Traders is the priority action."
    assert any(item.get("delegate") == "cs-orchestrator -> risk-analyst" for item in transcript)


def test_platform_api_and_ui_contracts(monkeypatch):
    import orchestrate

    previous_provider = orchestrate._ACCOUNT_PROVIDER
    import server

    monkeypatch.setattr(server.engine, "integrations", lambda: {"connectors": [{"system": "Churn Model (Redshift)", "direction": "read-only"}]})
    monkeypatch.setattr(server.engine, "writeback", lambda account_id, apply=False: {
        "audit_id": "audit-test", "account_id": account_id, "apply_requested": apply,
        "result": {"mode": "dry-run"},
    })

    http_server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", http_server.server_address[1])
    try:
        connection.request("GET", "/api/integrations")
        response = connection.getresponse()
        assert response.status == 200
        body = json.loads(response.read())
        assert body["connectors"][0]["direction"] == "read-only"

        connection.request("POST", "/api/accounts/AU1-5005/writeback",
                           body=json.dumps({}), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        body = json.loads(response.read())
        assert body["result"]["mode"] == "dry-run"

        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode()
        assert response.status == 200
        assert "Ask the CS Agent" in html
        assert "Churn Model (Redshift)" in html or "Integrations" in html
    finally:
        connection.close()
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)
        orchestrate.set_account_provider(previous_provider)


def test_agent_feedback_records_metadata_without_answer_content(monkeypatch, tmp_path):
    import server

    feedback_path = tmp_path / "feedback.jsonl"
    monkeypatch.setattr(server, "FEEDBACK_LOG", feedback_path)
    result = server._record_agent_feedback({
        "rating": "needs_correction",
        "reason": "wrong priority",
        "question": "Which customer should I call?",
        "action_count": 3,
        "judge_verdict": "PASS",
        "answer": "Private customer answer must not be stored",
    })
    stored = feedback_path.read_text()
    assert result["recorded"] is True
    assert "wrong priority" in stored
    assert "Private customer answer" not in stored
    assert "question_hash" in stored


def test_agent_feedback_summary_is_anonymized(monkeypatch, tmp_path):
    import server

    feedback_path = tmp_path / "feedback.jsonl"
    monkeypatch.setattr(server, "FEEDBACK_LOG", feedback_path)
    server._record_agent_feedback({"rating": "needs_correction", "reason": "wrong_priority", "question": "q"})
    server._record_agent_feedback({"rating": "helpful", "question": "q2"})
    summary = server._feedback_summary()
    assert summary["ratings"] == {"helpful": 1, "needs_correction": 1}
    assert summary["correction_reasons"] == {"wrong_priority": 1}
    assert summary["improvement_candidates"][0]["category"] == "wrong_priority"
    assert summary["privacy"] == "metadata-only"


def test_feedback_summary_get_route_matches_ui_contract(monkeypatch, tmp_path):
    import server

    feedback_path = tmp_path / "feedback.jsonl"
    monkeypatch.setattr(server, "FEEDBACK_LOG", feedback_path)
    server._record_agent_feedback({"rating": "helpful", "question": "q"})
    assert server._feedback_summary()["ratings"]["helpful"] == 1


def test_playbook_proposal_is_pending_and_does_not_change_rules(monkeypatch, tmp_path):
    import server

    proposal_path = tmp_path / "proposals.jsonl"
    monkeypatch.setattr(server, "PLAYBOOK_PROPOSALS", proposal_path)
    result = server._record_playbook_proposal({
        "category": "threshold",
        "title": "Raise Strategic risk threshold",
        "rationale": "Review threshold against CS outcomes.",
        "requested_by": "CS team",
    })
    assert result["status"] == "pending_review"
    assert result["review_stage"] == "CS Leadership"
    assert result["review_checklist"]["evidence_verified"] is False
    assert result["rule_section"] == "Other"
    assert "pending_review" in proposal_path.read_text()


def test_playbook_proposal_review_records_approval_audit(monkeypatch, tmp_path):
    import server

    proposal_path = tmp_path / "proposals.jsonl"
    monkeypatch.setattr(server, "PLAYBOOK_PROPOSALS", proposal_path)
    proposal = server._record_playbook_proposal({
        "title": "Evidence gate", "rationale": "Every risk action needs provenance.",
        "rule_section": "Predictive Risk Playbook", "evidence_source": "Zendesk + Redshift",
        "quality_risk": "Could delay low-confidence actions.", "test_cases": "Missing source blocks PASS.",
    })
    reviewed = server._record_playbook_review(proposal["proposal_id"], {
        "decision": "approve_for_implementation", "reviewed_by": "CS Leadership",
        "review_note": "Evidence and rollback reviewed.",
        "review_checklist": {
            "evidence_verified": True, "policy_conflict_checked": True,
            "tests_added": True, "rollback_defined": True,
        },
    })
    assert reviewed["status"] == "approved_for_implementation"
    summary = server._playbook_summary()
    current = summary["proposals"][0]
    assert current["review_history"][0]["decision"] == "approve_for_implementation"
    assert current["review_note"] == "Evidence and rollback reviewed."


# --------------------------------------------------------------------------- #
# Phase 2: CS Agent identity + role scoping + clarifying questions
# --------------------------------------------------------------------------- #
def test_agent_scope_accounts_admin_sees_all_csm_sees_own():
    import agent_runner
    accts = {
        "A1": {"hubspot": {"name": "Northwind", "csm_owner_id": "89671346", "csm_owner": "Sam CSM"}},
        "A2": {"hubspot": {"name": "Globex", "csm_owner_id": "99999999", "csm_owner": "Other"}},
    }
    admin = {"role": "admin", "name": "Tiago", "email": "t@x", "owner_id": None}
    csm = {"role": "csm", "name": "Sam", "email": "s@x", "owner_id": "89671346"}

    scoped_admin, owner_admin = agent_runner._scope_accounts(accts, admin)
    assert len(scoped_admin) == 2 and owner_admin is None

    scoped_csm, owner_csm = agent_runner._scope_accounts(accts, csm)
    assert list(scoped_csm) == ["A1"]
    assert owner_csm == "Sam CSM"


def test_agent_csm_without_owner_id_sees_empty_book():
    import agent_runner
    accts = {"A1": {"hubspot": {"name": "Northwind", "csm_owner_id": "89671346"}}}
    csm = {"role": "csm", "name": "New Hire", "email": "n@x", "owner_id": None}
    scoped, owner = agent_runner._scope_accounts(accts, csm)
    assert scoped == {}  # no owner id -> empty, never the whole portfolio
    assert owner == "New Hire"


def test_agent_identity_line_states_role_and_scope():
    import agent_runner
    admin = {"role": "admin", "name": "Tiago"}
    csm = {"role": "csm", "name": "Sam"}
    assert "Admin" in agent_runner._identity_line(admin, 25, 25)
    assert "full portfolio of 25" in agent_runner._identity_line(admin, 25, 25)
    csm_line = agent_runner._identity_line(csm, 4, 25)
    assert "CSM" in csm_line and "book of 4" in csm_line


def test_agent_clarify_only_on_ambiguous():
    import agent_runner
    accts = {"A1": {"hubspot": {"name": "Northwind"}}}
    # Vague opener -> clarify offered
    assert agent_runner._maybe_clarify("help", accts, None) is not None
    assert agent_runner._maybe_clarify("hi", accts, None) is not None
    # Concrete intent -> no clarify
    assert agent_runner._maybe_clarify("what should I do today?", accts, None) is None
    assert agent_runner._maybe_clarify("top actions", accts, None) is None
    assert agent_runner._maybe_clarify("where are the expansion signals?", accts, None) is None
    # Named account -> no clarify
    assert agent_runner._maybe_clarify("how is Northwind", accts, None) is None
    # Viewing a specific account -> never clarify
    assert agent_runner._maybe_clarify("help", accts, "A1") is None


def test_agent_run_scopes_to_csm_book(monkeypatch):
    """A CSM principal must make run() load and scope accounts to their owner id."""
    import agent_runner
    full = {
        "A1": {"hubspot": {"name": "Northwind", "csm_owner_id": "89671346", "csm_owner": "Sam CSM", "contacts": []}, "sources": {"hubspot": "live"}},
        "A2": {"hubspot": {"name": "Globex", "csm_owner_id": "99999999", "csm_owner": "Other", "contacts": []}, "sources": {"hubspot": "live"}},
    }
    monkeypatch.setattr(agent_runner, "dataaccess", type("DA", (), {"all_accounts": staticmethod(lambda: full)}), raising=False)
    # Also patch the imported module symbol used inside run()
    import dataaccess as _da
    monkeypatch.setattr(_da, "all_accounts", lambda: full)

    captured = {}

    def fake_tools_impl(accounts_override=None):
        captured["scoped"] = accounts_override
        queue = {"tasks": [], "judge": {"verdict": "PASS"}, "suppressed": []}
        return (accounts_override or {}, {"get_task_queue": (lambda _: queue, "q")}, {"queue_called": True}, queue)

    monkeypatch.setattr(agent_runner, "_tools_impl", fake_tools_impl)
    monkeypatch.setattr(agent_runner, "_client", lambda: type("C", (), {"meta": type("M", (), {"region_name": "us-east-1"})()})())
    monkeypatch.setattr(agent_runner, "_run_agent", lambda *a, **k: "Nothing urgent in your book right now.")

    csm = {"role": "csm", "name": "Sam", "email": "s@x", "owner_id": "89671346"}
    result = agent_runner.run("What should I do today?", principal=csm)
    assert result["ok"] is True
    # Scoped to exactly the owned account
    assert list(captured["scoped"]) == ["A1"]
    # Identity/scope is returned as a separate field (shown once in the panel header),
    # not prepended to every answer.
    assert "CSM" in result["scope"] and "book of 1" in result["scope"]


def test_single_account_answer_does_not_require_queue():
    """A single-account explainer is grounded by that account's own tools and must not
    be blocked for skipping the portfolio task queue (regression: Kain Colasanto)."""
    from agent_runner import validate_answer
    queue = {"tasks": [{"account": "Other Co", "priority": 1}], "judge": {"verdict": "PASS"}}
    accounts = {"au1-9": {"hubspot": {"name": "Kain Colasanto, LLC", "arr_usd": 11504}}}
    answer = "Kain Colasanto, LLC is a small agency and looks stable today."
    # Portfolio question with no queue call -> should be flagged.
    portfolio_findings = validate_answer(answer, queue, accounts, queue_called=False,
                                         question="what are my top actions?", account_focus=False)
    assert any("get_task_queue" in f for f in portfolio_findings)
    # Single-account question -> queue not required, no block.
    account_findings = validate_answer(answer, queue, accounts, queue_called=False,
                                       question="tell me about Kain Colasanto", account_focus=True)
    assert not any("get_task_queue" in f for f in account_findings)
    assert not any("does not reference any account" in f for f in account_findings)


def test_expansion_score_rewards_healthy_engaged_and_suppresses_churned():
    import engine
    healthy = {"hubspot": {"name": "Grow Co", "arr_usd": 5000, "renewal_date": "2026-11-15",
                            "lifecycle_stage": "Customer"},
               "usage": {"days_since_last_visit": 2, "logins_last_7d": 40, "logins_prev_7d": 30},
               "entitlements": {"license_utilization_pct": 90}, "churn": {}, "zendesk": {"csat_30d": 95, "sev1_open": 0}}
    import os
    os.environ["CS_TODAY"] = "2026-09-23"
    es = engine.expansion_score(healthy, segment_median_arr=10000)
    assert es["computable"] and es["score"] >= 60 and es["band"] == "high"
    assert any("utilization" in d for d in es["drivers"])
    # A churned account is never an expansion candidate.
    churned = {"hubspot": {"name": "Gone Co", "lifecycle_stage": "Churned Customer"},
               "churn": {"churn_status": "Churned"}, "usage": {"days_since_last_visit": 1},
               "zendesk": {"csat_30d": 90}}
    ec = engine.expansion_score(churned)
    assert ec["score"] == 0


def test_success_plans_record_and_read(tmp_path, monkeypatch):
    import importlib, os
    os.environ["CS_SUCCESS_PLANS_FILE"] = str(tmp_path / "sp.jsonl")
    import server
    importlib.reload(server)
    p = server._record_success_plan({"account_id": "auX", "goal": "Grow placements",
                                     "baseline": 4.0, "target": 5.0, "deadline": "2026-12-31"},
                                    {"email": "csm@x"})
    assert p["plan_id"] and p["goal"] == "Grow placements"
    plans = server._success_plans_for("auX")
    assert len(plans) == 1 and plans[0]["target"] == 5.0
    # account_id + goal are required
    import pytest
    with pytest.raises(ValueError):
        server._record_success_plan({"account_id": "auX"}, None)


def test_hubspot_ingest_lifecycle_stages_default_and_configurable(monkeypatch):
    """The whole-book HubSpot scan ingests only 'customer' + churned by default (so the
    customer book is unchanged), but CS_HUBSPOT_LIFECYCLE_STAGES can add onboarding/lead
    stages so onboarding accounts enter the platform. This is why the Pooled > Onboarding
    view is empty until that stage is configured."""
    from adapters import sources

    monkeypatch.delenv("CS_HUBSPOT_LIFECYCLE_STAGES", raising=False)
    assert sources.HubSpot._ingest_lifecycle_stages() == ["customer", "20251280"]

    monkeypatch.setenv("CS_HUBSPOT_LIFECYCLE_STAGES", "customer,20251280,onboarding,lead")
    assert sources.HubSpot._ingest_lifecycle_stages() == [
        "customer", "20251280", "onboarding", "lead"]

    # Whitespace and empty entries are tolerated.
    monkeypatch.setenv("CS_HUBSPOT_LIFECYCLE_STAGES", " customer , onboarding , ")
    assert sources.HubSpot._ingest_lifecycle_stages() == ["customer", "onboarding"]
