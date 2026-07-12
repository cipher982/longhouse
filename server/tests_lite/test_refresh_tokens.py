"""Tests for refresh token lifecycle: create, rotate, reuse detection, revoke.

Uses in-memory SQLite with inline setup (no shared conftest).
"""

from datetime import UTC
from datetime import datetime
from unittest.mock import patch

from zerg.auth import refresh_tokens
from zerg.auth.principal import AuthenticatedUser
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.refresh_session import RefreshSession
from zerg.models.user import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_refresh.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, user_id=1, email="test@local"):
    user = User(id=user_id, email=email, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Tests: create
# ---------------------------------------------------------------------------


def test_create_returns_raw_token(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        raw = refresh_tokens.create(db, user_id=user.id)
        db.commit()

    assert isinstance(raw, str)
    assert len(raw) > 20  # URL-safe base64 of 32 bytes


def test_create_stores_hash_not_raw(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        raw = refresh_tokens.create(db, user_id=user.id)
        db.commit()

        row = db.query(RefreshSession).first()
        assert row is not None
        assert row.token_hash != raw
        assert row.token_hash == refresh_tokens._hash_token(raw)
        assert row.user_id == user.id
        assert row.family_id  # auto-generated


# ---------------------------------------------------------------------------
# Tests: rotate
# ---------------------------------------------------------------------------


def test_rotate_returns_new_token(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        raw1 = refresh_tokens.create(db, user_id=user.id)
        db.commit()

        result = refresh_tokens.rotate(db, raw1)
        db.commit()

    assert result is not None
    assert result.raw_token != raw1
    assert result.user_id == user.id


def test_rotate_marks_old_as_used(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        raw1 = refresh_tokens.create(db, user_id=user.id)
        db.commit()

        refresh_tokens.rotate(db, raw1)
        db.commit()

        old_row = db.query(RefreshSession).filter_by(token_hash=refresh_tokens._hash_token(raw1)).first()
        assert old_row.used_at is not None


def test_rotate_invalid_token_returns_none(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_user(db)
        result = refresh_tokens.rotate(db, "totally-fake-token")
        assert result is None


def test_rotate_chain_works(tmp_path):
    """Rotate three times in succession — each yields a valid new token."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        raw = refresh_tokens.create(db, user_id=user.id)
        db.commit()

        for _ in range(3):
            result = refresh_tokens.rotate(db, raw)
            db.commit()
            assert result is not None
            raw = result.raw_token

        # All tokens in the same family
        families = {r.family_id for r in db.query(RefreshSession).all()}
        assert len(families) == 1


# ---------------------------------------------------------------------------
# Tests: reuse detection
# ---------------------------------------------------------------------------


def test_reuse_outside_grace_revokes_family(tmp_path):
    """Presenting a used token after the grace window revokes the whole family."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        raw1 = refresh_tokens.create(db, user_id=user.id)
        db.commit()

        # Rotate once — raw1 is now "used"
        result = refresh_tokens.rotate(db, raw1)
        db.commit()
        assert result is not None

        # Simulate time passing beyond the grace window
        with patch.object(refresh_tokens, "REUSE_GRACE_SECONDS", 0):
            reuse_result = refresh_tokens.rotate(db, raw1)
            db.commit()

        assert reuse_result is None

        # The new token from the first rotation should also be revoked
        second_result = refresh_tokens.rotate(db, result.raw_token)
        assert second_result is None

        # All rows in the family should be revoked
        active = db.query(RefreshSession).filter(RefreshSession.revoked_at.is_(None)).count()
        assert active == 0


# ---------------------------------------------------------------------------
# Tests: expiry
# ---------------------------------------------------------------------------


def test_expired_token_returns_none(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        raw = refresh_tokens.create(db, user_id=user.id)
        db.commit()

        # Backdate the expiry
        row = db.query(RefreshSession).first()
        row.absolute_expires_at = refresh_tokens._utcnow()
        db.commit()

        result = refresh_tokens.rotate(db, raw)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: revoke
# ---------------------------------------------------------------------------


def test_revoke_family(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        refresh_tokens.create(db, user_id=user.id)
        db.commit()

        row = db.query(RefreshSession).first()
        count = refresh_tokens.revoke_family(db, row.family_id)
        db.commit()

    assert count == 1


def test_revoke_all_for_user(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        # Create two separate families
        refresh_tokens.create(db, user_id=user.id)
        refresh_tokens.create(db, user_id=user.id)
        db.commit()

        count = refresh_tokens.revoke_all_for_user(db, user.id)
        db.commit()

    assert count == 2


def test_revoked_token_returns_none(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        raw = refresh_tokens.create(db, user_id=user.id)
        db.commit()

        row = db.query(RefreshSession).first()
        refresh_tokens.revoke_family(db, row.family_id)
        db.commit()

        result = refresh_tokens.rotate(db, raw)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: cleanup
# ---------------------------------------------------------------------------


def test_cleanup_expired(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _seed_user(db)
        refresh_tokens.create(db, user_id=user.id)
        db.commit()

        # Backdate
        row = db.query(RefreshSession).first()
        row.absolute_expires_at = refresh_tokens._utcnow()
        db.commit()

        deleted = refresh_tokens.cleanup_expired(db)
        db.commit()

    assert deleted == 1


# ---------------------------------------------------------------------------
# Tests: HTTP-level refresh endpoint
# ---------------------------------------------------------------------------


def test_refresh_endpoint_issues_new_tokens(tmp_path):
    """POST /auth/refresh with a valid RT cookie returns a new AT + rotated RT."""
    import os

    os.environ.setdefault("AUTH_DISABLED", "0")

    SessionLocal = _make_db(tmp_path)
    from zerg.main import api_app

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    from zerg.database import get_db

    api_app.dependency_overrides[get_db] = _override_db

    raw_rt = refresh_tokens._generate_token()

    from fastapi.testclient import TestClient

    user = AuthenticatedUser(id=1, email="test@local", created_at=datetime.now(UTC))
    with patch(
        "zerg.routers.auth_browser.rotate_refresh",
        return_value={"status": "rotated", "user": user, "commit_seq": "2"},
    ):
        client = TestClient(api_app)
        resp = client.post("/auth/refresh", cookies={"longhouse_refresh": raw_rt})

    api_app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["expires_in"] == 600  # 10 minutes


def test_refresh_endpoint_routes_rotation_through_catalog(tmp_path):
    """POST /auth/refresh sends only hashes through the catalog boundary."""
    import os

    os.environ.setdefault("AUTH_DISABLED", "0")

    SessionLocal = _make_db(tmp_path)
    from zerg.main import api_app

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    from zerg.database import get_db

    api_app.dependency_overrides[get_db] = _override_db

    raw_rt = refresh_tokens._generate_token()
    observed: dict = {}

    def _rotate_refresh(**params):
        observed.update(params)
        return {
            "status": "rotated",
            "user": AuthenticatedUser(id=1, email="test@local", created_at=datetime.now(UTC)),
            "commit_seq": "2",
        }

    from fastapi.testclient import TestClient

    with patch("zerg.routers.auth_browser.rotate_refresh", side_effect=_rotate_refresh):
        client = TestClient(api_app)
        resp = client.post("/auth/refresh", cookies={"longhouse_refresh": raw_rt})

    api_app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert observed["token_hash"] == refresh_tokens._hash_token(raw_rt)
    assert observed["next_token_hash"] != observed["token_hash"]


def test_refresh_endpoint_rejects_missing_cookie(tmp_path):
    """POST /auth/refresh without a cookie returns 401."""
    SessionLocal = _make_db(tmp_path)
    from zerg.main import api_app

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    from zerg.database import get_db

    api_app.dependency_overrides[get_db] = _override_db

    from fastapi.testclient import TestClient

    client = TestClient(api_app)
    resp = client.post("/auth/refresh")

    api_app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 401


def test_logout_endpoint_routes_refresh_family_revoke_through_catalog(tmp_path):
    """POST /auth/logout revokes by hash through the catalog boundary."""
    SessionLocal = _make_db(tmp_path)
    from zerg.main import api_app

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    from zerg.database import get_db

    api_app.dependency_overrides[get_db] = _override_db

    raw_rt = refresh_tokens._generate_token()
    observed: dict = {}

    from fastapi.testclient import TestClient

    def _revoke(**params):
        observed.update(params)
        return {"found": True, "changed": True, "revoked_count": 1, "commit_seq": "2"}

    with patch("zerg.routers.auth_browser.revoke_refresh_family", side_effect=_revoke):
        client = TestClient(api_app)
        resp = client.post("/auth/logout", cookies={"longhouse_refresh": raw_rt})

    api_app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 204
    assert observed["token_hash"] == refresh_tokens._hash_token(raw_rt)
