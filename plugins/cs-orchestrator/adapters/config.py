#!/usr/bin/env python3
"""Adapter configuration and HTTP helper.

Credentials are read ONLY from environment variables, never from code or the repo.
If a source's key is not set, its adapter reports `live()` == False and the caller
falls back to fixtures. This keeps the demo working offline and guarantees no secret
is ever committed.

Environment variables (set the ROTATED keys, not any that were shared in chat):
  HUBSPOT_TOKEN            HubSpot private app token (companies r/w, contacts r)
  STRIPE_KEY               Stripe RESTRICTED read-only key (rk_live_...), never sk_live_
  PENDO_KEY                Pendo integration key
  ZENDESK_SUBDOMAIN        e.g. jobadder
  ZENDESK_EMAIL            e.g. integrations@jobadder.com
  ZENDESK_TOKEN            Zendesk API token
  JIMINNY_KEY              Jiminny API key
    ROCKET_LANE_API_URL      Optional Rocket Lane base URL for onboarding status
    ROCKET_LANE_KEY          Optional Rocket Lane bearer key
    ROCKET_LANE_ACCOUNT_PATH Optional account route, default /accounts/{account_ref}
    PENDO_*_KEY              Optional explicit metadata keys for live expansion metrics:
                                                     PENDO_LICENSE_UTILIZATION_PCT_KEY, PENDO_ACTIVE_USERS_PCT_KEY,
                                                     PENDO_KEY_FEATURE_ADOPTION_PCT_KEY, PENDO_API_CALLS_LAST_7D_KEY,
                                                     PENDO_API_CALLS_PREV_7D_KEY, PENDO_LOGINS_LAST_7D_KEY,
                                                     PENDO_LOGINS_PREV_7D_KEY. Unmapped metrics remain unavailable.

  Churn model (Redshift Data API - no secret stored here; AWS creds come from
  the standard boto3 chain, e.g. AWS_PROFILE for the data platform account):
  REDSHIFT_DATABASE          Database holding the churn model output (required)
  REDSHIFT_WORKGROUP         Redshift Serverless workgroup name, OR
  REDSHIFT_CLUSTER_ID        provisioned cluster identifier (pick one)
  REDSHIFT_DB_USER           DB user for IAM auth (provisioned clusters only)
  REDSHIFT_SECRET_ARN        Secrets Manager ARN, alternative to REDSHIFT_DB_USER
    REDSHIFT_CHURN_TABLE       business-facing schema.view with the score (required)
    REDSHIFT_CHURN_ID_COLUMN   column holding the AUx-yyyyy ref (default: nk_ja_account)
    REDSHIFT_CHURN_MODE        "status" for calculated_churn_status, or "score" (default)
    REDSHIFT_CHURN_STATUS_COLUMN status column in status mode (default: calculated_churn_status)
    REDSHIFT_CHURN_SCORE_COLUMN column holding the 0-1 probability (default: churn_probability)
    REDSHIFT_CHURN_VERSION_COLUMN model version column (default: model_version)
    REDSHIFT_CHURN_DRIVER_1_COLUMN primary driver column (default: top_driver_1)
    REDSHIFT_CHURN_DRIVER_2_COLUMN secondary driver column (default: top_driver_2)
    REDSHIFT_CHURN_SCORED_AT_COLUMN score timestamp column (default: scored_at)
  AWS_REGION                 Redshift Data API region (default: ap-southeast-2)

Global:
  CS_USE_LIVE   "1"/"true" to enable live calls where a key exists (default: auto -
                live for any source whose key is present, fixtures otherwise).
    CS_ENV_FILE   Optional absolute path to a local credentials .env file when this
                                plugin is used from a different workspace; never commit this file.
    CS_CSM_OWNER_ID             Optional HubSpot owner ID to scope the portfolio to one CSM.
    CS_CSM_OWNER                Optional owner name for display/configuration; owner ID is preferred.
    CS_ACCOUNT_SCOPE             "all" (default) or "csm" for an owner-scoped book.
  CS_ALLOW_WRITE               "1" to permit HubSpot PATCH writes (default: off).
  CS_ALLOW_STRIPE_SECRET_KEY   "1" to permit STRIPE_KEY=sk_live_... (default: off;
                               use a restricted rk_live_... key instead).
    CS_FETCH_WORKERS              Concurrent account fetches (default: 2; keeps vendor
                                                                rate limits under control).
    CS_LOG_SOURCE_ERRORS          "1" to log per-account source lookup failures (default: off).
    CS_TASK_EVENTS_FILE            Optional append-only task status ledger path.
    CS_TASK_CAPACITY_PER_CSM       Capacity denominator for KPI utilisation (default: 20).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _load_dotenv() -> None:
    """Best-effort .env loader (no dependency): searches cwd and repo ancestors,
    never overrides a variable already set in the real environment."""
    configured = os.environ.get("CS_ENV_FILE")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.append(Path(__file__).resolve().parents[4] / "cs-orchestrator" / ".env")
    candidates += [Path.cwd() / ".env"]
    candidates += [p / ".env" for p in Path(__file__).resolve().parents]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            os.environ.setdefault(key, value)
        break  # first .env found wins


_load_dotenv()


def env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def live_enabled() -> bool:
    """Master switch. Defaults to True so any source with a key set goes live;
    set CS_USE_LIVE=0 to force fixtures everywhere even if keys are present."""
    v = os.environ.get("CS_USE_LIVE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def http_get(url: str, headers: dict[str, str], timeout: int = 12) -> Any:
    return _request_with_retry("GET", url, headers, None, timeout)


def _request_with_retry(method, url, headers, data, timeout, retries: int = 4):
    """Perform a request, retrying on HTTP 429 (rate limit) with exponential backoff."""
    import time as _time
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                ra = e.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra else delay
                except (ValueError, TypeError):
                    wait = delay
                _time.sleep(min(wait, 5.0))
                delay *= 2
                continue
            raise


def http_post(url: str, headers: dict[str, str], body: dict | str, timeout: int = 12) -> Any:
    """POST is used ONLY for vendor *search* endpoints that require POST but do not
    mutate (HubSpot CRM search, Stripe search-by-GET elsewhere). It must never target
    a create/update endpoint. As a guard, refuse POSTs to obvious write paths unless
    writes are explicitly enabled."""
    if not writes_allowed() and _looks_like_write(url):
        raise SourceError(f"Read-only mode: refusing POST to write endpoint {url}")
    data = (json.dumps(body) if isinstance(body, dict) else body).encode("utf-8")
    return _request_with_retry("POST", url, headers, data, timeout)


def http_patch(url: str, headers: dict[str, str], body: dict, timeout: int = 12) -> Any:
    """PATCH mutates. Hard-blocked unless CS_ALLOW_WRITE=1 (off by default)."""
    if not writes_allowed():
        raise SourceError(
            "Read-only mode: PATCH blocked. Set CS_ALLOW_WRITE=1 to permit writes "
            "(and use a write-scoped token)."
        )
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def writes_allowed() -> bool:
    """Master write switch. OFF by default: every adapter is read-only."""
    return os.environ.get("CS_ALLOW_WRITE", "").strip().lower() in ("1", "true", "yes", "on")


def stripe_secret_key_allowed() -> bool:
    """Explicit opt-in to bypass the sk_live_ guard. OFF by default; setting this
    does not change what the adapter does with the key (still read-only calls)."""
    return os.environ.get("CS_ALLOW_STRIPE_SECRET_KEY", "").strip().lower() in ("1", "true", "yes", "on")


def _looks_like_write(url: str) -> bool:
    u = url.lower()
    # Reads-via-POST that must be allowed in read-only mode: vendor 'search' endpoints
    # and HubSpot 'batch/read' (non-mutating). Anything else POSTed is treated as a write.
    read_via_post = ("search" in u) or ("batch/read" in u)
    return not read_via_post


def basic_auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


class SourceError(Exception):
    """Raised when a live source call fails; caller decides whether to fall back."""
