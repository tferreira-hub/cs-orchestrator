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
  CHURN_API_URL/CHURN_KEY  churn model endpoint (optional)

Global:
  CS_USE_LIVE   "1"/"true" to enable live calls where a key exists (default: auto -
                live for any source whose key is present, fixtures otherwise).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any


def env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def live_enabled() -> bool:
    """Master switch. Defaults to True so any source with a key set goes live;
    set CS_USE_LIVE=0 to force fixtures everywhere even if keys are present."""
    v = os.environ.get("CS_USE_LIVE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def http_get(url: str, headers: dict[str, str], timeout: int = 12) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url: str, headers: dict[str, str], body: dict | str, timeout: int = 12) -> Any:
    """POST is used ONLY for vendor *search* endpoints that require POST but do not
    mutate (HubSpot CRM search, Stripe search-by-GET elsewhere). It must never target
    a create/update endpoint. As a guard, refuse POSTs to obvious write paths unless
    writes are explicitly enabled."""
    if not writes_allowed() and _looks_like_write(url):
        raise SourceError(f"Read-only mode: refusing POST to write endpoint {url}")
    data = (json.dumps(body) if isinstance(body, dict) else body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


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


def _looks_like_write(url: str) -> bool:
    u = url.lower()
    # search endpoints are reads-via-POST; anything else POSTed is treated as a write.
    return "search" not in u


def basic_auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


class SourceError(Exception):
    """Raised when a live source call fails; caller decides whether to fall back."""
