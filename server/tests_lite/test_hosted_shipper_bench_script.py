from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qa" / "hosted-shipper-mixed-bench.sh"


def test_ephemeral_device_cleanup_survives_benchmark_process_exit():
    source = SCRIPT.read_text()

    assert "trap cleanup_ephemeral_device_token EXIT" in source
    assert "exec cargo run" not in source
