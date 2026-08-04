#!/usr/bin/env python3
"""Exercise installed native local-health against bounded fault fixtures.

This qualification lane runs the built native ``longhouse`` facade, not the
Python producer. Each case uses a disposable state root and checks that a
known local fault is represented with the expected severity, reason, and
scoped action. The fixtures cover faults the native fast surface can observe;
provider-owned prompts, sleep/wake, and disk exhaustion remain separate live
qualification concerns.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


CASES = (
    "missing_engine_status",
    "unreadable_engine_status",
    "permission_denied_engine_status",
    "stale_projection",
    "safe_source_conflict",
    "unresolved_source_conflict",
    "payload_rejected",
    "managed_launch_recovery_active",
    "managed_launch_recovery_exhausted",
    "managed_launch_recovery_resolved",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_binary(raw: str | None) -> Path:
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        resolved = shutil.which("longhouse")
        if resolved is None:
            raise RuntimeError("longhouse was not found on PATH")
        path = Path(resolved).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"native longhouse facade is not runnable: {path}")
    return path


def version_probe(binary: Path) -> dict[str, Any]:
    result = subprocess.run(
        [str(binary), "build-identity"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return {
        "path": str(binary),
        "sha256": sha256_file(binary),
        "returncode": result.returncode,
        "output": (result.stdout or result.stderr or "").strip(),
    }


def status_path(root: Path) -> Path:
    return root / "agent" / "engine-status.json"


def write_status(root: Path, payload: dict[str, Any]) -> Path:
    path = status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def current_status(**extra: Any) -> dict[str, Any]:
    now = rfc3339(utc_now())
    payload: dict[str, Any] = {
        "last_updated": now,
        "spool_pending_count": 0,
        "spool_dead_count": 0,
        "is_offline": False,
        "local_projection": {
            "generated_at": now,
            "engine_pulse_at": now,
            "reconciliation": {"state": "idle"},
        },
    }
    payload.update(extra)
    return payload


def retry_fixture(root: Path, *, exhausted: bool) -> Path:
    path = root / "agent" / "managed-local" / "registration-retries" / "fixture.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider_name": "FixtureProvider",
                "url": "http://127.0.0.1:43123",
                "token": "fixture-token-redacted",
                "payload": {
                    "session_id": "11111111-1111-4111-8111-111111111111",
                    "provider": "fixture",
                },
                "expected_session_id": "11111111-1111-4111-8111-111111111111",
                "expected_transport": "fixture_transport",
                "provider_ready": True,
                "attempts": 0,
                "next_attempt_at": None,
                "last_error": None,
                "created_at": rfc3339(utc_now()),
                "recovery_exhausted": exhausted,
                "exhausted_at": rfc3339(utc_now()) if exhausted else None,
                "provider_pid": 12345,
                "provider_process_start_time": "fixture-provider-start",
                "launcher_pid": 12346,
                "launcher_process_start_time": "fixture-launcher-start",
                "provider_exited": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def invoke(binary: Path, root: Path) -> dict[str, Any]:
    profile_root = root / "provider-profile"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(profile_root / "home"),
            "XDG_CONFIG_HOME": str(profile_root / "config"),
            "XDG_DATA_HOME": str(profile_root / "data"),
            "XDG_STATE_HOME": str(profile_root / "state"),
            "XDG_CACHE_HOME": str(profile_root / "cache"),
            "CODEX_HOME": str(profile_root / "codex"),
        }
    )
    result = subprocess.run(
        [str(binary), "local-health", "--fast", "--json", "--state-root", str(root)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"native local-health emitted invalid JSON (rc={result.returncode}): "
            f"{result.stdout!r} {result.stderr!r}"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError("native local-health payload was not an object")
    return {"returncode": result.returncode, "payload": payload}


def assert_health(
    observed: dict[str, Any],
    *,
    state: str,
    reason: str,
    action: str,
) -> None:
    payload = observed["payload"]
    reasons = payload.get("reasons")
    actions = payload.get("suggested_action_ids")
    if payload.get("health_state") != state:
        raise AssertionError(
            f"expected health_state={state}, got {payload.get('health_state')!r}"
        )
    if not isinstance(reasons, list) or reason not in reasons:
        raise AssertionError(f"expected reason {reason!r}, got {reasons!r}")
    if not isinstance(actions, list) or action not in actions:
        raise AssertionError(f"expected action {action!r}, got {actions!r}")


def run_case(case: str, binary: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="lh-health-fault.", dir="/tmp") as raw_root:
        root = Path(raw_root)
        expected: dict[str, str]

        if case == "missing_engine_status":
            expected = {
                "state": "broken",
                "reason": "engine_status_missing",
                "action": "inspect_local_health",
            }
        elif case == "unreadable_engine_status":
            path = status_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not-json\n", encoding="utf-8")
            expected = {
                "state": "broken",
                "reason": "engine_status_unreadable",
                "action": "inspect_local_health",
            }
        elif case == "permission_denied_engine_status":
            path = write_status(root, current_status())
            path.chmod(0)
            try:
                observed = invoke(binary, root)
                assert_health(
                    observed,
                    **{
                        "state": "broken",
                        "reason": "engine_status_unreadable",
                        "action": "inspect_local_health",
                    },
                )
                return {
                    "case": case,
                    "expected": {
                        "state": "broken",
                        "reason": "engine_status_unreadable",
                        "action": "inspect_local_health",
                    },
                    "observed": observed["payload"],
                }
            finally:
                path.chmod(0o600)
        elif case == "stale_projection":
            stale = rfc3339(utc_now() - timedelta(seconds=180))
            write_status(
                root,
                current_status(
                    local_projection={
                        "generated_at": stale,
                        "engine_pulse_at": stale,
                        "reconciliation": {"state": "idle"},
                    }
                ),
            )
            expected = {
                "state": "degraded",
                "reason": "engine_status_stale",
                "action": "inspect_local_health",
            }
        elif case == "safe_source_conflict":
            write_status(
                root,
                current_status(
                    storage_v2_outbox={
                        "blocked_source_count": 2,
                        "reconciling_blocked_source_count": 2,
                        "unresolved_blocked_source_count": 0,
                        "latest_block_kind": "source_epoch_conflict",
                    }
                ),
            )
            expected = {
                "state": "degraded",
                "reason": "storage_v2_sources_blocked",
                "action": "inspect_storage_source",
            }
        elif case == "unresolved_source_conflict":
            write_status(
                root,
                current_status(
                    storage_v2_outbox={
                        "blocked_source_count": 1,
                        "unresolved_blocked_source_count": 1,
                        "latest_block_kind": "source_epoch_conflict_unresolved",
                        "latest_block_source_epoch": "abcdefab-cdef-abcd-efab-cdefabcdefab",
                        "latest_unresolved_block_source_epoch": "abcdefab-cdef-abcd-efab-cdefabcdefab",
                    }
                ),
            )
            expected = {
                "state": "broken",
                "reason": "storage_v2_sources_unresolved",
                "action": "inspect_storage_source",
            }
        elif case == "payload_rejected":
            write_status(root, current_status(ship_payload_rejections_1h=1))
            expected = {
                "state": "broken",
                "reason": "payload_rejected",
                "action": "inspect_shipping",
            }
        elif case in {
            "managed_launch_recovery_active",
            "managed_launch_recovery_exhausted",
        }:
            write_status(root, current_status())
            retry_fixture(root, exhausted=case.endswith("exhausted"))
            expected = {
                "state": "broken" if case.endswith("exhausted") else "degraded",
                "reason": "managed_launch_recovery_exhausted"
                if case.endswith("exhausted")
                else "managed_launch_recovery_active",
                "action": "inspect_managed_session",
            }
        elif case == "managed_launch_recovery_resolved":
            write_status(root, current_status())
            retry_path = retry_fixture(root, exhausted=False)
            active = invoke(binary, root)
            assert_health(
                active,
                state="degraded",
                reason="managed_launch_recovery_active",
                action="inspect_managed_session",
            )
            retry_path.unlink()
            resolved = invoke(binary, root)
            payload = resolved["payload"]
            if (
                payload.get("health_state") != "healthy"
                or payload.get("managed_launch_recovery", {}).get("active_count") != 0
            ):
                raise AssertionError(f"managed recovery did not clear: {payload!r}")
            return {
                "case": case,
                "expected": {
                    "state": "healthy",
                    "reason": "resolved",
                    "action": "none",
                },
                "observed": payload,
            }
        else:
            raise ValueError(f"unsupported case: {case}")

        observed = invoke(binary, root)
        assert_health(observed, **expected)
        if case == "unresolved_source_conflict":
            expected_action = (
                "Inspect retained source evidence with longhouse shipping inspect "
                "--source-epoch abcdefab-cdef-abcd-efab-cdefabcdefab --json "
                "before retrying or discarding it."
            )
            suggested_actions = observed["payload"].get("suggested_actions")
            if suggested_actions != [expected_action]:
                raise AssertionError(
                    "expected the installed unresolved-source action to preserve the "
                    f"source epoch, got {suggested_actions!r}"
                )
            if observed["payload"].get("headline") != "Longhouse has unresolved durable source evidence":
                raise AssertionError(
                    "expected the installed unresolved-source headline to be precise, "
                    f"got {observed['payload'].get('headline')!r}"
                )
        return {"case": case, "expected": expected, "observed": observed["payload"]}


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    binary = resolve_binary(args.binary or os.environ.get("LONGHOUSE_HEALTH_BIN"))
    selected = tuple(args.case or CASES)
    results = [run_case(case, binary) for case in selected]
    return {
        "schema_version": 1,
        "artifact_kind": "installed_native_health_fault_matrix",
        "generated_at": rfc3339(utc_now()),
        "verdict": "green",
        "implementation": version_probe(binary),
        "scenarios": [result["case"] for result in results],
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    root = (
        args.evidence_root
        or Path(__file__).resolve().parents[2]
        / ".build/canaries/installed-native-health-fault-matrix"
        / stamp
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        artifact = run_matrix(args)
    except Exception as error:  # noqa: BLE001 - emit durable failure evidence.
        artifact = {
            "schema_version": 1,
            "artifact_kind": "installed_native_health_fault_matrix",
            "generated_at": rfc3339(utc_now()),
            "verdict": "red",
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(),
        }
    path = root / "installed-native-health-fault-matrix.json"
    path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"verdict: {artifact['verdict']}")
        print(f"artifact: {path}")
        if artifact["verdict"] != "green":
            print(
                artifact.get("error", "installed native health fault matrix failed"),
                file=sys.stderr,
            )
    return 0 if artifact["verdict"] == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
