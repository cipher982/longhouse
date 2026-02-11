"""Tests for large tool output storage in oikos engine."""

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
    from zerg.services import oikos_react_engine as engine

    class TestStore(store_module.ToolOutputStore):
        def __init__(self):
            super().__init__(base_path=str(tmp_path))

    monkeypatch.setattr(store_module, "ToolOutputStore", TestStore)
    monkeypatch.setenv("OIKOS_TOOL_OUTPUT_MAX_CHARS", "100")
    monkeypatch.setenv("OIKOS_TOOL_OUTPUT_PREVIEW_CHARS", "40")

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


@pytest.mark.asyncio
async def test_execute_tool_spawn_commis_no_type_error(monkeypatch, tmp_path, db_session, test_user):
    """Test that spawn_commis can be called through _execute_tool without TypeError.

    Regression test for bug where _execute_tool passed _skip_interrupt kwarg
    to spawn_commis_async which didn't accept that parameter.
    """
    from zerg.connectors.context import set_credential_resolver
    from zerg.connectors.resolver import CredentialResolver
    from zerg.managers.fiche_runner import FicheInterrupted
    from zerg.services import oikos_react_engine as engine

    # Set up credential context (required for spawn_commis)
    resolver = CredentialResolver(fiche_id=1, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    # Set up artifact store
    monkeypatch.setenv("LONGHOUSE_DATA_PATH", str(tmp_path))

    tool_call = {
        "name": "spawn_commis",
        "args": {"task": "Test task for spawn_commis"},
        "id": "tool-call-123",
    }

    # Note: We don't set oikos_context here because spawn_commis_async handles
    # the case where ctx is None - it just won't emit run events.
    # This is fine for testing the parameter passing fix.

    try:
        # This should NOT raise TypeError about unexpected _skip_interrupt kwarg
        # It should raise FicheInterrupted (normal flow) or return a message
        try:
            await engine._execute_tool(
                tool_call,
                {},  # spawn_commis is special-cased, doesn't need to be in tools_by_name
                run_id=None,
                owner_id=test_user.id,
            )
            # If we get here without error, the fix worked (job may have returned immediately)
        except FicheInterrupted as e:
            # This is expected behavior - spawn_commis raises FicheInterrupted
            # when a job is queued to pause the oikos
            assert e.value["type"] == "commis_pending"
            assert "job_id" in e.value
    finally:
        # Clean up contexts
        set_credential_resolver(None)
