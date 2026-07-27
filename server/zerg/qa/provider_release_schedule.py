from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from zerg.services.managed_provider_contracts import managed_provider_names

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCHEDULE_PATH = REPO_ROOT / "config" / "provider-release-schedule.yml"


class ProviderReleaseScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderScheduleRow:
    provider: str
    release_cadence: str
    release_executor: str
    weekly_unconditional: bool
    allow_failure: bool
    max_build_age_seconds: int | None

    def matrix_entry(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "allow_failure": self.allow_failure,
        }


@dataclass(frozen=True)
class ProviderReleaseSchedule:
    weekly_cron: str
    scheduled_evidence: str
    staleness_measurement_started_at: datetime
    providers: tuple[ProviderScheduleRow, ...]

    def matrix(self) -> dict[str, list[dict[str, Any]]]:
        return {"include": [row.matrix_entry() for row in self.providers if row.weekly_unconditional]}


def load_provider_release_schedule(path: Path = DEFAULT_SCHEDULE_PATH) -> ProviderReleaseSchedule:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProviderReleaseScheduleError(f"provider release schedule is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ProviderReleaseScheduleError("provider release schedule schema_version must be 1")
    raw_rows = payload.get("providers")
    if not isinstance(raw_rows, list):
        raise ProviderReleaseScheduleError("provider release schedule must contain provider rows")
    rows: list[ProviderScheduleRow] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            raise ProviderReleaseScheduleError("provider release schedule rows must be objects")
        provider = item.get("provider")
        cadence = item.get("release_cadence")
        executor = item.get("release_executor")
        max_age = item.get("max_build_age_seconds")
        if not all(isinstance(value, str) and value for value in (provider, cadence, executor)):
            raise ProviderReleaseScheduleError("provider schedule identity fields must be non-empty strings")
        if not isinstance(item.get("weekly_unconditional"), bool) or not isinstance(item.get("allow_failure"), bool):
            raise ProviderReleaseScheduleError("provider schedule booleans must be explicit")
        if max_age is not None and (not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= 0):
            raise ProviderReleaseScheduleError("max_build_age_seconds must be positive or null")
        rows.append(
            ProviderScheduleRow(
                provider=provider,
                release_cadence=cadence,
                release_executor=executor,
                weekly_unconditional=item["weekly_unconditional"],
                allow_failure=item["allow_failure"],
                max_build_age_seconds=max_age,
            )
        )
    names = [row.provider for row in rows]
    if len(names) != len(set(names)):
        raise ProviderReleaseScheduleError("provider release schedule has duplicate providers")
    expected = managed_provider_names()
    if set(names) != expected:
        raise ProviderReleaseScheduleError(
            f"provider release schedule drifted from managed contract: "
            f"missing={sorted(expected - set(names))}, extra={sorted(set(names) - expected)}"
        )
    live_token = payload.get("live_token")
    if live_token != {"cadence": "manual", "executor": "manual", "scheduled": False}:
        raise ProviderReleaseScheduleError("live-token release proof must remain manual and unscheduled")
    weekly_cron = payload.get("weekly_cron")
    scheduled_evidence = payload.get("scheduled_evidence")
    measurement_started_at = payload.get("staleness_measurement_started_at")
    if not isinstance(weekly_cron, str) or not weekly_cron:
        raise ProviderReleaseScheduleError("weekly_cron must be a non-empty string")
    if scheduled_evidence != "generated_fake_unconditional_full_column":
        raise ProviderReleaseScheduleError("scheduled evidence must be an unconditional generated-fake full column")
    if not isinstance(measurement_started_at, str):
        raise ProviderReleaseScheduleError("staleness_measurement_started_at must be an ISO timestamp")
    return ProviderReleaseSchedule(
        weekly_cron=weekly_cron,
        scheduled_evidence=scheduled_evidence,
        staleness_measurement_started_at=_timestamp(measurement_started_at),
        providers=tuple(rows),
    )


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderReleaseScheduleError(f"invalid provider build capture timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ProviderReleaseScheduleError(f"provider build capture timestamp has no timezone: {value}")
    return parsed.astimezone(UTC)


def build_store_staleness(
    *,
    store_root: Path,
    schedule: ProviderReleaseSchedule,
    now: datetime | None = None,
    providers: set[str] | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    lock_path = store_root / "provider-builds.lock"
    if lock_path.exists():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderReleaseScheduleError(f"provider build lock is unreadable: {lock_path}") from exc
    else:
        lock = {"schema_version": 1, "builds": {}}
    builds = lock.get("builds") if isinstance(lock, dict) else None
    if lock.get("schema_version") != 1 or not isinstance(builds, dict):
        raise ProviderReleaseScheduleError("provider build lock schema is invalid")
    requested_providers = providers
    provider_reports: dict[str, dict[str, Any]] = {}
    alerts: list[dict[str, Any]] = []
    for row in schedule.providers:
        if requested_providers is not None and row.provider not in requested_providers:
            continue
        if row.max_build_age_seconds is None:
            provider_reports[row.provider] = {"status": "not_monitored", "reason": "schedule_threshold_disabled"}
            continue
        captures: list[datetime] = []
        provider_builds = builds.get(row.provider, {})
        if isinstance(provider_builds, dict):
            for version_builds in provider_builds.values():
                if not isinstance(version_builds, dict):
                    continue
                for entry in version_builds.values():
                    captured_at = entry.get("first_captured_at") if isinstance(entry, dict) else None
                    if isinstance(captured_at, str):
                        captures.append(_timestamp(captured_at))
        if not captures:
            measurement_age = max(0, int((now - schedule.staleness_measurement_started_at).total_seconds()))
            status = "stale" if measurement_age > row.max_build_age_seconds else "collecting"
            detail = {
                "provider": row.provider,
                "status": status,
                "reason": "no_build_observed",
                "measurement_age_seconds": measurement_age,
                "max_build_age_seconds": row.max_build_age_seconds,
            }
            provider_reports[row.provider] = detail
            if status == "stale":
                alerts.append(detail)
            continue
        newest = max(captures)
        age_seconds = max(0, int((now - newest).total_seconds()))
        status = "stale" if age_seconds > row.max_build_age_seconds else "current"
        detail = {
            "provider": row.provider,
            "status": status,
            "newest_captured_at": newest.isoformat().replace("+00:00", "Z"),
            "age_seconds": age_seconds,
            "max_build_age_seconds": row.max_build_age_seconds,
        }
        provider_reports[row.provider] = detail
        if status == "stale":
            alerts.append(detail)
    return {
        "schema_version": 1,
        "artifact_kind": "provider_build_store_staleness",
        "status": "stale" if alerts else "current",
        "providers": provider_reports,
        "alerts": alerts,
    }
