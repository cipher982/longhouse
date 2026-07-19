from __future__ import annotations

from pathlib import Path

import yaml

from zerg.services.managed_provider_contracts import managed_provider_names
from zerg.services.session_state_contract import PRESENTATION_POLICY_VERSION
from zerg.services.session_state_contract import ACCESS_PRESENTATION_KEYS
from zerg.services.session_state_contract import PRIMARY_PRESENTATION_KEYS
from zerg.services.session_state_contract import STATE_CONTRACT_VERSION
from zerg.services.session_state_contract import TRANSCRIPT_PRESENTATION_KEYS
from zerg.services.session_state_contract import session_state_contract_manifest


def test_session_state_contract_schema_matches_versions_and_provider_adapters():
    root = Path(__file__).resolve().parents[2]
    schema = yaml.safe_load((root / "schemas" / "session_state_contract.yml").read_text(encoding="utf-8"))

    assert schema["schema_version"] == 1
    assert schema["state_contract_version"] == STATE_CONTRACT_VERSION
    assert schema["presentation_policy_version"] == PRESENTATION_POLICY_VERSION
    assert set(schema["providers"]) == managed_provider_names()
    assert "unsupported" in schema["enums"]["action_reason"]
    assert schema["presentation"]["primary_keys"][-1] == "activity_unknown"
    assert tuple(schema["presentation"]["primary_keys"]) == PRIMARY_PRESENTATION_KEYS
    assert tuple(schema["presentation"]["access_keys"]) == ACCESS_PRESENTATION_KEYS
    assert tuple(schema["presentation"]["transcript_keys"]) == TRANSCRIPT_PRESENTATION_KEYS
    assert len(session_state_contract_manifest()["fingerprint"]) == 64
