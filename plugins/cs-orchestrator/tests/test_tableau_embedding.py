"""Tests for Tableau embedding: connected-app config parsing and direct-trust JWT minting.

These verify the token conforms to Tableau's "Configure Connected Apps with Direct Trust"
spec (header alg/typ/kid/iss; claims iss/sub/aud/jti/iat/exp/scp; HS256 signature over
header.claims with the secret VALUE) WITHOUT needing a live Tableau site. Pure stdlib.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLATFORM))


_CA = {
    "TABLEAU_SERVER_URL": "https://10ax.online.tableau.com/",
    "TABLEAU_SITE": "csplatform",
    "TABLEAU_CA_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    "TABLEAU_CA_SECRET_ID": "22222222-2222-2222-2222-222222222222",
    "TABLEAU_CA_SECRET_VALUE": "s3cr3t-value-xyz",
}


def _b64d(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _configured(monkeypatch, **overrides):
    env = dict(_CA)
    env.update(overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Ensure optional vars are not leaking in from the environment.
    for k in ("TABLEAU_VIEWS", "TABLEAU_SCOPES", "TABLEAU_JWT_TTL_S"):
        if k not in overrides:
            monkeypatch.delenv(k, raising=False)
    import tableau
    return importlib.reload(tableau)


def test_is_configured_requires_all_core_fields(monkeypatch):
    t = _configured(monkeypatch)
    assert t.is_configured() is True
    # Drop the secret value -> not configured, and it is reported as missing.
    monkeypatch.delenv("TABLEAU_CA_SECRET_VALUE", raising=False)
    t = importlib.reload(t)
    assert t.is_configured() is False
    status = t.config_status()
    assert status["configured"] is False
    assert "TABLEAU_CA_SECRET_VALUE" in status["missing"]


def test_config_status_strips_trailing_slash_and_never_leaks_secret(monkeypatch):
    t = _configured(monkeypatch)
    status = t.config_status()
    assert status["server_url"] == "https://10ax.online.tableau.com"
    assert status["site"] == "csplatform"
    # The secret value must never appear anywhere in the client-facing status.
    assert "s3cr3t-value-xyz" not in json.dumps(status)


def test_views_parsing_label_and_bare_path(monkeypatch):
    t = _configured(
        monkeypatch,
        TABLEAU_VIEWS="CS Health=CSMDashboard/Health, Renewals=CSMDashboard/Renewals, PlainPath/View",
    )
    assert t.views() == [
        {"label": "CS Health", "path": "CSMDashboard/Health"},
        {"label": "Renewals", "path": "CSMDashboard/Renewals"},
        {"label": "PlainPath/View", "path": "PlainPath/View"},
    ]


def test_mint_jwt_header_and_claims_match_direct_trust_spec(monkeypatch):
    t = _configured(monkeypatch)
    now = 1_700_000_000
    res = t.mint_jwt("casey.cs@jobadder.com", now=now)
    header_b64, claims_b64, sig_b64 = res["token"].split(".")
    header = json.loads(_b64d(header_b64))
    claims = json.loads(_b64d(claims_b64))

    assert header == {
        "alg": "HS256", "typ": "JWT",
        "kid": _CA["TABLEAU_CA_SECRET_ID"],
        "iss": _CA["TABLEAU_CA_CLIENT_ID"],
    }
    assert claims["iss"] == _CA["TABLEAU_CA_CLIENT_ID"]
    assert claims["sub"] == "casey.cs@jobadder.com"
    assert claims["aud"] == "tableau"
    assert claims["iat"] == now
    assert claims["exp"] == now + 540  # default 9-minute TTL
    assert claims["scp"] == ["tableau:views:embed", "tableau:views:embed_authoring"]
    assert isinstance(claims["jti"], str) and len(claims["jti"]) >= 16
    assert res["exp"] == now + 540 and res["sub"] == "casey.cs@jobadder.com"


def test_mint_jwt_signature_is_valid_hs256_over_header_claims(monkeypatch):
    t = _configured(monkeypatch)
    res = t.mint_jwt("casey.cs@jobadder.com", now=1_700_000_000)
    header_b64, claims_b64, sig_b64 = res["token"].split(".")
    expected = base64.urlsafe_b64encode(
        hmac.new(_CA["TABLEAU_CA_SECRET_VALUE"].encode(),
                 f"{header_b64}.{claims_b64}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    assert hmac.compare_digest(sig_b64, expected)


def test_mint_jwt_jti_is_unique_per_token(monkeypatch):
    t = _configured(monkeypatch)
    now = 1_700_000_000
    j1 = json.loads(_b64d(t.mint_jwt("a@b.com", now=now)["token"].split(".")[1]))["jti"]
    j2 = json.loads(_b64d(t.mint_jwt("a@b.com", now=now)["token"].split(".")[1]))["jti"]
    assert j1 != j2


def test_mint_jwt_ttl_clamped_to_tableau_ceiling(monkeypatch):
    t = _configured(monkeypatch, TABLEAU_JWT_TTL_S="99999")  # absurd -> clamp to 600
    now = 1_700_000_000
    claims = json.loads(_b64d(t.mint_jwt("a@b.com", now=now)["token"].split(".")[1]))
    assert claims["exp"] - claims["iat"] == 600


def test_mint_jwt_requires_configuration(monkeypatch):
    import tableau
    for k in _CA:
        monkeypatch.delenv(k, raising=False)
    t = importlib.reload(tableau)
    assert t.is_configured() is False
    try:
        t.mint_jwt("x@y.com")
        assert False, "expected RuntimeError when not configured"
    except RuntimeError:
        pass


def test_mint_jwt_requires_username(monkeypatch):
    t = _configured(monkeypatch)
    try:
        t.mint_jwt("")
        assert False, "expected ValueError for empty username"
    except ValueError:
        pass
