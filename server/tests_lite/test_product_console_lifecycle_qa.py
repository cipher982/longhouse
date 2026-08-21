import json
import os
import subprocess
import sys
from pathlib import Path

from zerg.qa.product_console_lifecycle import ASSERTION_ID
from zerg.qa.product_console_lifecycle import REGISTRATION
from zerg.qa.product_console_lifecycle import run_product_console_lifecycle


def test_product_console_lifecycle_oracle_proves_user_visible_sequence(tmp_path):
    result = run_product_console_lifecycle(tmp_path / "evidence")

    assert REGISTRATION.subject_kind == "longhouse_product"
    assert REGISTRATION.providers == ()
    assert REGISTRATION.provider_artifact_required is False
    assert result["status"] == "pass"
    assert result["assertions"] == {ASSERTION_ID: True}
    assert result["observation"]["empty_ready_live_control"] is True
    assert result["observation"]["active_working_live_control"] is True
    assert result["observation"]["fifo_queue_accepted"] is True
    assert result["observation"]["unsupported_interrupt_preserves_control"] is True
    assert result["observation"]["terminal_settled_live_control"] is True
    assert result["observation"]["idempotent_turn_receipt"] is True


def test_product_console_lifecycle_cli_is_hermetic_without_database_env(tmp_path):
    server_root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(server_root),
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "zerg.qa.product_console_lifecycle",
            "--evidence-root",
            str(tmp_path / "cli-evidence"),
        ],
        cwd=server_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout.splitlines()[-1])["status"] == "pass"
