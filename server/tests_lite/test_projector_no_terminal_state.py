"""A projector row must never reach a state it cannot leave on its own.

One 95 MB Claude archive was quarantined on a permanent parser error and sat
there for six days, dragging the coverage watermark back with it and appearing
in no counter. Separately, three retired embedding generations held ~69k rows
that no worker polls, two of them `failed` for sixteen days. Both are the same
bug in different clothes: derived state that stops moving and stops being
visible, and only an operator RPC can restart it.

These tests pin the invariants that make "stuck" unreachable.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update

from zerg.catalogd import store as catalog_store
from zerg.catalogd.models import ProjectorState
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import ACTIVE_PROJECTORS
from zerg.catalogd.store import PERMANENT_FAILURE_RETRY_INTERVAL
from zerg.catalogd.store import CatalogStore
from zerg.embedding_space import EMBEDDING_PROJECTOR_ID


@pytest.fixture
def store(tmp_path: Path) -> CatalogStore:
    engine = create_catalog_engine(tmp_path / "live.db")
    initialize_catalog_schema(engine)
    return CatalogStore(engine)


def _seed_row(
    store: CatalogStore,
    *,
    projector: str,
    session_id: str,
    desired: int = 10,
    completed: int = 0,
    status: str = "idle",
    retry_at: datetime | None = None,
) -> None:
    table = ProjectorState.__table__
    now = datetime.now(UTC)
    with store.engine.begin() as connection:
        connection.execute(
            insert(table).values(
                projector=projector,
                session_id=session_id,
                desired_revision=desired,
                completed_revision=completed,
                status=status,
                failure_count=0,
                retry_at=retry_at,
                commit_seq=1,
                created_at=now,
                updated_at=now,
            )
        )


def _row(store: CatalogStore, *, projector: str, session_id: str):
    table = ProjectorState.__table__
    with store.engine.connect() as connection:
        return connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_id)).mappings().first()


def test_permanent_failure_retries_instead_of_quarantining(store: CatalogStore) -> None:
    """A "permanent" error describes this build, not the data. It must expire."""

    session_id = str(uuid4())
    _seed_row(store, projector="search-v2", session_id=session_id)
    now = datetime.now(UTC)
    claim_token = str(uuid4())
    claimed = store.claim_projector_lag(
        projector="search-v2",
        worker_id="worker-a",
        claim_token=claim_token,
        now=now,
        lease_seconds=60,
        limit=10,
    )
    assert [row["session_id"] for row in claimed["claimed"]] == [session_id]

    store.fail_projector_claim(
        projector="search-v2",
        session_id=UUID(session_id),
        claim_token=claim_token,
        error_code="semantic_recovery_permanent",
        error_message="Claude semantic replay scan exceeds its safe evidence bound",
        failed_at=now,
        retry_at=now + timedelta(seconds=30),
    )

    row = _row(store, projector="search-v2", session_id=session_id)
    assert row is not None
    # The old behaviour: status="quarantined", retry_at=None, gone forever.
    assert row["status"] == "failed"
    assert row["retry_at"] is not None
    # The error code is still recorded, so the permanence is observable even
    # though it is no longer enforced as terminal.
    assert row["last_error_code"] == "semantic_recovery_permanent"

    # Not claimable while the backoff is open ...
    too_soon = store.claim_projector_lag(
        projector="search-v2",
        worker_id="worker-b",
        claim_token=str(uuid4()),
        now=now + timedelta(minutes=1),
        lease_seconds=60,
        limit=10,
    )
    assert too_soon["claimed"] == []

    # ... and claimable again once it closes, so a deploy that fixes the parser
    # heals the row with no operator action.
    after_backoff = store.claim_projector_lag(
        projector="search-v2",
        worker_id="worker-c",
        claim_token=str(uuid4()),
        now=now + PERMANENT_FAILURE_RETRY_INTERVAL + timedelta(minutes=1),
        lease_seconds=60,
        limit=10,
    )
    assert [row["session_id"] for row in after_backoff["claimed"]] == [session_id]


def test_legacy_quarantined_row_becomes_claimable(store: CatalogStore) -> None:
    """Rows written before the fix carry status='quarantined' and retry_at=NULL."""

    session_id = str(uuid4())
    _seed_row(store, projector="search-v2", session_id=session_id)
    table = ProjectorState.__table__
    with store.engine.begin() as connection:
        connection.execute(
            update(table)
            .where(table.c.projector == "search-v2", table.c.session_id == session_id)
            .values(status="quarantined", retry_at=None, last_error_code="semantic_recovery_permanent")
        )

    claimed = store.claim_projector_lag(
        projector="search-v2",
        worker_id="worker-a",
        claim_token=str(uuid4()),
        now=datetime.now(UTC),
        lease_seconds=60,
        limit=10,
    )
    assert [row["session_id"] for row in claimed["claimed"]] == [session_id]


def test_idle_claim_poll_does_not_take_a_write_transaction(store: CatalogStore, monkeypatch) -> None:
    """Backoff and dependency waits must not reserve the global writer lane."""

    session_id = str(uuid4())
    _seed_row(
        store,
        projector="search-v2",
        session_id=session_id,
        status="failed",
        retry_at=datetime.now(UTC) + timedelta(hours=1),
    )

    def refuse_write(*_args, **_kwargs):
        raise AssertionError("idle projector poll took a write transaction")

    monkeypatch.setattr(catalog_store, "_write_transaction", refuse_write)
    result = store.claim_projector_lag(
        projector="search-v2",
        worker_id="worker-a",
        claim_token=str(uuid4()),
        now=datetime.now(UTC),
        lease_seconds=60,
        limit=10,
    )

    assert result["claimed"] == []
    assert result["exact_replay"] is False


def test_claim_replay_token_probes_use_indexes(store: CatalogStore) -> None:
    """Idempotency checks must not scan every historical projector row."""

    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().startswith("SELECT projector_state.commit_seq"):
            statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        store.claim_projector_lag(
            projector="search-v2",
            worker_id="worker-a",
            claim_token=str(uuid4()),
            now=datetime.now(UTC),
            lease_seconds=60,
            limit=10,
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)

    assert len(statements) == 2, "completion and failure tokens must be separate point probes"
    assert all(" OR " not in statement.upper() for statement in statements)
    with store.engine.connect() as connection:
        indexes = {row[1] for row in connection.exec_driver_sql("PRAGMA index_list('projector_state')")}
    assert "ix_projector_state_completion_token" in indexes
    assert "ix_projector_state_failure_token" in indexes


def test_reaper_deletes_retired_generations_and_spares_live_ones(store: CatalogStore) -> None:
    """Renaming the embedding projector must not strand its rows forever."""

    live_session = str(uuid4())
    retired_session = str(uuid4())
    _seed_row(store, projector=EMBEDDING_PROJECTOR_ID, session_id=live_session)
    _seed_row(store, projector="semantic-v2", session_id=live_session)
    _seed_row(store, projector="search-v2", session_id=live_session)
    _seed_row(store, projector="render-v2", session_id=live_session)
    # Two earlier generations of the same projector, one holding a failure that
    # no worker will ever retry because nothing polls that name.
    _seed_row(store, projector="embeddings-v1", session_id=retired_session)
    _seed_row(
        store,
        projector="embeddings-5090578d9565-256d",
        session_id=retired_session,
        status="failed",
        retry_at=datetime.now(UTC) - timedelta(days=16),
    )

    result = store.reap_retired_projector_states()
    assert result["reaped_rows"] == 2
    assert result["reaped_projectors"] == ["embeddings-5090578d9565-256d", "embeddings-v1"]

    for projector in ACTIVE_PROJECTORS:
        assert _row(store, projector=projector, session_id=live_session) is not None
    assert _row(store, projector="embeddings-v1", session_id=retired_session) is None
    assert _row(store, projector="embeddings-5090578d9565-256d", session_id=retired_session) is None

    # Idempotent: a second pass finds nothing and reports nothing.
    again = store.reap_retired_projector_states()
    assert again["reaped_rows"] == 0
    assert again["reaped_projectors"] == []


def test_telemetry_reports_stuck_rows_for_every_projector(store: CatalogStore) -> None:
    """The counters only ever covered search-v2, so embedding failures were unreportable."""

    session_id = str(uuid4())
    _seed_row(store, projector="search-v2", session_id=session_id, status="failed")
    _seed_row(store, projector=EMBEDDING_PROJECTOR_ID, session_id=session_id, status="failed")

    summary = store.read_storage_telemetry_summary()
    stuck = {entry["projector"]: entry for entry in summary["stuck_projectors"]}

    assert stuck[EMBEDDING_PROJECTOR_ID]["rows"] == 1
    assert stuck[EMBEDDING_PROJECTOR_ID]["status"] == "failed"
    assert stuck[EMBEDDING_PROJECTOR_ID]["retired"] is False
    assert stuck["search-v2"]["rows"] == 1
    assert stuck[EMBEDDING_PROJECTOR_ID]["oldest_updated_at"] is not None


def test_telemetry_flags_a_retired_generation_as_retired(store: CatalogStore) -> None:
    _seed_row(store, projector="embeddings-v1", session_id=str(uuid4()), status="failed")

    summary = store.read_storage_telemetry_summary()
    stuck = {entry["projector"]: entry for entry in summary["stuck_projectors"]}
    assert stuck["embeddings-v1"]["retired"] is True
