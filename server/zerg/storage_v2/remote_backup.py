"""Mirror verified storage-v2 restore points without changing live authority."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from zerg.catalogd.backup import BackupProofError
from zerg.catalogd.backup import load_manifest
from zerg.catalogd.backup import verify_restore_point
from zerg.storage_v2.object_store import ImmutableObjectStore

FORMAT = "longhouse-remote-restore"
VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class RemoteBackupError(RuntimeError):
    pass


def mirror_restore_point(
    *,
    store: ImmutableObjectStore,
    tenant_id: str,
    manifest_path: Path,
    data_root: Path,
) -> dict[str, object]:
    """Mirror a frozen, locally verified restore point under one tenant scope."""

    manifest_path = manifest_path.expanduser().resolve()
    data_root = data_root.expanduser().resolve()
    try:
        proof = verify_restore_point(manifest_path=manifest_path, data_root=data_root)
        local = load_manifest(manifest_path)
    except BackupProofError as exc:
        raise RemoteBackupError(str(exc)) from exc
    catalog = _mapping(local.get("catalog"), "catalog")
    objects = _mappings(local.get("objects"), "objects")
    artifacts = [
        _artifact(kind="restore_manifest", path="restore-manifest.json", source=manifest_path, sha256=_sha256(manifest_path)),
        _artifact(
            kind="catalog",
            path=_relative(catalog.get("path")),
            source=manifest_path.parent / _relative(catalog.get("path")),
            sha256=_hash(catalog.get("sha256"), "catalog sha256"),
            size=_size(catalog.get("size"), "catalog size"),
        ),
    ]
    for item in objects:
        relative = _relative(item.get("path"))
        artifacts.append(
            _artifact(
                kind=_text(item.get("kind"), "object kind"),
                path=relative,
                source=data_root / relative,
                sha256=_hash(item.get("sha256"), "object sha256"),
                size=_size(item.get("size"), "object size"),
            )
        )
    mirrored = [_mirror_artifact(store=store, tenant_id=tenant_id, artifact=artifact) for artifact in artifacts]
    document: dict[str, object] = {
        "format": FORMAT,
        "version": VERSION,
        "source_manifest_sha256": _sha256(manifest_path),
        "source_object_set_sha256": _hash(local.get("object_set_sha256"), "source object_set_sha256"),
        "artifacts": mirrored,
    }
    encoded = _canonical_document(document)
    manifest_hash = hashlib.sha256(encoded).hexdigest()
    manifest_key = f"backup/v1/manifests/{manifest_hash[:2]}/{manifest_hash}.json"
    store.put_if_absent(tenant_id=tenant_id, key=manifest_key, data=encoded, sha256=manifest_hash)
    return {
        "remote_manifest_key": manifest_key,
        "remote_manifest_sha256": manifest_hash,
        "artifact_count": len(mirrored),
        "object_count": int(proof["object_count"]),
        "mirrored_bytes": sum(int(artifact["size"]) for artifact in mirrored),
    }


def scrub_remote_restore_point(
    *,
    store: ImmutableObjectStore,
    tenant_id: str,
    remote_manifest_key: str,
    remote_manifest_sha256: str,
) -> dict[str, object]:
    """Read and hash-verify every mirrored artifact before a restore exercise."""

    document = _load_remote_manifest(
        store=store,
        tenant_id=tenant_id,
        remote_manifest_key=remote_manifest_key,
        remote_manifest_sha256=remote_manifest_sha256,
    )
    artifacts = _mappings(document.get("artifacts"), "artifacts")
    for artifact in artifacts:
        store.read_verified(
            tenant_id=tenant_id,
            key=_text(artifact.get("remote_key"), "remote_key"),
            sha256=_hash(artifact.get("sha256"), "artifact sha256"),
            max_bytes=_size(artifact.get("size"), "artifact size"),
        )
    return {"ok": True, "artifact_count": len(artifacts), "mirrored_bytes": sum(int(item["size"]) for item in artifacts)}


def restore_remote_rehearsal(
    *,
    store: ImmutableObjectStore,
    tenant_id: str,
    remote_manifest_key: str,
    remote_manifest_sha256: str,
    destination_root: Path,
    catalog_destination: Path | None = None,
) -> dict[str, object]:
    """Restore a remote mirror only into a blank root and re-run local proof."""

    destination = destination_root.expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise RemoteBackupError("remote restore destination must be empty")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    document = _load_remote_manifest(
        store=store,
        tenant_id=tenant_id,
        remote_manifest_key=remote_manifest_key,
        remote_manifest_sha256=remote_manifest_sha256,
    )
    artifacts = _mappings(document.get("artifacts"), "artifacts")
    catalog_path: Path | None = None
    manifest_path: Path | None = None
    for artifact in artifacts:
        kind = _text(artifact.get("kind"), "artifact kind")
        relative = _relative(artifact.get("path"))
        if kind == "catalog" and catalog_destination is not None:
            target = catalog_destination.expanduser().resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise RemoteBackupError("catalog restore destination must be inside the blank root") from exc
        else:
            target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = store.read_verified(
            tenant_id=tenant_id,
            key=_text(artifact.get("remote_key"), "remote_key"),
            sha256=_hash(artifact.get("sha256"), "artifact sha256"),
            max_bytes=_size(artifact.get("size"), "artifact size"),
        )
        _write_exact(target, data)
        if kind == "catalog":
            catalog_path = target
        elif kind == "restore_manifest":
            manifest_path = target
    if catalog_path is None or manifest_path is None:
        raise RemoteBackupError("remote restore manifest lacks catalog or restore manifest")
    try:
        proof = verify_restore_point(manifest_path=manifest_path, catalog_path=catalog_path, data_root=destination)
    except BackupProofError as exc:
        raise RemoteBackupError(str(exc)) from exc
    return {**proof, "destination_root": str(destination)}


def _load_remote_manifest(
    *, store: ImmutableObjectStore, tenant_id: str, remote_manifest_key: str, remote_manifest_sha256: str
) -> dict[str, object]:
    _hash(remote_manifest_sha256, "remote manifest sha256")
    data = store.read_verified(
        tenant_id=tenant_id,
        key=remote_manifest_key,
        sha256=remote_manifest_sha256,
        max_bytes=MAX_MANIFEST_BYTES,
    )
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteBackupError("remote restore manifest is unreadable") from exc
    if not isinstance(value, dict) or value.get("format") != FORMAT or value.get("version") != VERSION:
        raise RemoteBackupError("remote restore manifest format/version is unsupported")
    return value


def _mirror_artifact(*, store: ImmutableObjectStore, tenant_id: str, artifact: dict[str, object]) -> dict[str, object]:
    source = artifact.pop("source")
    assert isinstance(source, Path)
    data = source.read_bytes()
    sha256 = _hash(artifact.get("sha256"), "artifact sha256")
    size = _size(artifact.get("size"), "artifact size")
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise RemoteBackupError(f"artifact changed after local restore proof: {artifact['path']}")
    remote_key = f"backup/v1/blobs/{sha256[:2]}/{sha256}"
    store.put_if_absent(tenant_id=tenant_id, key=remote_key, data=data, sha256=sha256)
    return {**artifact, "remote_key": remote_key}


def _artifact(*, kind: str, path: str, source: Path, sha256: str, size: int | None = None) -> dict[str, object]:
    if not source.is_file():
        raise RemoteBackupError(f"required {kind} artifact is missing: {path}")
    return {"kind": kind, "path": path, "source": source, "sha256": sha256, "size": source.stat().st_size if size is None else size}


def _canonical_document(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_exact(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RemoteBackupError(f"{field} is invalid")
    return value


def _mappings(value: object, field: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RemoteBackupError(f"{field} is invalid")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RemoteBackupError(f"{field} is invalid")
    return value


def _hash(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise RemoteBackupError(f"{field} is not lowercase SHA-256 hex")
    return text


def _size(value: object, field: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise RemoteBackupError(f"{field} is invalid")
    return value


def _relative(value: object) -> str:
    path = Path(_text(value, "path"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RemoteBackupError("path is unsafe")
    return path.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["RemoteBackupError", "mirror_restore_point", "restore_remote_rehearsal", "scrub_remote_restore_point"]
