"""Google ID-token verification for /api/auth/google (PyJWT against Google's JWKS)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from zerg.routers import auth_browser

CLIENT_ID = "web-client.apps.googleusercontent.com"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _google_keys(monkeypatch):
    monkeypatch.setattr(
        auth_browser,
        "get_settings",
        lambda: SimpleNamespace(google_client_id=CLIENT_ID, google_ios_client_id=None),
    )
    signing_key = SimpleNamespace(key=_KEY.public_key())
    monkeypatch.setattr(auth_browser, "_google_jwks", lambda: SimpleNamespace(get_signing_key_from_jwt=lambda _token: signing_key))


def _token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1234567890",
        "email": "owner@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, _KEY, algorithm="RS256", headers={"kid": "k1"})


def test_valid_token_returns_claims():
    claims = auth_browser._verify_google_id_token(_token())
    assert claims["email"] == "owner@example.com"
    assert claims["sub"] == "1234567890"


def test_bare_issuer_is_accepted():
    assert auth_browser._verify_google_id_token(_token(iss="accounts.google.com"))["sub"] == "1234567890"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else.apps.googleusercontent.com"},
        {"iss": "https://evil.example.com"},
        {"exp": int(time.time()) - 60},
        {"exp": None},
        {"sub": None},
    ],
)
def test_invalid_claims_are_rejected(overrides):
    with pytest.raises(HTTPException) as exc_info:
        auth_browser._verify_google_id_token(_token(**overrides))
    assert exc_info.value.status_code == 401


def test_foreign_signature_is_rejected():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
        {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "x", "iat": now, "exp": now + 600},
        other,
        algorithm="RS256",
    )
    with pytest.raises(HTTPException) as exc_info:
        auth_browser._verify_google_id_token(forged)
    assert exc_info.value.status_code == 401


def test_jwks_outage_is_a_503(monkeypatch):
    def _unreachable(_token):
        raise jwt.PyJWKClientConnectionError("down")

    monkeypatch.setattr(auth_browser, "_google_jwks", lambda: SimpleNamespace(get_signing_key_from_jwt=_unreachable))
    with pytest.raises(HTTPException) as exc_info:
        auth_browser._verify_google_id_token(_token())
    assert exc_info.value.status_code == 503
