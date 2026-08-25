"""Deletion has to remove the bytes and describe honestly what it left.

Two claims are under test.

*The bytes go.* Media was previously listed in ``partial`` as unreachable and
left on disk forever. Deletion now fences the session first, then enumerates the
final object set — raw, render, and media — and removes the files, keeping only
media another session still actively references.

*The report is honest.* ``complete`` is false whenever anything survived, and
``partial`` names it. And a caller who cannot prove ownership gets one
indistinguishable answer for "never existed", "someone else's", and "already
deleted", so a guessed UUID is not an existence oracle.
"""

from __future__ import annotations

import hashlib
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.server import CatalogDaemon
from zerg.services.data_deletion import SessionNotFound
from zerg.services.data_deletion import delete_session_data
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import seal_raw_object
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import seal_render_object

DELETION_TEST_RPC_TIMEOUT_SECONDS = 5.0
TENANT = "tenant-a"


def _socket_root(prefix: str) -> Path:
    root = Path("/tmp") / f"{prefix}-{uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    return root


def _commit_params(object_root: Path, *, session_id: UUID, owner_id: str, now: datetime) -> tuple[dict, object, object]:
    source_epoch = uuid4()
    generation_id = uuid4()
    opaque_source_id = f"machine-agent/{source_epoch}.jsonl"
    sealed_raw = seal_raw_object(
        object_root,
        RawObjectSpec(
            tenant_id=TENANT,
            machine_id="cinder",
            session_id=session_id,
            provider="codex",
            opaque_source_id=opaque_source_id,
            source_epoch=source_epoch,
            range_kind="record_ordinal",
            range_start=0,
            range_end=1,
            records=(RawRecord(source_position=0, data=b"delete me"),),
        ),
    )
    sealed_render = seal_render_object(
        object_root,
        RenderObjectSpec(
            session_id=session_id,
            render_generation=generation_id,
            parser_revision="deletion-proof-v1",
            ordering_revision="semantic-order-v2",
            machine_id="cinder",
            provider="codex",
            opaque_source_id=opaque_source_id,
            source_epoch=source_epoch,
            source_envelope_id=sealed_raw.envelope_id,
            records=(
                RenderRecord(
                    event_id=f"event-{sealed_raw.envelope_id[:16]}",
                    order_time_us=int(now.timestamp() * 1_000_000),
                    source_position=0,
                    event_subordinal=0,
                    role="user",
                    content_text="delete me",
                    thread_id=str(uuid4()),
                    branch_kind="head",
                    raw_record_ordinal=0,
                ),
            ),
        ),
    )
    params = {
        "protocol_version": 2,
        "tenant_id": TENANT,
        "owner_id": owner_id,
        "session_id": str(session_id),
        "machine_id": "cinder",
        "provider": "codex",
        "opaque_source_id": opaque_source_id,
        "source_epoch": str(source_epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": now.isoformat(),
        "range_kind": "record_ordinal",
        "range_start": 0,
        "range_end": 1,
        "record_hashes": list(sealed_raw.record_hashes),
        "envelope_id": sealed_raw.envelope_id,
        "object_hash": sealed_raw.object_hash,
        "payload_hash": sealed_raw.payload_hash,
        "compressed_hash": sealed_raw.compressed_hash,
        "object_path": sealed_raw.object_path,
        "uncompressed_size": sealed_raw.uncompressed_size,
        "compressed_size": sealed_raw.compressed_size,
        "provenance_kind": "native",
        "render_state": "ready",
        "media_refs": [],
        "projectors": ["search-v2"],
        "render_manifest": {
            "generation_id": str(generation_id),
            "parser_revision": "deletion-proof-v1",
            "ordering_revision": "semantic-order-v2",
            "object_id": sealed_render.object_id,
            "object_hash": sealed_render.object_hash,
            "payload_hash": sealed_render.payload_hash,
            "object_path": sealed_render.object_path,
            "uncompressed_size": sealed_render.uncompressed_size,
            "compressed_size": sealed_render.compressed_size,
            "event_count": sealed_render.event_count,
            "first_order_key": sealed_render.first_order_key,
            "last_order_key": sealed_render.last_order_key,
            "user_messages": sealed_render.user_messages,
            "assistant_messages": sealed_render.assistant_messages,
            "tool_calls": sealed_render.tool_calls,
            "first_user_message_preview": sealed_render.first_user_message_preview,
            "last_visible_text_preview": sealed_render.last_visible_text_preview,
        },
        "session_facts": {
            "environment": "production",
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
    return params, sealed_raw, sealed_render


def _write_media(object_root: Path, *, payload: bytes) -> tuple[str, str, int]:
    """Put one content-addressed media file on disk and return its manifest facts."""

    media_hash = hashlib.sha256(payload).hexdigest()
    object_path = f"media/{media_hash[:2]}/{media_hash}.bin"
    path = object_root / object_path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(payload)
    return media_hash, object_path, len(payload)


async def _commit_media(client: CatalogClient, *, media_hash: str, object_path: str, byte_size: int, session_id: UUID, now: datetime):
    await client.call(
        "storage.media.commit.v2",
        {
            "media_hash": media_hash,
            "state": "present",
            "mime_type": "image/png",
            "byte_size": byte_size,
            "object_path": object_path,
            "session_refs": [{"session_id": str(session_id), "envelope_id": None, "ref_key": f"inline:{media_hash[:8]}"}],
            "observed_at": now.isoformat(),
        },
        timeout_seconds=DELETION_TEST_RPC_TIMEOUT_SECONDS,
    )


@pytest.mark.asyncio
async def test_session_deletion_removes_media_bytes_and_reports_what_it_left(tmp_path, monkeypatch):
    root = _socket_root("lh-delete-media")
    object_root = tmp_path / "objects-v2"
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    neighbour_id = uuid4()

    daemon = CatalogDaemon(database_path=root / "live.db", socket_path=root / "catalogd.sock")
    await daemon.start()
    client = CatalogClient(root / "catalogd.sock", default_timeout_seconds=DELETION_TEST_RPC_TIMEOUT_SECONDS)
    monkeypatch.setenv("LONGHOUSE_STORAGE_V2_ROOT", str(object_root))
    monkeypatch.setattr("zerg.services.data_deletion.get_catalogd_client", lambda: client)
    # searchd is a separate process; when it is down the report has to say the
    # index was not touched rather than claim a clean deletion.
    monkeypatch.setattr("zerg.services.data_deletion.get_searchd_client", lambda: None)
    try:
        params, sealed_raw, sealed_render = _commit_params(object_root, session_id=session_id, owner_id="42", now=now)
        await client.call("storage.raw_object.commit.v2", params, timeout_seconds=DELETION_TEST_RPC_TIMEOUT_SECONDS)
        neighbour_params, _, _ = _commit_params(object_root, session_id=neighbour_id, owner_id="42", now=now)
        await client.call("storage.raw_object.commit.v2", neighbour_params, timeout_seconds=DELETION_TEST_RPC_TIMEOUT_SECONDS)

        exclusive_hash, exclusive_path, exclusive_size = _write_media(object_root, payload=b"screenshot-only-this-session")
        shared_hash, shared_path, _ = _write_media(object_root, payload=b"screenshot-two-sessions-reference")
        await _commit_media(
            client,
            media_hash=exclusive_hash,
            object_path=exclusive_path,
            byte_size=exclusive_size,
            session_id=session_id,
            now=now,
        )
        for holder in (session_id, neighbour_id):
            await _commit_media(
                client,
                media_hash=shared_hash,
                object_path=shared_path,
                byte_size=len((object_root / shared_path).read_bytes()),
                session_id=holder,
                now=now,
            )

        raw_file = object_root / sealed_raw.object_path
        render_file = object_root / sealed_render.object_path
        exclusive_file = object_root / exclusive_path
        shared_file = object_root / shared_path
        assert raw_file.exists() and render_file.exists() and exclusive_file.exists() and shared_file.exists()

        report = await delete_session_data(session_id=session_id, owner_id=42)

        # The bytes are gone from disk, not merely fenced from serving.
        assert not raw_file.exists()
        assert not render_file.exists()
        assert not exclusive_file.exists()
        # Another session still references this one, so its bytes are not ours.
        assert shared_file.exists()

        assert report.raw_objects_deleted == 1
        assert report.render_objects_deleted == 1
        assert report.media_objects_deleted == 1
        assert report.media_objects_retained_shared == 1
        assert report.object_bytes_deleted == sealed_raw.compressed_size + sealed_render.compressed_size + exclusive_size
        assert report.manifest_rows_retired >= 3

        # Honest report: something survived, so complete is false and partial
        # names each survivor rather than leaving the caller to assume.
        assert report.complete is False
        assert any("media object(s) are still referenced" in note for note in report.partial)
        assert any("search index" in note for note in report.partial)
        assert any("archive database" in note for note in report.partial)

        # Repeating the delete is safe and says so; nothing is left to remove.
        again = await delete_session_data(session_id=session_id, owner_id=42)
        assert again.already_deleted is True
        assert again.raw_objects_deleted == 0
        assert again.media_objects_deleted == 0
    finally:
        await client.close()
        await daemon.close()
        for path in root.iterdir():
            path.unlink(missing_ok=True)
        root.rmdir()


@pytest.mark.asyncio
async def test_a_non_owner_cannot_tell_a_deleted_session_from_one_that_never_existed(tmp_path, monkeypatch):
    root = _socket_root("lh-delete-oracle")
    object_root = tmp_path / "objects-v2"
    now = datetime.now(UTC).replace(microsecond=0)
    live_session = uuid4()
    doomed_session = uuid4()

    daemon = CatalogDaemon(database_path=root / "live.db", socket_path=root / "catalogd.sock")
    await daemon.start()
    client = CatalogClient(root / "catalogd.sock", default_timeout_seconds=DELETION_TEST_RPC_TIMEOUT_SECONDS)
    monkeypatch.setenv("LONGHOUSE_STORAGE_V2_ROOT", str(object_root))
    monkeypatch.setattr("zerg.services.data_deletion.get_catalogd_client", lambda: client)
    monkeypatch.setattr("zerg.services.data_deletion.get_searchd_client", lambda: None)
    try:
        for owned in (live_session, doomed_session):
            params, _, _ = _commit_params(object_root, session_id=owned, owner_id="42", now=now)
            await client.call("storage.raw_object.commit.v2", params, timeout_seconds=DELETION_TEST_RPC_TIMEOUT_SECONDS)

        owner_report = await delete_session_data(session_id=doomed_session, owner_id=42)
        assert owner_report.already_deleted is False

        never_existed = uuid4()
        outcomes = []
        for probe in (never_existed, live_session, doomed_session):
            with pytest.raises(SessionNotFound) as error:
                await delete_session_data(session_id=probe, owner_id=99)
            outcomes.append((type(error.value), error.value.args))

        # All three answers carry the same type and the same payload shape: only
        # the id the caller already knew. A stranger learns nothing about which
        # ids exist, which are someone else's, or which were deleted.
        assert outcomes[0][0] is outcomes[1][0] is outcomes[2][0]
        assert [args for _, args in outcomes] == [(str(never_existed),), (str(live_session),), (str(doomed_session),)]

        # The owner, and only the owner, gets the terminal-state answer.
        assert (await delete_session_data(session_id=doomed_session, owner_id=42)).already_deleted is True
    finally:
        await client.close()
        await daemon.close()
        for path in root.iterdir():
            path.unlink(missing_ok=True)
        root.rmdir()
