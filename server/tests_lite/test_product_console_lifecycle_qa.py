import json
import os
import subprocess
import sys
from pathlib import Path

from zerg.qa.product_console_lifecycle import ASSERTION_ID
from zerg.qa.product_console_lifecycle import REGISTRATION
from zerg.qa.product_console_lifecycle import run_product_console_lifecycle


_FACTORY_PROOF_BUNDLE_CAP_BYTES = (2 * 1024 * 1024) - (64 * 1024)


def test_product_console_lifecycle_oracle_proves_user_visible_sequence(tmp_path):
    evidence = tmp_path / "evidence"
    result = run_product_console_lifecycle(evidence)

    assert REGISTRATION.subject_kind == "longhouse_product"
    assert REGISTRATION.providers == ()
    assert REGISTRATION.provider_artifact_required is False
    assert REGISTRATION.producer_revision == 2
    assert REGISTRATION.scenario_revision == 1
    assert result["status"] == "pass"
    assert result["assertions"] == {ASSERTION_ID: True}
    assert result["observation"]["empty_ready_live_control"] is True
    assert result["observation"]["active_working_live_control"] is True
    assert result["observation"]["fifo_queue_accepted"] is True
    assert result["observation"]["unsupported_interrupt_preserves_control"] is True
    assert result["observation"]["terminal_settled_live_control"] is True
    assert result["observation"]["idempotent_turn_receipt"] is True
    assert {path.name for path in evidence.iterdir()} == {
        "cleanup-receipt.json",
        "console-lifecycle-observation.json",
    }
    assert not tuple(evidence.rglob("*.db"))


def test_product_console_lifecycle_cli_is_hermetic_without_database_env(tmp_path):
    server_root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    runtime_home = tmp_path / "qualification-runtime"
    runtime_home.mkdir()
    evidence = tmp_path / "cli-evidence"
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LONGHOUSE_QUALIFICATION_HOME": str(runtime_home),
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(server_root),
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "zerg.qa.product_console_lifecycle",
            "--evidence-root",
            str(evidence),
        ],
        cwd=server_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["status"] == "pass"
    assert {item["path"] for item in result["artifact_manifest"]} == {
        "cleanup-receipt.json",
        "console-lifecycle-observation.json",
    }
    assert {path.name for path in evidence.iterdir()} == {
        "cleanup-receipt.json",
        "console-lifecycle-observation.json",
        "result.json",
    }
    assert not tuple(evidence.rglob("*.db"))
    assert not tuple(runtime_home.iterdir())
    retained_sizes = [item["size"] for item in result["artifact_manifest"]]
    retained_sizes.append((evidence / "result.json").stat().st_size)
    projected_base64_bytes = sum(4 * ((size + 2) // 3) for size in retained_sizes)
    assert projected_base64_bytes < _FACTORY_PROOF_BUNDLE_CAP_BYTES
