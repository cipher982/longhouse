from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci/require-launch-reliability-attestation.py"
SPEC = importlib.util.spec_from_file_location("require_launch_reliability_attestation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeAPI:
    def __init__(self, *, release_sha: str, artifact_zip: bytes):
        self.release_sha = release_sha
        self.artifact_zip = artifact_zip

    def json(self, path: str):
        if "/git/ref/tags/" in path:
            return {"object": {"type": "commit", "sha": self.release_sha}}
        if "/actions/workflows/" in path:
            return {"workflow_runs": [{"id": 42, "completed_at": "2026-08-05T12:00:00Z", "html_url": "https://example/run/42"}]}
        if path.endswith("/actions/runs/42/artifacts"):
            return {"artifacts": [{"id": 99, "name": "launch-reliability-attestation-42", "expired": False}]}
        raise AssertionError(path)

    def bytes(self, path: str) -> bytes:
        assert path.endswith("/actions/artifacts/99/zip")
        return self.artifact_zip


def _artifact_zip(source_sha: str) -> bytes:
    subject = {"report_provenance": {"git_sha": source_sha}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("trusted-launch-reliability-attestation-subject.json", json.dumps(subject))
        archive.writestr("launch-reliability-attestation.json", "{}")
    return output.getvalue()


def test_finds_newest_source_bound_receipt():
    source_sha = "a" * 40
    result = MODULE.find_qualifying_attestation(
        FakeAPI(release_sha=source_sha, artifact_zip=_artifact_zip(source_sha)),
        repository="cipher982/longhouse",
        release_tag="v0.1.33",
        verifier=lambda receipt, subject: None,
    )

    assert result == {
        "release_tag": "v0.1.33",
        "release_sha": source_sha,
        "run_id": 42,
        "run_url": "https://example/run/42",
        "artifact_id": 99,
        "artifact_name": "launch-reliability-attestation-42",
    }


def test_rejects_receipt_for_a_different_source():
    with pytest.raises(RuntimeError, match="does not match release commit"):
        MODULE.find_qualifying_attestation(
            FakeAPI(release_sha="a" * 40, artifact_zip=_artifact_zip("b" * 40)),
            repository="cipher982/longhouse",
            release_tag="v0.1.33",
            verifier=lambda receipt, subject: None,
        )


def test_rejects_zip_path_traversal():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../escape.json", "bad")
    with pytest.raises(RuntimeError, match="unsafe path"):
        MODULE._safe_extract(output.getvalue(), Path("/tmp/longhouse-attestation-test"))
