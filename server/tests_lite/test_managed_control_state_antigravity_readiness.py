"""Antigravity send is advertised only while its hook is observably firing.

Antigravity control is hook-delivered, and whether hooks fire is not a property
of the release: under GEMINI_API_KEY auth agy loads its hooks and never calls
them, so a session can be current, healthy and completely uncontrollable. The
contract cannot express that, so availability is gated on observation instead.
"""

from zerg.services.managed_control_state import _connection_capabilities_for_provider
from zerg.services.managed_control_state import antigravity_send_ready_session_ids


def _readiness(**overrides):
    fact = {
        "provider": "antigravity",
        "session_id": "sess-1",
        "operation": "send_input",
        "hook_installed": True,
        "recent_hook_observed": True,
    }
    fact.update(overrides)
    return {"readiness": [fact]}


def test_live_hook_marks_the_session_send_ready() -> None:
    assert antigravity_send_ready_session_ids(_readiness()) == {"sess-1"}


def test_installed_but_unobserved_hook_is_not_send_ready() -> None:
    # The GEMINI_API_KEY case: the hook is installed and simply never fires.
    assert antigravity_send_ready_session_ids(_readiness(recent_hook_observed=False)) == set()


def test_uninstalled_hook_is_not_send_ready() -> None:
    assert antigravity_send_ready_session_ids(_readiness(hook_installed=False)) == set()


def test_other_providers_and_operations_are_ignored() -> None:
    assert antigravity_send_ready_session_ids(_readiness(provider="claude")) == set()
    assert antigravity_send_ready_session_ids(_readiness(operation="interrupt")) == set()


def test_missing_or_malformed_evidence_is_not_send_ready() -> None:
    for evidence in (None, {}, {"readiness": None}, {"readiness": ["nope"]}):
        assert antigravity_send_ready_session_ids(evidence) == set()


def test_capability_follows_readiness() -> None:
    ready = _connection_capabilities_for_provider(
        "antigravity", "antigravity_hook_inbox", send_ready=True
    )
    assert ready["can_send_input"] == 1
    unready = _connection_capabilities_for_provider(
        "antigravity", "antigravity_hook_inbox", send_ready=False
    )
    assert unready["can_send_input"] == 0


def test_other_providers_are_not_readiness_gated() -> None:
    caps = _connection_capabilities_for_provider("claude", "claude_channel_bridge", send_ready=False)
    assert caps.get("can_send_input") == 1
