from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select

from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import _bind_control_evidence_identities
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread


def _control_fact(*, session_id: str, run_id: str, connection_id: str, lease_generation: str) -> ReducerFact:
    observed_at = datetime.now(UTC)
    value = {
        "authority_class": "provider_control",
        "provider": "codex",
        "session_id": session_id,
        "run_id": run_id,
        "connection_id": connection_id,
        "lease_generation": lease_generation,
        "granted_operations": ["send_input"],
        "ownership": "managed",
        "state": "attached",
        "lease_ttl_ms": 60_000,
        "source": "codex_bridge_scan",
        "observed_at": observed_at.isoformat(),
    }
    return ReducerFact(
        family="control",
        subject_key=f"connection:{connection_id}:{lease_generation}",
        source="codex_bridge_scan",
        source_epoch=lease_generation,
        source_seq=None,
        dedupe_key="a" * 64,
        evidence_hash="b" * 64,
        value=value,
        observed_at=observed_at,
        session_id=session_id,
        valid_until=None,
    )


def test_control_evidence_identity_binding_is_write_once(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    session_id = str(uuid4())
    thread_id = str(uuid4())
    run_id = str(uuid4())
    adapter_connection_id = str(uuid4())
    lease_generation = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            LiveSessionThread.__table__.insert().values(
                id=thread_id,
                session_id=session_id,
                provider="codex",
                branch_kind="root",
                is_primary=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            LiveSessionRun.__table__.insert().values(
                id=run_id,
                thread_id=thread_id,
                provider="codex",
                launch_origin="longhouse_spawned",
                started_at=now,
            )
        )
        connection.execute(
            LiveSessionConnection.__table__.insert().values(
                run_id=run_id,
                control_plane="codex_bridge",
                acquisition_kind="spawned_control",
                state="attached",
                acquired_at=now,
            )
        )
        fact = _control_fact(
            session_id=session_id,
            run_id=run_id,
            connection_id=adapter_connection_id,
            lease_generation=lease_generation,
        )
        assert _bind_control_evidence_identities(connection, [fact]) == {
            "bound": 1,
            "matched": 0,
            "unbound": 0,
            "mismatched": 0,
        }
        assert _bind_control_evidence_identities(connection, [fact]) == {
            "bound": 0,
            "matched": 1,
            "unbound": 0,
            "mismatched": 0,
        }
        mismatch = _control_fact(
            session_id=session_id,
            run_id=run_id,
            connection_id=str(uuid4()),
            lease_generation=str(uuid4()),
        )
        assert _bind_control_evidence_identities(connection, [mismatch])["mismatched"] == 1
        stored = connection.execute(select(LiveSessionConnection.__table__)).mappings().one()

    assert stored["adapter_connection_id"] == adapter_connection_id
    assert stored["lease_generation"] == lease_generation


def test_control_evidence_identity_does_not_bind_across_run_mismatch(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    fact = _control_fact(
        session_id=str(uuid4()),
        run_id=str(uuid4()),
        connection_id=str(uuid4()),
        lease_generation=str(uuid4()),
    )

    with engine.begin() as connection:
        result = _bind_control_evidence_identities(connection, [fact])

    assert result == {"bound": 0, "matched": 0, "unbound": 1, "mismatched": 0}
