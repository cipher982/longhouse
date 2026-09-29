"""Startup re-derives render_state for sessions a raw-only commit demoted.

Before this, ``commit_raw_object`` wrote its own envelope receipt (``pending``
for a commit with no render manifest) over ``sessions.render_state``. Cursor's
raw-only transcript projection therefore left rendered sessions permanently
"archive pending / lagging". A one-shot reconciliation restores ``ready``
exactly where the session serves a current generation that still has a live
render object, and touches nothing else. It is a data fix gated by its own
``catalog_meta`` marker: the catalog schema version does not move, so the
deployment pipeline's schema contract and older readers are untouched.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from zerg.catalogd import schema as catalog_schema
from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import SESSION_RENDER_STATE_GENERATION
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


def _add_migrating_session(connection) -> str:
    session_id = _add_session(connection, render_state="pending", generation_state="current")
    connection.execute(
        RenderGeneration.__table__.insert().values(
            generation_id=str(uuid4()),
            session_id=session_id,
            parser_revision="p2",
            ordering_revision="o",
            state="pending",
            source_chain_hash="0" * 64,
            commit_seq=1,
            created_at=NOW,
            updated_at=NOW,
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
            # Serving a current generation while a legacy migration builds a
            # second one: the flag reads what is served, so it is ready.
            "serving_while_migrating": _add_migrating_session(connection),
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


def _marker(engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(text("SELECT render_state_generation FROM catalog_meta WHERE singleton = 1")).scalar_one()


def _clear_marker(engine) -> None:
    """Rewind to a catalog that predates the reconciliation."""

    with engine.begin() as connection:
        connection.execute(text("UPDATE catalog_meta SET render_state_generation = NULL WHERE singleton = 1"))


def test_startup_regains_render_state_only_where_a_render_is_published(tmp_path):
    database = tmp_path / "longhouse-live.db"
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)
    assert _marker(engine) == SESSION_RENDER_STATE_GENERATION
    ids = _seed(engine)
    _clear_marker(engine)
    assert _states(engine, ids)["demoted"] == "pending"
    engine.dispose()

    engine = create_catalog_engine(database)
    metadata = initialize_catalog_schema(engine)

    # A data fix, not a schema advance: the reader contract does not move.
    assert metadata.schema_version == CATALOG_SCHEMA_VERSION
    assert _marker(engine) == SESSION_RENDER_STATE_GENERATION
    assert _states(engine, ids) == {
        "demoted": "ready",
        "never_rendered": "pending",
        "migration_in_flight": "pending",
        "serving_while_migrating": "ready",
        "render_retired": "pending",
        "ready": "ready",
        "retired": "retired",
    }
    engine.dispose()


def test_reconciliation_runs_once_and_is_idempotent(tmp_path):
    database = tmp_path / "longhouse-live.db"
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)
    ids = _seed(engine)

    # The marker is set, so a later start leaves even a demoted-looking row
    # alone: the write path owns the invariant from here on.
    engine.dispose()
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)
    assert _states(engine, ids)["demoted"] == "pending"

    # Re-running the reconciliation itself (marker cleared) converges and is stable.
    _clear_marker(engine)
    catalog_schema._reconcile_session_render_state(engine)
    first = _states(engine, ids)
    _clear_marker(engine)
    catalog_schema._reconcile_session_render_state(engine)

    assert first["demoted"] == "ready"
    assert _states(engine, ids) == first
    assert _marker(engine) == SESSION_RENDER_STATE_GENERATION
    engine.dispose()
