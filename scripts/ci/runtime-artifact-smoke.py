#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import ensure_runtime_artifact

COMPONENT_CHOICES = [
    RuntimeComponent.ENGINE.value,
    RuntimeComponent.DESKTOP_APP.value,
    RuntimeComponent.DESKTOP_WINDOW.value,
    "local-health-app",
    "local-health-window",
]


def _commit_matches(actual: str, expected: str) -> bool:
    return bool(actual and expected and actual == expected)


def _load_engine_identity(launch_path: str) -> dict[str, Any]:
    proc = subprocess.run(
        [launch_path, "build-identity", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"{launch_path} build-identity --json failed: {detail}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{launch_path} build-identity --json did not emit JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{launch_path} build-identity --json emitted a non-object payload")
    return payload


def _load_desktop_app_identity(app_path: str) -> dict[str, Any]:
    identity_path = Path(app_path) / "Contents" / "Resources" / "build-identity.json"
    if not identity_path.exists():
        raise RuntimeError(f"desktop app build identity missing: {identity_path}")
    try:
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"desktop app build identity is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"desktop app build identity emitted a non-object payload")
    return payload


def _load_runtime_identity(component: RuntimeComponent, path: str, launch_path: str) -> dict[str, Any]:
    if component == RuntimeComponent.ENGINE:
        return _load_engine_identity(launch_path)
    if component == RuntimeComponent.DESKTOP_APP:
        return _load_desktop_app_identity(path)
    raise RuntimeError(f"{component.value} does not expose a build identity")


def _assert_runtime_identity(
    component: RuntimeComponent,
    path: str,
    launch_path: str,
    *,
    expected_commit: str,
    expected_version: str,
) -> dict[str, Any]:
    identity = _load_runtime_identity(component, path, launch_path)
    actual_commit = str(identity.get("commit") or "")
    actual_version = str(identity.get("version") or "")
    errors: list[str] = []
    if not _commit_matches(actual_commit, expected_commit):
        errors.append(f"commit mismatch: expected {expected_commit}, got {actual_commit or '<missing>'}")
    if expected_version and actual_version != expected_version:
        errors.append(f"version mismatch: expected {expected_version}, got {actual_version or '<missing>'}")
    if errors:
        details = "\n".join(f"  - {error}" for error in errors)
        raise RuntimeError(
            f"{component.value} runtime artifact build identity mismatch:\n"
            f"{details}\n"
            f"identity: {json.dumps(identity, sort_keys=True)}"
        )
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or verify a Longhouse runtime artifact.")
    parser.add_argument("--component", required=True, choices=COMPONENT_CHOICES)
    parser.add_argument("--overwrite", action="store_true", help="Force a reinstall even if the artifact already exists.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--expected-build-commit", help="Expected full git commit SHA embedded in the runtime artifact.")
    parser.add_argument(
        "--expected-build-version",
        help="Expected release version embedded in the runtime artifact, without a leading v.",
    )
    args = parser.parse_args()

    component = RuntimeComponent(args.component)
    artifact = ensure_runtime_artifact(component, overwrite=args.overwrite)
    payload = asdict(artifact)
    payload["component"] = artifact.component.value
    payload["kind"] = artifact.kind.value
    expected_commit = (args.expected_build_commit or "").strip()
    expected_version = (args.expected_build_version or "").strip().removeprefix("v")
    if expected_commit:
        try:
            payload["build_identity"] = _assert_runtime_identity(
                component,
                artifact.path,
                artifact.launch_path,
                expected_commit=expected_commit,
                expected_version=expected_version,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{artifact.component.value}: {artifact.path}")
        print(f"  launch: {artifact.launch_path}")
        print(f"  source: {artifact.source}")
        print(f"  installed_now: {'yes' if artifact.installed_now else 'no'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
