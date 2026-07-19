from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import canonical_evidence_hash
from zerg.catalogd.fact_reducer import read_session_fact_heads
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.services.session_state_facts_projector import project_shadow_session_state_facts

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _head(
    *,
    family: str,
    value: dict,
    source: str,
    source_epoch: str = "epoch-1",
    evidence_hash: str | None = None,
) -> dict:
    subject_key = (
        f"run:{value['run_id']}"
        if family == "activity"
        else f"connection:{value['connection_id']}:{value['lease_generation']}"
    )
    return {
        "family": family,
        "session_id": value["session_id"],
        "subject_key": subject_key,
        "source": source,
        "source_epoch": source_epoch,
        "evidence_hash": evidence_hash or canonical_evidence_hash(value),
        "value_json": json.dumps(value),
        "valid_until": value.get("valid_until"),
    }


def _activity(*, observed_at: datetime, valid_until: datetime, source: str = "provider_a") -> dict:
    return _head(
        family="activity",
        source=source,
        value={
            "authority_class": "provider_runtime",
            "provider": "codex",
            "session_id": "session-1",
            "run_id": "run-1",
            "kind": "running",
            "raw_kind": "running",
            "tool_name": "Shell",
            "source": source,
            "observed_at": observed_at.isoformat(),
            "valid_until": valid_until.isoformat(),
        },
    )


def _control(*, observed_at: datetime, state: str = "attached", grants: list[str] | None = None) -> dict:
    return _head(
        family="control",
        source="provider_control",
        value={
            "authority_class": "provider_control",
            "provider": "codex",
            "session_id": "session-1",
            "run_id": "run-1",
            "connection_id": "connection-1",
            "lease_generation": "lease-1",
            "granted_operations": grants or [],
            "state": state,
            "lease_ttl_ms": 60_000,
            "source": "provider_control",
            "observed_at": observed_at.isoformat(),
        },
    )


def test_projector_selects_newest_unexpired_head_without_commit_or_receive_ranking():
    older = _activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=5), source="z-source")
    newer = _activity(
        observed_at=NOW + timedelta(seconds=1),
        valid_until=NOW + timedelta(minutes=5),
        source="a-source",
    )
    older["updated_commit_seq"] = 999
    older["received_at"] = NOW + timedelta(days=1)

    projection = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=42,
        heads=[older, newer],
        now=NOW + timedelta(seconds=2),
    )

    assert projection.commit_seq == 42
    assert projection.activity.state == "executing"
    assert projection.activity.source == "a-source"
    assert projection.activity.tool == "Shell"
    assert projection.control is None
    assert "mode" in projection.unsupported_families
    assert "run" in projection.unsupported_families
    assert "presentation" in projection.unsupported_families


def test_projector_expires_activity_and_derives_control_lease_from_ttl():
    projection = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=9,
        heads=[
            _activity(observed_at=NOW, valid_until=NOW + timedelta(seconds=1)),
            _control(observed_at=NOW, grants=["interrupt", "send_input"]),
        ],
        supported_operations={"send_input", "interrupt", "terminate"},
        now=NOW + timedelta(seconds=30),
    )

    assert projection.activity.state == "unknown"
    assert projection.control is not None
    assert projection.control.connection == "connected"
    assert projection.control.connection_id == "connection-1"
    assert projection.control.valid_until == NOW + timedelta(seconds=60)
    assert projection.control.actions.send_input.state == "available"
    assert projection.control.actions.interrupt.state == "available"
    assert projection.control.actions.terminate.reason == "not_granted"
    assert projection.control.actions.resume.reason == "unsupported"

    expired = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=9,
        heads=[_control(observed_at=NOW, grants=["send_input"])],
        supported_operations={"send_input"},
        now=NOW + timedelta(seconds=60),
    )
    assert expired.control is None


def test_projector_uses_stable_source_coordinate_for_exact_timestamp_tie():
    first = _activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=1), source="a-source")
    second = _activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=1), source="z-source")

    forward = project_shadow_session_state_facts(session_id="session-1", commit_seq=1, heads=[first, second], now=NOW)
    reverse = project_shadow_session_state_facts(session_id="session-1", commit_seq=1, heads=[second, first], now=NOW)

    assert forward.activity.source == reverse.activity.source == "z-source"


def test_projector_skips_corrupt_or_cross_session_heads_without_granting_control():
    valid = _activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=1))
    cross_session = _control(observed_at=NOW, grants=["send_input"])
    cross_session["value_json"] = json.dumps({**json.loads(cross_session["value_json"]), "session_id": "session-2"})
    malformed = {**_control(observed_at=NOW), "value_json": "{"}
    indexed_other_session = {**_control(observed_at=NOW), "session_id": "session-2"}
    missing_source = _control(observed_at=NOW)
    missing_source_value = {**json.loads(missing_source["value_json"]), "source": None}
    missing_source.update(
        source=None,
        value_json=json.dumps(missing_source_value),
        evidence_hash=canonical_evidence_hash(missing_source_value),
    )

    projection = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=3,
        heads=[cross_session, malformed, indexed_other_session, missing_source, valid],
        supported_operations={"send_input"},
        now=NOW,
    )

    assert projection.activity.state == "executing"
    assert projection.control is None
    assert projection.rejected_heads == 4


def test_session_head_read_and_projection_share_one_commit_snapshot(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    value = {
        "authority_class": "provider_runtime",
        "provider": "codex",
        "session_id": "session-1",
        "run_id": "run-1",
        "kind": "idle",
        "raw_kind": "idle",
        "source": "provider_runtime",
        "observed_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(minutes=1)).isoformat(),
    }
    fact = ReducerFact(
        family="activity",
        subject_key="run:run-1",
        source="provider_runtime",
        source_epoch="run-1",
        source_seq=1,
        dedupe_key="b" * 64,
        evidence_hash=canonical_evidence_hash(value),
        value=value,
        observed_at=NOW,
        session_id="session-1",
        valid_until=NOW + timedelta(minutes=1),
    )
    with engine.begin() as connection:
        reduced = reduce_fact_batch(connection, [fact], received_at=NOW)
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        query_plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT * FROM fact_heads WHERE session_id = ?",
            ("session-1",),
        ).all()
        commit_seq, heads = read_session_fact_heads(connection, session_id="session-1")
        projection = project_shadow_session_state_facts(
            session_id="session-1",
            commit_seq=commit_seq,
            heads=heads,
            now=NOW,
        )
        connection.rollback()

    assert commit_seq == reduced.commit_seq == projection.commit_seq
    assert projection.activity.state == "quiescent"
    assert any("ix_fact_heads_session_family_recent" in str(row) for row in query_plan)
