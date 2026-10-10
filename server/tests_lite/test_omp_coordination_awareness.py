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


def test_a_refusal_then_a_successful_retry_proves_the_tool():
    rows = [
        _assistant({"type": "toolCall", "id": "a", "name": "write", "arguments": {"path": "xd://peers/"}}),
        _result("a", json.dumps({"error": "registration_pending"})),
        _assistant({"type": "toolCall", "id": "b", "name": "write", "arguments": {"path": "xd://peers"}}),
        _result("b", json.dumps({"total": 0, "peers": []})),
    ]
    evidence = peers_invocation_evidence(rows)
    assert evidence is not None and evidence["is_error"] is False


def test_cleanup_carries_the_retirement_receipt_the_factory_admits():
    """2026-10-10: the factory refused the first live run because the cleanup
    receipt had only the boolean, not the session retirement it checks."""

    from zerg.qa.omp_coordination_awareness import session_retirement_cleanup

    retirement = {
        "status": "pass",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "hidden": True,
        "archived": True,
        "present_in_served_inventory": False,
    }
    facts = session_retirement_cleanup(retirement, "11111111-1111-4111-8111-111111111111")
    assert facts["session_retirement"] == retirement
    assert facts["canary_session_hidden"] is True
    receipt = facts["session_retirement"]
    assert receipt["status"] == "pass" and receipt["hidden"] is True and receipt["archived"] is True
    assert receipt["present_in_served_inventory"] is False
    missing = session_retirement_cleanup(None, "x")
    assert missing["session_retirement"] is None and missing["canary_session_hidden"] is False
