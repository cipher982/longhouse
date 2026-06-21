from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("TESTING", "1")

from zerg.services.agents.identity_resolver import ObservedLineageEdge
from zerg.services.agents.identity_resolver import ObservedSession
from zerg.services.agents.identity_resolver import resolve_session_projection


@pytest.mark.parametrize(
    (
        "case",
        "lineage_kind",
        "parent_resolved",
        "projection_kind",
        "visibility",
        "branch_kind",
        "attach_to_parent",
        "relink_later",
    ),
    [
        ("root", None, False, "root", "timeline", "root", False, False),
        ("parent_task", "task_child", True, "subagent", "hidden", "subagent", True, False),
        ("orphan_task", "task_child", False, "subagent", "hidden", "subagent", False, True),
        ("fork", "fork", False, "fork", "timeline", "fork", False, False),
        ("unknown_parent", "unknown", False, "linked", "timeline", "root", False, False),
        ("agent_switch", "agent_switch", False, "inline_event", "inline", None, False, False),
        ("async_prompt", "async_prompt", False, "run_control", "control", None, False, False),
    ],
)
def test_session_projection_semantics_are_provider_neutral(
    case,
    lineage_kind,
    parent_resolved,
    projection_kind,
    visibility,
    branch_kind,
    attach_to_parent,
    relink_later,
):
    lineage = None
    if lineage_kind:
        lineage = ObservedLineageEdge(
            provider="test-provider",
            kind=lineage_kind,
            parent_provider_session_id="parent-session",
            child_provider_session_id=f"{case}-child",
            parent_tool_call_id="call-task" if lineage_kind == "task_child" else None,
        )
    session = ObservedSession(
        provider="test-provider",
        provider_session_id=f"{case}-child",
        lineage=lineage,
    )

    decision = resolve_session_projection(session, parent_thread_resolved=parent_resolved)

    assert decision.projection_kind == projection_kind
    assert decision.visibility == visibility
    assert decision.branch_kind == branch_kind
    assert decision.attach_to_parent is attach_to_parent
    assert decision.relink_later is relink_later


def test_parent_alias_is_recorded_for_visible_lineage_and_task_children():
    for lineage_kind in ("task_child", "fork", "unknown"):
        session = ObservedSession(
            provider="test-provider",
            provider_session_id="child-session",
            lineage=ObservedLineageEdge(
                provider="test-provider",
                kind=lineage_kind,
                parent_provider_session_id="parent-session",
                child_provider_session_id="child-session",
            ),
        )

        decision = resolve_session_projection(session)

        assert decision.record_parent_alias is True
