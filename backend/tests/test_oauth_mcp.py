"""Unit tests for the MCP OAuth helpers.

The full token flow needs a live DB and is exercised end-to-end, but the
pure pieces — PKCE verification, refresh-token hashing, access-token shape —
are where subtle bugs hide, so they're covered here. These guard the refresh
token support that keeps claude.ai connected past the 24h access-token TTL."""
import base64
import hashlib
from types import SimpleNamespace

import jwt

from app.routers.auth import ALGORITHM, SECRET_KEY
from app.routers.oauth import (
    ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_TOKEN_TTL_SECONDS,
    _hash_refresh_token,
    _issue_access_token,
    _token_error,
    _verify_pkce,
)


def test_refresh_ttl_is_30_days():
    assert REFRESH_TOKEN_TTL_SECONDS == 30 * 24 * 3600


def test_hash_refresh_token_is_sha256_hex_and_deterministic():
    raw = "some-opaque-refresh-token"
    h = _hash_refresh_token(raw)
    assert h == hashlib.sha256(raw.encode()).hexdigest()
    assert len(h) == 64
    # Same input → same hash (lookup depends on this); different input → different.
    assert _hash_refresh_token(raw) == h
    assert _hash_refresh_token(raw + "x") != h


def test_pkce_s256_roundtrip():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert _verify_pkce(verifier, challenge, "S256") is True
    assert _verify_pkce("wrong-verifier", challenge, "S256") is False


def test_pkce_rejects_unknown_method():
    assert _verify_pkce("v", "v", "bogus") is False


def test_access_token_carries_audience_and_expiry():
    user = SimpleNamespace(id="11111111-1111-1111-1111-111111111111",
                           email="a@b.com", role="editor")
    audience = "https://meander-hid-dashboard.zeabur.app/mcp"
    token, expires_in = _issue_access_token(user, audience)
    assert expires_in == ACCESS_TOKEN_TTL_SECONDS
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM],
                         options={"verify_aud": False})
    # aud is what separates MCP tokens from web-session tokens in the middleware.
    assert payload["aud"] == audience
    assert payload["sub"] == str(user.id)
    assert payload["exp"] - payload["iat"] == ACCESS_TOKEN_TTL_SECONDS


def test_token_error_shape():
    resp = _token_error("invalid_grant", "nope")
    assert resp.status_code == 400
    assert resp.body == b'{"error":"invalid_grant","error_description":"nope"}'
