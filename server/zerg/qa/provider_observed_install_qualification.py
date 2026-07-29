"""Qualify an explicit observed provider installation through the universal matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa.provider_adapters.cursor import CURSOR_GATE0_ARTIFACT_ENV
from zerg.qa.provider_factory_model import DEFAULT_HARNESS_SCENARIOS
from zerg.qa.provider_harness_qualification import _full_column_gate
from zerg.qa.provider_semantic_qualification import temporary_environment
from zerg.qa.universal_agent_harness import HarnessOptions
from zerg.qa.universal_agent_harness import run_harness

_IGNORED_CURSOR_ROOTS = frozenset({".running"})


class ObservedInstallError(ValueError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def observed_closure(root: Path) -> tuple[str, int]:
    """Hash Cursor's immutable install tree while excluding its PID leases."""

    resolved_root = root.expanduser().resolve(strict=True)
    if not resolved_root.is_dir():
        raise ObservedInstallError(f"provider root is not a directory: {resolved_root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(resolved_root.rglob("*"), key=lambda item: item.relative_to(resolved_root).as_posix()):
        relative = path.relative_to(resolved_root)
        if relative.parts and relative.parts[0] in _IGNORED_CURSOR_ROOTS:
            continue
        if path.is_symlink():
            entries.append({"path": relative.as_posix(), "kind": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            entries.append(
                {
                    "path": relative.as_posix(),
                    "kind": "file",
                    "executable": bool(path.stat().st_mode & 0o111),
                    "sha256": _sha256_file(path),
                }
            )
    if not entries:
        raise ObservedInstallError(f"provider root has no immutable files: {resolved_root}")
    manifest = {
        "closure_manifest_version": 1,
        "closure_granularity": "observed_install_tree",
        "ignored_top_level_paths": sorted(_IGNORED_CURSOR_ROOTS),
        "entries": entries,
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), len(entries)


def qualify_cursor_observed_install(
    *,
    provider_bin: Path,
    provider_root: Path,
    gate0_artifact: Path,
    expected_version: str,
    output_root: Path,
) -> dict[str, Any]:
    binary = provider_bin.expanduser().resolve(strict=True)
    root = provider_root.expanduser().resolve(strict=True)
    gate0 = gate0_artifact.expanduser().resolve(strict=True)
    try:
        binary.relative_to(root)
    except ValueError as exc:
        raise ObservedInstallError("provider binary must be contained by the explicit provider root") from exc
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ObservedInstallError(f"provider binary is not executable: {binary}")
    if not gate0.is_file():
        raise ObservedInstallError(f"Cursor Gate 0 artifact does not exist: {gate0}")
    normalized_version = expected_version.strip()
    if not normalized_version:
        raise ObservedInstallError("expected_version is required")

    output = output_root.expanduser()
    output.mkdir(parents=True, exist_ok=True)
    pre_executable_identity = _sha256_file(binary)
    pre_closure_digest, entry_count = observed_closure(root)
    with temporary_environment({CURSOR_GATE0_ARTIFACT_ENV: str(gate0)}):
        harness = run_harness(
            HarnessOptions(
                providers=("cursor",),
                scenarios=DEFAULT_HARNESS_SCENARIOS,
                evidence_root=output / "harness-evidence",
                provider_bins={"cursor": binary},
            )
        )
    post_executable_identity = _sha256_file(binary)
    post_closure_digest, post_entry_count = observed_closure(root)
    gate = _full_column_gate(harness, provider="cursor")
    probe_rows = [row for row in harness.get("results", []) if row.get("provider") == "cursor" and row.get("scenario") == "probe_identity"]
    reported_version = ""
    if len(probe_rows) == 1:
        reported_version = str((probe_rows[0].get("data") or {}).get("version") or "").strip()
    passed = all(
        (
            gate.get("status") == "pass",
            reported_version == normalized_version,
            pre_executable_identity == post_executable_identity,
            pre_closure_digest == post_closure_digest,
            entry_count == post_entry_count,
        )
    )
    artifact = {
        "schema_version": 1,
        "artifact_kind": "provider_observed_install_qualification",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "provider": "cursor",
        "status": "pass" if passed else "fail",
        "failure_code": None if passed else "cursor_observed_install_regression",
        "build_provenance": "observed_install",
        "provider_bin": str(binary),
        "provider_root": str(root),
        "expected_provider_version": normalized_version,
        "reported_provider_version": reported_version,
        "pre_executable_identity": f"sha256:{pre_executable_identity}",
        "post_executable_identity": f"sha256:{post_executable_identity}",
        "pre_closure_identity": f"sha256:{pre_closure_digest}",
        "post_closure_identity": f"sha256:{post_closure_digest}",
        "closure_granularity": "observed_install_tree",
        "closure_entry_count": entry_count,
        "ignored_top_level_paths": sorted(_IGNORED_CURSOR_ROOTS),
        "gate0_artifact": str(gate0),
        "gate0_artifact_identity": f"sha256:{_sha256_file(gate0)}",
        "full_column_gate": gate,
        "harness_result": harness,
    }
    artifact_path = output / "observed-install-qualification.json"
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**artifact, "artifact_path": str(artifact_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-bin", type=Path, required=True)
    parser.add_argument("--provider-root", type=Path, required=True)
    parser.add_argument("--gate0-artifact", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = qualify_cursor_observed_install(
            provider_bin=args.provider_bin,
            provider_root=args.provider_root,
            gate0_artifact=args.gate0_artifact,
            expected_version=args.expected_version,
            output_root=args.output_root,
        )
    except (ObservedInstallError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(result["artifact_path"])
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
