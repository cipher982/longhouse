from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from zerg.qa.opencode_qualification_profile import configured_openrouter_model
from zerg.qa.opencode_qualification_profile import prepare_opencode_qualification_profile


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("deepseek/deepseek-v4-flash", ("openrouter/deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash")),
        ("openrouter/anthropic/claude-sonnet-4.5", ("openrouter/anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.5")),
    ],
)
def test_configured_openrouter_model_normalizes_the_factory_pin(
    configured: str,
    expected: tuple[str, str],
) -> None:
    assert configured_openrouter_model({"LONGHOUSE_OPENCODE_QUALIFICATION_MODEL": configured}) == expected


@pytest.mark.parametrize("configured", ["", "openrouter/"])
def test_configured_openrouter_model_rejects_missing_or_wrong_authority(configured: str) -> None:
    with pytest.raises(RuntimeError):
        configured_openrouter_model({"LONGHOUSE_OPENCODE_QUALIFICATION_MODEL": configured})


def test_prepare_profile_registers_the_exact_model_without_a_credential(tmp_path: Path) -> None:
    environment = {
        "LONGHOUSE_OPENCODE_QUALIFICATION_MODEL": "deepseek/deepseek-v4-flash",
        "OPENROUTER_API_KEY": "not-written-to-profile",
    }

    receipt = prepare_opencode_qualification_profile(tmp_path, environment)

    config_path = tmp_path / ".config/opencode/opencode.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload == {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": ["openrouter"],
        "model": "openrouter/deepseek/deepseek-v4-flash",
        "small_model": "openrouter/deepseek/deepseek-v4-flash",
        "provider": {"openrouter": {"models": {"deepseek/deepseek-v4-flash": {}}}},
    }
    assert "not-written-to-profile" not in config_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert environment["LONGHOUSE_OPENCODE_MODEL"] == "openrouter/deepseek/deepseek-v4-flash"
    assert receipt == {
        "provider": "openrouter",
        "model": "openrouter/deepseek/deepseek-v4-flash",
        "model_id": "deepseek/deepseek-v4-flash",
        "config_path": str(config_path),
        "selection_authority": "disposable_profile_and_runtime_override",
        "credential_authority": "process_environment:OPENROUTER_API_KEY",
    }
