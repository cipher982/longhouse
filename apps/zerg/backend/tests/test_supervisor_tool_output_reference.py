"""Tests for large tool output storage in supervisor engine."""

import re

import pytest

from zerg.services import tool_output_store as store_module


class DummyTool:
    def __init__(self, payload: str):
        self._payload = payload

    def invoke(self, _args):
        return self._payload


@pytest.mark.asyncio
async def test_execute_tool_stores_large_output(monkeypatch, tmp_path):
    from zerg.services import supervisor_react_engine as engine

    class TestStore(store_module.ToolOutputStore):
        def __init__(self):
            super().__init__(base_path=str(tmp_path))

    monkeypatch.setattr(store_module, "ToolOutputStore", TestStore)
    monkeypatch.setenv("SUPERVISOR_TOOL_OUTPUT_MAX_CHARS", "100")
    monkeypatch.setenv("SUPERVISOR_TOOL_OUTPUT_PREVIEW_CHARS", "40")

    payload = "x" * 500
    tool_call = {"name": "dummy_tool", "args": {}, "id": "tool-1"}

    message = await engine._execute_tool(
        tool_call,
        {"dummy_tool": DummyTool(payload)},
        run_id=1,
        owner_id=123,
    )

    assert "[TOOL_OUTPUT:" in message.content

    match = re.search(r"artifact_id=([a-f0-9]+)", message.content)
    assert match
    artifact_id = match.group(1)

    store = TestStore()
    stored = store.read_output(owner_id=123, artifact_id=artifact_id)
    assert stored == payload
