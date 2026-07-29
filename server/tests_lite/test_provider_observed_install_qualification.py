from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from zerg.qa.provider_observed_install_qualification import observed_closure
from zerg.qa.provider_observed_install_qualification import qualify_cursor_observed_install


def _fake_cursor_install(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "cursor-install"
    root.mkdir()
    binary = root / "cursor-agent"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        'if sys.argv[1:] == ["--version"]:\n'
        '    print("2026.07.23-test")\n'
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    identity = "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()
    return root, binary, identity


def _gate0_artifact(tmp_path: Path, *, identity: str) -> Path:
    path = tmp_path / "gate0.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "gate": "cursor_helm_gate0",
                "provider": "cursor",
                "provider_version": "2026.07.23-test",
                "provider_executable_identity": identity,
                "status": "passed",
                "scenarios": {
                    name: {
                        "status": "passed",
                        "provider_conversation_id": "cursor-session-1",
                    }
                    for name in (
                        "workspace_trust",
                        "create_chat_resume",
                        "native_resume_continuity",
                        "ctrl_c_cancel",
                        "permission_allow",
                        "permission_deny",
                        "permission_ask",
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_observed_closure_ignores_cursor_pid_leases(tmp_path: Path) -> None:
    root, _, _ = _fake_cursor_install(tmp_path)
    before = observed_closure(root)
    lease = root / ".running" / "1234"
    lease.parent.mkdir()
    lease.write_text("runtime-only\n", encoding="utf-8")

    assert observed_closure(root) == before


def test_cursor_observed_install_runs_full_exact_column(tmp_path: Path) -> None:
    root, binary, identity = _fake_cursor_install(tmp_path)
    gate0 = _gate0_artifact(tmp_path, identity=identity)

    result = qualify_cursor_observed_install(
        provider_bin=binary,
        provider_root=root,
        gate0_artifact=gate0,
        expected_version="2026.07.23-test",
        output_root=tmp_path / "output",
    )

    assert result["status"] == "pass"
    assert result["build_provenance"] == "observed_install"
    assert result["full_column_gate"]["status"] == "pass"
    assert result["full_column_gate"]["captured_scenario_count"] == 22
    assert result["full_column_gate"]["coverage_gap_kind_counts"] == {
        "passed": 29,
        "no_token_safety_gate": 1,
        "not_applicable": 2,
        "provider_contract_unsupported": 1,
    }
    assert Path(result["artifact_path"]).is_file()


def test_cursor_observed_install_rejects_gate_for_different_executable(tmp_path: Path) -> None:
    root, binary, _ = _fake_cursor_install(tmp_path)
    gate0 = _gate0_artifact(tmp_path, identity="sha256:" + "0" * 64)

    result = qualify_cursor_observed_install(
        provider_bin=binary,
        provider_root=root,
        gate0_artifact=gate0,
        expected_version="2026.07.23-test",
        output_root=tmp_path / "output",
    )

    assert result["status"] == "fail"
    assert result["full_column_gate"]["status"] == "fail"
    failure_codes = {
        item["actual_failure_code"] for item in result["full_column_gate"]["unexpected_results"]
    }
    assert "cursor_gate0_identity_mismatch" in failure_codes
