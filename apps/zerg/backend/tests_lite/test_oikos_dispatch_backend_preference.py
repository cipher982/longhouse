"""Dispatch-contract tests for backend preference normalization."""

from zerg.services.oikos_react_engine import _apply_dispatch_contract
from zerg.services.oikos_react_engine import _infer_requested_backend
from zerg.types.messages import HumanMessage
from zerg.types.messages import SystemMessage


def _messages(prompt: str):
    return [
        SystemMessage(content="You are Oikos."),
        HumanMessage(content=prompt),
    ]


def test_infer_requested_backend_detects_known_backends():
    assert _infer_requested_backend(_messages("Use codex for this")) == "codex"
    assert _infer_requested_backend(_messages("run this with Gemini")) == "gemini"
    assert _infer_requested_backend(_messages("delegate via z.ai")) == "zai"
    assert _infer_requested_backend(_messages("use bedrock")) == "bedrock"
    assert _infer_requested_backend(_messages("anthropic only")) == "anthropic"


def test_apply_dispatch_contract_injects_backend_into_spawn_calls():
    tool_calls = [
        {
            "id": "call_1",
            "name": "spawn_workspace_commis",
            "args": {"task": "Check disk usage"},
        }
    ]

    normalized = _apply_dispatch_contract(tool_calls, _messages("Use gemini backend for this"))

    assert normalized is not None
    assert normalized[0]["args"]["backend"] == "gemini"


def test_apply_dispatch_contract_does_not_override_explicit_backend():
    tool_calls = [
        {
            "id": "call_1",
            "name": "spawn_workspace_commis",
            "args": {"task": "Check disk usage", "backend": "codex"},
        }
    ]

    normalized = _apply_dispatch_contract(tool_calls, _messages("Use gemini backend for this"))

    assert normalized is not None
    assert normalized[0]["args"]["backend"] == "codex"


def test_apply_dispatch_contract_leaves_non_spawn_tools_unchanged():
    tool_calls = [
        {
            "id": "call_1",
            "name": "search_memory",
            "args": {"query": "deployment notes"},
        }
    ]

    normalized = _apply_dispatch_contract(tool_calls, _messages("Use codex for this"))

    assert normalized == tool_calls
