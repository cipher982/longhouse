from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import create_engine
from sqlalchemy import insert
from sqlalchemy import select

from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.internal_sessions import is_hatch_execution_contract
from zerg.services.internal_sessions import is_factory_title_assurance_session
from zerg.services.internal_sessions import is_provider_coordination_awareness_marker
from zerg.services.internal_sessions import is_provider_evidence_cwd
from zerg.services.internal_sessions import is_provider_factory_cwd
from zerg.services.internal_sessions import is_provider_factory_machine_id
from zerg.services.internal_sessions import is_provider_product_canary_marker
from zerg.services.internal_sessions import is_provider_reply_exact_marker
from zerg.services.internal_sessions import provider_proof_session_clause
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import build_managed_local_launch_plan


def test_cursor_product_canary_marker_is_exact_and_bounded():
    marker = "Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_91b38069e7"

    assert is_provider_product_canary_marker(marker)
    assert classify_provider_proof_environment(first_user_text=marker) == "test"
    assert not is_provider_product_canary_marker("Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_not-hex")
    assert classify_provider_proof_environment(first_user_text="Please fix LONGHOUSE_CURSOR_PRODUCT_ONE_91b38069e7") is None


def test_provider_reply_exact_marker_is_bounded_to_longhouse_canary_shapes():
    marker = "Reply exactly LONGHOUSE_OPENCODE_RESUME_SEED_94afb881e8684faca669fefd44ec40 and nothing else."

    assert is_provider_reply_exact_marker(marker)
    assert is_provider_reply_exact_marker("Reply with exactly LONGHOUSE_CLAUDE_TURN_BOUNDARY_27eeb18bb0b349b0b1778e83a51c7b6e and nothing else.")
    assert classify_provider_proof_environment(first_user_text=marker) == "test"
    assert is_provider_reply_exact_marker("Reply exactly LONGHOUSE_CODEX_COLD_RESUME_SEED_8ee711c900c448f18c7762b3fa0c649c")
    assert is_provider_reply_exact_marker("Reply exactly FRESH_AFTER_CANCEL_OK.")
    assert is_provider_reply_exact_marker(
        "Reply with exactly LH_CODEX_CONSOLE_5bc00d062a0444ea8450f0a3ff822a45 and nothing else."
    )
    assert is_provider_reply_exact_marker(
        "Reply with exactly LH_CURSOR_CONSOLE_CANARY_e2fe0989b70f433ab3724ef10ba84690 and nothing else. Do not use tools."
    )
    assert is_provider_reply_exact_marker(
        "Reply with exactly LH_PROBE_CODEX_MANAGED_latency-b2c-dbclean-4x-20260803T031500Z-i01 and nothing else."
    )
    assert is_provider_reply_exact_marker("Reply with exactly lh-hosted-claude-stress-01-deadbeef")
    assert is_provider_reply_exact_marker(
        "Reply with exactly LH_TMUX_TURN_1_31455 on the first line and nothing else."
    )
    assert is_provider_reply_exact_marker(
        "Reply with exactly lh-claude-stress-01-582a5e64 and nothing else. Do not use any tools."
    )
    assert not is_provider_reply_exact_marker("Reply exactly OK")
    assert not is_provider_reply_exact_marker("Please reply exactly LONGHOUSE_OPENCODE_RESUME_SEED_abc123")
    assert not is_provider_reply_exact_marker("Please investigate LH_PROBE_CODEX_MANAGED_deadbeef")


def test_provider_reply_exact_python_and_sql_classifiers_have_parity():
    prompts = (
        "Reply exactly LONGHOUSE_OPENCODE_RESUME_SEED_94afb881e8684faca669fefd44ec40 and nothing else.",
        "Reply with exactly LONGHOUSE_CLAUDE_PRINT_74694349fb694c97af560ac98572f989 and nothing else.\n",
        "\tReply with exactly LONGHOUSE_CODEX_COLD_RESUME_SEED_8ee711c900c448f18c7762b3fa0c649c\r\n",
        "Reply exactly FRESH_AFTER_CANCEL_OK.",
        "Reply exactly OK",
        "Please reply exactly LONGHOUSE_OPENCODE_RESUME_SEED_abc123",
        "Reply with exactly LONGHOUSE_CLAUDE_PRINT_not-hex and nothing else.\n",
        "Reply with exactly LONGHOUSE_CLAUDE_PRINT_abcdefZabcdef",
        "reply with exactly LONGHOUSE_CLAUDE_PRINT_abcdef",
        "Reply with exactly LONGHOUSE_CLAUDE_PRINT_abcdef\v",
        "Reply with exactly LONGHOUSE_CLAUDE_PRINT_abcdef\f",
        "Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_91b38069e7",
        "LONGHOUSE_CLAUDE_NOREPLY_74694349fb694c97af560ac98572f989",
        "Reply with exactly LH_CLAUDE_CONSOLE_57384914d8ed4467b545f0e0cf9b0bd3 and nothing else.",
    )
    metadata = MetaData()
    sessions = Table(
        "sessions",
        metadata,
        Column("session_id", String, primary_key=True),
        Column("cwd", String),
        Column("machine_id", String),
        Column("first_user_message_preview", String),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(sessions),
            [
                {
                    "session_id": str(index),
                    "cwd": "/Users/test/repo",
                    "machine_id": "laptop",
                    "first_user_message_preview": prompt,
                }
                for index, prompt in enumerate(prompts)
            ],
        )
        sql_results = dict(
            connection.execute(select(sessions.c.session_id, provider_proof_session_clause(sessions))).all()
        )

    assert [bool(sql_results[str(index)]) for index in range(len(prompts))] == [
        classify_provider_proof_environment(first_user_text=prompt) == "test" for prompt in prompts
    ]


def test_provider_factory_evidence_workspace_is_automation_classified_without_hiding_user_repos():
    assert is_provider_factory_cwd(
        "/var/lib/provider-factory/artifacts/_assurance/executions/run-1/cursor/process_loss/evidence/cursor-workspace"
    )
    assert is_provider_factory_cwd("/tmp/live-cell-run-cursor.coordination.directed.v1-abc123/evidence/cursor-workspace")
    assert is_provider_factory_machine_id("provider-factory-resume")
    assert is_provider_coordination_awareness_marker("print exactly LONGHOUSE_CURSOR_COORD_AWARENESS_f70043f7b0")
    assert classify_provider_proof_environment(
        cwd="/tmp/lhx-claude-coord-create-abc123/workspace"
    ) == "test"
    assert classify_provider_proof_environment(machine_id="provider-factory-resume") == "test"
    assert not is_provider_factory_cwd("/Users/davidrose/git/control-plane/provider_factory")
    assert not is_provider_factory_cwd("/Users/davidrose/git/provider-factory-project")
    assert not is_provider_factory_cwd("/Users/davidrose/git/live-cell-run-project")
    assert classify_provider_proof_environment(
        cwd="/Users/davidrose/git/user-repo",
        machine_id="provider-factory-resume",
        first_user_text="Review the deployment plan",
    ) == "test"


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
    assert classify_provider_proof_environment(cwd=cwd, first_user_text="Review the deployment plan") == "test"
    assert not is_provider_evidence_cwd("/Users/davidrose/git/evidence/raw/my-project")


def test_hatch_execution_contract_is_exact_and_automation_classified():
    contract = (
        "Hatch execution contract:\n"
        "This is a single bounded, non-interactive run. A human is waiting for a useful answer."
    )

    assert is_hatch_execution_contract(contract)
    assert is_hatch_execution_contract(
        "Hatch execution contract:\n"
        "This is a single bounded, non-interactive run with a time budget of about 15 minutes."
    )
    assert not is_hatch_execution_contract("Hatch execution contract: please help me write one")


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
