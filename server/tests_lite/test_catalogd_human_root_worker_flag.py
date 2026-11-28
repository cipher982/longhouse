"""A worker flag a merged worker left on a human-launched root is released once.

An Antigravity or OMP worker inherits its parent's managed session id. Before
the engine refused that id, the worker's transcript shipped under the parent and
its ``is_subagent`` fact hid the parent from the timeline (OMP Helm task files,
Antigravity ``invoke_subagent`` workers).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select

from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import catalog_meta
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import release_human_roots_from_worker_flag_once

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def _session(connection, **values) -> str:
    session_id = str(uuid4())
    connection.execute(
        StorageSession.__table__.insert().values(
            session_id=session_id,
            tenant_id="default",
            provider=values.pop("provider", "antigravity"),
            provider_session_id=str(uuid4()),
            owner_id="1",
            environment="local",
            machine_id="cinder",
            cwd="/Users/someone",
            started_at=NOW,
            last_activity_at=NOW,
            raw_state="durable",
            render_state="ready",
            media_state="complete",
            commit_seq=1,
            created_at=NOW,
            updated_at=NOW,
            **values,
        )
    )
    return session_id


def _row(connection, session_id: str):
    return (
        connection.execute(
            select(StorageSession.is_subagent, StorageSession.hidden_from_default_timeline, StorageSession.commit_seq).where(
                StorageSession.session_id == session_id
            )
        )
        .mappings()
        .one()
    )


def test_a_flagged_human_root_without_a_parent_edge_is_released_once(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        merged_root = _session(
            connection, launch_actor="human_shell", launch_surface="terminal", is_subagent=1, hidden_from_default_timeline=1
        )
        omp_root = _session(
            connection, provider="omp", launch_actor="human_shell", launch_surface="terminal", is_subagent=1, hidden_from_default_timeline=1
        )
        # A real worker names its parent, whoever launched it.
        worker = _session(
            connection,
            launch_actor="human_shell",
            is_subagent=1,
            hidden_from_default_timeline=1,
            subagent_parent_provider_session_id=str(uuid4()),
        )
        unlaunched_worker = _session(connection, is_subagent=1, hidden_from_default_timeline=1)
        # Another hiding reason survives the release.
        test_root = _session(
            connection,
            launch_actor="human_shell",
            launch_surface="test",
            is_subagent=1,
            hidden_from_default_timeline=1,
        )
        connection.execute(catalog_meta.update().values(human_root_worker_flag_generation=None))
        before = connection.execute(select(catalog_meta.c.commit_seq)).scalar_one()

    assert release_human_roots_from_worker_flag_once(engine) == 3

    with engine.begin() as connection:
        after = connection.execute(select(catalog_meta.c.commit_seq)).scalar_one()
        assert after == before + 1
        for root in (merged_root, omp_root):
            assert dict(_row(connection, root)) == {"is_subagent": 0, "hidden_from_default_timeline": 0, "commit_seq": after}
        assert dict(_row(connection, test_root))["hidden_from_default_timeline"] == 1
        assert dict(_row(connection, test_root))["is_subagent"] == 0
        for kept in (worker, unlaunched_worker):
            assert dict(_row(connection, kept))["is_subagent"] == 1
            assert dict(_row(connection, kept))["hidden_from_default_timeline"] == 1

    # The marker makes it a one-shot pass.
    with engine.begin() as connection:
        _session(connection, launch_actor="human_shell", is_subagent=1, hidden_from_default_timeline=1)
    assert release_human_roots_from_worker_flag_once(engine) == 0
