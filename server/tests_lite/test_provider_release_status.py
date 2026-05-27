from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zerg import provider_release_status as prs


@pytest.fixture(autouse=True)
def clear_release_status_env(monkeypatch) -> None:
    for key in (
        prs.PROVIDER_RELEASE_STATUS_DIR_ENV,
        prs.PROVIDER_RELEASE_STATUS_URL_ENV,
        prs.CODEX_RELEASE_STATUS_FILE_ENV,
        prs.CODEX_RELEASE_STATUS_URL_ENV,
        prs.PROVIDER_RELEASE_STATUS_MAX_AGE_SECONDS_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


def test_normalizes_codex_cli_version() -> None:
    assert prs.normalize_provider_version("codex-cli 0.133.0") == "0.133.0"
    assert prs.normalize_provider_version("0.134.0") == "0.134.0"
    assert prs.normalize_provider_version("rust-v0.134.0-rc.1") == "0.134.0"


def test_red_matching_local_version_blocks(monkeypatch, tmp_path: Path) -> None:
    artifact = {
        "provider": "codex",
        "codex_version": "0.133.0",
        "schema_version": prs.PROVIDER_STATUS_SCHEMA_VERSION,
        "verdict": "red",
        "failure_code": "managed_tui_attach_active_thread_error",
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


def test_red_nonmatching_local_version_warns_for_untested_current_version(monkeypatch, tmp_path: Path) -> None:
    status_file = tmp_path / "codex.json"
    status_file.write_text(
        json.dumps(
            {
                "provider": "codex",
                "schema_version": prs.PROVIDER_STATUS_SCHEMA_VERSION,
                "codex_version": "0.133.0",
                "verdict": "red",
                "generated_at": "2026-05-27T00:00:00Z",
            }
        ),
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
    assert status["warning_count"] == 1
    assert status["statuses"]["codex"]["status"] == "unknown_for_current_version"
    assert status["statuses"]["codex"]["local_version_matches"] is False


def test_stale_matching_green_artifact_warns(monkeypatch, tmp_path: Path) -> None:
    status_file = tmp_path / "codex.json"
    status_file.write_text(
        json.dumps(
            {
                "provider": "codex",
                "schema_version": prs.PROVIDER_STATUS_SCHEMA_VERSION,
                "codex_version": "0.133.0",
                "verdict": "green",
                "generated_at": "2020-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(prs.CODEX_RELEASE_STATUS_FILE_ENV, str(status_file))
    monkeypatch.setattr(
        prs.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="codex-cli 0.133.0\n", stderr=""),
    )

    status = prs.collect_provider_release_status({"codex": {"path": "/opt/homebrew/bin/codex"}})

    assert status["warning_count"] == 1
    assert status["statuses"]["codex"]["status"] == "stale"
    assert status["statuses"]["codex"]["freshness_status"] == "stale"


def test_schema_mismatch_warns(monkeypatch, tmp_path: Path) -> None:
    status_file = tmp_path / "codex.json"
    status_file.write_text(
        json.dumps(
            {
                "provider": "codex",
                "codex_version": "0.133.0",
                "verdict": "green",
                "generated_at": "2026-05-27T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(prs.CODEX_RELEASE_STATUS_FILE_ENV, str(status_file))
    monkeypatch.setattr(
        prs.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="codex-cli 0.133.0\n", stderr=""),
    )

    status = prs.collect_provider_release_status({"codex": {"path": "/opt/homebrew/bin/codex"}})

    assert status["warning_count"] == 1
    assert status["statuses"]["codex"]["status"] == "schema_mismatch"
    assert status["statuses"]["codex"]["schema_status"] == "mismatch"


def test_configured_but_unavailable_artifact_warns(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(prs.CODEX_RELEASE_STATUS_FILE_ENV, str(tmp_path / "missing.json"))

    status = prs.collect_provider_release_status({"codex": {"path": "/opt/homebrew/bin/codex"}})

    assert status["warning_count"] == 1
    assert status["statuses"]["codex"]["status"] == "unavailable"


def test_collects_all_managed_provider_statuses_without_configuration() -> None:
    status = prs.collect_provider_release_status({})

    assert status["enabled"] is False
    assert status["blocking_count"] == 0
    assert status["warning_count"] == 0
    assert set(status["statuses"]) == {"antigravity", "claude", "codex", "opencode"}
    assert {item["status"] for item in status["statuses"].values()} == {"not_configured"}


def test_collects_provider_status_artifacts_for_all_managed_providers(monkeypatch, tmp_path: Path) -> None:
    artifacts = {
        "codex": "codex-cli 0.134.0",
        "claude": "1.2.3",
        "opencode": "0.4.5",
        "antigravity": "0.6.7",
    }
    for provider, version in artifacts.items():
        (tmp_path / f"{provider}.json").write_text(
            json.dumps(
                {
                    "provider": provider,
                    "schema_version": prs.PROVIDER_STATUS_SCHEMA_VERSION,
                    "provider_version": version,
                    "verdict": "green",
                    "generated_at": "2026-05-27T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setenv(prs.PROVIDER_RELEASE_STATUS_DIR_ENV, str(tmp_path))

    def fake_run(args, **kwargs):
        provider = Path(args[0]).name
        return SimpleNamespace(returncode=0, stdout=f"{artifacts[provider]}\n", stderr="")

    monkeypatch.setattr(prs.subprocess, "run", fake_run)

    status = prs.collect_provider_release_status(
        {provider: {"path": f"/opt/homebrew/bin/{provider}"} for provider in artifacts}
    )

    assert status["enabled"] is True
    assert status["blocking_count"] == 0
    assert status["warning_count"] == 0
    assert {provider: item["status"] for provider, item in status["statuses"].items()} == {
        "antigravity": "ok",
        "claude": "ok",
        "codex": "ok",
        "opencode": "ok",
    }


def test_generic_status_dir_missing_provider_artifacts_are_not_configured(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "codex.json").write_text(
        json.dumps(
            {
                "provider": "codex",
                "schema_version": prs.PROVIDER_STATUS_SCHEMA_VERSION,
                "provider_version": "codex-cli 0.134.0",
                "verdict": "green",
                "generated_at": "2026-05-27T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(prs.PROVIDER_RELEASE_STATUS_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        prs.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="codex-cli 0.134.0\n", stderr=""),
    )

    status = prs.collect_provider_release_status({"codex": {"path": "/opt/homebrew/bin/codex"}})

    assert status["enabled"] is True
    assert status["warning_count"] == 0
    assert status["statuses"]["codex"]["status"] == "ok"
    assert status["statuses"]["claude"]["status"] == "not_configured"
    assert status["statuses"]["opencode"]["status"] == "not_configured"
    assert status["statuses"]["antigravity"]["status"] == "not_configured"


def test_fast_local_health_skips_provider_status() -> None:
    status = prs.collect_provider_release_status({"codex": {"path": "/opt/homebrew/bin/codex"}}, fast=True)

    assert status["enabled"] is False
    assert status["skipped_reason"] == "fast_local_health"
