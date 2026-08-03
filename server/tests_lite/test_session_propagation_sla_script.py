from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "session-propagation-sla.sh"


def test_ci_bootstrap_uses_durable_device_identity_with_native_auth_contract():
    source = SCRIPT.read_text()

    assert '[[ "$LONGHOUSE_DEVICE_TOKEN" != zdt_* ]]' in source
    assert "/api/agents/storage/v2/capabilities" in source
    assert 'print(json.load(response)["machine_id"])' in source
    assert "--token-env LONGHOUSE_DEVICE_TOKEN" in source
    assert '--machine-name "$token_machine_id"' in source
    assert "--force" not in source
