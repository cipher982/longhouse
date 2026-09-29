"""The 5->6 catalog migration re-derives render_state for demoted sessions.

Before 6, ``commit_raw_object`` wrote its own envelope receipt (``pending`` for a
commit with no render manifest) over ``sessions.render_state``. Cursor's
raw-only transcript projection therefore left rendered sessions permanently
"archive pending / lagging". The migration restores ``ready`` exactly where the
session serves a current generation that still has a live render object, and
touches nothing else.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import CATALOG_SCHEMA_MIGRATIONS
from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _add_session(connection, *, render_state: str, generation_state: str | None = None, live_object: bool = True) -> str:
    """One storage session, optionally pointing at a generation with one render object."""

    session_id = str(uuid4())
    generation_id = str(uuid4()) if generation_state is not None else None
    connection.execute(
        StorageSession.__table__.insert().values(
            session_id=session_id,
            tenant_id="default",
            provider="cursor",
            environment="local",
            machine_id="cinder",
            started_at=NOW,
            last_activity_at=NOW,
            current_render_generation=generation_id,
            raw_state="durable",
            render_state=render_state,
            media_state="complete",
            commit_seq=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    if generation_id is not None:
        connection.execute(
            RenderGeneration.__table__.insert().values(
                generation_id=generation_id,
                session_id=session_id,
                parser_revision="p",
                ordering_revision="o",
                state=generation_state,
                source_chain_hash="0" * 64,
                commit_seq=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        connection.execute(
            RenderObject.__table__.insert().values(
                object_id=str(uuid4()),
                generation_id=generation_id,
                session_id=session_id,
                source_envelope_id=uuid4().hex + uuid4().hex,
                object_hash="1" * 64,
                payload_hash="2" * 64,
                object_path="objects/x",
                uncompressed_size=1,
                compressed_size=1,
                event_count=1,
                commit_seq=1,
                created_at=NOW,
                retired_at=None if live_object else NOW,
            )
        )
    return session_id


def _seed(engine) -> dict[str, str]:
    """One session per shape the migration must and must not touch."""

    with engine.begin() as connection:
        return {
            # Demoted by a raw-only commit: a current generation with a live render object.
            "demoted": _add_session(connection, render_state="pending", generation_state="current"),
            # Raw-only from the start: nothing is published, so pending is the truth.
            "never_rendered": _add_session(connection, render_state="pending"),
            # A legacy migration still publishing holds a 'pending' generation.
            "migration_in_flight": _add_session(connection, render_state="pending", generation_state="pending"),
            # The current generation's every render object has retired.
            "render_retired": _add_session(connection, render_state="pending", generation_state="current", live_object=False),
            "ready": _add_session(connection, render_state="ready", generation_state="current"),
            # Retired sessions keep their pointer and stay retired.
            "retired": _add_session(connection, render_state="retired", generation_state="superseded"),
        }


def _states(engine, ids: dict[str, str]) -> dict[str, str]:
    with engine.connect() as connection:
        return {
            name: connection.execute(text("SELECT render_state FROM sessions WHERE session_id = :sid"), {"sid": sid}).scalar_one()
            for name, sid in ids.items()
        }


def test_v5_catalog_regains_render_state_only_where_a_render_is_published(tmp_path):
    database = tmp_path / "longhouse-live.db"
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)
    ids = _seed(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE catalog_meta SET schema_version = 5 WHERE singleton = 1")
        connection.exec_driver_sql("PRAGMA user_version=5")
    assert _states(engine, ids)["demoted"] == "pending"
    engine.dispose()

    engine = create_catalog_engine(database)
    metadata = initialize_catalog_schema(engine)

    assert metadata.schema_version == CATALOG_SCHEMA_VERSION
    assert _states(engine, ids) == {
        "demoted": "ready",
        "never_rendered": "pending",
        "migration_in_flight": "pending",
        "render_retired": "pending",
        "ready": "ready",
        "retired": "retired",
    }
    engine.dispose()


def test_render_state_migration_is_idempotent(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    ids = _seed(engine)
    migration = CATALOG_SCHEMA_MIGRATIONS[5]

    with engine.begin() as connection:
        migration(connection)
    first = _states(engine, ids)
    with engine.begin() as connection:
        migration(connection)

    assert first["demoted"] == "ready"
    assert _states(engine, ids) == first
    engine.dispose()
