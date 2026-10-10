"""A retired predecessor session never makes a subagent's parent ambiguous.

A source replacement retires the predecessor session but keeps its row, with the
same provider-native id as the session that replaced it. Parent resolution
counted both, found two candidates and resolved nothing, so the replaced
session's subagents never bound to it. Every append of the parent then
re-resolved all of them inside the catalog writer: ~0.9 s holds on david010
(2026-10-08, 26 unbound children, each resolution a sessions scan plus a
fact-table LIKE scan). See docket longhouse-commit-raw-object-writer-hold.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select

from zerg.catalogd.models import SessionProviderFact
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import SUBAGENT_PARENT_GENERATION
from zerg.catalogd.schema import catalog_meta
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import _resolve_session_id_by_provider_session_id
from zerg.catalogd.store import adopt_orphan_subagents_once

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _session(connection, *, provider: str = "claude", native_id: str | None = None, raw_state: str = "durable", **values) -> str:
    session_id = values.pop("session_id", None) or str(uuid4())
    connection.execute(
        StorageSession.__table__.insert().values(
            session_id=session_id,
            tenant_id="default",
            provider=provider,
            provider_session_id=native_id,
            owner_id="1",
            environment="local",
            machine_id="cinder",
            started_at=NOW,
            last_activity_at=NOW,
            raw_state=raw_state,
            render_state="retired" if raw_state == "retired" else "ready",
            media_state="complete",
            commit_seq=1,
            created_at=NOW,
            updated_at=NOW,
            **values,
        )
    )
    return session_id


def _resolve(connection, native_id: str, provider: str = "claude") -> str | None:
    return _resolve_session_id_by_provider_session_id(
        connection, provider=provider, provider_session_id=native_id, owner_id="1", machine_id="cinder"
    )


def _engine(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    return engine


def test_a_retired_predecessor_does_not_make_the_parent_ambiguous(tmp_path):
    engine = _engine(tmp_path)
    native_id = str(uuid4())
    with engine.begin() as connection:
        parent = _session(connection, native_id=native_id)
        _session(connection, session_id=native_id, native_id=native_id, raw_state="retired", hidden_from_default_timeline=1)
        assert _resolve(connection, native_id) == parent


def test_two_live_sessions_with_one_native_id_stay_ambiguous(tmp_path):
    engine = _engine(tmp_path)
    native_id = str(uuid4())
    with engine.begin() as connection:
        _session(connection, native_id=native_id)
        _session(connection, native_id=native_id)
        assert _resolve(connection, native_id) is None


def test_only_opencode_resolves_through_delegation_facts(tmp_path):
    """The fact path is OpenCode's evidence; other providers never scan it."""

    engine = _engine(tmp_path)
    native_id = str(uuid4())
    with engine.begin() as connection:
        for provider in ("opencode", "claude"):
            parent = _session(connection, provider=provider)
            connection.execute(
                SessionProviderFact.__table__.insert().values(
                    session_id=parent,
                    kind="delegation.spawn",
                    at=NOW,
                    source_epoch=str(uuid4()),
                    source_position=0,
                    payload_json=json.dumps({"parentSessionId": native_id}, separators=(",", ":")),
                    commit_seq=1,
                    created_at=NOW,
                )
            )
        assert _resolve(connection, native_id, provider="opencode") is not None
        assert _resolve(connection, native_id, provider="claude") is None


def test_startup_adopts_orphans_once_and_only_where_the_parent_resolves(tmp_path):
    engine = _engine(tmp_path)
    native_id = str(uuid4())
    unknown_parent = str(uuid4())
    with engine.begin() as connection:
        parent = _session(connection, native_id=native_id)
        _session(connection, session_id=native_id, native_id=native_id, raw_state="retired")
        children = [_session(connection, is_subagent=1, subagent_parent_provider_session_id=native_id) for _ in range(3)]
        stranded = _session(connection, is_subagent=1, subagent_parent_provider_session_id=unknown_parent)
        connection.execute(catalog_meta.update().values(subagent_parent_generation=None))

    assert adopt_orphan_subagents_once(engine) == 3
    with engine.begin() as connection:
        rows = dict(
            connection.execute(
                select(StorageSession.session_id, StorageSession.subagent_parent_session_id).where(
                    StorageSession.session_id.in_([*children, stranded])
                )
            ).all()
        )
        marker = connection.execute(select(catalog_meta.c.subagent_parent_generation)).scalar_one()
    assert all(rows[child] == parent for child in children)
    assert rows[stranded] is None
    assert marker == SUBAGENT_PARENT_GENERATION
    # The marker makes it one-shot: a second start does no work.
    assert adopt_orphan_subagents_once(engine) == 0


def test_the_native_id_lookup_has_an_index(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT session_id FROM sessions "
            "WHERE provider = 'claude' AND provider_session_id = 'x' AND owner_id = '1' AND machine_id = 'cinder'"
        ).fetchall()
    assert any("ix_sessions_provider_native_scope" in str(row) for row in plan), plan


def test_hot_path_reads_are_pinned_to_their_indexes(tmp_path):
    """Planner statistics on a long-lived catalog can be stale; the plans may not drift."""

    from zerg.catalogd import store

    engine = _engine(tmp_path)
    cases = (
        (store._ORPHANS_BY_PARENT_SOURCE, {"parent_source_id": "s"}, "ix_sessions_subagent_parent_source_id"),
        (store._ORPHANS_BY_PARENT_NATIVE_ID, {"alias_values": ["p"]}, "ix_sessions_subagent_parent_provider_session_id"),
        (store._NEWEST_SCOPED_SPAWN_FACTS, {"session_id": "x", "limit": 256}, "ix_session_provider_facts_kind_at"),
    )
    scope = {"provider": "claude", "owner_id": "1", "machine_id": "cinder", "session_key": "x"}
    from sqlalchemy import bindparam
    from sqlalchemy import text

    with engine.connect() as connection:
        for statement, params, index in cases:
            explain = text("EXPLAIN QUERY PLAN " + statement.text)
            if "alias_values" in params:
                explain = explain.bindparams(bindparam("alias_values", expanding=True))
            plan = connection.execute(explain, {**scope, **params}).fetchall()
            assert any(index in str(row) for row in plan), (index, plan)
            assert not any("TEMP B-TREE FOR ORDER BY" in str(row) for row in plan), plan
