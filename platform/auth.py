#!/usr/bin/env python3
"""CS Platform authentication.

Mirrors the ja-observe-ui identity model (Okta -> AWS Identity Center -> Cognito),
implemented for this stdlib http.server stack:

  * Production: OIDC Authorization Code + PKCE against the shared Cognito user pool
    (the same corporate SSO chain ja-observe uses). Config via AUTH_COGNITO_* env.
  * Local/demo: a dev-login fallback (AUTH_DEV_LOGIN=1) so the platform is fully
    runnable and demonstrable before a Cognito app client exists — the same spirit
    as ja-observe's basic-auth / AUTH_ADMIN_EMAILS dev path.

Sessions are a compact HMAC-signed, URL-safe token stored in an httpOnly cookie
(__Secure- when behind TLS). The signature is verified on every request; the role
is read ONLY from this signed value, never from client-editable data, so a user
cannot elevate their own privileges (same guarantee as ja-observe's signed JWT).

No secret is stored in code; AUTH_SECRET and the Cognito client secret come from env.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

import rbac

SESSION_COOKIE = "cs_session"
SESSION_TTL_S = 8 * 60 * 60  # 8 hours, matching ja-observe


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _secret() -> bytes:
    """HMAC signing secret. AUTH_SECRET in prod; a dev default only when dev-login
    is explicitly enabled (never a silent insecure default in production)."""
    s = os.environ.get("AUTH_SECRET")
    if s:
        return s.encode("utf-8")
    if dev_login_enabled():
        return b"cs-platform-dev-secret-not-for-production"
    raise RuntimeError("AUTH_SECRET is required (or set AUTH_DEV_LOGIN=1 for local dev).")


def dev_login_enabled() -> bool:
    return os.environ.get("AUTH_DEV_LOGIN", "").strip().lower() in ("1", "true", "yes", "on")


def cognito_configured() -> bool:
    return bool(os.environ.get("AUTH_COGNITO_ISSUER") and os.environ.get("AUTH_COGNITO_ID"))


def auth_required() -> bool:
    """Auth is enforced when either Cognito is configured or dev-login is on. If
    neither is set the platform runs open (unchanged legacy behaviour) so existing
    local/test flows are not broken until an operator opts in."""
    return cognito_configured() or dev_login_enabled()


def _secure_cookie() -> bool:
    # Behind TLS/Cloudflare set CS_SECURE_COOKIE=1 for __Secure- semantics.
    return os.environ.get("CS_SECURE_COOKIE", "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Signed session token
# --------------------------------------------------------------------------- #
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def make_session(email: str, name: str, role: str, owner_id: str | None,
                 groups: str = "") -> str:
    """Create a signed session token carrying the authenticated principal."""
    payload = {
        "email": email, "name": name, "role": role,
        "owner_id": owner_id, "groups": groups,
        "iat": int(time.time()), "exp": int(time.time()) + SESSION_TTL_S,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session(token: str | None) -> dict[str, Any] | None:
    """Verify and decode a session token. Returns the principal dict or None if the
    token is missing, tampered, or expired. Constant-time signature check."""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def cookie_header(token: str, max_age: int = SESSION_TTL_S) -> str:
    """Build a Set-Cookie header value for the session."""
    name = ("__Secure-" + SESSION_COOKIE) if _secure_cookie() else SESSION_COOKIE
    parts = [f"{name}={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if _secure_cookie():
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header() -> str:
    name = ("__Secure-" + SESSION_COOKIE) if _secure_cookie() else SESSION_COOKIE
    parts = [f"{name}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
    if _secure_cookie():
        parts.append("Secure")
    return "; ".join(parts)


def session_cookie_name() -> str:
    return ("__Secure-" + SESSION_COOKIE) if _secure_cookie() else SESSION_COOKIE


def parse_cookies(cookie_header_value: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not cookie_header_value:
        return out
    for part in cookie_header_value.split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Stateless OIDC flow state (PKCE verifier + state) — carried in a signed,
# short-lived cookie instead of server memory, so ANY task/replica behind the ALB
# can complete the /auth/callback (in-memory state broke with >1 task -> loop).
# --------------------------------------------------------------------------- #
FLOW_COOKIE = "cs_oidc_flow"
FLOW_TTL_S = 600  # 10 minutes to complete the SSO round-trip


def make_flow_token(state: str, verifier: str) -> str:
    payload = {"state": state, "verifier": verifier, "exp": int(time.time()) + FLOW_TTL_S}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_flow_token(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def flow_cookie_name() -> str:
    return ("__Secure-" + FLOW_COOKIE) if _secure_cookie() else FLOW_COOKIE


def flow_cookie_header(token: str) -> str:
    name = flow_cookie_name()
    parts = [f"{name}={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={FLOW_TTL_S}"]
    if _secure_cookie():
        parts.append("Secure")
    return "; ".join(parts)


def clear_flow_cookie_header() -> str:
    name = flow_cookie_name()
    parts = [f"{name}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
    if _secure_cookie():
        parts.append("Secure")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# OIDC (Cognito) Authorization Code + PKCE
# --------------------------------------------------------------------------- #
def _cognito_endpoints() -> dict[str, str]:
    issuer = os.environ.get("AUTH_COGNITO_ISSUER", "")
    region_match = issuer.split("cognito-idp.")[-1].split(".")[0] if "cognito-idp." in issuer else \
        os.environ.get("AWS_REGION", "ap-southeast-2")
    domain = os.environ.get("AUTH_COGNITO_DOMAIN", "")
    hosted = f"https://{domain}.auth.{region_match}.amazoncognito.com" if domain else issuer
    return {
        "authorize": f"{hosted}/oauth2/authorize",
        "token": f"{hosted}/oauth2/token",
        "userinfo": f"{hosted}/oauth2/userInfo",
        "logout": f"{hosted}/logout",
    }


def pkce_pair() -> tuple[str, str]:
    verifier = _b64e(os.urandom(48))
    challenge = _b64e(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
    ep = _cognito_endpoints()
    params = {
        "response_type": "code",
        "client_id": os.environ.get("AUTH_COGNITO_ID", ""),
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{ep['authorize']}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
    ep = _cognito_endpoints()
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": os.environ.get("AUTH_COGNITO_ID", ""),
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    secret = os.environ.get("AUTH_COGNITO_SECRET")
    if secret:
        basic = base64.b64encode(f"{os.environ.get('AUTH_COGNITO_ID','')}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    req = urllib.request.Request(ep["token"], data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_userinfo(access_token: str) -> dict[str, Any]:
    ep = _cognito_endpoints()
    req = urllib.request.Request(ep["userinfo"],
                                 headers={"Authorization": f"Bearer {access_token}"}, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def principal_from_userinfo(info: dict[str, Any]) -> dict[str, Any]:
    """Build the session principal from a Cognito userinfo response, resolving role
    from SSO groups (custom:groups) with the email break-glass fallback."""
    email = info.get("email") or info.get("username") or ""
    name = info.get("name") or info.get("preferred_username") or email
    groups = info.get("custom:groups") or info.get("groups") or ""
    if isinstance(groups, list):
        groups = "[" + ", ".join(str(g) for g in groups) + "]"
    role = rbac.resolve_role(email, groups)
    return {
        "email": email, "name": name, "role": role, "groups": groups,
        # Hard entitlement: may this user use the CS Platform at all?
        "has_access": rbac.has_cs_access(email, groups),
    }
