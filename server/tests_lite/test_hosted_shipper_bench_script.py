from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qa" / "hosted-shipper-mixed-bench.sh"


def test_ephemeral_device_cleanup_survives_benchmark_process_exit():
    source = SCRIPT.read_text()

    assert "trap cleanup_ephemeral_device_token EXIT" in source
    assert "exec cargo run" not in source


def test_hosted_script_resolves_machine_id_from_storage_v2_capabilities():
    source = SCRIPT.read_text()

    assert "resolve_ship_machine_id" in source
    assert "/api/agents/storage/v2/capabilities" in source
    assert "--ship-machine-id" in source
    assert "X-Agents-Token:" in source
    # Do not echo secrets or the resolved machine id into the primary progress line.
    assert 'echo "Running hosted mixed live/archive shipper bench against $API_URL"' in source
    assert "echo \"$LONGHOUSE_DEVICE_TOKEN\"" not in source
    assert "echo \"$SHIP_MACHINE_ID\"" not in source
