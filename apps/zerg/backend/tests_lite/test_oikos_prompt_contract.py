"""Guardrails for Oikos prompt/tool contract drift."""

from zerg.prompts.templates import BASE_OIKOS_ASSISTANT_PROMPT
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


def test_prompt_defines_explicit_dispatch_lanes():
    assert "Dispatch Contract" in BASE_OIKOS_PROMPT
    assert "Direct response" in BASE_OIKOS_PROMPT
    assert "Quick-tool execution" in BASE_OIKOS_PROMPT
    assert "CLI delegation (`spawn_workspace_commis`)" in BASE_OIKOS_PROMPT
    assert "Prefer Direct → Quick-tool → CLI delegation" in BASE_OIKOS_PROMPT


def test_prompt_uses_wait_for_commis_not_removed_wait_parameter():
    assert "wait_for_commis(job_id)" in BASE_OIKOS_PROMPT
    assert "wait parameter" not in BASE_OIKOS_PROMPT
    assert "wait=True" not in BASE_OIKOS_PROMPT
    assert "wait=False" not in BASE_OIKOS_PROMPT


def test_prompt_documents_direct_runner_exec_for_lightweight_commands():
    assert "runner_exec" in BASE_OIKOS_PROMPT
    assert "single lightweight runner command" in BASE_OIKOS_PROMPT
    assert "already-connected runners" in BASE_OIKOS_PROMPT


def test_prompt_requires_runner_verification_before_claiming_offline():
    assert "Never guess whether a runner is online/offline from memory" in BASE_OIKOS_PROMPT
    assert "verify with `runner_list`" in BASE_OIKOS_PROMPT
    assert "Before saying a runner is unavailable or offline" in BASE_OIKOS_PROMPT


def test_assistant_prompt_mentions_runner_verification_rule():
    assert "verify with `runner_list` before calling it offline" in BASE_OIKOS_ASSISTANT_PROMPT
    assert "use `runner_exec` for lightweight commands" in BASE_OIKOS_ASSISTANT_PROMPT


def test_tool_descriptions_match_prompt_semantics():
    workspace_description = _tool_description("spawn_workspace_commis")
    assert "PRIMARY" in workspace_description


def test_prompt_documents_backend_intent_mapping_for_spawn():
    assert "backend intent mapping" in BASE_OIKOS_PROMPT.lower()
    for backend in ("zai", "codex", "gemini", "bedrock", "anthropic"):
        assert f"`{backend}`" in BASE_OIKOS_PROMPT


def test_oikos_tool_names_do_not_include_removed_spawn_alias():
    assert "spawn_commis" not in OIKOS_TOOL_NAMES
