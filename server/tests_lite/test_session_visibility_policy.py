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
from zerg.services.session_visibility_policy import visible_in_test_scope

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
    ),
    # An ordinary human session on an ordinary machine. It stays visible no
    # matter what its transcript says -- prompt text is content, not evidence.
    SessionVisibilityFacts(
        provider="claude",
        project="longhouse",
        environment="local",
        machine_id="cinder",
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
                    "hidden_from_default_timeline": 0,
                }
                for index, facts in enumerate(scalar_cases)
            ],
        )
        sql_results = dict(connection.execute(select(sessions.c.session_id, known_hidden_evidence_clause(sessions))).all())

    assert [bool(sql_results[str(index)]) for index in range(len(scalar_cases))] == [
        evaluate_origin_visibility(facts).system_hidden for facts in scalar_cases
    ]


def test_visibility_facts_cannot_carry_prompt_text():
    """Prompt content is not intent, and the type makes that unarguable.

    This used to be a behavioral test: hand the policy a Hatch contract or a
    canary token and check it stayed visible anyway. The field is gone now, so
    the guarantee is structural -- there is no longer a way to route transcript
    text into a visibility decision. Re-adding one breaks this test first.
    """
    assert "first_user_message" not in SessionVisibilityFacts.__dataclass_fields__
    assert not [
        name for name in SessionVisibilityFacts.__dataclass_fields__ if "message" in name or "prompt" in name
    ]

    ordinary = SessionVisibilityFacts(
        provider="claude",
        project="longhouse",
        environment="local",
        machine_id="cinder",
    )
    decision = evaluate_origin_visibility(ordinary)
    assert decision.system_hidden is False
    assert decision.title_origin_eligible is True


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


def test_declared_harness_launch_surface_is_test_scope_evidence():
    """A QA harness that declares its launch surface stays out of the timeline.

    `console-served-state-e2e` created real Console sessions with
    `launch_surface="product-e2e"` and an ordinary `development` environment,
    so every run surfaced in the user's iOS "New results" section. The
    declaration was recorded and then ignored by the policy; this pins that it
    is evidence, and that an explicit test-scope read still reveals it.
    """

    facts = SessionVisibilityFacts(
        provider="claude",
        project="console-served-state-e2e",
        environment="development",
        origin_kind="console",
        launch_actor="user",
        launch_surface="product-e2e",
        machine_id="cinder",
    )
    decision = evaluate_origin_visibility(facts)

    assert decision.system_hidden is True
    assert decision.reason_keys == ("test_launch_surface",)
    assert visible_in_test_scope(decision) is True

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
            [
                {
                    "session_id": "harness",
                    "provider": "claude",
                    "project": "console-served-state-e2e",
                    "environment": "development",
                    "origin_kind": "console",
                    "launch_actor": "user",
                    "launch_surface": "product-e2e",
                    "machine_id": "cinder",
                    # Rows created before this policy carry a stale 0; the
                    # evidence clause must hide them without a backfill.
                    "hidden_from_default_timeline": 0,
                },
                {
                    "session_id": "human-console",
                    "provider": "claude",
                    "project": "longhouse",
                    "environment": "development",
                    "origin_kind": "console",
                    "launch_actor": "user",
                    "launch_surface": "ios",
                    "machine_id": "cinder",
                    "hidden_from_default_timeline": 0,
                },
            ],
        )
        default_scope = dict(
            connection.execute(select(sessions.c.session_id, effective_system_hidden_clause(sessions))).all()
        )
        test_scope = dict(
            connection.execute(
                select(sessions.c.session_id, effective_system_hidden_clause(sessions, include_test=True))
            ).all()
        )

    assert bool(default_scope["harness"]) is True
    assert bool(default_scope["human-console"]) is False
    # `include_test=true` is how the QA harnesses find their own sessions.
    assert bool(test_scope["harness"]) is False
