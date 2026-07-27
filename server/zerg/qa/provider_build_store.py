from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Mapping

CLOSURE_MANIFEST_VERSION = 1
LOCK_SCHEMA_VERSION = 1
GENERATED_FAKE_PROVENANCE = "generated_fake"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ProviderBuildStoreError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderBuildRef:
    provider: str
    version: str
    platform: str
    architecture: str
    artifact_provenance: str
    closure_manifest_version: int
    closure_digest: str
    build_root: Path
    entrypoint_relative: str

    @property
    def entrypoint(self) -> Path:
        return self.build_root / self.entrypoint_relative

    def to_evidence(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "version": self.version,
            "platform": self.platform,
            "architecture": self.architecture,
            "artifact_provenance": self.artifact_provenance,
            "closure_manifest_version": self.closure_manifest_version,
            "closure_digest": self.closure_digest,
            "entrypoint": self.entrypoint_relative,
        }


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _file_entry(path: Path, relative_path: str) -> dict[str, Any]:
    stat_result = path.lstat()
    if path.is_symlink():
        return {
            "path": relative_path,
            "kind": "symlink",
            "target": os.readlink(path),
        }
    if not path.is_file():
        raise ProviderBuildStoreError(f"unsupported provider build entry: {path}")
    return {
        "path": relative_path,
        "kind": "file",
        "executable": bool(stat_result.st_mode & 0o111),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def closure_manifest(root: Path) -> dict[str, Any]:
    root = root.expanduser()
    if not root.is_dir():
        raise ProviderBuildStoreError(f"provider build root is not a directory: {root}")
    entries = [
        _file_entry(path, path.relative_to(root).as_posix())
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        if path.is_symlink() or path.is_file()
    ]
    if not entries:
        raise ProviderBuildStoreError(f"provider build root is empty: {root}")
    return {
        "closure_manifest_version": CLOSURE_MANIFEST_VERSION,
        "entries": entries,
    }


def closure_digest(root: Path) -> str:
    return hashlib.sha256(_canonical_json(closure_manifest(root))).hexdigest()


def _single_entrypoint_identity(source: Path, relative_path: str) -> tuple[dict[str, Any], str]:
    manifest = {
        "closure_manifest_version": CLOSURE_MANIFEST_VERSION,
        "entries": [_file_entry(source, relative_path)],
    }
    return manifest, hashlib.sha256(_canonical_json(manifest)).hexdigest()


def _read_lock(lock_path: Path) -> dict[str, Any]:
    if not lock_path.is_file():
        return {"schema_version": LOCK_SCHEMA_VERSION, "builds": {}}
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderBuildStoreError(f"provider build lock is unreadable: {lock_path}") from exc
    if payload.get("schema_version") != LOCK_SCHEMA_VERSION or not isinstance(payload.get("builds"), dict):
        raise ProviderBuildStoreError(f"provider build lock has an unsupported schema: {lock_path}")
    return payload


def _write_lock(lock_path: Path, payload: Mapping[str, Any]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = lock_path.with_name(f".{lock_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(lock_path)


def _platform_identity() -> tuple[str, str]:
    return platform.system().lower(), platform.machine().lower()


def _validate_segment(label: str, value: str) -> None:
    if not _SAFE_SEGMENT.fullmatch(value):
        raise ProviderBuildStoreError(f"invalid {label} for provider build store: {value!r}")


def materialize_generated_fake_builds(
    provider_bins: Mapping[str, Path],
    *,
    store_root: Path,
) -> dict[str, ProviderBuildRef]:
    """Copy generated fakes into the verified, human-readable build store."""

    store_root = store_root.expanduser()
    lock_path = store_root / "provider-builds.lock"
    lock = _read_lock(lock_path)
    builds = lock["builds"]
    platform_name, architecture = _platform_identity()
    platform_key = f"{platform_name}-{architecture}"
    refs: dict[str, ProviderBuildRef] = {}

    for provider, raw_source in sorted(provider_bins.items()):
        _validate_segment("provider", provider)
        source = raw_source.expanduser()
        if source.is_symlink() or not source.is_file():
            raise ProviderBuildStoreError(f"generated fake entrypoint is not a regular file: {source}")
        _validate_segment("entrypoint", source.name)
        relative_entrypoint = f"bin/{source.name}"
        manifest, expected_digest = _single_entrypoint_identity(source, relative_entrypoint)
        version = f"fake-{expected_digest[:12]}"
        build_root = store_root / provider / version / platform_key
        entrypoint = build_root / relative_entrypoint
        expected_lock_entry = {
            "artifact_provenance": GENERATED_FAKE_PROVENANCE,
            "closure_digest": expected_digest,
            "closure_manifest": manifest,
            "closure_manifest_version": CLOSURE_MANIFEST_VERSION,
            "entrypoint": relative_entrypoint,
        }

        provider_builds = builds.setdefault(provider, {})
        version_builds = provider_builds.setdefault(version, {})
        existing_lock_entry = version_builds.get(platform_key)
        if existing_lock_entry is not None and existing_lock_entry != expected_lock_entry:
            raise ProviderBuildStoreError(f"provider build lock would rewrite {provider}/{version}/{platform_key}")

        if build_root.exists():
            actual_digest = closure_digest(build_root)
            if actual_digest != expected_digest:
                raise ProviderBuildStoreError(
                    f"provider build store is tampered for {provider}/{version}/{platform_key}: "
                    f"expected {expected_digest}, got {actual_digest}"
                )
        else:
            entrypoint.parent.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(source, entrypoint)
            entrypoint.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
            actual_digest = closure_digest(build_root)
            if actual_digest != expected_digest:
                raise ProviderBuildStoreError(
                    f"materialized provider build digest mismatch for {provider}: expected {expected_digest}, got {actual_digest}"
                )

        version_builds[platform_key] = expected_lock_entry
        refs[provider] = ProviderBuildRef(
            provider=provider,
            version=version,
            platform=platform_name,
            architecture=architecture,
            artifact_provenance=GENERATED_FAKE_PROVENANCE,
            closure_manifest_version=CLOSURE_MANIFEST_VERSION,
            closure_digest=expected_digest,
            build_root=build_root,
            entrypoint_relative=relative_entrypoint,
        )

    _write_lock(lock_path, lock)
    return refs


def verify_provider_builds(builds: Mapping[str, ProviderBuildRef]) -> None:
    for provider, build in sorted(builds.items()):
        actual_digest = closure_digest(build.build_root)
        if actual_digest != build.closure_digest:
            raise ProviderBuildStoreError(f"provider build mutated for {provider}: expected {build.closure_digest}, got {actual_digest}")
