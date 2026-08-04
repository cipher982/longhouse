from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "qa" / "launch_reliability_dogfood.py"
SPEC = importlib.util.spec_from_file_location("launch_reliability_dogfood", SCRIPT)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def _args(tmp_path: Path, binary: Path, **overrides: object) -> SimpleNamespace:
    values = {
        "repo_root": tmp_path,
        "longhouse_bin": binary,
        "challenge": None,
        "episode_id": "episode-1",
        "timeout_seconds": 2.0,
        "expected_health_state": "degraded",
        "expected_producer_freshness": "fresh",
        "expected_red_eligible": False,
        "expected_action": "inspect_storage_source",
        "recovery_duration_seconds": None,
        "issue_status": None,
        "issue_opened_at": None,
        "issue_resolved_at": None,
        "evidence_conservation_json": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _fake_binary(tmp_path: Path, payload: dict[str, object], *, exit_code: int = 0) -> Path:
    # Keep the executable outside the fixture repository so provenance tests
    # can distinguish a clean source tree from the test harness itself.
    path = tmp_path.parent / f"{tmp_path.name}-longhouse"
    path.write_text(
        f"#!/usr/bin/env python3\nimport json, sys\nprint({json.dumps(json.dumps(payload))})\nraise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _git_repo(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    marker = tmp_path / "marker"
    marker.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "marker"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "fixture"], check=True)


def test_collects_explicit_truth_and_native_action_ids(tmp_path: Path):
    _git_repo(tmp_path)
    binary = _fake_binary(
        tmp_path,
        {
            "schema_version": 1,
            "collected_at": "2026-08-05T00:00:00Z",
            "health_state": "degraded",
            "suggested_action_ids": ["inspect_storage_source"],
            "engine_status": {"exists": True, "fresh": True},
        },
    )
    artifact = collector.collect(_args(tmp_path, binary))
    assert artifact["artifact_kind"] == "launch_reliability_dogfood_series"
    assert artifact["provenance"]["repository_dirty"] is False
    assert artifact["episodes"][0]["expected"]["red_eligible"] is False
    assert artifact["episodes"][0]["observed"]["suggested_action_ids"] == ["inspect_storage_source"]


def test_command_failure_is_recorded_as_unknown_not_green(tmp_path: Path):
    _git_repo(tmp_path)
    binary = _fake_binary(tmp_path, {"not": "health"}, exit_code=1)
    artifact = collector.collect(_args(tmp_path, binary))
    episode = artifact["episodes"][0]
    assert episode["observed"]["health_state"] == "unknown"
    assert episode["observed"]["producer_freshness"] == "unknown"
    assert "observation_error" in episode


@pytest.mark.parametrize(
    "payload",
    [
        {
            "health_state": "unknown",
            "suggested_action_ids": [],
            "engine_status": {"exists": True, "fresh": True},
        },
        {
            "health_state": "healthy",
            "suggested_action_ids": [""],
            "engine_status": {"exists": True, "fresh": True},
        },
        {
            "health_state": "healthy",
            "suggested_action_ids": [],
            "engine_status": {"exists": "yes", "fresh": True},
        },
        {
            "schema_version": 999,
            "health_state": "healthy",
            "suggested_action_ids": [],
            "engine_status": {"exists": True, "fresh": True},
        },
    ],
)
def test_malformed_native_payload_is_recorded_as_observation_error(tmp_path: Path, payload: dict[str, object]):
    _git_repo(tmp_path)
    binary = _fake_binary(tmp_path, payload)

    artifact = collector.collect(_args(tmp_path, binary))

    assert "observation_error" in artifact["episodes"][0]


def test_conservation_and_issue_require_valid_explicit_inputs(tmp_path: Path):
    _git_repo(tmp_path)
    binary = _fake_binary(
        tmp_path,
        {
            "schema_version": 1,
            "collected_at": "2026-08-05T00:00:00Z",
            "health_state": "healthy",
            "suggested_action_ids": [],
            "engine_status": {"exists": True, "fresh": True},
        },
    )
    conservation = tmp_path / "conservation.json"
    conservation.write_text(
        json.dumps(
            {
                "source_events": 4,
                "archived_events": 2,
                "replayed_events": 1,
                "duplicate_events": 1,
                "discarded_events": 0,
                "unresolved_events": 2,
            }
        ),
        encoding="utf-8",
    )
    artifact = collector.collect(
        _args(
            tmp_path,
            binary,
            expected_health_state="healthy",
            expected_producer_freshness="fresh",
            expected_red_eligible=False,
            expected_action=None,
            issue_status="resolved",
            issue_opened_at="2026-08-04T23:00:00Z",
            issue_resolved_at="2026-08-04T23:30:00Z",
            evidence_conservation_json=conservation,
        )
    )
    episode = artifact["episodes"][0]
    assert episode["event_bearing_issue"]["status"] == "resolved"
    assert episode["evidence_conservation"]["source_events"] == 4

    with pytest.raises(ValueError, match="issue-opened-at"):
        collector.collect(
            _args(
                tmp_path,
                binary,
                issue_status="unresolved",
                issue_opened_at=None,
            )
        )


def test_dirty_repository_is_marked_and_never_hidden(tmp_path: Path):
    _git_repo(tmp_path)
    (tmp_path / "marker").write_text("dirty\n", encoding="utf-8")
    binary = _fake_binary(
        tmp_path,
        {
            "schema_version": 1,
            "collected_at": "2026-08-05T00:00:00Z",
            "health_state": "healthy",
            "engine_status": {"exists": True, "fresh": True},
        },
    )
    artifact = collector.collect(_args(tmp_path, binary))
    assert artifact["provenance"]["repository_dirty"] is True


def test_binary_name_resolves_through_path():
    resolved = collector._resolve_binary(Path("python3"))
    assert resolved.is_absolute()
    assert resolved.name.startswith("python3")
