from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from zerg.qa.provider_observed_install_qualification import observed_closure
from zerg.qa.provider_observed_install_qualification import qualify_cursor_observed_install
from zerg.qa.universal_agent_harness import DEFAULT_HARNESS_PROMPT


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


def _gate0_artifact(tmp_path: Path, *, identity: str, active_steer: str = "passed") -> Path:
    native_root = tmp_path / "native-stores"
    native_root.mkdir(exist_ok=True)
    store = native_root / "store.db"
    connection = sqlite3.connect(store)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    connection.execute("INSERT INTO meta VALUES ('0', ?)", [json.dumps({"agentId": "cursor-session-1"})])
    connection.execute("INSERT INTO blobs VALUES ('fixture', ?)", [b'{"role":"user"}'])
    connection.commit()
    connection.close()
    events = tmp_path / "events.ndjson"
    events.write_text('{"event":"sessionStart"}\n', encoding="utf-8")
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
                "artifact_root": str(tmp_path),
                "native_evidence": [
                    {
                        "kind": "cursor_store_db",
                        "path": "native-stores/store.db",
                        "sha256": hashlib.sha256(store.read_bytes()).hexdigest(),
                        "size": store.stat().st_size,
                    },
                    {
                        "kind": "cursor_hook_events",
                        "path": "events.ndjson",
                        "sha256": hashlib.sha256(events.read_bytes()).hexdigest(),
                        "size": events.stat().st_size,
                    },
                ],
                "scenarios": {
                    name: {
                        "status": active_steer if name == "active_steer" else "passed",
                        "provider_conversation_id": "cursor-session-1",
                        **(
                            {"prompt_sha256": hashlib.sha256(DEFAULT_HARNESS_PROMPT.encode("utf-8")).hexdigest()}
                            if name == "create_chat_resume"
                            else {}
                        ),
                    }
                    for name in (
                        "workspace_trust",
                        "create_chat_resume",
                        "native_resume_continuity",
                        "ctrl_c_cancel",
                        "permission_allow",
                        "permission_deny",
                        "permission_ask",
                        "active_steer",
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


def _suite_actions(result: dict) -> dict[str, dict]:
    suite = next(row for row in result["harness_result"]["results"] if row["scenario"] == "full_action_suite")
    return {row["action_id"]: row for row in suite["data"]["actions"]}


@pytest.mark.timeout(60)
def test_cursor_observed_install_column_passes_with_native_cold_resume_as_a_declared_limit(tmp_path: Path) -> None:
    root, binary, identity = _fake_cursor_install(tmp_path)
    gate0 = _gate0_artifact(tmp_path, identity=identity)

    result = qualify_cursor_observed_install(
        provider_bin=binary,
        provider_root=root,
        gate0_artifact=gate0,
        expected_version="2026.07.23-test",
        output_root=tmp_path / "output",
    )

    gate = result["full_column_gate"]
    assert result["status"] == "pass"
    assert result["build_provenance"] == "observed_install"
    assert gate["status"] == "pass"
    assert gate["unexpected_results"] == []
    assert gate["captured_scenario_count"] == 32
    assert gate["coverage_gap_kind_counts"] == {
        "passed": 28,
        "no_token_safety_gate": 1,
        # Native cold Resume: Gate 0 proves reattach, and the column runs no
        # cold Resume of its own. The resume-assurance cells check it; this
        # column carries no result of theirs.
        "missing_live_canary": 1,
        "not_applicable": 1,
        "provider_contract_unsupported": 1,
    }
    cold_resume = next(row for row in result["harness_result"]["results"] if row["scenario"] == "helm_cold_resume")
    assert (cold_resume["status"], cold_resume["failure_code"]) == ("unsupported_gap", "cursor_cold_resume_canary_missing")
    assert gate["expected_limits"]["helm_cold_resume"] == {"status": "unsupported_gap", "failure_code": "cursor_cold_resume_canary_missing"}
    native = cold_resume["data"]["native_resume_evidence"]
    assert native["status"] == "unsupported_gap"
    assert native["proof_scope"] == "external.resume_assurance_cells.native_provider_resume_proven"
    assert cold_resume["data"]["assertions"]["native_provider_resume_proven"] is False
    # Every copy of the gap record names where native cold Resume is checked
    # and claims no result from there: its own assertion above is False.
    records = [
        json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "output").rglob("assertions/helm_cold_resume_native.json")
    ]
    assert records
    for record in records:
        assert record["status"] == "unsupported_gap"
        assert "does not prove" in record["message"] and "carries no result" in record["message"]
        assert "native_provider_resume_proven" in record["message"] and "native_provider_resume_proven" in record["next"]
        assert "prove it" not in record["message"]
    actions = _suite_actions(result)
    # Gate 0's active_steer scenario is the steer evidence.
    assert actions["steer_active_turn"]["coverage_status"] == "pass"
    assert actions["resume_reattach"]["coverage_gap_kind"] == "missing_live_canary"
    assert actions["resume_reattach"]["scenario_statuses"]["resume_reattach"] == "pass"
    assert Path(result["artifact_path"]).is_file()


@pytest.mark.timeout(60)
def test_cursor_observed_install_fails_when_gate0_did_not_steer(tmp_path: Path) -> None:
    root, binary, identity = _fake_cursor_install(tmp_path)
    # Gate 0 records "unsupported" when the reply after a mid-turn message was the original one.
    gate0 = _gate0_artifact(tmp_path, identity=identity, active_steer="unsupported")

    result = qualify_cursor_observed_install(
        provider_bin=binary,
        provider_root=root,
        gate0_artifact=gate0,
        expected_version="2026.07.23-test",
        output_root=tmp_path / "output",
    )

    assert result["status"] == "fail"
    assert result["full_column_gate"]["unexpected_coverage_gap_kinds"] == {"unexpected_failure": 1}
    steer = _suite_actions(result)["steer_active_turn"]
    assert (steer["coverage_status"], steer["failure_code"]) == ("fail", "cursor_gate0_contract_failed")


@pytest.mark.timeout(60)
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
    failure_codes = {item["actual_failure_code"] for item in result["full_column_gate"]["unexpected_results"]}
    assert "cursor_gate0_identity_mismatch" in failure_codes
