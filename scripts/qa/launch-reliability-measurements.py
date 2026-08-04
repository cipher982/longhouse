#!/usr/bin/env python3
"""Summarize retained launch-reliability evidence without inventing metrics.

The installed managed-launch matrix is deliberately allowed to be yellow. This
report keeps provider setup gaps, provider-owned failures, and successful
recovery separate, and marks measures that the supplied artifacts cannot prove
as ``not_observed``. It is a report over existing evidence, not a new incident
registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


MATRIX_FILENAME = "installed-managed-launch-fault-matrix.json"
EXPECTED_PROVIDERS = frozenset({"claude", "codex", "cursor", "opencode"})
DOGFOOD_ARTIFACT_KIND = "launch_reliability_dogfood_series"
DOGFOOD_SCHEMA_VERSION = 1
MIN_DOGFOOD_EPISODES = 3
MIN_DOGFOOD_SPAN_SECONDS = 3600.0
CONSERVATION_FIELDS = (
    "source_events",
    "archived_events",
    "replayed_events",
    "duplicate_events",
    "discarded_events",
    "unresolved_events",
)
HEALTH_STATES = frozenset({"healthy", "degraded", "broken", "unknown"})
PRODUCER_FRESHNESS_STATES = frozenset({"fresh", "stale", "unknown"})


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def report_provenance() -> dict[str, Any]:
    """Bind the report to the exact reporter and repository state that made it."""

    path = Path(__file__).resolve()
    repository = path.parents[2]
    relative_path = path.relative_to(repository)
    status_lines = _git(repository, "status", "--porcelain", "--untracked-files=all").splitlines()
    file_status = _git(repository, "status", "--porcelain", "--untracked-files=all", "--", str(relative_path))
    return {
        "argv": list(sys.argv),
        "cwd": str(Path.cwd()),
        "git_sha": _git(repository, "rev-parse", "HEAD"),
        "harness_file_dirty": bool(file_status),
        "path": str(path),
        "repository": str(repository),
        "repository_dirty": bool(status_lines),
        "sha256": _sha256(path),
    }


def discover_matrix_artifacts(inputs: Iterable[Path]) -> list[Path]:
    """Resolve files/directories and return unique matrix artifacts in order."""

    paths: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        path = raw.expanduser().resolve()
        if not path.exists():
            raise ValueError(f"matrix input does not exist: {path}")
        if path.is_file():
            candidates = [path]
        else:
            candidates = []
            direct = path / MATRIX_FILENAME
            if direct.is_file():
                candidates.append(direct)
            candidates.extend(sorted(path.glob(f"*/{MATRIX_FILENAME}")))
        if not candidates:
            raise ValueError(f"matrix input contains no {MATRIX_FILENAME}: {path}")
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)
    return paths


def _is_full_run(artifact: dict[str, Any]) -> bool:
    provider_counts = Counter(
        str(entry.get("provider") or "") for entry in artifact.get("providers") or [] if isinstance(entry, dict)
    )
    return (
        len(artifact.get("providers") or []) == 8
        and provider_counts == {provider: 2 for provider in EXPECTED_PROVIDERS}
        and artifact.get("artifact_kind") == "installed_managed_launch_fault_matrix"
    )


def _cleanup_statuses(artifact: dict[str, Any]) -> list[str]:
    statuses: list[str] = []
    for provider in artifact.get("providers") or []:
        cleanup = provider.get("detached_cleanup")
        if isinstance(cleanup, dict):
            statuses.append(str(cleanup.get("status") or "unknown"))
    return statuses


def _auth_gaps(artifact: dict[str, Any]) -> list[str]:
    return sorted(
        provider
        for provider, auth in (artifact.get("provider_auth") or {}).items()
        if isinstance(auth, dict) and auth.get("status") != "ready"
    )


def _startup_failure_counts(artifact: dict[str, Any]) -> dict[str, int]:
    counts = {"provider_owned": 0, "harness_precondition": 0, "unknown": 0}
    for failure in artifact.get("provider_startup_failures") or []:
        if not isinstance(failure, dict) or not failure.get("provider"):
            continue
        qualification = failure.get("qualification")
        if qualification == "provider_owned_start_failure":
            counts["provider_owned"] += 1
        elif qualification == "harness_precondition_unmet":
            counts["harness_precondition"] += 1
        else:
            counts["unknown"] += 1
    return counts


def _measured_run(artifact: dict[str, Any]) -> bool:
    measurements = artifact.get("measurements") or {}
    return bool(
        _is_full_run(artifact)
        and isinstance(measurements, dict)
        and measurements.get("recovery_duration_seconds") is not None
        and measurements.get("run_duration_seconds") is not None
        and artifact.get("harness", {}).get("repository_dirty") is False
        and artifact.get("harness", {}).get("harness_file_dirty") is False
    )


def _excluded_run_reason(artifact: dict[str, Any]) -> str | None:
    if not _is_full_run(artifact):
        return "not_exact_four_provider_eight_launch_run"
    measurements = artifact.get("measurements") or {}
    harness = artifact.get("harness") or {}
    if measurements.get("recovery_duration_seconds") is None or measurements.get("run_duration_seconds") is None:
        return "missing_timing_measurements"
    if harness.get("repository_dirty") is None or harness.get("harness_file_dirty") is None:
        return "missing_clean_harness_provenance"
    if harness.get("repository_dirty") is not False or harness.get("harness_file_dirty") is not False:
        return "dirty_harness_or_repository"
    return None


def _input_reference(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _sha256(path)}


def _successful_recovery(artifact: dict[str, Any]) -> bool:
    before = artifact.get("retry_intents_before_recovery")
    after = artifact.get("retry_intents_after_recovery")
    cleanup = _cleanup_statuses(artifact)
    return bool(
        _measured_run(artifact)
        and isinstance(before, int)
        and isinstance(after, int)
        and before > 0
        and after == 0
        and len(cleanup) == len(artifact.get("providers") or [])
        and all(status == "pass" for status in cleanup)
    )


def _not_observed(reason: str) -> dict[str, str]:
    return {"status": "not_observed", "reason": reason}


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _nonnegative_count(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _health_truth(episode: dict[str, Any], *, label: str) -> None:
    expected = episode.get("expected")
    observed = episode.get("observed")
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        raise ValueError(f"{label} must include expected and observed health truth objects")
    for side, value in (("expected", expected), ("observed", observed)):
        if value.get("health_state") not in HEALTH_STATES:
            raise ValueError(f"{label}.{side}.health_state is invalid")
        if value.get("producer_freshness") not in PRODUCER_FRESHNESS_STATES:
            raise ValueError(f"{label}.{side}.producer_freshness is invalid")
    if not isinstance(expected.get("red_eligible"), bool):
        raise ValueError(f"{label}.expected.red_eligible must be boolean")
    action = expected.get("action")
    if action is not None and not isinstance(action, str):
        raise ValueError(f"{label}.expected.action must be a string or null")
    suggested = observed.get("suggested_action_ids")
    if suggested is not None and (
        not isinstance(suggested, list) or not all(isinstance(item, str) for item in suggested)
    ):
        raise ValueError(f"{label}.observed.suggested_action_ids must be a string list")


def _load_dogfood_series(
    paths: Iterable[Path],
    invalid: list[dict[str, str]],
    *,
    as_of: datetime | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Load validated longitudinal observations without filling missing facts."""

    references: list[dict[str, str]] = []
    episodes: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    seen_hashes: set[str] = set()
    seen_episode_ids: set[str] = set()
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if path in seen_paths:
            continue
        try:
            content_hash = _sha256(path)
            if content_hash in seen_hashes:
                continue
            seen_paths.add(path)
            seen_hashes.add(content_hash)
            artifact = _json(path)
            if artifact.get("artifact_kind") != DOGFOOD_ARTIFACT_KIND:
                raise ValueError(f"artifact_kind must be {DOGFOOD_ARTIFACT_KIND!r}")
            if artifact.get("schema_version") != DOGFOOD_SCHEMA_VERSION:
                raise ValueError(f"schema_version must be {DOGFOOD_SCHEMA_VERSION}")
            generated_at = _timestamp(artifact.get("generated_at"), label="dogfood.generated_at")
            if as_of is not None and generated_at > as_of:
                raise ValueError("dogfood.generated_at is after report generation time")
            provenance = artifact.get("provenance")
            if not isinstance(provenance, dict):
                raise ValueError("dogfood.provenance must be an object")
            if not isinstance(provenance.get("generator"), str) or not provenance["generator"]:
                raise ValueError("dogfood.provenance.generator must be non-empty")
            git_sha = provenance.get("git_sha")
            if not isinstance(git_sha, str) or len(git_sha) != 40 or any(
                character not in "0123456789abcdefABCDEF" for character in git_sha
            ):
                raise ValueError("dogfood.provenance.git_sha must be a 40-character SHA-1")
            if provenance.get("repository_dirty") is not False:
                raise ValueError("dogfood.provenance.repository_dirty must be false")
            raw_episodes = artifact.get("episodes")
            if not isinstance(raw_episodes, list) or not raw_episodes:
                raise ValueError("dogfood artifact has no non-empty episodes list")
            loaded_episodes: list[dict[str, Any]] = []
            loaded_ids: set[str] = set()
            for index, episode in enumerate(raw_episodes):
                if not isinstance(episode, dict):
                    raise ValueError(f"episode {index} is not an object")
                if not isinstance(episode.get("episode_id"), str) or not episode["episode_id"]:
                    raise ValueError(f"episode {index} has no episode_id")
                observed_at = _timestamp(
                    episode.get("observed_at"),
                    label=f"episode {index}.observed_at",
                )
                if observed_at > generated_at:
                    raise ValueError(f"episode {index}.observed_at is after dogfood.generated_at")
                episode_id = episode["episode_id"]
                if episode_id in seen_episode_ids or episode_id in loaded_ids:
                    raise ValueError(f"duplicate episode_id: {episode_id}")
                loaded_ids.add(episode_id)
                _health_truth(episode, label=f"episode {index}")
                recovery = episode.get("recovery_duration_seconds")
                if recovery is not None and (
                    isinstance(recovery, bool)
                    or not isinstance(recovery, (int, float))
                    or recovery < 0
                    or not math.isfinite(float(recovery))
                ):
                    raise ValueError(
                        f"episode {index}.recovery_duration_seconds must be non-negative"
                    )
                issue = episode.get("event_bearing_issue")
                if issue is not None:
                    if not isinstance(issue, dict):
                        raise ValueError(f"episode {index}.event_bearing_issue is not an object")
                    if issue.get("status") not in {"resolved", "unresolved"}:
                        raise ValueError(
                            f"episode {index}.event_bearing_issue.status must be resolved or unresolved"
                        )
                    _timestamp(issue.get("opened_at"), label=f"episode {index}.event_bearing_issue.opened_at")
                    if issue.get("status") == "resolved":
                        resolved_at = _timestamp(
                            issue.get("resolved_at"),
                            label=f"episode {index}.event_bearing_issue.resolved_at",
                        )
                        opened_at = _timestamp(
                            issue.get("opened_at"),
                            label=f"episode {index}.event_bearing_issue.opened_at",
                        )
                        if resolved_at < opened_at:
                            raise ValueError(
                                f"episode {index}.event_bearing_issue.resolved_at precedes opened_at"
                            )
                        if resolved_at > observed_at:
                            raise ValueError(
                                f"episode {index}.event_bearing_issue.resolved_at is after observed_at"
                            )
                    else:
                        opened_at = _timestamp(
                            issue.get("opened_at"),
                            label=f"episode {index}.event_bearing_issue.opened_at",
                        )
                        if observed_at < opened_at:
                            raise ValueError(
                                f"episode {index}.observed_at precedes event_bearing_issue.opened_at"
                            )
                conservation = episode.get("evidence_conservation")
                if conservation is not None:
                    if not isinstance(conservation, dict):
                        raise ValueError(f"episode {index}.evidence_conservation is not an object")
                    for field in CONSERVATION_FIELDS:
                        _nonnegative_count(
                            conservation.get(field),
                            label=f"episode {index}.evidence_conservation.{field}",
                        )
                    accounted = sum(
                        conservation[field]
                        for field in ("archived_events", "discarded_events", "unresolved_events")
                    )
                    if accounted > conservation["source_events"]:
                        raise ValueError(
                            f"episode {index}.evidence_conservation accounts for more events than source_events"
                        )
                    for field in ("replayed_events", "duplicate_events"):
                        if conservation[field] > conservation["source_events"]:
                            raise ValueError(
                                f"episode {index}.evidence_conservation.{field} exceeds source_events"
                            )
                loaded_episodes.append(dict(episode))
            episodes.extend(loaded_episodes)
            seen_episode_ids.update(loaded_ids)
            references.append(_input_reference(path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append({"path": str(path), "error": str(exc)})
    return references, episodes


def _dogfood_summary(
    episodes: list[dict[str, Any]],
    references: list[dict[str, str]],
) -> dict[str, Any]:
    """Summarize only facts present in the retained dogfood observations."""

    observed_times = sorted(
        _timestamp(episode["observed_at"], label="episode.observed_at")
        for episode in episodes
    )
    first_observed_at = (
        observed_times[0].isoformat().replace("+00:00", "Z") if observed_times else None
    )
    last_observed_at = (
        observed_times[-1].isoformat().replace("+00:00", "Z") if observed_times else None
    )
    span_seconds = (
        (observed_times[-1] - observed_times[0]).total_seconds()
        if observed_times
        else None
    )
    distinct_observation_count = len(set(observed_times))
    series_eligible = bool(
        len(observed_times) >= MIN_DOGFOOD_EPISODES
        and distinct_observation_count >= MIN_DOGFOOD_EPISODES
        and span_seconds is not None
        and span_seconds >= MIN_DOGFOOD_SPAN_SECONDS
    )
    series_reason = None if series_eligible else (
        f"requires at least {MIN_DOGFOOD_EPISODES} episodes spanning "
        f"{MIN_DOGFOOD_SPAN_SECONDS:g} seconds"
    )
    recovery = [
        float(episode["recovery_duration_seconds"])
        for episode in episodes
        if episode.get("recovery_duration_seconds") is not None
    ]
    health = [
        episode
        for episode in episodes
        if isinstance(episode.get("expected"), dict)
        and isinstance(episode.get("observed"), dict)
        and isinstance(episode["expected"].get("health_state"), str)
        and isinstance(episode["observed"].get("health_state"), str)
    ]
    broken_cases = sum(episode["observed"]["health_state"] == "broken" for episode in health)
    false_red_cases = sum(
        episode["observed"]["health_state"] == "broken"
        and not episode["expected"]["red_eligible"]
        for episode in health
    )
    eligible_failure_cases = sum(
        episode["expected"]["red_eligible"]
        or episode["expected"]["producer_freshness"] != "fresh"
        for episode in health
    )
    hidden_failure_cases = sum(
        (
            episode["expected"]["red_eligible"]
            or episode["expected"]["producer_freshness"] != "fresh"
        )
        and episode["observed"]["health_state"] == "healthy"
        and episode["observed"]["producer_freshness"] == "fresh"
        for episode in health
    )
    actionable = [
        episode
        for episode in health
        if episode["expected"].get("action") not in {None, "none"}
    ]
    action_pass = sum(
        episode["expected"]["action"]
        in {str(value) for value in episode["observed"].get("suggested_action_ids") or []}
        for episode in actionable
    )

    issue_ages: list[float] = []
    unresolved_issues = 0
    for episode in episodes:
        issue = episode.get("event_bearing_issue")
        if not isinstance(issue, dict):
            continue
        observed_at = _timestamp(episode["observed_at"], label="episode.observed_at")
        opened_at = _timestamp(issue["opened_at"], label="event_bearing_issue.opened_at")
        if issue["status"] == "unresolved":
            unresolved_issues += 1
            issue_ages.append((observed_at - opened_at).total_seconds())

    conservation = [
        episode["evidence_conservation"]
        for episode in episodes
        if isinstance(episode.get("evidence_conservation"), dict)
    ]
    conservation_totals = {
        field: sum(item[field] for item in conservation)
        for field in CONSERVATION_FIELDS
    }
    accounted_events = sum(
        conservation_totals[field]
        for field in ("archived_events", "discarded_events", "unresolved_events")
    )
    source_events = conservation_totals["source_events"]
    return {
        "scope": "dogfood_episode_series",
        "episode_count": len(episodes),
        "source": references,
        "observation_window": {
            "status": "eligible" if series_eligible else "insufficient",
            "reason": series_reason,
            "first_observed_at": first_observed_at,
            "last_observed_at": last_observed_at,
            "span_seconds": span_seconds,
            "distinct_observation_count": distinct_observation_count,
            "minimum_episode_count": MIN_DOGFOOD_EPISODES,
            "minimum_span_seconds": MIN_DOGFOOD_SPAN_SECONDS,
        },
        "automatic_recovery_time": {
            "status": "observed" if series_eligible and recovery else "not_observed",
            **({} if series_eligible else {"reason": series_reason}),
            "scope": "dogfood_episode_series",
            "source": references,
            "sample_count": len(recovery) if series_eligible else None,
            "seconds": {
                "min": min(recovery) if series_eligible and recovery else None,
                "median": statistics.median(recovery) if series_eligible and recovery else None,
                "max": max(recovery) if series_eligible and recovery else None,
            },
        },
        "false_red_rate": {
            "status": "observed" if series_eligible and broken_cases else "not_observed",
            **({} if series_eligible else {"reason": series_reason}),
            "scope": "dogfood_episode_series",
            "source": references,
            "numerator": false_red_cases if series_eligible else None,
            "denominator": broken_cases if series_eligible else None,
            "rate": false_red_cases / broken_cases if series_eligible and broken_cases else None,
            "numerator_definition": "dogfood_observed_broken_cases_expected_red_ineligible",
            "denominator_definition": "dogfood_observed_broken_cases",
        },
        "hidden_failure_rate": {
            "status": "observed" if series_eligible and eligible_failure_cases else "not_observed",
            **({} if series_eligible else {"reason": series_reason}),
            "scope": "dogfood_episode_series",
            "source": references,
            "numerator": hidden_failure_cases if series_eligible else None,
            "denominator": eligible_failure_cases if series_eligible else None,
            "rate": hidden_failure_cases / eligible_failure_cases if series_eligible and eligible_failure_cases else None,
            "numerator_definition": "dogfood_expected_risk_observed_healthy_and_fresh",
            "denominator_definition": "dogfood_expected_red_risk_or_nonfresh_producer_cases",
        },
        "action_coverage": {
            "status": "observed" if series_eligible and actionable else "not_observed",
            **({} if series_eligible else {"reason": series_reason}),
            "scope": "dogfood_episode_series",
            "source": references,
            "numerator": action_pass if series_eligible else None,
            "denominator": len(actionable) if series_eligible else None,
            "rate": action_pass / len(actionable) if series_eligible and actionable else None,
            "numerator_definition": "dogfood_expected_action_present_in_observed_actions",
            "denominator_definition": "dogfood_cases_with_a_non_none_expected_action",
        },
        "unresolved_event_bearing_issue_age": {
            "status": "observed" if series_eligible and issue_ages else "not_observed",
            **({} if series_eligible else {"reason": series_reason}),
            "scope": "dogfood_episode_series",
            "source": references,
            "unresolved_count": unresolved_issues if series_eligible else None,
            "sample_count": len(issue_ages) if series_eligible else None,
            "max_age_seconds": max(issue_ages) if series_eligible and issue_ages else None,
            "median_age_seconds": statistics.median(issue_ages) if series_eligible and issue_ages else None,
            "denominator_definition": "dogfood_unresolved_event_bearing_issues",
        },
        "duplicate_replayed_discarded_evidence": {
            "status": "observed" if series_eligible and conservation else "not_observed",
            **({} if series_eligible else {"reason": series_reason}),
            "scope": "dogfood_episode_series",
            "source": references,
            "sample_count": len(conservation) if series_eligible else None,
            "totals": conservation_totals if series_eligible else {field: None for field in CONSERVATION_FIELDS},
            "accounted_events": accounted_events if series_eligible else None,
            "unaccounted_events": source_events - accounted_events if series_eligible and conservation else None,
            "conservation_status": (
                "complete"
                if series_eligible
                and conservation
                and source_events == accounted_events
                and conservation_totals["discarded_events"] == 0
                else "complete_with_discard"
                if series_eligible and conservation and source_events == accounted_events
                else "gap"
            )
            if series_eligible and conservation
            else None,
            "denominator_definition": "dogfood_source_events",
        },
    }


def _invalidate_dogfood_summary(summary: dict[str, Any]) -> None:
    """Remove usable numeric claims when any dogfood input is invalid."""

    summary["input_status"] = "invalid"
    summary["episode_count"] = 0
    summary["source"] = []
    summary["observation_window"] = {
        "status": "invalid",
        "reason": "one or more dogfood artifacts failed validation",
        "first_observed_at": None,
        "last_observed_at": None,
        "span_seconds": None,
        "distinct_observation_count": 0,
        "minimum_episode_count": MIN_DOGFOOD_EPISODES,
        "minimum_span_seconds": MIN_DOGFOOD_SPAN_SECONDS,
    }
    for metric_name in (
        "automatic_recovery_time",
        "false_red_rate",
        "hidden_failure_rate",
        "action_coverage",
        "unresolved_event_bearing_issue_age",
        "duplicate_replayed_discarded_evidence",
    ):
        metric = summary[metric_name]
        metric["status"] = "not_observed"
        metric["reason"] = "one or more dogfood artifacts failed validation"
        metric["source"] = []
        for field in (
            "sample_count",
            "numerator",
            "denominator",
            "rate",
            "unresolved_count",
            "max_age_seconds",
            "median_age_seconds",
            "accounted_events",
            "unaccounted_events",
        ):
            if field in metric:
                metric[field] = None
        if "seconds" in metric:
            metric["seconds"] = {"min": None, "median": None, "max": None}
        if "totals" in metric:
            metric["totals"] = {field: None for field in CONSERVATION_FIELDS}
        if "conservation_status" in metric:
            metric["conservation_status"] = None


def _health_measurements(paths: Iterable[Path], invalid: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    references: list[dict[str, str]] = []
    seen_paths: set[Path] = set()
    seen_hashes: set[str] = set()
    total = 0
    broken_cases = 0
    false_red_cases = 0
    eligible_failure_cases = 0
    hidden_failure_cases = 0
    action_total = 0
    action_pass = 0
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if path in seen_paths:
            continue
        try:
            content_hash = _sha256(path)
            if content_hash in seen_hashes:
                continue
            seen_paths.add(path)
            seen_hashes.add(content_hash)
            artifact = _json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append({"path": str(path), "error": str(exc)})
            continue
        results = artifact.get("results")
        if not isinstance(results, list):
            invalid.append({"path": str(path), "error": "health artifact has no results list"})
            continue
        references.append(_input_reference(path))
        for result in results:
            if not isinstance(result, dict):
                continue
            expected = result.get("expected") or {}
            observed = result.get("observed") or {}
            expected_state = str(expected.get("state") or "unknown")
            observed_state = str(observed.get("health_state") or "unknown")
            total += 1
            if observed_state == "broken":
                broken_cases += 1
                if expected_state != "broken":
                    false_red_cases += 1
            if expected_state in {"broken", "degraded"}:
                eligible_failure_cases += 1
                if observed_state == "healthy":
                    hidden_failure_cases += 1
            expected_action = str(expected.get("action") or "none")
            if expected_action != "none":
                action_total += 1
                suggested = {str(value) for value in observed.get("suggested_action_ids") or []}
                if expected_action in suggested:
                    action_pass += 1

    scope = "installed_health_fault_matrix"
    measurements = {
        "false_red_rate": {
            "status": "observed" if broken_cases else "not_observed",
            **({} if broken_cases else {"reason": "no observed broken cases in supplied health fault matrix"}),
            "scope": scope,
            "basis": "fault_matrix_expected_state",
            "numerator": false_red_cases,
            "denominator": broken_cases,
            "numerator_definition": "observed_broken_cases_expected_not_broken",
            "denominator_definition": "observed_broken_cases",
            "rate": (false_red_cases / broken_cases) if broken_cases else None,
            "source": references,
        },
        "hidden_failure_rate": {
            "status": "observed" if eligible_failure_cases else "not_observed",
            **({} if eligible_failure_cases else {"reason": "no expected broken/degraded cases in supplied health fault matrix"}),
            "scope": scope,
            "numerator": hidden_failure_cases,
            "denominator": eligible_failure_cases,
            "numerator_definition": "observed_healthy_expected_broken_or_degraded",
            "denominator_definition": "expected_broken_or_degraded_cases",
            "rate": (hidden_failure_cases / eligible_failure_cases) if eligible_failure_cases else None,
            "source": references,
        },
        "action_coverage": {
            "status": "observed" if action_total else "not_observed",
            **({} if action_total else {"reason": "no cases with an expected action in supplied health fault matrix"}),
            "scope": scope,
            "numerator": action_pass,
            "denominator": action_total,
            "denominator_definition": "cases_with_a_non_none_expected_action",
            "rate": (action_pass / action_total) if action_total else None,
            "source": references,
        },
    }
    return references, {"case_count": total, "measurements": measurements}


def build_report(
    matrix_paths: Iterable[Path],
    harness_paths: Iterable[Path] = (),
    health_paths: Iterable[Path] = (),
    dogfood_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    matrix_artifacts: list[tuple[Path, dict[str, Any]]] = []
    invalid: list[dict[str, str]] = []
    seen_matrix_paths: set[Path] = set()
    seen_matrix_hashes: set[str] = set()
    for path in matrix_paths:
        path = path.expanduser().resolve()
        if path in seen_matrix_paths:
            continue
        try:
            content_hash = _sha256(path)
            if content_hash in seen_matrix_hashes:
                continue
            seen_matrix_paths.add(path)
            seen_matrix_hashes.add(content_hash)
            matrix_artifacts.append((path, _json(path)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append({"path": str(path), "error": str(exc)})

    full = [(path, artifact) for path, artifact in matrix_artifacts if _is_full_run(artifact)]
    measured = [(path, artifact) for path, artifact in full if _measured_run(artifact)]
    excluded = [
        {"path": str(path), "reason": reason}
        for path, artifact in full
        if (reason := _excluded_run_reason(artifact)) is not None
    ]
    successful = [(path, artifact) for path, artifact in measured if _successful_recovery(artifact)]
    cleanup_statuses = [
        status for _, artifact in measured for status in _cleanup_statuses(artifact)
    ]
    recovery_seconds = [float(artifact["measurements"]["recovery_duration_seconds"]) for _, artifact in successful]
    run_seconds = [float(artifact["measurements"]["run_duration_seconds"]) for _, artifact in successful]
    verdicts = Counter(str(artifact.get("verdict") or "unknown") for _, artifact in matrix_artifacts)
    auth_gap_runs = [
        {"path": str(path), "providers": _auth_gaps(artifact)}
        for path, artifact in measured
        if _auth_gaps(artifact)
    ]
    provider_failures = [
        {"path": str(path), "count": _startup_failure_counts(artifact)["provider_owned"]}
        for path, artifact in measured
        if _startup_failure_counts(artifact)["provider_owned"]
    ]
    startup_failure_totals = Counter()
    for _, artifact in measured:
        startup_failure_totals.update(_startup_failure_counts(artifact))

    history = []
    for path, artifact in measured:
        cleanup = _cleanup_statuses(artifact)
        measurements = artifact.get("measurements") or {}
        history.append(
            {
                "path": str(path),
                "generated_at": artifact.get("generated_at"),
                "verdict": artifact.get("verdict"),
                "recovery_qualification": artifact.get("recovery_qualification"),
                "retry_intents": {
                    "before": artifact.get("retry_intents_before_recovery"),
                    "after": artifact.get("retry_intents_after_recovery"),
                },
                "cleanup": {"pass": cleanup.count("pass"), "total": len(cleanup)},
                "auth_gaps": _auth_gaps(artifact),
                "startup_failures": _startup_failure_counts(artifact),
                "recovery_duration_seconds": measurements.get("recovery_duration_seconds"),
                "run_duration_seconds": measurements.get("run_duration_seconds"),
                "harness_repository_git_sha": (artifact.get("harness") or {}).get("repository_git_sha"),
                "implementation_source_git_sha": ((artifact.get("implementation") or {}).get("longhouse") or {}).get("source_git_sha"),
            }
        )

    harness_results: Counter[str] = Counter()
    harness_levels: Counter[str] = Counter()
    harness_operation_statuses: Counter[str] = Counter()
    blocked_operations: list[dict[str, str]] = []
    harness_inputs: list[str] = []
    seen_harness_paths: set[Path] = set()
    seen_harness_hashes: set[str] = set()
    for path in harness_paths:
        path = path.expanduser().resolve()
        if path in seen_harness_paths:
            continue
        try:
            content_hash = _sha256(path)
            if content_hash in seen_harness_hashes:
                continue
            seen_harness_paths.add(path)
            seen_harness_hashes.add(content_hash)
            artifact = _json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append({"path": str(path), "error": str(exc)})
            continue
        if not isinstance(artifact.get("results"), list):
            invalid.append({"path": str(path), "error": "provider harness artifact has no results list"})
            continue
        harness_inputs.append(str(path))
        for result in artifact.get("results") or []:
            if not isinstance(result, dict):
                continue
            status = str(result.get("status") or "unknown")
            harness_results[status] += 1
            operation_evidence = (result.get("data") or {}).get("operation_evidence", {})
            for operation, evidence in operation_evidence.items():
                if isinstance(evidence, dict):
                    operation_status = str(evidence.get("status") or "unknown")
                    harness_operation_statuses[operation_status] += 1
                    if operation_status == "blocked":
                        blocked_operations.append(
                            {
                                "provider": str(result.get("provider") or "unknown"),
                                "scenario": str(result.get("scenario") or "unknown"),
                                "operation": str(operation),
                                "failure_code": str(evidence.get("failure_code") or "unknown"),
                            }
                        )
                    level = str(evidence.get("level") or "unknown")
                    harness_levels[level] += 1

    report_generated_at = datetime.now(UTC)
    health_inputs, health_summary = _health_measurements(health_paths, invalid)
    invalid_before_dogfood = len(invalid)
    dogfood_inputs, dogfood_episodes = _load_dogfood_series(
        dogfood_paths,
        invalid,
        as_of=report_generated_at,
    )
    dogfood_summary = _dogfood_summary(dogfood_episodes, dogfood_inputs)
    dogfood_invalid = len(invalid) > invalid_before_dogfood
    dogfood_summary["input_status"] = (
        "invalid" if dogfood_invalid else "valid" if dogfood_inputs else "absent"
    )
    if dogfood_invalid:
        _invalidate_dogfood_summary(dogfood_summary)

    def observed_or_fallback(name: str, fallback: dict[str, Any]) -> dict[str, Any]:
        candidate = dogfood_summary[name]
        return (
            candidate
            if dogfood_summary["input_status"] == "valid" and candidate.get("status") == "observed"
            else fallback
        )

    controlled_false_red = health_summary["measurements"]["false_red_rate"]
    controlled_hidden_failure = health_summary["measurements"]["hidden_failure_rate"]
    controlled_action_coverage = health_summary["measurements"]["action_coverage"]

    report = {
        "schema_version": 2,
        "artifact_kind": "launch_reliability_measurements",
        "report_status": "invalid" if invalid else "ok",
        "generated_at": report_generated_at.isoformat().replace("+00:00", "Z"),
        "provenance": report_provenance(),
        "inputs": {
            "matrix_artifacts": [_input_reference(path) for path, _ in matrix_artifacts],
            "provider_harness_artifacts": [_input_reference(Path(path)) for path in harness_inputs],
            "health_artifacts": health_inputs,
            "dogfood_series_artifacts": dogfood_inputs,
            "invalid_artifacts": invalid,
        },
        "matrix": {
            "artifact_count": len(matrix_artifacts),
            "full_run_count": len(full),
            "measured_clean_run_count": len(measured),
            "successful_recovery_count": len(successful),
            "verdict_counts": dict(sorted(verdicts.items())),
            "retry_drain_rate": (len(successful) / len(measured)) if measured else None,
            "retry_drain_sample_count": len(measured),
            "retry_drain_denominator": "measured_clean_runs",
            "cleanup_pass_rate": (
                cleanup_statuses.count("pass") / len(cleanup_statuses)
                if cleanup_statuses
                else None
            ),
            "cleanup_pass_count": cleanup_statuses.count("pass"),
            "cleanup_scope_count": len(cleanup_statuses),
            "cleanup_rate_denominator": "measured_cleanup_scopes",
            "startup_failure_attribution": {
                "scope": "measured_clean_runs",
                "auth_precondition_runs": auth_gap_runs,
                "provider_owned_failure_runs": provider_failures,
                "totals": dict(sorted(startup_failure_totals.items())),
            },
            "recovery_duration_seconds": {
                "count": len(recovery_seconds),
                "min": min(recovery_seconds) if recovery_seconds else None,
                "median": statistics.median(recovery_seconds) if recovery_seconds else None,
                "max": max(recovery_seconds) if recovery_seconds else None,
            },
            "run_duration_seconds": {
                "count": len(run_seconds),
                "min": min(run_seconds) if run_seconds else None,
                "median": statistics.median(run_seconds) if run_seconds else None,
                "max": max(run_seconds) if run_seconds else None,
            },
            "history": history,
            "excluded_full_runs": excluded,
        },
        "provider_scenarios": {
            "result_status_counts": dict(sorted(harness_results.items())),
            "operation_status_counts": dict(sorted(harness_operation_statuses.items())),
            "blocked_operations": blocked_operations,
            "evidence_level_counts": dict(sorted(harness_levels.items())),
        },
        "health_fault_matrix": health_summary,
        "dogfood_series": dogfood_summary,
        "measures": {
            "automatic_recovery_time": observed_or_fallback(
                "automatic_recovery_time",
                {
                    "status": "observed",
                    "scope": "measured_clean_runs",
                    "sample_count": len(successful),
                    "source": [_input_reference(path) for path, _ in successful],
                    "seconds": {
                        "min": min(recovery_seconds) if recovery_seconds else None,
                        "median": statistics.median(recovery_seconds) if recovery_seconds else None,
                        "max": max(recovery_seconds) if recovery_seconds else None,
                    },
                }
                if successful
                else _not_observed("no measured matrix run with a positive retry queue converged to zero and complete cleanup")
            ),
            "false_red_rate": observed_or_fallback(
                "false_red_rate",
                controlled_false_red
                if controlled_false_red["status"] == "observed"
                else _not_observed(
                    controlled_false_red.get("reason")
                    or "retained artifacts do not include a user-action/data-risk ground-truth label"
                ),
            ),
            "hidden_failure_rate": observed_or_fallback(
                "hidden_failure_rate",
                controlled_hidden_failure
                if controlled_hidden_failure["status"] == "observed"
                else _not_observed(
                    controlled_hidden_failure.get("reason")
                    or "no longitudinal producer-freshness truth series was supplied"
                ),
            ),
            "unresolved_event_bearing_issue_age": observed_or_fallback(
                "unresolved_event_bearing_issue_age",
                _not_observed("no event-bearing issue lifecycle series was supplied"),
            ),
            "action_coverage": observed_or_fallback(
                "action_coverage",
                controlled_action_coverage
                if controlled_action_coverage["status"] == "observed"
                else _not_observed(
                    controlled_action_coverage.get("reason")
                    or "local action tests are separate evidence and were not passed as a coverage artifact"
                ),
            ),
            "duplicate_replayed_discarded_evidence": observed_or_fallback(
                "duplicate_replayed_discarded_evidence",
                _not_observed("matrix artifacts do not carry end-to-end evidence conservation counters"),
            ),
        },
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-root", action="append", type=Path, required=True, help="Matrix artifact file or directory; repeatable.")
    parser.add_argument("--provider-harness-artifact", action="append", type=Path, default=[], help="Universal provider harness JSON; repeatable.")
    parser.add_argument("--health-artifact", action="append", type=Path, default=[], help="Installed native health fault matrix JSON; repeatable.")
    parser.add_argument("--dogfood-series", action="append", type=Path, default=[], help="Retained longitudinal dogfood episode JSON; repeatable.")
    parser.add_argument("--output", type=Path, help="Write the report JSON to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        discover_matrix_artifacts(args.matrix_root),
        args.provider_harness_artifact,
        args.health_artifact,
        args.dogfood_series,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
        print(args.output)
    else:
        print(encoded, end="")
    return 0 if not report["inputs"]["invalid_artifacts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
