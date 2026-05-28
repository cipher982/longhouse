from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from zerg import provider_live_route_e2e as route_e2e
from zerg import provider_release_status as prs


@pytest.fixture(autouse=True)
def isolate_longhouse_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / ".longhouse"))


def _generated_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _route_artifact(*, verdict: str = "green", status: str = "pass") -> dict[str, object]:
    result: dict[str, object] = {
        "provider": "opencode",
        "expected_provider_version": "1.15.11",
        "status": status,
        "verdict": "green",
        "version_match": {"status": "match"},
        "match": {
            "status_code": 200,
            "payload": {"result": {"provider_version_match": {"status": "match"}}},
        },
        "match_attempt_count": 1,
        "mismatch": {
            "status_code": 409,
            "payload": {"detail": {"code": "provider_version_mismatch"}},
        },
        "mismatch_attempt_count": 1,
    }
    if status != "pass":
        result["failure_code"] = "provider_live_mismatch_not_typed"
        result["message"] = "mismatch was not typed"
    return {
        "schema_version": prs.PROVIDER_STATUS_SCHEMA_VERSION,
        "artifact_kind": route_e2e.ROUTE_E2E_ARTIFACT_KIND,
        "generated_at": _generated_at(),
        "api_url": "https://david010.longhouse.ai",
        "device_id": "cinder",
        "engine_build": "abc1234",
        "providers": ["opencode"],
        "require_verdict": "green",
        "mismatch_checked": True,
        "verdict": verdict,
        "failure_count": 0 if status == "pass" and verdict == "green" else 1,
        "results": [result],
    }


def _write_artifact(base_dir: Path, payload: dict[str, object]) -> Path:
    path = route_e2e.configured_provider_live_route_e2e_path(base_dir)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_route_e2e_green_artifact_applies(tmp_path: Path) -> None:
    path = _write_artifact(tmp_path, _route_artifact())

    proof = route_e2e.collect_provider_live_route_e2e(base_dir=tmp_path, expected_providers=["opencode"])

    assert proof["enabled"] is True
    assert proof["configured"] is True
    assert proof["status"] == "ok"
    assert proof["applies"] is True
    assert proof["providers"] == ["opencode"]
    assert proof["coverage_status"] == "complete"
    assert proof["expected_providers"] == ["opencode"]
    assert proof["covered_providers"] == ["opencode"]
    assert proof["missing_providers"] == []
    assert proof["results"] == [
        {
            "provider": "opencode",
            "status": "pass",
            "verdict": "green",
            "expected_provider_version": "1.15.11",
            "version_match": "match",
            "match_status_code": 200,
            "match_attempt_count": 1,
            "match_version_match": "match",
            "mismatch_status_code": 409,
            "mismatch_attempt_count": 1,
            "mismatch_code": "provider_version_mismatch",
            "failure_code": None,
            "message": None,
        }
    ]
    assert proof["source"]["path"] == str(path)


def test_route_e2e_reports_missing_expected_provider_coverage(tmp_path: Path) -> None:
    _write_artifact(tmp_path, _route_artifact())

    proof = route_e2e.collect_provider_live_route_e2e(
        base_dir=tmp_path,
        expected_providers=["claude", "opencode"],
    )

    assert proof["status"] == "ok"
    assert proof["coverage_status"] == "missing"
    assert proof["applies"] is False
    assert proof["expected_providers"] == ["claude", "opencode"]
    assert proof["covered_providers"] == ["opencode"]
    assert proof["missing_providers"] == ["claude"]


def test_route_e2e_missing_artifact_is_not_configured(tmp_path: Path) -> None:
    proof = route_e2e.collect_provider_live_route_e2e(base_dir=tmp_path, expected_providers=["opencode"])

    assert proof["enabled"] is False
    assert proof["configured"] is False
    assert proof["status"] == "not_configured"
    assert proof["coverage_status"] == "missing"
    assert proof["missing_providers"] == ["opencode"]


def test_route_e2e_corrupt_artifact_is_unavailable(tmp_path: Path) -> None:
    path = route_e2e.configured_provider_live_route_e2e_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    proof = route_e2e.collect_provider_live_route_e2e(base_dir=tmp_path)

    assert proof["enabled"] is True
    assert proof["configured"] is True
    assert proof["status"] == "unavailable"
    assert "JSONDecodeError" in proof["source"]["error"]


def test_route_e2e_failed_result_does_not_apply(tmp_path: Path) -> None:
    _write_artifact(tmp_path, _route_artifact(verdict="red", status="fail"))

    proof = route_e2e.collect_provider_live_route_e2e(base_dir=tmp_path)

    assert proof["status"] == "failed"
    assert proof["applies"] is False
    assert proof["failure_count"] == 1
    assert proof["results"][0]["failure_code"] == "provider_live_mismatch_not_typed"


def test_route_e2e_malformed_failure_count_does_not_crash(tmp_path: Path) -> None:
    payload = _route_artifact()
    payload["failure_count"] = "not-an-int"
    _write_artifact(tmp_path, payload)

    proof = route_e2e.collect_provider_live_route_e2e(base_dir=tmp_path)

    assert proof["status"] == "malformed_results"
    assert proof["applies"] is False


def test_route_e2e_stale_artifact_does_not_apply(monkeypatch, tmp_path: Path) -> None:
    payload = _route_artifact()
    payload["generated_at"] = "2000-01-01T00:00:00Z"
    _write_artifact(tmp_path, payload)
    monkeypatch.setattr(route_e2e, "_max_artifact_age_seconds", lambda: 1)

    proof = route_e2e.collect_provider_live_route_e2e(base_dir=tmp_path)

    assert proof["status"] == "stale"
    assert proof["applies"] is False
    assert proof["freshness_status"] == "stale"


def test_fast_local_health_skips_route_e2e() -> None:
    proof = route_e2e.collect_provider_live_route_e2e(fast=True)

    assert proof["enabled"] is False
    assert proof["configured"] is False
    assert proof["status"] == "skipped"
    assert proof["skipped_reason"] == "fast_local_health"


def test_expected_route_providers_from_live_proof_uses_current_applying_sidecars() -> None:
    expected = route_e2e.expected_route_providers_from_live_proof(
        {
            "statuses": {
                "claude": {"configured": True, "status": "ok", "applies": True},
                "codex": {"configured": True, "status": "ok", "applies": True},
                "opencode": {"configured": True, "status": "ok", "applies": True},
            }
        }
    )

    assert expected == ["claude", "opencode"]
