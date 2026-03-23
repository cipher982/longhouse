from __future__ import annotations

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.surface_ingress import SurfaceIngressClaim
from zerg.models.user import User
from zerg.surfaces.idempotency import SurfaceIdempotencyError
from zerg.surfaces.idempotency import SurfaceIngressClaimStore


def _make_db(tmp_path):
    db_path = tmp_path / "test_surface_idempotency.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def test_surface_ingress_claim_store_claims_first_and_dedupes_second(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = User(email="surface-claim@test.local", role="USER")
        db.add(user)
        db.commit()
        db.refresh(user)

        store = SurfaceIngressClaimStore(db)

        first = store.claim(
            owner_id=user.id,
            surface_id="telegram",
            dedupe_key="telegram:42:1001",
            conversation_id="telegram:42",
            source_event_id="1001",
            source_message_id="2002",
        )
        second = store.claim(
            owner_id=user.id,
            surface_id="telegram",
            dedupe_key="telegram:42:1001",
            conversation_id="telegram:42",
            source_event_id="1001",
            source_message_id="2002",
        )

        assert first is True
        assert second is False

        rows = db.query(SurfaceIngressClaim).all()
        assert len(rows) == 1
        assert rows[0].dedupe_key == "telegram:42:1001"


def test_surface_ingress_claim_store_scope_is_owner_and_surface(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user_a = User(email="surface-a@test.local", role="USER")
        user_b = User(email="surface-b@test.local", role="USER")
        db.add(user_a)
        db.add(user_b)
        db.commit()
        db.refresh(user_a)
        db.refresh(user_b)

        store = SurfaceIngressClaimStore(db)

        assert (
            store.claim(
                owner_id=user_a.id,
                surface_id="telegram",
                dedupe_key="dup-key",
                conversation_id="telegram:42",
                source_event_id="1",
                source_message_id="10",
            )
            is True
        )

        # Same key, different owner => allowed
        assert (
            store.claim(
                owner_id=user_b.id,
                surface_id="telegram",
                dedupe_key="dup-key",
                conversation_id="telegram:42",
                source_event_id="1",
                source_message_id="10",
            )
            is True
        )

        # Same owner, same key, different surface => allowed
        assert (
            store.claim(
                owner_id=user_a.id,
                surface_id="web",
                dedupe_key="dup-key",
                conversation_id="web:main",
                source_event_id="web-1",
                source_message_id="msg-1",
            )
            is True
        )


def test_surface_ingress_claim_store_wraps_unexpected_db_errors(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = User(email="surface-error@test.local", role="USER")
        db.add(user)
        db.commit()
        db.refresh(user)

        store = SurfaceIngressClaimStore(db)

        original_commit = db.commit

        def _boom():
            raise RuntimeError("db commit failed")

        db.commit = _boom  # type: ignore[method-assign]
        try:
            try:
                store.claim(
                    owner_id=user.id,
                    surface_id="telegram",
                    dedupe_key="telegram:42:2002",
                    conversation_id="telegram:42",
                    source_event_id="2002",
                    source_message_id="3003",
                )
                assert False, "expected SurfaceIdempotencyError"
            except SurfaceIdempotencyError:
                pass
        finally:
            db.commit = original_commit  # type: ignore[method-assign]
