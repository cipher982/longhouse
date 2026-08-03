from types import SimpleNamespace

import pytest

from zerg.services.session_resume import build_session_resume_intent


def _session(*, provider: str = "codex", state: str = "available", reason: str | None = None):
    action = SimpleNamespace(state=state, reason=reason)
    control = SimpleNamespace(actions=SimpleNamespace(resume=action))
    return SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111",
        provider=provider,
        device_id="david-mac",
        origin_label="David's Mac",
        home_label="On this Mac",
        cwd="/Users/david/project with space",
        session_state=SimpleNamespace(control=control),
    )


def test_resume_intent_returns_provider_native_terminal_argv() -> None:
    intent = build_session_resume_intent(_session(provider="opencode"))

    assert intent.available is True
    assert intent.argv == [
        "longhouse",
        "opencode",
        "--cwd",
        "/Users/david/project with space",
        "--resume-session",
        "11111111-1111-4111-8111-111111111111",
    ]
    assert "'/Users/david/project with space'" in intent.command
    assert intent.handoff == "terminal_command"
    assert intent.machine_label == "David's Mac"


@pytest.mark.parametrize(
    ("provider", "selector"),
    [
        ("codex", "--resume-session"),
        ("claude", "--resume"),
        ("cursor", "--resume-session"),
        ("opencode", "--resume-session"),
    ],
)
def test_resume_intent_command_matches_each_managed_cli_selector(provider: str, selector: str) -> None:
    intent = build_session_resume_intent(_session(provider=provider))
    assert intent.available is True
    assert intent.argv[-2:] == [selector, "11111111-1111-4111-8111-111111111111"]


def test_resume_intent_preserves_typed_unavailable_reason() -> None:
    intent = build_session_resume_intent(
        _session(state="unavailable", reason="provider_state_missing")
    )

    assert intent.available is False
    assert intent.reason == "provider_state_missing"
    assert intent.argv == []
    assert intent.command is None
