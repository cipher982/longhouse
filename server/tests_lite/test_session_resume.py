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
        control=SimpleNamespace(source_runner_name="David's Mac"),
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
        ("omp", "--resume-session"),
        ("pi", "--resume-session"),
    ],
)
def test_resume_intent_command_matches_each_managed_cli_selector(provider: str, selector: str) -> None:
    intent = build_session_resume_intent(_session(provider=provider))
    assert intent.available is True
    assert intent.argv[-2:] == [selector, "11111111-1111-4111-8111-111111111111"]


def test_resume_intent_preserves_typed_unavailable_reason() -> None:
    intent = build_session_resume_intent(_session(state="unavailable", reason="provider_state_missing"))

    assert intent.available is False
    assert intent.reason == "provider_state_missing"
    assert intent.argv == []
    assert intent.command is None


@pytest.mark.parametrize(
    ("runner_name", "expected_label"),
    [(None, "cinder"), ("Cinder laptop", "Cinder laptop"), ("   ", "cinder")],
)
def test_resume_intent_names_the_recorded_machine_not_the_environment(runner_name, expected_label) -> None:
    session = _session()
    session.device_id = "cinder"
    session.origin_label = "development"
    session.home_label = "On this Mac"
    session.control = SimpleNamespace(source_runner_name=runner_name)

    intent = build_session_resume_intent(session)

    assert intent.machine_id == "cinder"
    assert intent.machine_label == expected_label


@pytest.mark.parametrize("device_id", [None, ""])
def test_resume_intent_does_not_name_an_environment_when_the_machine_is_unknown(device_id) -> None:
    session = _session()
    session.device_id = device_id
    session.control = None
    session.origin_label = "development"

    intent = build_session_resume_intent(session)

    assert intent.available is False
    assert intent.reason == "machine_unknown"
    assert intent.machine_label is None
    assert intent.command is None
