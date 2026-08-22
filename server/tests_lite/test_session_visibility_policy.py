from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import create_engine
from sqlalchemy import insert
from sqlalchemy import select

from zerg.services.session_visibility_policy import SessionVisibilityFacts
from zerg.services.session_visibility_policy import effective_system_hidden_clause
from zerg.services.session_visibility_policy import evaluate_origin_visibility
from zerg.services.session_visibility_policy import known_hidden_evidence_clause
from zerg.services.session_visibility_policy import primary_worker_only_clause
from zerg.services.session_visibility_policy import title_origin_eligible_clause

CASES = (
    SessionVisibilityFacts(provider="codex", project="longhouse", environment="local", machine_id="cinder"),
    SessionVisibilityFacts(provider="codex", project="longhouse", environment="test", machine_id="cinder"),
    SessionVisibilityFacts(
        provider="codex",
        project="longhouse",
        environment="local",
        launch_actor="automation",
        launch_surface="terminal",
        machine_id="cinder",
    ),
    SessionVisibilityFacts(
        provider="codex",
        project="provider-console-codex",
        environment="local",
        launch_actor="user",
        launch_surface="test",
        machine_id="provider-factory-resume",
        first_user_message="Reply with exactly LH_CODEX_CONSOLE_deadbeef and nothing else.",
    ),
    SessionVisibilityFacts(
        provider="claude",
        project="longhouse",
        environment="local",
        machine_id="cinder",
        first_user_message=(
            "Hatch execution contract:\nThis is a single bounded, non-interactive run. A human is waiting for a useful answer."
        ),
    ),
    SessionVisibilityFacts(
        provider="claude",
        project="longhouse",
        environment="local",
        machine_id="cinder",
        first_user_message="Please investigate LH_CODEX_CONSOLE_deadbeef and our Hatch execution contract",
    ),
    SessionVisibilityFacts(provider="canary", project="canary", environment="local", machine_id="cinder"),
    SessionVisibilityFacts(
        provider="cursor", project="longhouse", environment="local", machine_id="cinder", primary_thread_is_worker_only=True
    ),
)


def test_python_and_sql_visibility_evidence_have_scalar_parity():
    metadata = MetaData()
    sessions = Table(
        "sessions",
        metadata,
        Column("session_id", String, primary_key=True),
        Column("provider", String),
        Column("project", String),
        Column("environment", String),
        Column("origin_kind", String),
        Column("launch_actor", String),
        Column("launch_surface", String),
        Column("cwd", String),
        Column("machine_id", String),
        Column("first_user_message_preview", String),
        Column("hidden_from_default_timeline", Integer, default=0),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    scalar_cases = CASES[:-1]
    with engine.begin() as connection:
        connection.execute(
            insert(sessions),
            [
                {
                    "session_id": str(index),
                    "provider": facts.provider,
                    "project": facts.project,
                    "environment": facts.environment,
                    "origin_kind": facts.origin_kind,
                    "launch_actor": facts.launch_actor,
                    "launch_surface": facts.launch_surface,
                    "cwd": facts.cwd,
                    "machine_id": facts.machine_id,
                    "first_user_message_preview": facts.first_user_message,
                    "hidden_from_default_timeline": 0,
                }
                for index, facts in enumerate(scalar_cases)
            ],
        )
        sql_results = dict(connection.execute(select(sessions.c.session_id, known_hidden_evidence_clause(sessions))).all())

    assert [bool(sql_results[str(index)]) for index in range(len(scalar_cases))] == [
        evaluate_origin_visibility(facts).system_hidden for facts in scalar_cases
    ]


def test_persisted_projection_is_read_defense_but_not_title_authority():
    metadata = MetaData()
    sessions = Table(
        "sessions",
        metadata,
        Column("session_id", String, primary_key=True),
        Column("provider", String),
        Column("project", String),
        Column("environment", String),
        Column("origin_kind", String),
        Column("launch_actor", String),
        Column("launch_surface", String),
        Column("cwd", String),
        Column("machine_id", String),
        Column("first_user_message_preview", String),
        Column("hidden_from_default_timeline", Integer),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(sessions),
            {
                "session_id": "human-stale-hidden",
                "provider": "codex",
                "project": "longhouse",
                "environment": "local",
                "launch_actor": "human_shell",
                "machine_id": "cinder",
                "first_user_message_preview": "Fix timeline visibility",
                "hidden_from_default_timeline": 1,
            },
        )
        row = connection.execute(
            select(
                effective_system_hidden_clause(sessions),
                title_origin_eligible_clause(sessions),
            )
        ).one()

    assert bool(row[0]) is True
    assert bool(row[1]) is True


def test_graph_worker_evidence_remains_a_store_level_input():
    decision = evaluate_origin_visibility(CASES[-1])

    assert decision.system_hidden is True
    assert decision.reason_keys == ("worker_only",)
    assert decision.title_origin_eligible is False


def test_include_test_discounts_only_the_test_environment_projection():
    metadata = MetaData()
    sessions = Table(
        "sessions",
        metadata,
        Column("session_id", String, primary_key=True),
        Column("provider", String),
        Column("project", String),
        Column("environment", String),
        Column("origin_kind", String),
        Column("launch_actor", String),
        Column("cwd", String),
        Column("machine_id", String),
        Column("first_user_message_preview", String),
        Column("hidden_from_default_timeline", Integer),
    )
    threads = Table(
        "session_threads",
        metadata,
        Column("id", String, primary_key=True),
        Column("session_id", String),
        Column("is_primary", Integer),
        Column("branch_kind", String),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(sessions),
            [
                {
                    "session_id": "ordinary-test",
                    "provider": "codex",
                    "environment": "test",
                    "launch_actor": "human_shell",
                    "hidden_from_default_timeline": 1,
                },
                {
                    "session_id": "automated-test",
                    "provider": "codex",
                    "environment": "test",
                    "launch_actor": "automation",
                    "hidden_from_default_timeline": 1,
                },
                {
                    "session_id": "worker-test",
                    "provider": "codex",
                    "environment": "test",
                    "launch_actor": "human_shell",
                    "hidden_from_default_timeline": 1,
                },
            ],
        )
        connection.execute(
            insert(threads),
            {
                "id": "worker-primary",
                "session_id": "worker-test",
                "is_primary": 1,
                "branch_kind": "subagent",
            },
        )
        results = dict(
            connection.execute(
                select(
                    sessions.c.session_id,
                    effective_system_hidden_clause(
                        sessions,
                        include_test=True,
                        worker_only_evidence=primary_worker_only_clause(sessions, threads),
                    ),
                )
            ).all()
        )

    assert bool(results["ordinary-test"]) is False
    assert bool(results["automated-test"]) is True
    assert bool(results["worker-test"]) is True
