from __future__ import annotations

import pytest

from zerg.qa.provider_generic_resume import REGISTRATION
from zerg.qa.provider_generic_resume import run_generic_resume
from zerg.qa.provider_resume_factory import SCENARIOS
from zerg.qa.provider_resume_factory import run_provider_resume_scenario


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


def test_generic_producer_retains_hermetic_observation_and_cleanup(tmp_path) -> None:
    result = run_generic_resume(provider="codex", scenario_id="resume_attempt_idempotency", root=tmp_path)

    assert result["status"] == "pass"
    assert result["producer"]["producer_id"] == REGISTRATION.producer_id
    assert result["observation"]["scenario_oracle_evaluated"] is True
    assert result["observation"]["generic_cleanup_verified"] is True
    assert {item["path"] for item in result["artifact_manifest"]} == {
        "cleanup-receipt.json",
        "generic-observation.json",
    }
