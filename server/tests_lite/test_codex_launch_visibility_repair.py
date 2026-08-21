from __future__ import annotations

from dataclasses import replace

import pytest

from zerg.services.codex_launch_visibility_repair import CodexLaunchVisibilityRepairFacts
from zerg.services.codex_launch_visibility_repair import plan_codex_launch_visibility_repair


def _facts(**overrides) -> CodexLaunchVisibilityRepairFacts:
    values = {
        "session_id": "1cb3a7d3-a8dc-4d7d-9788-80f9b0f54cd1",
        "provider": "codex",
        "mode": "helm",
        "execution_home": "managed_local",
        "control_ownership": "owned",
        "fresh_exact_terminal_attached": True,
        "fresh_exact_active_run": False,
        "launch_actor": None,
        "launch_surface": None,
        "origin_kind": None,
        "is_sidechain": False,
        "environment": "development",
        "hidden_from_default_timeline": True,
        "user_hidden_from_timeline": False,
    }
    values.update(overrides)
    return CodexLaunchVisibilityRepairFacts(**values)


def test_repair_plan_names_every_eligibility_fact_in_its_compare_and_set():
    plan = plan_codex_launch_visibility_repair(_facts())

    assert plan is not None
    assert plan.updates == {
        "launch_actor": "human_shell",
        "launch_surface": "terminal",
        "hidden_from_default_timeline": False,
    }
    assert plan.compare_and_set == {
        "provider": "codex",
        "mode": "helm",
        "execution_home": "managed_local",
        "control_ownership": "owned",
        "fresh_exact_terminal_attached": True,
        "fresh_exact_active_run": False,
        "launch_actor": None,
        "launch_surface": None,
        "origin_kind": None,
        "is_sidechain": False,
        "environment": "development",
        "hidden_from_default_timeline": True,
        "user_hidden_from_timeline": False,
    }


def test_fresh_exact_active_run_is_sufficient_without_terminal_attachment():
    assert (
        plan_codex_launch_visibility_repair(
            _facts(fresh_exact_terminal_attached=False, fresh_exact_active_run=True)
        )
        is not None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "claude"),
        ("mode", "shadow"),
        ("execution_home", "unmanaged_local"),
        ("control_ownership", "unowned"),
        ("launch_actor", "human_shell"),
        ("launch_surface", "terminal"),
        ("origin_kind", "test_or_canary"),
        ("is_sidechain", True),
        ("environment", None),
        ("environment", "test"),
        ("environment", "e2e"),
        ("hidden_from_default_timeline", False),
        ("user_hidden_from_timeline", True),
    ],
)
def test_repair_refuses_every_ambiguous_or_user_hidden_row(field, value):
    assert plan_codex_launch_visibility_repair(replace(_facts(), **{field: value})) is None


def test_repair_requires_current_exact_attachment_or_active_run():
    assert (
        plan_codex_launch_visibility_repair(
            _facts(fresh_exact_terminal_attached=False, fresh_exact_active_run=False)
        )
        is None
    )
