#!/usr/bin/env python3
"""Canonical account identity: the AUx-yyyyy / AUx_yyyyy shard+tenant key.

Every source (HubSpot external id, Stripe customer metadata, Pendo account id,
Zendesk org external id) carries this same key, so the multi-source join is a
lookup on one normalised string rather than a five-way vendor-ID mapping table.

Normalisation rule (prevents the classic "-" vs "_" / case false-negative):
  - lowercase
  - treat '-' and '_' as the same separator (canonical form uses '-')
  - strip surrounding whitespace

Instance hierarchy is derived from the id shape so the suppression hook can tell a
primary tenant instance from a secondary/test one:
  au1-12345           -> primary
  au1-12345-sbx       -> test/secondary (has a suffix beyond shard-tenant)
  au2-12345           -> secondary shard of the same tenant
"""

from __future__ import annotations

import re

# shard = 2+ letters then digits (au1, au2, us1, ...); tenant = digits/alnum.
_ID_RE = re.compile(r"^(?P<shard>[a-z]{2}\d+)-(?P<tenant>[a-z0-9]+)(?:-(?P<suffix>[a-z0-9]+))?$")
_TEST_SUFFIXES = {"sbx", "sandbox", "test", "dev", "uat", "staging", "demo"}


def normalise(account_ref: str) -> str:
    """Canonical form: lowercase, '_' -> '-', trimmed. Safe to call on any source's value."""
    if account_ref is None:
        return ""
    return account_ref.strip().lower().replace("_", "-")


def same_account(a: str, b: str) -> bool:
    """True if two source values refer to the same tenant (ignores instance suffix)."""
    pa, pb = parse(a), parse(b)
    return bool(pa and pb and pa["shard"] == pb["shard"] and pa["tenant"] == pb["tenant"]) \
        or normalise(a) == normalise(b)


def parse(account_ref: str) -> dict | None:
    """Break an AUx-yyyyy id into shard / tenant / suffix, or None if it doesn't match."""
    m = _ID_RE.match(normalise(account_ref))
    if not m:
        return None
    return {"shard": m.group("shard"), "tenant": m.group("tenant"), "suffix": m.group("suffix")}


def instance_type(account_ref: str) -> str:
    """Classify an instance id as 'primary', 'test', or 'secondary'."""
    p = parse(account_ref)
    if not p:
        return "unknown"
    if p["suffix"] in _TEST_SUFFIXES:
        return "test"
    if p["suffix"]:
        return "secondary"
    return "primary"


if __name__ == "__main__":
    for s in ["AU1_12345", "au1-12345", "au1-12345-sbx", "au2-12345", "us1-99"]:
        print(f"{s:18} -> norm={normalise(s):14} type={instance_type(s)} parse={parse(s)}")
