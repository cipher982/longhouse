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
import base64
import hashlib
import hmac
import json
import math
import shutil
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from datetime import timedelta
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
CHALLENGE_ARTIFACT_KIND = "launch_reliability_dogfood_challenge"
CHALLENGE_SCHEMA_VERSION = 1
CHALLENGE_ISSUER = "scripts/qa/launch-reliability-measurements.py"
LOCAL_HEALTH_SCHEMA_VERSION = 1


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


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
        "generator_path": str(script),
        "generator_sha256": _sha256(script),
        "git_sha": _git(root, "rev-parse", "HEAD"),
        "repository": str(root),
        "repository_dirty": bool(status),
        "generator_dirty": bool(script_status),
    }


def _binary_identity(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        result = subprocess.run(
            [str(path), "build-identity", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"longhouse build identity failed: {type(exc).__name__}: {exc}"
    raw_identity = (result.stdout or "").strip()
    if result.returncode != 0 or not raw_identity:
        detail = (result.stderr or result.stdout or "no output").strip()[-500:]
        return None, f"longhouse build identity exited {result.returncode}: {detail}"
    try:
        identity = json.loads(raw_identity)
    except json.JSONDecodeError as exc:
        return None, f"longhouse build identity returned invalid JSON: {exc}"
    if not isinstance(identity, dict):
        return None, "longhouse build identity returned a non-object JSON value"
    facade = identity.get("facade")
    engine = identity.get("engine")
    if not isinstance(facade, dict) or not isinstance(engine, dict):
        return None, "longhouse build identity is missing facade or engine identity"
    for label, value in (("facade", facade), ("engine", engine)):
        commit = value.get("commit")
        if (
            not isinstance(commit, str)
            or len(commit) != 40
            or any(character not in "0123456789abcdefABCDEF" for character in commit)
        ):
            return None, f"longhouse {label} build identity has an invalid commit"
        if not isinstance(value.get("commit_short"), str) or not value["commit_short"]:
            return None, f"longhouse {label} build identity has no commit_short"
        if not isinstance(value.get("dirty"), bool):
            return None, f"longhouse {label} build identity has invalid dirty flag"
    if facade["commit"] != engine["commit"]:
        return None, "longhouse facade and engine build identities disagree"
    engine_path_value = identity.get("engine_path")
    if not isinstance(engine_path_value, str) or not engine_path_value:
        return None, "longhouse build identity has no engine_path"
    engine_path = Path(engine_path_value).expanduser().resolve()
    if not engine_path.is_file():
        return None, "paired longhouse-engine does not exist"
    identity["facade_path"] = str(path)
    identity["engine_path"] = str(engine_path)
    return {
        "build_identity": identity,
        "engine_path": str(engine_path),
        "engine_sha256": _sha256(engine_path),
        "path": str(path),
        "sha256": _sha256(path),
    }, None


def _load_challenge(path: Path, *, root: Path) -> tuple[dict[str, Any], bytes]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("artifact_kind") != CHALLENGE_ARTIFACT_KIND
    ):
        raise ValueError("dogfood challenge has the wrong artifact kind")
    if payload.get("issuer") != CHALLENGE_ISSUER:
        raise ValueError("dogfood challenge was not issued by the measurement reporter")
    if payload.get("schema_version") != CHALLENGE_SCHEMA_VERSION:
        raise ValueError("dogfood challenge has the wrong schema version")
    if not isinstance(payload.get("challenge_id"), str) or not payload["challenge_id"]:
        raise ValueError("dogfood challenge has no challenge_id")
    if not isinstance(payload.get("nonce"), str) or not payload["nonce"]:
        raise ValueError("dogfood challenge has no nonce")
    created_at = _parse_timestamp(payload.get("created_at"))
    expires_at = _parse_timestamp(payload.get("expires_at"))
    if expires_at <= created_at:
        raise ValueError("dogfood challenge expires_at must follow created_at")
    created_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    expires_time = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if expires_time - created_time > timedelta(hours=24):
        raise ValueError("dogfood challenge window must not exceed 24 hours")
    try:
        key = base64.urlsafe_b64decode(str(payload.get("key") or "").encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("dogfood challenge key is not base64") from exc
    if len(key) < 32:
        raise ValueError("dogfood challenge key is too short")
    challenge_provenance = payload.get("provenance")
    if not isinstance(challenge_provenance, dict):
        raise ValueError("dogfood challenge has no provenance")
    current = provenance(root)
    for field in ("git_sha", "generator_sha256"):
        if challenge_provenance.get(field) != current.get(field):
            raise ValueError(f"dogfood challenge {field} does not match the collector")
    if (
        challenge_provenance.get("repository_dirty") is not False
        or challenge_provenance.get("generator_dirty") is not False
    ):
        raise ValueError("dogfood challenge provenance is dirty")
    return payload, key


def _sign_artifact(artifact: dict[str, Any], key: bytes) -> str:
    body = dict(artifact)
    body.pop("signature", None)
    return hmac.new(key, _canonical_json(body), hashlib.sha256).hexdigest()


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
    if payload.get("schema_version") != LOCAL_HEALTH_SCHEMA_VERSION:
        return payload, "local-health payload has an unsupported schema_version"
    missing = [
        field for field in ("health_state", "engine_status") if field not in payload
    ]
    if missing:
        return (
            payload,
            f"local-health payload is missing required fields: {', '.join(missing)}",
        )
    if not isinstance(payload.get("health_state"), str) or payload[
        "health_state"
    ] not in HEALTH_STATES - {"unknown"}:
        return payload, "local-health payload has an invalid health_state"
    engine_status = payload.get("engine_status")
    if not isinstance(engine_status, dict):
        return payload, "local-health payload has an invalid engine_status"
    if not isinstance(engine_status.get("exists"), bool) or not isinstance(
        engine_status.get("fresh"), bool
    ):
        return payload, "local-health payload has invalid engine_status booleans"
    action_ids = payload.get("suggested_action_ids")
    if action_ids is not None and (
        not isinstance(action_ids, list)
        or not all(isinstance(item, str) and item for item in action_ids)
    ):
        return payload, "local-health payload has invalid suggested_action_ids"
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


def _resolve_binary(value: Path) -> Path:
    raw = str(value)
    if "/" not in raw:
        resolved = shutil.which(raw)
        if resolved is None:
            raise ValueError(f"longhouse binary is not on PATH: {raw}")
        return Path(resolved).resolve()
    return value.expanduser().resolve()


def collect(args: argparse.Namespace) -> dict[str, Any]:
    root = (getattr(args, "repo_root", None) or _repository_root()).resolve()
    longhouse_bin = _resolve_binary(args.longhouse_bin)
    if not longhouse_bin.is_file():
        raise ValueError(f"longhouse binary does not exist: {longhouse_bin}")
    if not args.episode_id:
        raise ValueError("--episode-id is required")
    if args.expected_health_state not in HEALTH_STATES - {"unknown"}:
        raise ValueError("--expected-health-state is required and must be known")
    if args.expected_producer_freshness not in FRESHNESS_STATES:
        raise ValueError("--expected-producer-freshness is required")
    if args.expected_red_eligible is None:
        raise ValueError("one expected red-eligibility flag is required")
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
    challenge_payload = None
    challenge_key = None
    if args.challenge is not None:
        challenge_payload, challenge_key = _load_challenge(args.challenge, root=root)
    binary_info, binary_error = _binary_identity(longhouse_bin)
    artifact_provenance = provenance(root)
    if binary_info is not None:
        artifact_provenance["sampled_binary"] = binary_info
    else:
        artifact_provenance["sampled_binary_error"] = binary_error
    issue = _issue(args)
    conservation = _conservation(args.evidence_conservation_json)
    snapshot_started_at = _utc_now()
    snapshot, error = collect_snapshot(
        longhouse_bin,
        timeout_seconds=args.timeout_seconds,
    )
    # The provider-reported collected_at can be cached or malformed. The
    # collector's wall clock is the authoritative observation time used for
    # the longitudinal window; retain the provider value only as context.
    observed_at = _utc_now()
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
    if isinstance(snapshot.get("collected_at"), str):
        observed["producer_collected_at"] = snapshot["collected_at"]
    episode: dict[str, Any] = {
        "episode_id": args.episode_id,
        "expected": expected,
        "observed": observed,
        "observed_at": observed_at,
        "collector_started_at": snapshot_started_at,
    }
    if args.recovery_duration_seconds is not None:
        episode["recovery_duration_seconds"] = args.recovery_duration_seconds
    if issue is not None:
        episode["event_bearing_issue"] = issue
    if conservation is not None:
        episode["evidence_conservation"] = conservation
    challenge_time_error = None
    if challenge_payload is not None:
        observed_time = _parse_timestamp(observed_at)
        created_time = _parse_timestamp(challenge_payload["created_at"])
        expires_time = _parse_timestamp(challenge_payload["expires_at"])
        if observed_time < created_time:
            challenge_time_error = "observation predates dogfood challenge creation"
        elif observed_time > expires_time:
            challenge_time_error = "observation is after dogfood challenge expiry"
    observation_errors = [
        detail for detail in (binary_error, error, challenge_time_error) if detail
    ]
    if observation_errors:
        # The measurement consumer rejects any episode carrying this field.
        # Keeping the diagnostic artifact makes the failed observation
        # inspectable without allowing it into a denominator.
        episode["observation_error"] = "; ".join(observation_errors)
    artifact: dict[str, Any] = {
        "artifact_kind": "launch_reliability_dogfood_series",
        "generated_at": _utc_now(),
        "provenance": artifact_provenance,
        "schema_version": 1,
        "episodes": [episode],
    }
    if challenge_payload is not None and challenge_key is not None:
        artifact["challenge"] = {
            "challenge_id": challenge_payload["challenge_id"],
            "nonce": challenge_payload["nonce"],
        }
        artifact["signature"] = _sign_artifact(artifact, challenge_key)
    return artifact


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--output", type=Path)
    command.add_argument("--challenge", type=Path)
    command.add_argument("--longhouse-bin", type=Path, default=Path("longhouse"))
    command.add_argument("--episode-id")
    command.add_argument("--timeout-seconds", type=float, default=20.0)
    command.add_argument(
        "--expected-health-state",
        choices=sorted(HEALTH_STATES - {"unknown"}),
    )
    command.add_argument(
        "--expected-producer-freshness", choices=sorted(FRESHNESS_STATES)
    )
    red = command.add_mutually_exclusive_group()
    red.add_argument(
        "--expected-red-eligible",
        dest="expected_red_eligible",
        action="store_true",
        default=None,
    )
    red.add_argument(
        "--expected-not-red-eligible",
        dest="expected_red_eligible",
        action="store_false",
        default=None,
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
        if args.output is None:
            raise ValueError("--output is required when collecting an episode")
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
