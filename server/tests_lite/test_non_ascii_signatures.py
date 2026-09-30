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
    from zerg.services import session_shares

    settings = SimpleNamespace(jwt_secret="non-ascii-signature-test-only")
    monkeypatch.setattr(media_url_tokens, "get_settings", lambda: settings)
    monkeypatch.setattr(session_shares.auth_deps, "JWT_SECRET", settings.jwt_secret)
    return media_url_tokens, session_shares


def test_media_url_token_with_non_ascii_signature_is_refused(signing):
    media_url_tokens, _ = signing
    token = media_url_tokens.media_url_token(owner_id=1, sha256="a" * 64)
    payload = token.partition(".")[0]
    assert media_url_tokens.parse_media_url_token(f"{payload}.café", sha256="a" * 64) is None
    assert media_url_tokens.parse_media_url_token(token, sha256="a" * 64) == 1


def test_share_token_with_non_ascii_signature_is_not_found(signing):
    _, session_shares = signing
    token = session_shares._build_token(7, "nonce")
    prefix = f"{session_shares.TOKEN_PREFIX}_"
    payload = token[len(prefix) :].split(".", 1)[0]
    with pytest.raises(session_shares.SessionShareNotFound):
        session_shares.parse_share_token(f"{prefix}{payload}.café")
    assert session_shares.parse_share_token(token) == 7
