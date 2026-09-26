"""Tests for CS Platform authentication, RBAC, and per-user account scoping."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLATFORM))


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
def test_rbac_defaults_to_scoped_csm(monkeypatch):
    import rbac
    monkeypatch.delenv("AUTH_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("CS_ADMIN_GROUPS", raising=False)
    assert rbac.resolve_role("someone@jobadder.com", None) == rbac.CSM
    assert rbac.is_admin(rbac.resolve_role("someone@jobadder.com", None)) is False


def test_rbac_admin_via_email_breakglass(monkeypatch):
    import rbac
    monkeypatch.setenv("AUTH_ADMIN_EMAILS", "boss@jobadder.com, lead@jobadder.com")
    assert rbac.resolve_role("boss@jobadder.com", None) == rbac.ADMIN
    assert rbac.resolve_role("BOSS@jobadder.com", None) == rbac.ADMIN  # case-insensitive
    assert rbac.resolve_role("other@jobadder.com", None) == rbac.CSM


def test_rbac_admin_via_group(monkeypatch):
    import rbac
    monkeypatch.setenv("CS_ADMIN_GROUPS", "CS-Platform-Admins")
    assert rbac.resolve_role("x@jobadder.com", "[CS-Platform-Admins, CS-Users]") == rbac.ADMIN
    assert rbac.resolve_role("x@jobadder.com", "[CS-Users]") == rbac.CSM


def test_rbac_no_admin_group_without_explicit_config(monkeypatch):
    """Secure by default: with CS_ADMIN_GROUPS unset, membership in a group that
    merely shares a placeholder name must NOT grant admin."""
    import rbac
    monkeypatch.delenv("CS_ADMIN_GROUPS", raising=False)
    monkeypatch.delenv("AUTH_ADMIN_EMAILS", raising=False)
    assert rbac.resolve_role("x@jobadder.com", "[CS-Platform-Admins, CS-Leadership]") == rbac.CSM


def test_rbac_admin_group_matches_by_id(monkeypatch):
    """Identity Center may send the group id rather than the name; substring match
    handles both (as in ja-observe)."""
    import rbac
    monkeypatch.setenv("CS_ADMIN_GROUPS", "a4d8e4c8-5041-7005-a1cf-87f1c635306b")
    assert rbac.resolve_role("x@jobadder.com", "[a4d8e4c8-5041-7005-a1cf-87f1c635306b]") == rbac.ADMIN


def test_has_cs_access_admin_user_and_denied(monkeypatch):
    """The hard entitlement gate: admin group OR user group OR break-glass email
    grants access; everyone else is denied. Role: admin group -> admin, else csm."""
    import rbac
    monkeypatch.setenv("CS_ADMIN_GROUPS", "CS-Platform-Admins")
    monkeypatch.setenv("CS_USER_GROUPS", "CS-Platform-Users")
    monkeypatch.delenv("AUTH_ADMIN_EMAILS", raising=False)
    # Admin group: access + admin role.
    assert rbac.has_cs_access("a@x.com", "[CS-Platform-Admins]") is True
    assert rbac.resolve_role("a@x.com", "[CS-Platform-Admins]") == rbac.ADMIN
    # User group: access + csm (own-book) role.
    assert rbac.has_cs_access("u@x.com", "[CS-Platform-Users]") is True
    assert rbac.resolve_role("u@x.com", "[CS-Platform-Users]") == rbac.CSM
    # JA Observe user without any CS group: DENIED.
    assert rbac.has_cs_access("o@x.com", "[JA-Observe-Users]") is False


def test_has_cs_access_secure_by_default(monkeypatch):
    """With no CS groups and no break-glass configured, access is denied."""
    import rbac
    monkeypatch.delenv("CS_ADMIN_GROUPS", raising=False)
    monkeypatch.delenv("CS_USER_GROUPS", raising=False)
    monkeypatch.delenv("AUTH_ADMIN_EMAILS", raising=False)
    assert rbac.has_cs_access("anyone@x.com", "[JA-Observe-Users]") is False


def test_has_cs_access_breakglass_email(monkeypatch):
    """Break-glass admin email grants access even with no CS group."""
    import rbac
    monkeypatch.delenv("CS_ADMIN_GROUPS", raising=False)
    monkeypatch.delenv("CS_USER_GROUPS", raising=False)
    monkeypatch.setenv("AUTH_ADMIN_EMAILS", "boss@jobadder.com")
    assert rbac.has_cs_access("boss@jobadder.com", "") is True
    assert rbac.resolve_role("boss@jobadder.com", "") == rbac.ADMIN


# --------------------------------------------------------------------------- #
# Session token
# --------------------------------------------------------------------------- #
def test_session_roundtrip_and_tamper(monkeypatch):
    import auth
    monkeypatch.setenv("AUTH_SECRET", "unit-test-secret")
    tok = auth.make_session("csm@jobadder.com", "A CSM", "csm", "owner-42", "[CS-Users]")
    p = auth.read_session(tok)
    assert p and p["email"] == "csm@jobadder.com" and p["role"] == "csm" and p["owner_id"] == "owner-42"
    # Tampering the payload invalidates the signature.
    body, _, sig = tok.partition(".")
    forged = body[:-2] + ("AA" if not body.endswith("AA") else "BB") + "." + sig
    assert auth.read_session(forged) is None
    # A wrong signature is rejected.
    assert auth.read_session(body + ".deadbeef") is None
    assert auth.read_session(None) is None


def test_session_expiry(monkeypatch):
    import auth
    monkeypatch.setenv("AUTH_SECRET", "unit-test-secret")
    monkeypatch.setattr(auth, "SESSION_TTL_S", -1)  # already expired
    tok = auth.make_session("x@jobadder.com", "X", "csm", None)
    assert auth.read_session(tok) is None


def test_role_cannot_be_forged_by_editing_cookie(monkeypatch):
    """A user cannot flip their own role to admin by editing the cookie payload —
    the signature won't verify, so read_session returns None (no elevation)."""
    import auth, json, base64
    monkeypatch.setenv("AUTH_SECRET", "unit-test-secret")
    tok = auth.make_session("csm@jobadder.com", "CSM", "csm", "owner-1")
    body, _, sig = tok.partition(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    payload["role"] = "admin"
    forged_body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    assert auth.read_session(f"{forged_body}.{sig}") is None


# --------------------------------------------------------------------------- #
# Per-user scoping in the engine
# --------------------------------------------------------------------------- #
def _fake_accounts():
    def mk(aid, owner_id, name):
        return {aid: {
            "hubspot": {"name": name, "segment": "Strategic", "arr_usd": 100000,
                        "csm_owner": name + " owner", "csm_owner_id": owner_id, "contacts": [],
                        "instances": [{"instance_id": aid, "instance_type": "primary"}]},
            "sources": {"hubspot": "live"}, "zendesk": {}, "usage": {}, "churn": {},
            "stripe": {}, "jiminny": {}, "onboarding": {},
        }}
    data = {}
    data.update(mk("au1-1", "owner-A", "Alpha"))
    data.update(mk("au1-2", "owner-A", "Alpha Two"))
    data.update(mk("au1-3", "owner-B", "Bravo"))
    return data


def test_engine_scopes_accounts_to_owner(monkeypatch):
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    # CSM owner-A sees only their two accounts.
    engine.set_principal({"email": "a@x.com", "name": "A", "role": "csm", "owner_id": "owner-A"})
    scoped = engine._scoped_accounts()
    assert set(scoped) == {"au1-1", "au1-2"}
    # Admin sees all three.
    engine.set_principal({"email": "boss@x.com", "name": "Boss", "role": "admin", "owner_id": None})
    assert set(engine._scoped_accounts()) == {"au1-1", "au1-2", "au1-3"}
    # No principal (open/legacy mode) sees all.
    engine.set_principal(None)
    assert set(engine._scoped_accounts()) == {"au1-1", "au1-2", "au1-3"}


def test_can_view_account_and_forbidden(monkeypatch):
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    engine.set_principal({"email": "a@x.com", "name": "A", "role": "csm", "owner_id": "owner-A"})
    assert engine.can_view_account("au1-1") is True     # owns it
    assert engine.can_view_account("au1-3") is False    # owned by B
    # account_detail on a non-owned account raises ForbiddenError (-> 403).
    import pytest
    with pytest.raises(engine.ForbiddenError):
        engine.account_detail("au1-3")
    engine.set_principal(None)  # reset for other tests


def test_admin_can_view_any_account(monkeypatch):
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    engine.set_principal({"email": "boss@x.com", "name": "Boss", "role": "admin", "owner_id": None})
    assert engine.can_view_account("au1-3") is True
    engine.set_principal(None)


def test_portfolio_summary_reports_scope(monkeypatch):
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    engine.set_principal({"email": "a@x.com", "name": "Alpha CSM", "role": "csm", "owner_id": "owner-A"})
    summary = engine.portfolio()["summary"]
    assert summary["account_scope"] == "csm"
    assert summary["accounts"] == 2
    assert summary["principal"]["role"] == "csm"
    engine.set_principal(None)
