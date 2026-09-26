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


def resolve_role(email: str | None, groups: str | None = None) -> Role:
    """Resolve a user's CS role. Group-based admin wins first; then the email
    break-glass; otherwise the safe default of a scoped CSM.

    `groups` is the raw Cognito custom:groups string, e.g.
    "[CS-Platform-Admins, CS-Users]" or a list of group ids. Matched by substring
    like ja-observe (both name and id arrive in the SAML assertion)."""
    if groups:
        for g in _admin_groups():
            if g and g in groups:
                return ADMIN
    if email and email.strip().lower() in _admin_emails():
        return ADMIN
    return CSM


def is_admin(role: Role | None) -> bool:
    return role == ADMIN
