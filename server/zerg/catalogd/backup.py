"""Exact catalog snapshot manifests and blank-root restore proof."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from sqlalchemy import Engine

MANIFEST_VERSION = 1
MANIFEST_NAME = "restore-manifest.json"
CATALOG_NAME = "catalog.db"


class BackupProofError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise BackupProofError(f"{field} is not lowercase SHA-256 hex")
    return value


def _safe_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise BackupProofError("object path is missing")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BackupProofError(f"object path is unsafe: {value}")
    return path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _snapshot_database(engine: Engine, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".catalog-snapshot-", suffix=".db", dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
        with engine.connect() as source_connection, sqlite3.connect(temporary) as target:
            source_connection.connection.driver_connection.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        os.chmod(temporary, 0o600)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _snapshot_manifest(snapshot: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise BackupProofError(f"catalog integrity check failed: {integrity[0] if integrity else 'no result'}")
        meta = connection.execute("SELECT catalog_id, schema_version, commit_seq FROM catalog_meta WHERE singleton = 1").fetchone()
        if meta is None:
            raise BackupProofError("catalog metadata is missing")
        objects: list[dict[str, object]] = []
        for row in connection.execute(
            """
            SELECT object_path AS path, object_hash AS sha256, compressed_size AS size
            FROM raw_objects
            WHERE retired_at IS NULL
            ORDER BY object_path, object_hash
            """
        ):
            objects.append({"kind": "raw", "path": row["path"], "sha256": row["sha256"], "size": row["size"]})
        for row in connection.execute(
            """
            SELECT DISTINCT media.object_path AS path, media.media_hash AS sha256, media.byte_size AS size
            FROM media_objects AS media
            JOIN session_media_refs AS ref ON ref.media_hash = media.media_hash
            WHERE ref.state = 'active'
              AND ref.retired_at IS NULL
              AND media.state = 'present'
              AND media.deleted_at IS NULL
            ORDER BY media.object_path, media.media_hash
            """
        ):
            objects.append({"kind": "media", "path": row["path"], "sha256": row["sha256"], "size": row["size"]})
        objects.sort(key=lambda item: (str(item["path"]), str(item["kind"]), str(item["sha256"])))
        return (
            {
                "catalog_id": str(meta["catalog_id"]),
                "schema_version": int(meta["schema_version"]),
                "commit_seq": str(meta["commit_seq"]),
            },
            objects,
        )
    finally:
        connection.close()


def _object_set_hash(objects: list[dict[str, object]]) -> str:
    encoded = json.dumps(objects, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_catalog_snapshot(*, engine: Engine, output_dir: Path) -> Path:
    """Take the only live-catalog step; callers serialize this with catalog writes."""
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot = output_dir / CATALOG_NAME
    manifest_path = output_dir / MANIFEST_NAME
    if snapshot.exists() or manifest_path.exists():
        raise BackupProofError("restore-point output must not contain catalog.db or restore-manifest.json")
    _snapshot_database(engine, snapshot)
    return snapshot


def publish_restore_point(*, snapshot: Path, data_root: Path) -> dict[str, object]:
    """Verify the frozen snapshot's exact object set, then atomically publish its manifest."""

    snapshot = snapshot.expanduser().resolve()
    data_root = data_root.expanduser().resolve()
    output_dir = snapshot.parent
    manifest_path = output_dir / MANIFEST_NAME
    meta, objects = _snapshot_manifest(snapshot)
    for item in objects:
        path = data_root / _safe_relative_path(item["path"])
        expected_hash = _canonical_hash(item["sha256"], "object sha256")
        if not path.is_file():
            raise BackupProofError(f"required {item['kind']} object is missing: {item['path']}")
        if path.stat().st_size != int(item["size"]):
            raise BackupProofError(f"required {item['kind']} object size mismatch: {item['path']}")
        if _sha256(path) != expected_hash:
            raise BackupProofError(f"required {item['kind']} object hash mismatch: {item['path']}")
    manifest: dict[str, object] = {
        "format": "longhouse-restore",
        "version": MANIFEST_VERSION,
        "catalog": {
            **meta,
            "path": CATALOG_NAME,
            "sha256": _sha256(snapshot),
            "size": snapshot.stat().st_size,
        },
        "objects": objects,
        "object_set_sha256": _object_set_hash(objects),
    }
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(encoded)
        os.chmod(temporary, 0o600)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
        _fsync_directory(output_dir)
    finally:
        temporary.unlink(missing_ok=True)
    return {**manifest, "manifest_path": str(manifest_path)}


def create_restore_point(*, engine: Engine, output_dir: Path, data_root: Path) -> dict[str, object]:
    """Synchronous convenience wrapper used by focused tests and offline tooling."""

    snapshot = create_catalog_snapshot(engine=engine, output_dir=output_dir)
    return publish_restore_point(snapshot=snapshot, data_root=data_root)


def load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupProofError(f"restore manifest is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("format") != "longhouse-restore" or value.get("version") != MANIFEST_VERSION:
        raise BackupProofError("restore manifest format/version is unsupported")
    return value


def verify_restore_point(*, manifest_path: Path, catalog_root: Path | None = None, data_root: Path) -> dict[str, object]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    catalog = manifest.get("catalog")
    objects = manifest.get("objects")
    if not isinstance(catalog, dict) or not isinstance(objects, list) or any(not isinstance(item, dict) for item in objects):
        raise BackupProofError("restore manifest catalog/object set is invalid")
    if manifest.get("object_set_sha256") != _object_set_hash(objects):
        raise BackupProofError("restore manifest object-set hash mismatch")
    catalog_base = manifest_path.parent if catalog_root is None else catalog_root.expanduser().resolve()
    snapshot = catalog_base / _safe_relative_path(catalog.get("path"))
    expected_catalog_hash = _canonical_hash(catalog.get("sha256"), "catalog sha256")
    if not snapshot.is_file() or snapshot.stat().st_size != int(catalog.get("size", -1)):
        raise BackupProofError("catalog snapshot is missing or has the wrong size")
    if _sha256(snapshot) != expected_catalog_hash:
        raise BackupProofError("catalog snapshot hash mismatch")
    meta, snapshot_objects = _snapshot_manifest(snapshot)
    for field in ("catalog_id", "schema_version", "commit_seq"):
        if meta[field] != catalog.get(field):
            raise BackupProofError(f"catalog snapshot {field} does not match manifest")
    if snapshot_objects != objects:
        raise BackupProofError("catalog snapshot object set does not match manifest")
    root = data_root.expanduser().resolve()
    for item in objects:
        relative = _safe_relative_path(item.get("path"))
        expected_hash = _canonical_hash(item.get("sha256"), "object sha256")
        path = root / relative
        if not path.is_file() or path.stat().st_size != int(item.get("size", -1)):
            raise BackupProofError(f"required {item.get('kind')} object is missing or truncated: {relative}")
        if _sha256(path) != expected_hash:
            raise BackupProofError(f"required {item.get('kind')} object hash mismatch: {relative}")
    return {
        "ok": True,
        "catalog_sha256": expected_catalog_hash,
        "commit_seq": str(catalog["commit_seq"]),
        "object_count": len(objects),
        "object_set_sha256": manifest["object_set_sha256"],
    }


def restore_rehearsal(*, manifest_path: Path, source_data_root: Path, destination_root: Path) -> dict[str, object]:
    manifest_path = manifest_path.expanduser().resolve()
    destination = destination_root.expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise BackupProofError("restore rehearsal destination must be empty")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = load_manifest(manifest_path)
    catalog = manifest["catalog"]
    objects = manifest["objects"]
    assert isinstance(catalog, dict) and isinstance(objects, list)
    snapshot_relative = _safe_relative_path(catalog["path"])
    shutil.copy2(manifest_path.parent / snapshot_relative, destination / snapshot_relative)
    source_root = source_data_root.expanduser().resolve()
    for item in objects:
        assert isinstance(item, dict)
        relative = _safe_relative_path(item["path"])
        target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, target)
    if (destination / "longhouse.db").exists():
        raise BackupProofError("restore rehearsal unexpectedly copied longhouse.db")
    proof = verify_restore_point(manifest_path=manifest_path, catalog_root=destination, data_root=destination)
    return {**proof, "destination_root": str(destination)}


__all__ = [
    "BackupProofError",
    "create_catalog_snapshot",
    "create_restore_point",
    "load_manifest",
    "publish_restore_point",
    "restore_rehearsal",
    "verify_restore_point",
]
