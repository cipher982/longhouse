"""A mangled signature in a URL is a refusal, not a server error.

`hmac.compare_digest` on two `str` raises TypeError when either holds a non-ASCII
character, and a query or path token is caller-controlled text.
"""

from types import SimpleNamespace

import pytest


@pytest.fixture
def signing(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'archive.db'}")
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("FERNET_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "non-ascii-signature-test-only")

    from zerg.auth import media_url_tokens

    settings = SimpleNamespace(jwt_secret="non-ascii-signature-test-only")
    monkeypatch.setattr(media_url_tokens, "get_settings", lambda: settings)
    return media_url_tokens


def test_media_url_token_with_non_ascii_signature_is_refused(signing):
    media_url_tokens = signing
    token = media_url_tokens.media_url_token(owner_id=1, sha256="a" * 64)
    payload = token.partition(".")[0]
    assert media_url_tokens.parse_media_url_token(f"{payload}.café", sha256="a" * 64) is None
    assert media_url_tokens.parse_media_url_token(token, sha256="a" * 64) == 1
