#!/usr/bin/env python3
"""Role-Based Access Control for the CS Platform.

Mirrors the ja-observe-ui model (`src/lib/rbac.ts`): roles are resolved from the
authenticated user's SSO group membership (passed through the corporate IdP ->
AWS Identity Center -> Cognito `custom:groups` claim), with an email-based
break-glass fallback sourced from configuration, never hardcoded.

Two CS roles:
  - admin: Customer Success leadership / RevOps. Sees ALL accounts and portfolio-
           wide KPIs. Maps from the CS admin SSO group(s) or AUTH_ADMIN_EMAILS.
  - csm:   A Customer Success Manager. Sees ONLY the accounts they own (their book).
           This is the default for any authenticated user without an admin grant.

Role resolution is always performed from the SIGNED session (see auth.py) —
never from a client-editable value — so a user cannot elevate their own role.
"""

from __future__ import annotations

import os

Role = str  # "admin" | "csm"
ADMIN: Role = "admin"
CSM: Role = "csm"


def _admin_groups() -> list[str]:
    """CS admin SSO groups (display name or Identity Center group id). Configurable
    via CS_ADMIN_GROUPS (comma-separated group NAMES or Identity Center group IDs) so
    admin membership is explicit configuration, never a hardcoded default. Matching
    by both name and id mirrors how ja-observe pins its JA-Observe-* groups (both
    forms arrive in the SAML assertion).

    SECURE BY DEFAULT: when CS_ADMIN_GROUPS is unset there is NO admin group — admin
    can then only come from AUTH_ADMIN_EMAILS break-glass. This prevents a group that
    merely happens to share a placeholder name from silently granting admin."""
    raw = os.environ.get("CS_ADMIN_GROUPS", "")
    return [g.strip() for g in raw.split(",") if g.strip()]


def _admin_emails() -> set[str]:
    """Break-glass admin emails (comma-separated AUTH_ADMIN_EMAILS), lowercased.
    Configuration, not source — same pattern as ja-observe's EMAIL_ROLE_MAP."""
    raw = os.environ.get("AUTH_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _user_groups() -> list[str]:
    """CS normal-user SSO groups (Identity Center group names or ids). A member of
    one of these may ACCESS the CS Platform as a scoped CSM (sees only their own
    book). Configurable via CS_USER_GROUPS. Admin groups also grant access; they do
    not need to be listed here."""
    raw = os.environ.get("CS_USER_GROUPS", "")
    return [g.strip() for g in raw.split(",") if g.strip()]


def _in_any(groups: str | None, names: list[str]) -> bool:
    return bool(groups) and any(n and n in groups for n in names)


def has_cs_access(email: str | None, groups: str | None = None) -> bool:
    """Hard entitlement gate: may this user use the CS Platform at all?

    True only if the user is in a CS admin group, a CS user group, or the admin
    break-glass email list. Everyone else is DENIED entry (not silently treated as a
    scoped CSM). This is defense-in-depth behind the UI launcher, which already hides
    the CS Platform from non-entitled users — a direct-URL visitor still cannot get in.

    SECURE BY DEFAULT: if neither CS_ADMIN_GROUPS, CS_USER_GROUPS, nor
    AUTH_ADMIN_EMAILS is configured, access is denied (nothing grants it)."""
    if _in_any(groups, _admin_groups()) or _in_any(groups, _user_groups()):
        return True
    if email and email.strip().lower() in _admin_emails():
        return True
    return False


def resolve_role(email: str | None, groups: str | None = None) -> Role:
    """Resolve the CS role of a user who HAS access (call has_cs_access first).
    Admin group / break-glass email -> admin (all accounts); otherwise -> csm
    (own book). Matched by substring like ja-observe (name and id both appear in
    the SAML assertion)."""
    if _in_any(groups, _admin_groups()):
        return ADMIN
    if email and email.strip().lower() in _admin_emails():
        return ADMIN
    return CSM


def is_admin(role: Role | None) -> bool:
    return role == ADMIN
