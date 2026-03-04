"""Guardrails for Oikos prompt/tool contract drift."""

from zerg.prompts.templates import BASE_OIKOS_PROMPT
from zerg.tools.builtin.oikos_tools import OIKOS_TOOL_NAMES
from zerg.tools.builtin.oikos_tools import TOOLS


def _tool_description(tool_name: str) -> str:
    for tool in TOOLS:
        if tool.name == tool_name:
            return tool.description or ""
    raise AssertionError(f"Tool not found in TOOLS: {tool_name}")


def test_prompt_teaches_workspace_first_spawn_contract():
    assert "spawn_workspace_commis" in BASE_OIKOS_PROMPT
    assert "(PRIMARY)" in BASE_OIKOS_PROMPT
    assert "(DEPRECATED)" not in BASE_OIKOS_PROMPT


def test_prompt_uses_wait_for_commis_not_removed_wait_parameter():
    assert "wait_for_commis(job_id)" in BASE_OIKOS_PROMPT
    assert "wait parameter" not in BASE_OIKOS_PROMPT
    assert "wait=True" not in BASE_OIKOS_PROMPT
    assert "wait=False" not in BASE_OIKOS_PROMPT


def test_tool_descriptions_match_prompt_semantics():
    workspace_description = _tool_description("spawn_workspace_commis")
    assert "PRIMARY" in workspace_description


def test_oikos_tool_names_do_not_include_removed_spawn_alias():
    assert "spawn_commis" not in OIKOS_TOOL_NAMES
