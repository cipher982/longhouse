"""Tests for ToolOutputStore."""

import pytest

from zerg.services.tool_output_store import ToolOutputStore


def test_tool_output_store_roundtrip(tmp_path):
    store = ToolOutputStore(base_path=str(tmp_path))

    artifact_id = store.save_output(
        owner_id=1,
        tool_name="runner_exec",
        content="hello world",
        course_id=42,
        tool_call_id="call-1",
    )

    assert store.read_output(owner_id=1, artifact_id=artifact_id) == "hello world"

    metadata = store.read_metadata(owner_id=1, artifact_id=artifact_id)
    assert metadata["artifact_id"] == artifact_id
    assert metadata["owner_id"] == 1
    assert metadata["tool_name"] == "runner_exec"
    assert metadata["course_id"] == 42
    assert metadata["tool_call_id"] == "call-1"


def test_tool_output_store_invalid_artifact_id(tmp_path):
    store = ToolOutputStore(base_path=str(tmp_path))

    with pytest.raises(ValueError):
        store.read_output(owner_id=1, artifact_id="../oops")
