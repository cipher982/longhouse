"""Statements hoisted out of the envelope commit path keep their meaning.

The catalog writer spent a quarter of each storage-v2 envelope commit building
and cache-keying SQLAlchemy expressions for three lookups. They are now built
once at import with bind parameters. These tests pin that the prebuilt forms
answer exactly what the per-call forms answered.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from zerg.catalogd.models import ProjectorState
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import _is_hash
from zerg.catalogd.store import StorageSession
from zerg.catalogd.store import _resolve_session_id_by_provider_session_id
from zerg.catalogd.store import _storage_title_candidate_clause
from zerg.catalogd.store_mixins.ingest import _ADVANCE_PROJECTOR_DESIRED_REVISION
from zerg.catalogd.store_mixins.ingest import _TITLE_GENERATION_REQUIRED

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _engine(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    return engine


def _insert(connection, **values) -> str:
    session_id = str(uuid4())
    row = {
        "session_id": session_id,
        "tenant_id": "tenant-a",
        "owner_id": "42",
        "provider": "opencode",
        "environment": "local",
        "machine_id": "cinder",
        "project": "zerg",
        "started_at": NOW,
        "last_activity_at": NOW,
        "user_messages": 1,
        "first_user_message_preview": "fix the flaky deploy check",
        "commit_seq": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(values)
    connection.execute(StorageSession.__table__.insert().values(**row))
    return session_id


def test_prebuilt_title_query_matches_the_per_call_clause(tmp_path) -> None:
    table = StorageSession.__table__
    with _engine(tmp_path).begin() as connection:
        due = _insert(connection)
        retry_passed = _insert(connection, title_retry_at=NOW - timedelta(minutes=1))
        retry_pending = _insert(connection, title_retry_at=NOW + timedelta(minutes=1))
        no_user_turn = _insert(connection, user_messages=0)
        already_titled = _insert(connection, anchor_title="Fix deploy check", anchor_title_source="ai")
        cases = {due: True, retry_passed: True, retry_pending: False, no_user_turn: False, already_titled: False}
        for session_id, expected in cases.items():
            per_call = connection.execute(
                select(table.c.session_id)
                .where(table.c.session_id == session_id, _storage_title_candidate_clause(table, observed_at=NOW))
                .limit(1)
            ).first()
            prebuilt = connection.execute(_TITLE_GENERATION_REQUIRED, {"session_key": session_id, "observed_at": NOW}).first()
            assert (per_call is not None) is expected, session_id
            assert (prebuilt is not None) is expected, session_id


def test_prebuilt_native_id_resolution_keeps_scope_and_retirement(tmp_path) -> None:
    def resolve(connection, native: str, *, owner_id="42", machine_id="cinder", provider="opencode"):
        return _resolve_session_id_by_provider_session_id(
            connection, provider=provider, provider_session_id=native, owner_id=owner_id, machine_id=machine_id
        )

    with _engine(tmp_path).begin() as connection:
        live = _insert(connection, provider_session_id="native-a")
        _insert(connection, provider_session_id="native-b", raw_state="retired")
        _insert(connection, provider_session_id="native-c")
        _insert(connection, provider_session_id="native-c")

        assert resolve(connection, "native-a") == live
        assert resolve(connection, "native-a", owner_id="43") is None
        assert resolve(connection, "native-a", machine_id="other") is None
        assert resolve(connection, "native-a", provider="codex") is None
        assert resolve(connection, "native-b") is None  # only candidate is retired
        assert resolve(connection, "native-c") is None  # ambiguous


def test_projector_upsert_inserts_idle_and_only_advances_a_lagging_desired_revision(tmp_path) -> None:
    table = ProjectorState.__table__
    later = NOW + timedelta(seconds=5)

    def advance(connection, revision: int, at: datetime) -> None:
        connection.execute(
            _ADVANCE_PROJECTOR_DESIRED_REVISION,
            [{"p_projector": name, "p_session_id": "s-1", "p_revision": revision, "p_at": at} for name in ("search-v2", "semantic-v2")],
        )

    def rows(connection) -> dict[str, dict]:
        return {row["projector"]: dict(row) for row in connection.execute(select(table)).mappings()}

    with _engine(tmp_path).begin() as connection:
        advance(connection, 5, NOW)
        created = rows(connection)
        assert {
            name: (row["desired_revision"], row["completed_revision"], row["status"], row["commit_seq"]) for name, row in created.items()
        } == {
            "search-v2": (5, 0, "idle", 5),
            "semantic-v2": (5, 0, "idle", 5),
        }
        connection.execute(
            table.update().where(table.c.projector == "search-v2").values(completed_revision=5, status="running", claim_token="t")
        )

        advance(connection, 4, later)  # an older revision never moves a row backwards
        assert rows(connection)["search-v2"]["desired_revision"] == 5
        assert rows(connection)["search-v2"]["updated_at"] == created["search-v2"]["updated_at"]

        advance(connection, 9, later)
        search = rows(connection)["search-v2"]
        assert (search["desired_revision"], search["commit_seq"], search["completed_revision"]) == (9, 9, 5)
        assert (search["status"], search["claim_token"]) == ("running", "t")  # worker-owned columns untouched
        assert search["created_at"] == created["search-v2"]["created_at"]
        lagging = connection.exec_driver_sql("SELECT search_projector_rows, search_projector_lagging FROM storage_telemetry_counters").one()
        assert tuple(lagging) == (1, 1)


def test_record_hash_check_accepts_only_lowercase_sha256_hex() -> None:
    assert _is_hash("0" * 64)
    assert _is_hash("a1" * 32)
    assert not _is_hash("A" * 64)
    assert not _is_hash("0" * 63)
    assert not _is_hash("0" * 65)
    assert not _is_hash("0" * 63 + "\n")
    assert not _is_hash("g" * 64)
    assert not _is_hash(None)
    assert not _is_hash(b"0" * 64)
