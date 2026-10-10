"""The OMP coordination producer reads a real peers invocation, not its docs."""

from __future__ import annotations

import json

from zerg.qa.omp_coordination_awareness import REGISTRATION
from zerg.qa.omp_coordination_awareness import peers_invocation_evidence


def _assistant(*calls):
    return {"type": "message", "message": {"role": "assistant", "content": list(calls)}}


def _result(call_id, text, *, is_error=False):
    return {
        "type": "message",
        "message": {"role": "toolResult", "toolCallId": call_id, "isError": is_error, "content": [{"type": "text", "text": text}]},
    }


def test_a_write_to_the_peers_device_is_an_invocation():
    rows = [
        _assistant({"type": "toolCall", "id": "docs", "name": "read", "arguments": {"path": "xd://peers"}}),
        _result("docs", "# peers — Longhouse peers"),
        _assistant({"type": "toolCall", "id": "call", "name": "write", "arguments": {"path": "xd://peers", "content": "{}"}}),
        _result("call", json.dumps({"repo": "probe", "active_only": False, "total": 0, "peers": []})),
    ]
    evidence = peers_invocation_evidence(rows)
    assert evidence is not None and evidence["tool_name"] == "write" and evidence["is_error"] is False


def test_reading_the_docs_alone_is_not_an_invocation():
    rows = [
        _assistant({"type": "toolCall", "id": "docs", "name": "read", "arguments": {"path": "xd://peers"}}),
        _result("docs", "# peers — Longhouse peers"),
    ]
    assert peers_invocation_evidence(rows) is None


def test_a_top_level_peers_call_counts_and_a_refusal_is_an_error():
    rows = [
        _assistant({"type": "toolCall", "id": "p", "name": "peers", "arguments": {"repo": "probe"}}),
        _result("p", json.dumps({"error": "registration_pending"})),
    ]
    evidence = peers_invocation_evidence(rows)
    assert evidence is not None and evidence["is_error"] is True


def test_registration_names_the_omp_coordination_cell():
    assert REGISTRATION.providers == ("omp",)
    assert REGISTRATION.assertion_cells == (("coordination_instructions_model_visible", None),)
    assert REGISTRATION.scenario_id == "omp_coordination_awareness_create"
