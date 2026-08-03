from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from zerg.qa.provider_release_schedule import DEFAULT_SCHEDULE_PATH
from zerg.qa.provider_release_schedule import ProviderReleaseScheduleError
from zerg.qa.provider_release_schedule import build_store_staleness
from zerg.qa.provider_release_schedule import load_provider_release_schedule
from zerg.services.managed_provider_contracts import managed_provider_names


def test_schedule_covers_the_contract_and_declares_private_live_token_ownership() -> None:
    schedule = load_provider_release_schedule()

    assert {row.provider for row in schedule.providers} == managed_provider_names()
    assert {entry["provider"] for entry in schedule.matrix()["include"]} == managed_provider_names() - {"antigravity"}
    assert schedule.scheduled_evidence == "generated_fake_unconditional_full_column"


def test_schedule_rejects_a_scheduled_live_token_executor_that_is_not_private_factory(tmp_path: Path) -> None:
    payload = yaml.safe_load(DEFAULT_SCHEDULE_PATH.read_text(encoding="utf-8"))
    payload["live_token"]["executor"] = "github_actions"
    path = tmp_path / "schedule.yml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ProviderReleaseScheduleError, match="private factory"):
        load_provider_release_schedule(path)


def test_weekly_workflow_uses_the_declared_cron_and_independent_cells() -> None:
    workflow = (DEFAULT_SCHEDULE_PATH.parents[1] / ".github/workflows/provider-release-weekly.yml").read_text(
        encoding="utf-8"
    )
    schedule = load_provider_release_schedule()

    assert f"cron: '{schedule.weekly_cron}'" in workflow
    assert "fail-fast: false" in workflow
    assert "continue-on-error: ${{ matrix.allow_failure }}" in workflow


def test_schedule_rejects_a_missing_contract_provider(tmp_path: Path) -> None:
    payload = yaml.safe_load(DEFAULT_SCHEDULE_PATH.read_text(encoding="utf-8"))
    payload["providers"] = payload["providers"][1:]
    path = tmp_path / "schedule.yml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ProviderReleaseScheduleError, match=r"missing=\['codex'\]"):
        load_provider_release_schedule(path)


def test_build_store_staleness_alerts_on_old_and_missing_builds(tmp_path: Path) -> None:
    lock = {
        "schema_version": 1,
        "builds": {
            "codex": {
                "1.2.3": {
                    "linux-x86_64": {
                        "closure_digest": "a" * 64,
                        "first_captured_at": "2026-01-01T00:00:00Z",
                    }
                }
            }
        },
    }
    (tmp_path / "provider-builds.lock").write_text(json.dumps(lock), encoding="utf-8")

    report = build_store_staleness(
        store_root=tmp_path,
        schedule=load_provider_release_schedule(),
        now=datetime(2026, 12, 27, tzinfo=UTC),
    )

    assert report["status"] == "stale"
    assert report["providers"]["codex"]["status"] == "stale"
    assert report["providers"]["claude"]["status"] == "stale"
    assert report["providers"]["claude"]["reason"] == "no_build_observed"
    assert report["providers"]["antigravity"]["status"] == "not_monitored"


def test_missing_builds_collect_during_the_initial_measurement_window(tmp_path: Path) -> None:
    (tmp_path / "provider-builds.lock").write_text(
        json.dumps({"schema_version": 1, "builds": {}}),
        encoding="utf-8",
    )

    report = build_store_staleness(
        store_root=tmp_path,
        schedule=load_provider_release_schedule(),
        now=datetime(2026, 7, 28, tzinfo=UTC),
        providers={"codex"},
    )

    assert report["status"] == "current"
    assert report["alerts"] == []
    assert report["providers"]["codex"]["status"] == "collecting"
