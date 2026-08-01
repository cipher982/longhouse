"""Legacy binding_signal reducer must persist the provider-native id alias.

Parity check for the catalogd live path (LiveCatalogStore.apply_session_runtime),
which upserts a provider_session_id thread alias. The legacy archive reducer used
to set only state.session_id, leaving the native id unresolvable.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThreadAlias
from zerg.services.session_kernel_projection import resolve_session_id_by_provider_session_id
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import runtime_key_for_session


def _make_db(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)


def _seed_session(db, provider: str = "claude") -> AgentSession:
    now = datetime.now(timezone.utc)
    session = AgentSession(
        id=uuid4(),
        provider=provider,
        environment="test",
        project="runtime",
        started_at=now,
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _binding(session_id, provider_session_id: str, provider: str = "claude") -> RuntimeEventIngest:
    return RuntimeEventIngest(
        runtime_key=runtime_key_for_session(provider, str(session_id)),
        session_id=session_id,
        provider=provider,
        device_id="cinder",
        source="claude_print",
        kind="binding_signal",
        occurred_at=datetime.now(timezone.utc),
        dedupe_key=f"binding:{session_id}:{provider_session_id}",
        payload={"provider_session_id": provider_session_id},
    )


def test_legacy_binding_signal_writes_resolvable_alias(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "binding_alias.db")
    native_id = str(uuid4())
    with SessionLocal() as db:
        session = _seed_session(db)
        ingest_runtime_events(db, [_binding(session.id, native_id)])
        db.commit()

        assert resolve_session_id_by_provider_session_id(db, native_id) == session.id
        # Replaying the same binding stays idempotent: one alias row.
        ingest_runtime_events(db, [_binding(session.id, native_id)])
        db.commit()
        rows = (
            db.query(SessionThreadAlias)
            .filter(
                SessionThreadAlias.alias_kind == "provider_session_id",
                SessionThreadAlias.alias_value == native_id,
            )
            .all()
        )
        assert len(rows) == 1
    engine.dispose()


def test_legacy_binding_signal_conflict_does_not_crash(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "binding_alias_conflict.db")
    native_id = str(uuid4())
    with SessionLocal() as db:
        first = _seed_session(db)
        second = _seed_session(db)
        ingest_runtime_events(db, [_binding(first.id, native_id)])
        db.commit()

        # Same native id bound to a second session: conflict is logged, not raised,
        # and the alias keeps resolving to the first session.
        ingest_runtime_events(db, [_binding(second.id, native_id)])
        db.commit()
        assert resolve_session_id_by_provider_session_id(db, native_id) == first.id
    engine.dispose()
