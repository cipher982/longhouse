"""The stock stream's init or prompt echo cannot masquerade as its result."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "antigravity_control_canary",
    Path(__file__).resolve().parents[2] / "scripts/qa/provider-control-e2e-canary.py",
)
assert SPEC and SPEC.loader
CANARY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CANARY)
CID = "00000000-0000-4000-8000-000000000010"
INIT = {"event": "init", "conversation_id": CID}
RESULT = {"event": "result", "result": {"conversation_id": CID, "status": "SUCCESS", "response": "STREAM_READY"}}


def stream(*records):
    return "\n".join(json.dumps(record) for record in records)


def test_one_structured_result_is_bound_to_the_native_init():
    result = CANARY._antigravity_stream_result(
        stream(INIT, {"event": "step_update", "step_update": {"text_delta": "STREAM_READY"}}, RESULT)
    )
    assert result == RESULT["result"]


@pytest.mark.parametrize(
    "records",
    [
        [INIT],
        [INIT, RESULT, RESULT],
        [{"event": "init", "conversation_id": "00000000-0000-4000-8000-000000000011"}, RESULT],
        [RESULT["result"]],
        [INIT, {"event": "result", "result": {"conversation_id": CID, "status": "SUCCESS", "response": {"echo": "STREAM_READY"}}}],
    ],
)
def test_partial_duplicate_or_unbound_output_cannot_qualify(records):
    assert CANARY._antigravity_stream_result(stream(*records)) is None


def test_a_native_failure_remains_a_structured_failure_not_a_parse_error():
    failure = {"event": "result", "result": {"conversation_id": CID, "status": "ERROR", "response": "provider failed"}}
    assert CANARY._antigravity_stream_result(stream(INIT, failure))["status"] == "ERROR"
