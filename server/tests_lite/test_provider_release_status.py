from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from zerg import provider_release_status as prs


def test_normalizes_codex_cli_version() -> None:
    assert prs.normalize_provider_version("codex-cli 0.133.0") == "0.133.0"
    assert prs.normalize_provider_version("0.134.0") == "0.134.0"


def test_red_matching_local_version_blocks(monkeypatch, tmp_path: Path) -> None:
    artifact = {
        "provider": "codex",
        "codex_version": "0.133.0",
        "verdict": "red",
        "failure_code": "managed_resume_active_thread_error",
        "recommendation": "block_upgrade_recommendation",
        "generated_at": "2026-05-27T00:00:00Z",
        "evidence_root": "/data/provider-release-status/codex",
    }
    status_file = tmp_path / "codex.json"
    status_file.write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setenv(prs.CODEX_RELEASE_STATUS_FILE_ENV, str(status_file))

    monkeypatch.setattr(
        prs.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="codex-cli 0.133.0\n", stderr=""),
    )

    status = prs.collect_provider_release_status(
        {"codex": {"path": "/opt/homebrew/bin/codex"}},
        fast=False,
    )

    assert status["blocking_count"] == 1
    assert status["statuses"]["codex"]["status"] == "blocked"
    assert status["statuses"]["codex"]["local_version_matches"] is True


def test_red_nonmatching_local_version_is_ok(monkeypatch, tmp_path: Path) -> None:
    status_file = tmp_path / "codex.json"
    status_file.write_text(
        json.dumps({"provider": "codex", "codex_version": "0.133.0", "verdict": "red"}),
        encoding="utf-8",
    )
    monkeypatch.setenv(prs.CODEX_RELEASE_STATUS_FILE_ENV, str(status_file))
    monkeypatch.setattr(
        prs.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="codex-cli 0.134.0\n", stderr=""),
    )

    status = prs.collect_provider_release_status({"codex": {"path": "/opt/homebrew/bin/codex"}})

    assert status["blocking_count"] == 0
    assert status["statuses"]["codex"]["status"] == "ok"
    assert status["statuses"]["codex"]["local_version_matches"] is False


def test_fast_local_health_skips_provider_status() -> None:
    status = prs.collect_provider_release_status({"codex": {"path": "/opt/homebrew/bin/codex"}}, fast=True)

    assert status["enabled"] is False
    assert status["skipped_reason"] == "fast_local_health"
