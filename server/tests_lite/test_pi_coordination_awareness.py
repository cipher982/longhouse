"""The Pi coordination producer proves a real peers call in a Pi Helm session."""

from __future__ import annotations

import json

from zerg.qa.pi_coordination_awareness import REGISTRATION
from zerg.qa.pi_coordination_awareness import peers_invocation_evidence


def test_registration_names_the_pi_coordination_cell():
    assert REGISTRATION.providers == ("pi",)
    assert REGISTRATION.modes == ("helm",)
    assert REGISTRATION.assertion_cells == (("coordination_instructions_model_visible", None),)
    assert REGISTRATION.scenario_id == "pi_coordination_awareness_create"
    assert REGISTRATION.executable_module == "zerg.qa.pi_coordination_awareness"


def test_a_pi_peers_call_with_a_result_is_the_evidence():
    """Pi exposes extension tools by name; its session rows share OMP's format."""

    rows = [
        {
            "type": "message",
            "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "p", "name": "peers", "arguments": {"repo": "probe"}}]},
        },
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "p",
                "isError": False,
                "content": [{"type": "text", "text": json.dumps({"repo": "probe", "total": 0, "peers": []})}],
            },
        },
    ]
    evidence = peers_invocation_evidence(rows)
    assert evidence is not None and evidence["tool_name"] == "peers" and evidence["is_error"] is False


def test_cleanup_passes_only_with_verified_birth_identities():
    """Dead process groups whose identities never matched their birth records prove nothing."""

    from zerg.qa.pi_coordination_awareness import REGISTRATION
    from zerg.qa.pi_coordination_awareness import _cleanup_status

    held = {key: True for key in REGISTRATION.required_cleanup}
    assert _cleanup_status({**held, "birth_identities_verified": True}) == "pass"
    assert _cleanup_status({**held, "birth_identities_verified": False}) == "fail"
    assert _cleanup_status(held) == "fail"
    assert _cleanup_status({**held, "birth_identities_verified": True, "isolation_removed": False}) == "fail"
