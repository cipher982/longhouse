#!/usr/bin/env python3
"""Collect explicit, fail-closed dogfood episodes for launch reliability.

This collector samples the installed native ``local-health`` surface. It does
not infer expected truth from the observation: callers must provide the
expected health state, producer freshness, red eligibility, and optional
action. A dirty repository can be sampled for diagnosis, but its artifact is
marked dirty and the measurement report will reject it as qualification
evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

HEALTH_STATES = frozenset({"healthy", "degraded", "broken", "unknown"})
FRESHNESS_STATES = frozenset({"fresh", "stale", "unknown"})
CONSERVATION_FIELDS = (
    "source_events",
    "archived_events",
    "replayed_events",
    "duplicate_events",
    "discarded_events",
    "unresolved_events",
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty RFC3339 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def provenance(root: Path) -> dict[str, Any]:
    script = Path(__file__).resolve()
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    try:
        relative = script.relative_to(root)
    except ValueError:
        # Unit tests may point the collector at a temporary fixture repository.
        # The real collector always runs with its script inside the repository.
        script_status = ""
    else:
        script_status = _git(root, "status", "--porcelain", "--", str(relative))
    return {
        "generator": "scripts/qa/launch_reliability_dogfood.py",
        "generator_sha256": _sha256(script),
        "git_sha": _git(root, "rev-parse", "HEAD"),
        "repository": str(root),
        "repository_dirty": bool(status),
        "generator_dirty": bool(script_status),
    }


def _freshness(snapshot: dict[str, Any]) -> str:
    engine = snapshot.get("engine_status")
    if not isinstance(engine, dict) or engine.get("exists") is not True:
        return "unknown"
    fresh = engine.get("fresh")
    if fresh is True:
        return "fresh"
    if fresh is False:
        return "stale"
    return "unknown"


def _health_state(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("health_state")
    return (
        value
        if isinstance(value, str) and value in HEALTH_STATES - {"unknown"}
        else "unknown"
    )


def _action_ids(snapshot: dict[str, Any]) -> list[str]:
    actions = snapshot.get("suggested_action_ids")
    if not isinstance(actions, list):
        return []
    return [item for item in actions if isinstance(item, str) and item]


def collect_snapshot(
    longhouse_bin: Path,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str | None]:
    command = [str(longhouse_bin), "local-health", "--fast", "--json"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, f"local-health command failed: {type(exc).__name__}: {exc}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        detail = (result.stderr or result.stdout or "no output").strip()[-500:]
        return {}, f"local-health returned invalid JSON: {exc}; output={detail!r}"
    if not isinstance(payload, dict):
        return {}, "local-health returned a non-object JSON value"
    if result.returncode != 0:
        detail = (result.stderr or "non-zero exit").strip()[-500:]
        return payload, f"local-health exited {result.returncode}: {detail}"
    return payload, None


def _conservation(path: Path | None) -> dict[str, int] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("evidence conservation input must be a JSON object")
    result: dict[str, int] = {}
    for field in CONSERVATION_FIELDS:
        count = value.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(
                f"evidence conservation {field} must be a non-negative integer"
            )
        result[field] = count
    accounted = sum(
        result[field]
        for field in ("archived_events", "discarded_events", "unresolved_events")
    )
    if accounted > result["source_events"]:
        raise ValueError(
            "evidence conservation accounts for more events than source_events"
        )
    for field in ("replayed_events", "duplicate_events"):
        if result[field] > result["source_events"]:
            raise ValueError(f"evidence conservation {field} exceeds source_events")
    return result


def _issue(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.issue_status is None:
        return None
    if args.issue_opened_at is None:
        raise ValueError("--issue-opened-at is required with --issue-status")
    opened = _parse_timestamp(args.issue_opened_at)
    issue: dict[str, Any] = {"opened_at": opened, "status": args.issue_status}
    if args.issue_status == "resolved":
        if args.issue_resolved_at is None:
            raise ValueError("--issue-resolved-at is required for a resolved issue")
        issue["resolved_at"] = _parse_timestamp(args.issue_resolved_at)
    elif args.issue_resolved_at is not None:
        raise ValueError("--issue-resolved-at is only valid for a resolved issue")
    return issue


def collect(args: argparse.Namespace) -> dict[str, Any]:
    root = (args.repo_root or _repository_root()).resolve()
    longhouse_bin = args.longhouse_bin.resolve()
    if not longhouse_bin.is_file():
        raise ValueError(f"longhouse binary does not exist: {longhouse_bin}")
    if args.sample_count < 1:
        raise ValueError("--sample-count must be at least 1")
    if args.interval_seconds < 0:
        raise ValueError("--interval-seconds must be non-negative")
    if args.recovery_duration_seconds is not None and (
        args.recovery_duration_seconds < 0
        or not math.isfinite(args.recovery_duration_seconds)
    ):
        raise ValueError("--recovery-duration-seconds must be finite and non-negative")
    expected = {
        "action": args.expected_action,
        "health_state": args.expected_health_state,
        "producer_freshness": args.expected_producer_freshness,
        "red_eligible": args.expected_red_eligible,
    }
    issue = _issue(args)
    conservation = _conservation(args.evidence_conservation_json)
    episodes: list[dict[str, Any]] = []
    prefix = args.episode_id or datetime.now(UTC).strftime("dogfood-%Y%m%dT%H%M%SZ")
    for index in range(args.sample_count):
        if index:
            time.sleep(args.interval_seconds)
        snapshot, error = collect_snapshot(
            longhouse_bin,
            timeout_seconds=args.timeout_seconds,
        )
        observed_at = (
            _parse_timestamp(snapshot.get("collected_at"))
            if snapshot.get("collected_at")
            else _utc_now()
        )
        observed: dict[str, Any] = {
            "health_state": _health_state(snapshot),
            "producer_freshness": _freshness(snapshot),
            "suggested_action_ids": _action_ids(snapshot),
        }
        if isinstance(snapshot.get("severity"), str):
            observed["severity"] = snapshot["severity"]
        if isinstance(snapshot.get("reasons"), list):
            observed["reasons"] = [
                item for item in snapshot["reasons"] if isinstance(item, str)
            ]
        episode: dict[str, Any] = {
            "episode_id": prefix if args.sample_count == 1 else f"{prefix}-{index + 1}",
            "expected": expected,
            "observed": observed,
            "observed_at": observed_at,
        }
        if args.recovery_duration_seconds is not None:
            episode["recovery_duration_seconds"] = args.recovery_duration_seconds
        if issue is not None:
            episode["event_bearing_issue"] = issue
        if conservation is not None:
            episode["evidence_conservation"] = conservation
        if error is not None:
            episode["observation_error"] = error
        episodes.append(episode)
    return {
        "artifact_kind": "launch_reliability_dogfood_series",
        "generated_at": _utc_now(),
        "provenance": provenance(root),
        "schema_version": 1,
        "episodes": episodes,
    }


def parser() -> argparse.ArgumentParser:
    root = _repository_root()
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--longhouse-bin", type=Path, default=Path("longhouse"))
    command.add_argument("--repo-root", type=Path, default=root)
    command.add_argument("--episode-id")
    command.add_argument("--sample-count", type=int, default=1)
    command.add_argument("--interval-seconds", type=float, default=0.0)
    command.add_argument("--timeout-seconds", type=float, default=20.0)
    command.add_argument(
        "--expected-health-state",
        choices=sorted(HEALTH_STATES - {"unknown"}),
        required=True,
    )
    command.add_argument(
        "--expected-producer-freshness", choices=sorted(FRESHNESS_STATES), required=True
    )
    red = command.add_mutually_exclusive_group(required=True)
    red.add_argument(
        "--expected-red-eligible", dest="expected_red_eligible", action="store_true"
    )
    red.add_argument(
        "--expected-not-red-eligible",
        dest="expected_red_eligible",
        action="store_false",
    )
    command.add_argument("--expected-action")
    command.add_argument("--recovery-duration-seconds", type=float)
    command.add_argument("--issue-status", choices=("resolved", "unresolved"))
    command.add_argument("--issue-opened-at")
    command.add_argument("--issue-resolved-at")
    command.add_argument("--evidence-conservation-json", type=Path)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        artifact = collect(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"dogfood collector: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
