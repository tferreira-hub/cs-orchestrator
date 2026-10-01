#!/usr/bin/env python3
"""Tableau embedding support for the CS Platform.

CS keeps Tableau as the source of truth for analytics (per the CS team) and embeds
the existing dashboards into the platform rather than replicating them. Embedding
uses the Tableau Embedding API v3 web component (<tableau-viz>) on the frontend and
a Connected App "direct trust" JWT, minted here, for seamless SSO — a CSM already
signed in to the CS Platform does not get a second Tableau login prompt.

Direct-trust JWT (per Tableau docs, "Configure Connected Apps with Direct Trust"):
  header : {alg: HS256, typ: JWT, kid: <secret id>, iss: <client id>}
  claims : {iss: <client id>, exp: <now+ttl>, jti: <unique>, aud: "tableau",
            sub: <tableau username/email>, scp: ["tableau:views:embed", ...]}
  signature: HMAC-SHA256 over "<b64url(header)>.<b64url(claims)>" with the secret VALUE.

Implemented with the Python standard library only (base64url + hmac), consistent with
auth.py — no third-party JWT dependency is added. All configuration comes from the
environment; no client id, secret, or URL is ever hardcoded. When the connected app
is not configured the platform reports an honest "not configured" state so the Reports
page can explain what is pending instead of failing opaquely.

Required environment (provided by the Tableau site admin):
  TABLEAU_SERVER_URL   e.g. https://10ax.online.tableau.com  (Tableau Cloud pod) or
                       https://tableau.yourco.com            (Tableau Server)
  TABLEAU_SITE         Tableau Cloud/Server site content-url (contentUrl); "" for the
                       Default site on Tableau Server.
  TABLEAU_CA_CLIENT_ID     Connected App ID (a.k.a. client id)        -> JWT iss
  TABLEAU_CA_SECRET_ID     Secret ID generated for the connected app  -> JWT header kid
  TABLEAU_CA_SECRET_VALUE  Secret VALUE (the signing key)             -> HMAC key
Optional:
  TABLEAU_VIEWS        Comma-separated list of views to show, each "Label=workbook/view"
                       (the Tableau content path, e.g. "CS Health=CSMDashboard/Health").
                       If a label is omitted the view path is used as the label.
  TABLEAU_JWT_TTL_S    JWT lifetime in seconds (default 540 = 9 min; Tableau requires
                       a short-lived token, max 10 min).
  TABLEAU_SCOPES       Comma-separated embedding scopes
                       (default "tableau:views:embed,tableau:views:embed_authoring").
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

AUDIENCE = "tableau"
DEFAULT_TTL_S = 540          # 9 minutes (Tableau caps direct-trust JWTs at 10 min)
DEFAULT_SCOPES = ("tableau:views:embed", "tableau:views:embed_authoring")


# --------------------------------------------------------------------------- #
# Configuration (env only — never hardcoded)
# --------------------------------------------------------------------------- #
def server_url() -> str:
    return os.environ.get("TABLEAU_SERVER_URL", "").strip().rstrip("/")


def site() -> str:
    # Tableau Cloud always has a site content-url; Tableau Server "Default" site is "".
    return os.environ.get("TABLEAU_SITE", "").strip()


def client_id() -> str:
    return os.environ.get("TABLEAU_CA_CLIENT_ID", "").strip()


def secret_id() -> str:
    return os.environ.get("TABLEAU_CA_SECRET_ID", "").strip()


def _secret_value() -> str:
    return os.environ.get("TABLEAU_CA_SECRET_VALUE", "").strip()


def jwt_ttl_s() -> int:
    raw = os.environ.get("TABLEAU_JWT_TTL_S", "").strip()
    try:
        ttl = int(raw) if raw else DEFAULT_TTL_S
    except ValueError:
        ttl = DEFAULT_TTL_S
    # Clamp to Tableau's hard ceiling (10 min) and a sane floor.
    return max(60, min(ttl, 600))


def scopes() -> list[str]:
    raw = os.environ.get("TABLEAU_SCOPES", "").strip()
    if not raw:
        return list(DEFAULT_SCOPES)
    return [s.strip() for s in raw.split(",") if s.strip()]


def views() -> list[dict[str, str]]:
    """Parse TABLEAU_VIEWS ("Label=workbook/view, ...") into [{label, path}].
    A bare "workbook/view" (no '=') uses the path as its own label."""
    raw = os.environ.get("TABLEAU_VIEWS", "").strip()
    out: list[dict[str, str]] = []
    if not raw:
        return out
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            label, _, path = item.partition("=")
            label, path = label.strip(), path.strip()
        else:
            label = path = item
        if path:
            out.append({"label": label or path, "path": path})
    return out


def is_configured() -> bool:
    """True only when every value needed to mint a valid, resolvable embed token is
    present: server URL + connected-app client id + secret id + secret value."""
    return bool(server_url() and client_id() and secret_id() and _secret_value())


def config_status() -> dict[str, Any]:
    """A non-secret description of the embedding config for the frontend. Never leaks
    the secret value; reports which required pieces are missing so the Reports page can
    show an honest, actionable 'not configured yet' state."""
    missing = [k for k, v in (
        ("TABLEAU_SERVER_URL", server_url()),
        ("TABLEAU_CA_CLIENT_ID", client_id()),
        ("TABLEAU_CA_SECRET_ID", secret_id()),
        ("TABLEAU_CA_SECRET_VALUE", _secret_value()),
    ) if not v]
    return {
        "configured": not missing,
        "server_url": server_url(),
        "site": site(),
        "views": views(),
        "missing": missing,
    }


# --------------------------------------------------------------------------- #
# Direct-trust JWT (HS256) — stdlib implementation
# --------------------------------------------------------------------------- #
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segment(obj: dict[str, Any]) -> str:
    return _b64url(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def mint_jwt(username: str, extra_scopes: list[str] | None = None,
             now: int | None = None) -> dict[str, Any]:
    """Mint a Tableau direct-trust embedding JWT for `username` (the user's Tableau
    identity — normally their email). Returns {token, exp, sub}.

    Raises RuntimeError if the connected app is not configured (callers should check
    is_configured() first and present the honest not-configured state)."""
    if not is_configured():
        raise RuntimeError("Tableau connected app is not configured.")
    if not username:
        raise ValueError("username is required to mint a Tableau embed token.")

    iat = int(now if now is not None else time.time())
    exp = iat + jwt_ttl_s()
    scp = extra_scopes if extra_scopes is not None else scopes()

    header = {"alg": "HS256", "typ": "JWT", "kid": secret_id(), "iss": client_id()}
    claims = {
        "iss": client_id(),
        "sub": username,
        "aud": AUDIENCE,
        "jti": str(uuid.uuid4()),   # unique per token; Tableau rejects reuse
        "iat": iat,
        "exp": exp,
        "scp": scp,
    }
    signing_input = f"{_segment(header)}.{_segment(claims)}"
    sig = hmac.new(_secret_value().encode("utf-8"),
                   signing_input.encode("ascii"), hashlib.sha256).digest()
    token = f"{signing_input}.{_b64url(sig)}"
    return {"token": token, "exp": exp, "sub": username}
