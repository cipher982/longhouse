from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import create_engine
from sqlalchemy import insert
from sqlalchemy import select

from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.internal_sessions import is_factory_title_assurance_session
from zerg.services.internal_sessions import is_provider_evidence_cwd
from zerg.services.internal_sessions import is_provider_factory_cwd
from zerg.services.internal_sessions import is_provider_factory_machine_id
from zerg.services.internal_sessions import provider_proof_session_clause
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import build_managed_local_launch_plan


def test_provider_proof_python_and_sql_classifiers_agree_on_real_namespaces():
    """The SQL twin and the Python classifier agree on the namespaces we ship.

    Classification reads path and machine namespace only, so this is checked
    over workspaces and machine ids -- never over prompt text. The two are not
    equal in general; see the divergence test below for the shapes where they
    part and why that direction is the safe one.
    """

    workspaces = (
        ("/canaries/provider-live/codex/workspace", "laptop"),
        ("/Users/david/.longhouse/canaries/provider-live/opencode/proof/workspace", "laptop"),
        ("/Users/david/git/_wt/longhouse-provider-live-proof-owner", "laptop"),
        ("/var/lib/provider-factory/artifacts/_assurance/executions/run-1/cursor/evidence/workspace", "laptop"),
        ("/tmp/provider-factory-abc123/workspace", "laptop"),
        ("/private/tmp/provider-factory-abc123/workspace", "laptop"),
        ("/tmp/live-cell-run-cursor.coordination.directed.v1-abc123/evidence/cursor-workspace", "laptop"),
        ("/tmp/lhx-claude-coord-create-abc123/workspace", "laptop"),
        ("/private/tmp/longhouse-claude-real-print-abc/evidence/raw/claude/workspace", "laptop"),
        ("/Users/david/git/user-repo", "provider-factory-resume"),
        ("/Users/david/git/user-repo", "laptop"),
        ("/Users/david/git/provider-factory-project", "laptop"),
        ("/Users/david/git/live-cell-run-project", "laptop"),
        ("/Users/david/git/evidence/raw/my-project", "laptop"),
    )
    metadata = MetaData()
    sessions = Table(
        "sessions",
        metadata,
        Column("session_id", String, primary_key=True),
        Column("cwd", String),
        Column("machine_id", String),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(sessions),
            [{"session_id": str(index), "cwd": cwd, "machine_id": machine_id} for index, (cwd, machine_id) in enumerate(workspaces)],
        )
        sql_results = dict(connection.execute(select(sessions.c.session_id, provider_proof_session_clause(sessions))).all())

    python_results = [classify_provider_proof_environment(cwd=cwd, machine_id=machine_id) == "test" for cwd, machine_id in workspaces]

    assert [bool(sql_results[str(index)]) for index in range(len(workspaces))] == python_results
    assert python_results == [True] * 10 + [False] * 4


def test_sql_candidate_clause_is_a_subset_of_the_python_classifier():
    """SQL narrows, Python decides -- and it must stay that way round.

    The SQL clause is a LIKE approximation of the Python predicates, so shapes
    exist where Python says "test" and SQL does not: SQL's
    ``/tmp/%/evidence/raw/%`` requires an intermediate segment that Python's
    ``startswith`` does not. That costs the repair tool recall -- rows it will
    not offer to fix -- which is safe.

    The opposite direction would not be. If SQL ever selected a row the
    classifier rejects, the repair tool would present a real user session as
    proof traffic. Nothing else pins that direction, so this does.
    """

    divergent = ("/tmp/evidence/raw/claude/workspace", "laptop")
    metadata = MetaData()
    sessions = Table(
        "sessions",
        metadata,
        Column("session_id", String, primary_key=True),
        Column("cwd", String),
        Column("machine_id", String),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(sessions),
            [{"session_id": "0", "cwd": divergent[0], "machine_id": divergent[1]}],
        )
        matched = dict(connection.execute(select(sessions.c.session_id, provider_proof_session_clause(sessions))).all())

    assert classify_provider_proof_environment(cwd=divergent[0], machine_id=divergent[1]) == "test"
    assert not bool(matched["0"]), "SQL must stay no broader than the Python classifier"


def test_provider_factory_evidence_workspace_is_automation_classified_without_hiding_user_repos():
    assert is_provider_factory_cwd(
        "/var/lib/provider-factory/artifacts/_assurance/executions/run-1/cursor/process_loss/evidence/cursor-workspace"
    )
    assert is_provider_factory_cwd("/tmp/live-cell-run-cursor.coordination.directed.v1-abc123/evidence/cursor-workspace")
    assert is_provider_factory_machine_id("provider-factory-resume")
    assert classify_provider_proof_environment(cwd="/tmp/lhx-claude-coord-create-abc123/workspace") == "test"
    assert classify_provider_proof_environment(machine_id="provider-factory-resume") == "test"
    assert not is_provider_factory_cwd("/Users/davidrose/git/control-plane/provider_factory")
    assert not is_provider_factory_cwd("/Users/davidrose/git/provider-factory-project")
    assert not is_provider_factory_cwd("/Users/davidrose/git/live-cell-run-project")
    assert (
        classify_provider_proof_environment(
            cwd="/Users/davidrose/git/user-repo",
            machine_id="provider-factory-resume",
        )
        == "test"
    )


def test_factory_title_assurance_requires_every_typed_identity_field():
    exact = {
        "provider": "claude",
        "environment": "local",
        "project": "longhouse-title-assurance",
        "cwd": "/factory/title-assurance",
        "machine_id": "provider-factory-resume",
        "origin_kind": "console",
        "hidden_from_default_timeline": True,
        "launch_actor": "automation",
        "launch_surface": "factory_assurance",
    }
    near_misses = {
        "provider": "codex",
        "environment": "test",
        "project": "longhouse-title-assurance-near-miss",
        "cwd": "/factory/title-assurance-near-miss",
        "machine_id": "provider-factory-other",
        "origin_kind": "test_or_canary",
        "hidden_from_default_timeline": False,
        "launch_actor": "human_ui",
        "launch_surface": "test",
    }

    assert is_factory_title_assurance_session(**exact)
    for field, value in near_misses.items():
        assert not is_factory_title_assurance_session(**{**exact, field: value}), field


def test_temporary_raw_provider_evidence_workspace_is_automation_classified():
    cwd = "/private/tmp/longhouse-claude-real-print-abc/evidence/raw/claude/workspace"

    assert is_provider_evidence_cwd(cwd)
    assert classify_provider_proof_environment(cwd=cwd) == "test"
    assert not is_provider_evidence_cwd("/Users/davidrose/git/evidence/raw/my-project")


def test_managed_canary_launch_carries_hidden_provenance_without_hiding_normal_helm():
    canary = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=42,
            runner_target="provider-factory-resume",
            cwd="/Users/davidrose/git/user-repo",
            provider="cursor",
            project="managed-local",
            machine_name="provider-factory-resume",
        )
    )
    human = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=42,
            runner_target="cinder",
            cwd="/Users/davidrose/git/zerg/longhouse",
            provider="codex",
            project="longhouse",
            machine_name="cinder",
        )
    )

    assert (canary.environment, canary.origin_kind, canary.hidden_from_default_timeline) == (
        "test",
        "test_or_canary",
        1,
    )
    assert (human.environment, human.origin_kind, human.hidden_from_default_timeline) == ("development", None, 0)
