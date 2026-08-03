from __future__ import annotations

import pytest

from zerg.qa.provider_resume_factory import SCENARIOS, run_provider_resume_scenario


@pytest.mark.parametrize("provider", ["codex", "claude", "cursor", "opencode"])
@pytest.mark.parametrize("scenario", [value for value in SCENARIOS if value != "resume_unsupported"])
@pytest.mark.timeout(30)
def test_launch_provider_resume_factory_matrix(provider: str, scenario: str) -> None:
    result = run_provider_resume_scenario(provider, scenario)
    assert result["status"] == "pass", result
    assert result["evidence_class"] == "hermetic"
    assert all(result["assertions"].values())


def test_maintenance_provider_resume_is_typed_and_side_effect_free() -> None:
    result = run_provider_resume_scenario("antigravity", "resume_unsupported")
    assert result["status"] == "pass"
    assert result["observation"] == {
        "disposition": "policy_disabled",
        "registration_count": 0,
        "provider_spawn_count": 0,
    }
