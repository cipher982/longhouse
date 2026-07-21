from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.backup import BackupProofError
from zerg.catalogd.backup import restore_rehearsal
from zerg.catalogd.backup import verify_restore_point
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.server import CatalogDaemon
from zerg.config import resolve_live_database_url
from zerg.config import sqlite_file_path
from zerg.storage_v2.media_objects import MediaObjectSpec
from zerg.storage_v2.media_objects import seal_media_object
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import seal_raw_object
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import read_render_object
from zerg.storage_v2.render_objects import seal_render_object


@pytest.fixture
def backup_root() -> Path:
    root = Path("/tmp") / f"lhbk-{uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _render_manifest(*, sealed, generation_id) -> dict[str, object]:
    return {
        "generation_id": str(generation_id),
        "parser_revision": "backup-restore-v1",
        "ordering_revision": "semantic-order-v2",
        "object_id": sealed.object_id,
        "object_hash": sealed.object_hash,
        "payload_hash": sealed.payload_hash,
        "object_path": sealed.object_path,
        "uncompressed_size": sealed.uncompressed_size,
        "compressed_size": sealed.compressed_size,
        "event_count": sealed.event_count,
        "first_order_key": sealed.first_order_key,
        "last_order_key": sealed.last_order_key,
        "user_messages": sealed.user_messages,
        "assistant_messages": sealed.assistant_messages,
        "tool_calls": sealed.tool_calls,
        "first_user_message_preview": sealed.first_user_message_preview,
        "last_visible_text_preview": sealed.last_visible_text_preview,
    }


def _raw_commit_params(
    *,
    sealed,
    spec: RawObjectSpec,
    now: datetime,
    render_state: str = "pending",
    render_manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": 2,
        "tenant_id": spec.tenant_id,
        "owner_id": "42",
        "session_id": str(spec.session_id),
        "machine_id": spec.machine_id,
        "provider": spec.provider,
        "opaque_source_id": spec.opaque_source_id,
        "source_epoch": str(spec.source_epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": now.isoformat(),
        "range_kind": spec.range_kind,
        "range_start": spec.range_start,
        "range_end": spec.range_end,
        "record_hashes": list(sealed.record_hashes),
        "envelope_id": sealed.envelope_id,
        "object_hash": sealed.object_hash,
        "payload_hash": sealed.payload_hash,
        "compressed_hash": sealed.compressed_hash,
        "object_path": sealed.object_path,
        "uncompressed_size": sealed.uncompressed_size,
        "compressed_size": sealed.compressed_size,
        "provenance_kind": "native",
        "render_state": render_state,
        "media_refs": [],
        "projectors": ["render-v2", "search-v2"],
        "render_manifest": render_manifest,
        "session_facts": {
            "environment": "local",
            "project": "longhouse",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "git_branch": "main",
            "started_at": now.isoformat(),
            "last_activity_at": now.isoformat(),
            "ended_at": None,
            "origin_kind": "shadow",
            "hidden_from_default_timeline": False,
            "launch_actor": None,
            "launch_surface": None,
        },
        "sealed_at": now.isoformat(),
    }


@pytest.mark.asyncio
async def test_exact_backup_verify_and_blank_root_restore_without_monolith(backup_root: Path) -> None:
    tmp_path = backup_root
    database_path = tmp_path / "live-catalog.db"
    socket_path = tmp_path / "catalogd.sock"
    data_root = tmp_path / "data"
    data_root.mkdir(mode=0o700)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    raw_spec = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="codex",
        opaque_source_id="history.jsonl",
        source_epoch=uuid4(),
        range_kind="byte_offset",
        range_start=0,
        range_end=6,
        records=(RawRecord(source_position=0, data=b"hello\n"),),
    )
    sealed_raw = seal_raw_object(data_root, raw_spec)
    render_generation = uuid4()
    render_spec = RenderObjectSpec(
        session_id=session_id,
        render_generation=render_generation,
        parser_revision="backup-restore-v1",
        ordering_revision="semantic-order-v2",
        machine_id=raw_spec.machine_id,
        provider=raw_spec.provider,
        opaque_source_id=raw_spec.opaque_source_id,
        source_epoch=raw_spec.source_epoch,
        source_envelope_id=sealed_raw.envelope_id,
        records=(
            RenderRecord(
                event_id="backup-restore-event",
                order_time_us=int(now.timestamp() * 1_000_000),
                source_position=0,
                event_subordinal=0,
                role="user",
                content_text="hello",
            ),
        ),
    )
    sealed_render = seal_render_object(data_root, render_spec)
    media_bytes = b"lossless-media"
    media_hash = hashlib.sha256(media_bytes).hexdigest()
    sealed_media = seal_media_object(
        data_root,
        MediaObjectSpec(media_hash=media_hash, mime_type="application/octet-stream", data=media_bytes),
    )
    (data_root / "search.db").write_bytes(b"disposable")
    (data_root / "longhouse.db").write_bytes(b"must-not-restore")

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    restore_point = tmp_path / "restore-point"
    try:
        await client.call(
            "storage.raw_object.commit.v2",
            _raw_commit_params(
                sealed=sealed_raw,
                spec=raw_spec,
                now=now,
                render_state="ready",
                render_manifest=_render_manifest(sealed=sealed_render, generation_id=render_generation),
            ),
        )
        await client.call(
            "storage.media.commit.v2",
            {
                "media_hash": sealed_media.media_hash,
                "state": "present",
                "mime_type": sealed_media.mime_type,
                "byte_size": sealed_media.byte_size,
                "object_path": sealed_media.object_path,
                "session_refs": [
                    {"session_id": str(session_id), "envelope_id": sealed_raw.envelope_id, "ref_key": "inline:0"}
                ],
                "observed_at": now.isoformat(),
            },
        )
        backup = await client.call(
            "backup.snapshot.create.v2",
            {"output_dir": str(restore_point), "data_root": str(data_root)},
            timeout_seconds=10,
        )
        assert int(backup["commit_seq"]) >= 2
        assert backup["object_count"] == 3
    finally:
        await client.close()
        await daemon.close()

    manifest_path = restore_point / "restore-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["objects"] == sorted(
        manifest["objects"], key=lambda item: (item["path"], item["kind"], item["sha256"])
    )
    assert {item["kind"] for item in manifest["objects"]} == {"raw", "render", "media"}
    assert "created_at" not in manifest
    proof = verify_restore_point(manifest_path=manifest_path, data_root=data_root)
    assert proof["ok"] is True and proof["object_count"] == 3

    restored = tmp_path / "blank-root"
    deployed_catalog = sqlite_file_path(resolve_live_database_url(f"sqlite:///{restored / 'longhouse.db'}"))
    assert deployed_catalog is not None
    rehearsal = restore_rehearsal(
        manifest_path=manifest_path,
        source_data_root=data_root,
        destination_root=restored,
        catalog_destination=deployed_catalog,
    )
    assert rehearsal["ok"] is True
    assert deployed_catalog.is_file()
    assert not (restored / "catalog.db").exists()
    assert not (restored / "longhouse.db").exists()
    assert not (restored / "search.db").exists()

    restored_daemon = CatalogDaemon(database_path=deployed_catalog, socket_path=restored / "catalogd.sock")
    await restored_daemon.start()
    restored_client = CatalogClient(restored / "catalogd.sock")
    try:
        assert (await restored_client.call("ping.v2"))["ready"] is True
        timeline = await restored_client.call(
            "storage.session.timeline.list.v2",
            {
                "owner_id": "42",
                "before_last_activity_at": None,
                "before_session_id": None,
                "project": None,
                "provider": None,
                "include_test": False,
                "limit": 10,
            },
        )
        assert [row["session_id"] for row in timeline["sessions"]] == [str(session_id)]
        detail = await restored_client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert detail["found"] is True
        raw_manifest = await restored_client.call(
            "storage.session.raw_manifest.v2",
            {"session_id": str(session_id), "owner_id": "42", "after_source_key": None, "limit": 10},
        )
        assert raw_manifest["objects"][0]["object_hash"] == sealed_raw.object_hash
        assert read_render_object(
            restored,
            sealed_render.object_path,
            expected_object_hash=sealed_render.object_hash,
        ).spec == render_spec
    finally:
        await restored_client.close()
        await restored_daemon.close()

    raw_path = data_root / sealed_raw.object_path
    original = raw_path.read_bytes()
    raw_path.write_bytes(original + b"corrupt")
    with pytest.raises(BackupProofError, match="raw object is missing or truncated"):
        verify_restore_point(manifest_path=manifest_path, data_root=data_root)
