from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from zerg.qa import codex_release_identity as bridge
from zerg.services.provider_capability_proof import proof_record_from_mapping


@pytest.fixture(autouse=True)
def _stable_runner_checkout(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "_git_sha", lambda _root: "test-sha")
    monkeypatch.setattr(bridge, "_git_dirty", lambda _root: False)


def _fake(tmp_path: Path, body: str) -> tuple[Path, str]:
    path = tmp_path / "codex"
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o700)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, f"sha256:{digest}"


def _request(tmp_path: Path, binary: Path, identity: str, **changes: str) -> Path:
    payload = {
        "schema_version": 1,
        "provider": "codex",
        "profile": bridge.PROFILE,
        "provider_bin": str(binary),
        "expected_provider_version": "1.2.3",
        "expected_executable_identity": identity,
        "invocation_id": "invocation-1",
        "producer_class": "local_diagnostic",
        "producer_version": "test",
        "longhouse_git_sha": "test-sha",
    }
    payload.update(changes)
    path = tmp_path / "REQUEST.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_success_emits_v2_bundle_and_linked_raw_evidence(tmp_path: Path) -> None:
    binary, identity = _fake(tmp_path, 'printf "codex-cli 1.2.3\\n"\n')
    request = _request(tmp_path, binary, identity)
    output = tmp_path / "output"

    assert bridge.main(["--request", str(request), "--output-root", str(output), "--json"]) == 0
    bundle = json.loads((output / "proof-bundle.json").read_text())
    assert bundle["artifact_kind"] == "provider_capability_proof_bundle"
    assert bundle["schema_version"] == 2
    assert {record["outcome"] for record in bundle["records"]} == {"pass"}
    assert {record["evidence_class"] for record in bundle["records"]} == {"live_no_token"}
    raw = (output / "raw-observation.json").read_bytes()
    raw_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert bundle["execution_metadata"]["raw_evidence_digest"] == raw_digest
    assert all(record["raw_reference_digests"] == [raw_digest] for record in bundle["records"])
    assert all(proof_record_from_mapping(record).schema_version == 2 for record in bundle["records"])


def test_version_mismatch_is_semantic_failure_but_exit_stays_success(tmp_path: Path) -> None:
    binary, identity = _fake(tmp_path, 'printf "codex-cli 9.9.9\\n"\n')
    output = tmp_path / "output"

    assert bridge.main(["--request", str(_request(tmp_path, binary, identity)), "--output-root", str(output)]) == 0
    records = json.loads((output / "proof-bundle.json").read_text())["records"]
    outcomes = {record["assertion_id"]: record["outcome"] for record in records}
    assert outcomes == {
        "exact_executable_identity_observed": "pass",
        "reported_version_matches_expected": "semantic_fail",
    }


def test_digest_mismatch_does_not_execute_unexpected_bytes(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    binary, _ = _fake(tmp_path, f'touch "{marker}"\nprintf "codex-cli 1.2.3\\n"\n')
    request = _request(tmp_path, binary, "sha256:" + "0" * 64)

    assert bridge.main(["--request", str(request), "--output-root", str(tmp_path / "output")]) == 2
    assert not marker.exists()
    assert not (tmp_path / "output").exists()


def test_timeout_and_nonzero_are_infrastructure_records(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bridge, "TIMEOUT_SECONDS", 0.01)
    timeout_binary, timeout_identity = _fake(tmp_path, "sleep 1\n")
    timeout_output = tmp_path / "timeout-output"
    timeout_request = _request(tmp_path, timeout_binary, timeout_identity)
    assert bridge.main(["--request", str(timeout_request), "--output-root", str(timeout_output)]) == 0
    timeout_records = json.loads((timeout_output / "proof-bundle.json").read_text())["records"]
    assert {r["outcome"] for r in timeout_records} == {"pass", "infrastructure_error"}
    assert json.loads((timeout_output / "execution-summary.json").read_text())["status"] == "timed_out"

    monkeypatch.setattr(bridge, "TIMEOUT_SECONDS", 10)
    nonzero_binary, nonzero_identity = _fake(tmp_path, 'printf "codex-cli 1.2.3\\n"\nexit 7\n')
    nonzero_output = tmp_path / "nonzero-output"
    nonzero_request = _request(tmp_path, nonzero_binary, nonzero_identity)
    assert bridge.main(["--request", str(nonzero_request), "--output-root", str(nonzero_output)]) == 0
    nonzero_records = json.loads((nonzero_output / "proof-bundle.json").read_text())["records"]
    assert {r["outcome"] for r in nonzero_records} == {"pass", "infrastructure_error"}
    assert json.loads((nonzero_output / "execution-summary.json").read_text())["status"] == "completed"


def test_unknown_keys_and_output_collision_fail_before_execution(tmp_path: Path) -> None:
    binary, identity = _fake(tmp_path, 'printf "codex-cli 1.2.3\\n"\n')
    request = _request(tmp_path, binary, identity, unexpected="nope")
    assert bridge.main(["--request", str(request), "--output-root", str(tmp_path / "new")]) == 2

    collision = tmp_path / "collision"
    collision.mkdir()
    request = _request(tmp_path, binary, identity)
    assert bridge.main(["--request", str(request), "--output-root", str(collision)]) == 2


def test_runner_sha_mismatch_fails_before_output(tmp_path: Path) -> None:
    binary, identity = _fake(tmp_path, 'printf "codex-cli 1.2.3\n"\n')
    request = _request(tmp_path, binary, identity, longhouse_git_sha="wrong-sha")
    output = tmp_path / "output"

    assert bridge.main(["--request", str(request), "--output-root", str(output)]) == 2
    assert not output.exists()


def test_malformed_success_output_is_semantic_failure(tmp_path: Path) -> None:
    binary, identity = _fake(tmp_path, 'printf "version is 1.2.3\n"\n')
    output = tmp_path / "output"

    assert bridge.main(["--request", str(_request(tmp_path, binary, identity)), "--output-root", str(output)]) == 0
    records = json.loads((output / "proof-bundle.json").read_text())["records"]
    outcomes = {record["assertion_id"]: record["outcome"] for record in records}
    assert outcomes == {
        "exact_executable_identity_observed": "pass",
        "reported_version_matches_expected": "semantic_fail",
    }
    assert json.loads((output / "execution-summary.json").read_text())["status"] == "completed"


def test_provider_output_secrets_are_absent_from_retained_artifacts(tmp_path: Path) -> None:
    secret = "sk-proj-ThisIsASeededFakeToken1234567890"
    binary, identity = _fake(
        tmp_path,
        f'printf "codex-cli 1.2.3\\n"\nprintf "{secret}\\n" >&2\n',
    )
    output = tmp_path / "output"

    assert bridge.main(["--request", str(_request(tmp_path, binary, identity)), "--output-root", str(output)]) == 0
    retained = b"".join(path.read_bytes() for path in output.rglob("*") if path.is_file())
    assert secret.encode() not in retained
    assert b"[OPENAI_KEY]" in retained


def test_subject_mutation_during_execution_invalidates_identity_proof(tmp_path: Path) -> None:
    binary, identity = _fake(
        tmp_path,
        'printf "codex-cli 1.2.3\\n"\nprintf "mutated" >> "$0"\n',
    )
    output = tmp_path / "output"

    assert bridge.main(["--request", str(_request(tmp_path, binary, identity)), "--output-root", str(output)]) == 0
    records = json.loads((output / "proof-bundle.json").read_text())["records"]
    outcomes = {record["assertion_id"]: record["outcome"] for record in records}
    assert outcomes["exact_executable_identity_observed"] == "infrastructure_error"
    raw = json.loads((output / "raw-observation.json").read_text())
    assert raw["pre_execution_identity"] != raw["post_execution_identity"]
