from pathlib import Path
from subprocess import CompletedProcess

import pytest

from zerg.qa import provider_interaction_probe


@pytest.mark.parametrize(
    ("provider", "version_output", "expected"),
    (
        ("claude", "2.1.219 (Claude Code)\n", "2.1.219"),
        ("codex", "codex-cli 0.133.0\n", "0.133.0"),
        ("opencode", "1.2.3\n", "1.2.3"),
        ("antigravity", "1.2.3\n", "1.2.3"),
        ("cursor", "2026.07.23-e383d2b\n", "2026.07.23-e383d2b"),
    ),
)
def test_provider_version_probe_returns_the_request_normalized_value(
    monkeypatch,
    tmp_path: Path,
    provider: str,
    version_output: str,
    expected: str,
) -> None:
    monkeypatch.setattr(
        provider_interaction_probe.subprocess,
        "run",
        lambda *_args, **_kwargs: CompletedProcess([], 0, version_output, ""),
    )

    assert (
        provider_interaction_probe._provider_version(  # noqa: SLF001
            provider,
            tmp_path / provider,
            environment={},
        )
        == expected
    )


@pytest.mark.parametrize(
    "name",
    (
        "AWS_PROFILE",
        "CODEX_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_USE_VERTEX",
    ),
)
def test_no_token_environment_rejects_ambient_provider_credentials(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, "ambient-credential")

    with pytest.raises(RuntimeError, match="credential-free environment"):
        provider_interaction_probe._no_token_environment()  # noqa: SLF001


def test_no_token_environment_scrubs_provider_controls_and_configuration(monkeypatch) -> None:
    for name in provider_interaction_probe._NO_TOKEN_AUTH_ENV_NAMES:  # noqa: SLF001
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LONGHOUSE_PROVIDER_INTERACTION_LIVE", "1")
    monkeypatch.setenv("LONGHOUSE_CLAUDE_INTERACTION_ARTIFACT", "/tmp/fixture.json")
    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "openrouter/model")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")

    environment = provider_interaction_probe._no_token_environment()  # noqa: SLF001

    assert "LONGHOUSE_PROVIDER_INTERACTION_LIVE" not in environment
    assert "LONGHOUSE_CLAUDE_INTERACTION_ARTIFACT" not in environment
    assert "LONGHOUSE_OPENCODE_QUALIFICATION_MODEL" not in environment
    assert "ANTHROPIC_MODEL" not in environment
    assert "ANTHROPIC_BASE_URL" not in environment
    assert "OPENAI_BASE_URL" not in environment
    assert environment["AWS_EC2_METADATA_DISABLED"] == "true"
