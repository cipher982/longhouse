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
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_state_contract import SessionHostFacts
from zerg.services.session_state_contract import SessionTranscriptFacts
from zerg.services.session_state_facts_projector import authorize_exact_control_fact
from zerg.services.session_state_facts_projector import project_served_session_state_facts
from zerg.services.session_state_facts_projector import project_shadow_session_state_facts

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
CATALOG_FACTS = {
    "catalog": {
        "started_at": (NOW - timedelta(hours=1)).isoformat(),
        "closed_at": None,
        "origin_kind": None,
        "launch_surface": None,
    },
    "readiness": None,
    "latest_run": None,
    "connections": [],
}
RUN_CATALOG_FACTS = {
    **CATALOG_FACTS,
    "latest_run": {
        "id": "run-1",
        "started_at": (NOW - timedelta(minutes=5)).isoformat(),
        "ended_at": None,
    },
}
BOUND_CONTROL_CATALOG_FACTS = {
    **RUN_CATALOG_FACTS,
    "connections": [
        {
            "run_id": "run-1",
            "adapter_connection_id": "connection-1",
            "lease_generation": "lease-1",
            "state": "attached",
            "released_at": None,
        }
    ],
}


def _capabilities(**overrides) -> KernelSessionCapabilities:
    values = {
        "session_id": "session-1",
        "thread_id": "thread-1",
        "run_id": None,
        "connection_id": None,
        "control_plane": None,
        "connection_state": None,
        "control_label": "imported",
        "live_control_available": False,
        "host_reattach_available": False,
        "observe_only": False,
        "search_only": True,
        "can_send_input": False,
        "can_interrupt": False,
        "can_terminate": False,
        "can_tail_output": False,
        "can_resume": False,
    }
    values.update(overrides)
    return KernelSessionCapabilities(**values)


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
        catalog_facts=RUN_CATALOG_FACTS,
        heads=[older, newer],
        now=NOW + timedelta(seconds=2),
    )

    assert projection.commit_seq == 42
    assert projection.activity.state == "executing"
    assert projection.activity.source == "a-source"
    assert projection.activity.tool == "Shell"
    assert projection.control is None
    assert projection.fact_sources["activity"].source == "a-source"
    assert projection.fact_sources["activity"].subject_key == "run:run-1"
    assert projection.fact_sources["activity"].valid_until == NOW + timedelta(minutes=5)
    assert "control" not in projection.fact_sources
    assert projection.mode == "shadow"
    assert projection.disposition.state == "open"
    assert projection.run is not None and projection.run.id == "run-1"
    assert "transcript" in projection.unsupported_families
    assert "presentation" in projection.unsupported_families


def test_projector_derives_durable_mode_disposition_launch_and_run_without_process_inference():
    ended_at = NOW - timedelta(minutes=1)
    projection = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=43,
        catalog_facts={
            "catalog": {
                "started_at": (NOW - timedelta(hours=2)).isoformat(),
                "closed_at": NOW.isoformat(),
                "close_reason": "user_closed",
                "origin_kind": "console",
                "launch_surface": "web",
            },
            "readiness": {
                "state": "adopted",
                "execution_lifetime": "one_shot",
                "error_code": None,
                "error_message": None,
            },
            "latest_run": {
                "id": "run-1",
                "launch_origin": "longhouse_spawned",
                "started_at": (NOW - timedelta(hours=1)).isoformat(),
                "ended_at": ended_at.isoformat(),
                "exit_status": "completed",
            },
            "connections": [],
        },
        heads=[],
        now=NOW,
    )

    assert projection.mode == "console"
    assert projection.disposition.state == "closed"
    assert projection.disposition.closed_at == NOW
    assert projection.launch is not None and projection.launch.state == "adopted"
    assert projection.run is not None and projection.run.lifecycle == "ended"
    assert projection.run.id == "run-1"
    assert projection.run.ended_at == ended_at
    assert projection.run.end_reason == "completed"
    assert projection.activity.state == "unknown"
    assert projection.control is None


def test_projector_derives_helm_and_starting_from_durable_launch_facts():
    projection = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=44,
        catalog_facts={
            "catalog": {
                "started_at": NOW.isoformat(),
                "closed_at": None,
                "origin_kind": None,
                "launch_surface": None,
            },
            "readiness": {"state": "dispatched", "execution_lifetime": "live_control"},
            "latest_run": None,
            "connections": [],
        },
        heads=[],
        now=NOW,
    )

    assert projection.mode == "helm"
    assert projection.launch is not None and projection.launch.state == "dispatched"
    assert projection.run is not None and projection.run.lifecycle == "starting"
    assert projection.run.started_at == NOW


def test_projector_expires_activity_and_derives_control_lease_from_ttl():
    projection = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=9,
        catalog_facts=BOUND_CONTROL_CATALOG_FACTS,
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
    assert projection.control_run_id == "run-1"
    assert projection.control.valid_until == NOW + timedelta(seconds=60)
    assert projection.fact_sources["control"].source == "provider_control"
    assert projection.fact_sources["control"].subject_key == "connection:connection-1:lease-1"
    assert projection.control.actions.send_input.state == "available"
    assert projection.control.actions.interrupt.state == "available"
    assert projection.control.actions.terminate.reason == "not_granted"
    assert projection.control.actions.resume.reason == "unsupported"

    expired = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=9,
        catalog_facts=BOUND_CONTROL_CATALOG_FACTS,
        heads=[_control(observed_at=NOW, grants=["send_input"])],
        supported_operations={"send_input"},
        now=NOW + timedelta(seconds=60),
    )
    assert expired.control is None


def test_projector_rejects_heads_not_bound_to_durable_run_and_connection():
    wrong_run_activity = _activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=1))
    wrong_run_value = json.loads(wrong_run_activity["value_json"])
    wrong_run_value["run_id"] = "run-old"
    wrong_run_activity.update(
        subject_key="run:run-old",
        value_json=json.dumps(wrong_run_value),
        evidence_hash=canonical_evidence_hash(wrong_run_value),
    )
    unbound_control = _control(observed_at=NOW, grants=["send_input"])

    projection = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=10,
        catalog_facts=RUN_CATALOG_FACTS,
        heads=[wrong_run_activity, unbound_control],
        supported_operations={"send_input"},
        now=NOW,
    )

    assert projection.activity.state == "unknown"
    assert projection.control is None
    assert projection.fact_sources == {}
    assert projection.rejected_heads == 2
    assert projection.rejected_activity_heads == 1
    assert projection.rejected_control_heads == 1

    ended_run = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=11,
        catalog_facts={
            **BOUND_CONTROL_CATALOG_FACTS,
            "latest_run": {**RUN_CATALOG_FACTS["latest_run"], "ended_at": NOW.isoformat()},
        },
        heads=[_activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=1)), unbound_control],
        supported_operations={"send_input"},
        now=NOW,
    )
    assert ended_run.rejected_activity_heads == 1
    assert ended_run.rejected_control_heads == 1
    assert ended_run.control is None

    released_connection = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=12,
        catalog_facts={
            **BOUND_CONTROL_CATALOG_FACTS,
            "connections": [
                {**BOUND_CONTROL_CATALOG_FACTS["connections"][0], "state": "released", "released_at": NOW.isoformat()}
            ],
        },
        heads=[unbound_control],
        supported_operations={"send_input"},
        now=NOW,
    )
    assert released_connection.rejected_control_heads == 1
    assert released_connection.control is None


def test_projector_uses_stable_source_coordinate_for_exact_timestamp_tie():
    first = _activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=1), source="a-source")
    second = _activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=1), source="z-source")

    forward = project_shadow_session_state_facts(
        session_id="session-1", commit_seq=1, catalog_facts=RUN_CATALOG_FACTS, heads=[first, second], now=NOW
    )
    reverse = project_shadow_session_state_facts(
        session_id="session-1", commit_seq=1, catalog_facts=RUN_CATALOG_FACTS, heads=[second, first], now=NOW
    )

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
        catalog_facts=RUN_CATALOG_FACTS,
        heads=[cross_session, malformed, indexed_other_session, missing_source, valid],
        supported_operations={"send_input"},
        now=NOW,
    )

    assert projection.activity.state == "executing"
    assert projection.control is None
    assert projection.rejected_heads == 4


def test_projector_rejects_a_head_with_mismatched_evidence_hash():
    tampered = _activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=1))
    value = json.loads(tampered["value_json"])
    value["tool_name"] = "Changed after hashing"
    tampered["value_json"] = json.dumps(value)

    projection = project_shadow_session_state_facts(
        session_id="session-1",
        commit_seq=4,
        catalog_facts=RUN_CATALOG_FACTS,
        heads=[tampered],
        now=NOW,
    )

    assert projection.activity.state == "unknown"
    assert projection.rejected_heads == 1


def test_session_head_read_and_projection_share_fact_commit_snapshot(tmp_path):
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
            catalog_facts=RUN_CATALOG_FACTS,
            heads=heads,
            now=NOW,
        )
        connection.rollback()

    assert commit_seq == reduced.commit_seq == projection.commit_seq
    assert projection.activity.state == "quiescent"
    assert any("ix_fact_heads_session_family_recent" in str(row) for row in query_plan)


def test_served_projector_assembles_full_contract_at_snapshot_commit():
    served = project_served_session_state_facts(
        session_id="session-1",
        commit_seq=81,
        catalog_facts=BOUND_CONTROL_CATALOG_FACTS,
        heads=[
            _activity(observed_at=NOW, valid_until=NOW + timedelta(minutes=1)),
            _control(observed_at=NOW, grants=["interrupt", "send_input"]),
        ],
        supported_operations={"send_input", "interrupt", "terminate"},
        catalog_capabilities=_capabilities(
            run_id="run-1",
            control_plane="codex_app_server",
            control_label="live",
            live_control_available=True,
            can_send_input=True,
            can_interrupt=True,
            can_terminate=True,
        ),
        pending_interaction=None,
        transcript=SessionTranscriptFacts(convergence="current", last_append_at=NOW),
        host=SessionHostFacts(state="online", observed_at=NOW),
        now=NOW,
    )

    assert served.commit_seq == 81
    assert served.mode == "helm"
    assert served.activity.state == "executing"
    assert served.control.connection_id == "connection-1"
    assert served.control.control_plane == "codex_app_server"
    assert served.control.actions.send_input.state == "available"
    assert served.control.actions.terminate.reason == "not_granted"
    assert served.transcript.convergence == "current"
    assert served.host.state == "online"
    assert served.presentation.primary is not None
    assert served.presentation.primary.key == "executing"
    assert served.presentation.access is not None
    assert served.presentation.access.key == "live_control"


def test_served_projector_without_control_head_fails_actions_closed():
    served = project_served_session_state_facts(
        session_id="session-1",
        commit_seq=82,
        catalog_facts={
            **CATALOG_FACTS,
            "readiness": {"state": "adopted", "execution_lifetime": "live_control"},
        },
        heads=[],
        supported_operations={"send_input", "interrupt", "terminate"},
        catalog_capabilities=_capabilities(
            control_label="reattach",
            host_reattach_available=True,
            can_resume=True,
            control_owned=True,
        ),
        pending_interaction=None,
        transcript=SessionTranscriptFacts(convergence="unknown"),
        host=SessionHostFacts(state="unknown"),
        now=NOW,
    )

    assert served.mode == "helm"
    assert served.control.ownership == "owned"
    assert served.control.connection == "unknown"
    assert served.control.actions.send_input.reason == "control_unknown"
    assert served.control.actions.interrupt.reason == "control_unknown"
    assert served.control.actions.terminate.reason == "control_unknown"
    assert served.control.actions.reattach.state == "available"
    assert served.control.actions.resume.state == "available"


def test_exact_control_authorization_rejects_expiry_divergence_and_tampering():
    head = _control(observed_at=NOW, grants=["send_input"])
    allowed = authorize_exact_control_fact(
        session_id="session-1",
        run_id="run-1",
        provider="codex",
        connection_id="connection-1",
        lease_generation="lease-1",
        operation="send_input",
        heads=[head],
        supported_operations={"send_input"},
        now=NOW,
    )
    assert allowed.allowed is True

    expired = authorize_exact_control_fact(
        session_id="session-1",
        run_id="run-1",
        provider="codex",
        connection_id="connection-1",
        lease_generation="lease-1",
        operation="send_input",
        heads=[head],
        supported_operations={"send_input"},
        now=NOW + timedelta(minutes=2),
    )
    assert expired.allowed is False
    assert expired.reason == "lease_expired"

    diverged = authorize_exact_control_fact(
        session_id="session-1",
        run_id="different-run",
        provider="codex",
        connection_id="connection-1",
        lease_generation="lease-1",
        operation="send_input",
        heads=[head],
        supported_operations={"send_input"},
        now=NOW,
    )
    assert diverged.allowed is False
    assert diverged.reason == "identity_diverged"

    tampered = {**head, "evidence_hash": "0" * 64}
    rejected = authorize_exact_control_fact(
        session_id="session-1",
        run_id="run-1",
        provider="codex",
        connection_id="connection-1",
        lease_generation="lease-1",
        operation="send_input",
        heads=[tampered],
        supported_operations={"send_input"},
        now=NOW,
    )
    assert rejected.allowed is False
    assert rejected.reason == "control_head_rejected"
