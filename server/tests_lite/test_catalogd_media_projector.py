from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.models import MediaObject
from zerg.catalogd.models import SessionTombstone
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-media-projector-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _media_params(
    *,
    media_hash: str,
    state: str,
    observed_at: datetime,
    session_id: str | None = None,
) -> dict:
    present = state == "present"
    return {
        "media_hash": media_hash,
        "state": state,
        "mime_type": "image/png" if present else None,
        "byte_size": 123 if present else None,
        "object_path": f"media/{media_hash[:2]}/{media_hash}.bin" if present else None,
        "session_refs": (
            [{"session_id": session_id, "envelope_id": None, "ref_key": "inline:0"}] if session_id is not None else []
        ),
        "observed_at": observed_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_media_manifest_is_content_addressed_idempotent_and_restart_durable(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    media_hash = "a" * 64
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        missing = _media_params(
            media_hash=media_hash,
            state="missing",
            observed_at=now,
            session_id=session_id,
        )
        first = await client.call("storage.media.commit.v2", missing)
        replay = await client.call("storage.media.commit.v2", missing)
        assert first["created"] is True and first["commit_seq"] == "1"
        assert first["media"]["state"] == "missing"
        assert replay["exact_replay"] is True and replay["commit_seq"] == "1"

        present = _media_params(
            media_hash=media_hash,
            state="present",
            observed_at=now + timedelta(seconds=1),
            session_id=session_id,
        )
        completed = await client.call("storage.media.commit.v2", present)
        assert completed["commit_seq"] == "2"
        assert completed["media"]["state"] == "present"
        assert len(completed["refs"]) == 1

        moved_path = {**present, "object_path": f"compacted/{media_hash}.bin"}
        moved_replay = await client.call("storage.media.commit.v2", moved_path)
        assert moved_replay["exact_replay"] is True
        assert moved_replay["media"]["object_path"] == present["object_path"]

        conflicting_size = {**present, "byte_size": 124}
        with pytest.raises(CatalogRemoteError) as conflict:
            await client.call("storage.media.commit.v2", conflicting_size)
        assert conflict.value.code == "conflict"

        corrupt_hash = "b" * 64
        deleted_hash = "c" * 64
        await client.call(
            "storage.media.commit.v2",
            _media_params(media_hash=corrupt_hash, state="corrupt", observed_at=now),
        )
        await client.call(
            "storage.media.commit.v2",
            _media_params(media_hash=deleted_hash, state="deleted", observed_at=now),
        )
        existence = await client.call(
            "storage.media.exists.batch.v2",
            {"media_hashes": [media_hash, corrupt_hash, deleted_hash, "d" * 64]},
        )
        assert [row["state"] for row in existence["objects"]] == [
            "present",
            "corrupt",
            "deleted",
            "missing",
        ]
        with pytest.raises(CatalogRemoteError) as resurrection:
            await client.call(
                "storage.media.commit.v2",
                _media_params(media_hash=deleted_hash, state="present", observed_at=now),
            )
        assert resurrection.value.code == "conflict"
    finally:
        await client.close()
        await daemon.close()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        read = await client.call(
            "storage.media.read.v2",
            {"media_hash": media_hash, "session_id": session_id, "limit": 100},
        )
        assert read["found"] is True
        assert read["media"]["state"] == "present"
        assert read["refs"][0]["session_id"] == session_id
        assert (await client.call("ping.v2"))["commit_seq"] == "4"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_media_reference_cannot_resurrect_tombstoned_session(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            SessionTombstone.__table__.insert().values(
                session_id=session_id,
                deletion_revision=17,
                deleted_at=now,
                commit_seq=1,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as deleted:
            await client.call(
                "storage.media.commit.v2",
                _media_params(media_hash="e" * 64, state="missing", observed_at=now, session_id=session_id),
            )
        assert deleted.value.code == "session_deleted"
        assert deleted.value.details["deletion_revision"] == "17"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(MediaObject.__table__.select()).first() is None
    engine.dispose()


@pytest.mark.asyncio
async def test_projector_state_coalesces_claims_completion_failure_and_restart(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    projector = "render-v2"
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        advance = {
            "projector": projector,
            "session_id": session_id,
            "desired_revision": 1,
            "observed_at": now.isoformat(),
        }
        first = await client.call("projector.state.advance.v2", advance)
        replay = await client.call("projector.state.advance.v2", advance)
        assert first["changed"] is True and first["commit_seq"] == "1"
        assert replay["changed"] is False and replay["commit_seq"] == "1"
        await client.call("projector.state.advance.v2", {**advance, "desired_revision": 2})

        claim_token = str(uuid4())
        claim = {
            "projector": projector,
            "worker_id": "worker-a",
            "claim_token": claim_token,
            "now": now.isoformat(),
            "lease_seconds": 60,
            "limit": 10,
        }
        claimed = await client.call("projector.state.claim.v2", claim)
        claimed_replay = await client.call("projector.state.claim.v2", claim)
        assert claimed["claimed"][0]["claimed_revision"] == "2"
        assert claimed_replay["exact_replay"] is True

        await client.call("projector.state.advance.v2", {**advance, "desired_revision": 5})
        completed = await client.call(
            "projector.state.complete.v2",
            {
                "projector": projector,
                "session_id": session_id,
                "claim_token": claim_token,
                "completed_revision": 2,
                "completed_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        assert completed["state"]["desired_revision"] == "5"
        assert completed["state"]["completed_revision"] == "2"
        lag = await client.call(
            "projector.state.list_lag.v2",
            {"projector": projector, "after_session_id": None, "limit": 100},
        )
        assert [(row["desired_revision"], row["completed_revision"]) for row in lag["states"]] == [("5", "2")]

        failure_token = str(uuid4())
        second_claim = await client.call("projector.state.claim.v2", {**claim, "claim_token": failure_token})
        assert second_claim["claimed"][0]["claimed_revision"] == "5"
        retry_at = now + timedelta(minutes=1)
        failure = {
            "projector": projector,
            "session_id": session_id,
            "claim_token": failure_token,
            "error_code": "parser_failed",
            "error_message": "bad input",
            "failed_at": (now + timedelta(seconds=2)).isoformat(),
            "retry_at": retry_at.isoformat(),
        }
        failed = await client.call("projector.state.fail.v2", failure)
        failed_replay = await client.call("projector.state.fail.v2", failure)
        assert failed["state"]["failure_count"] == 1
        assert failed_replay["exact_replay"] is True
        too_early = await client.call(
            "projector.state.claim.v2",
            {**claim, "claim_token": str(uuid4()), "now": (now + timedelta(seconds=30)).isoformat()},
        )
        assert too_early["claimed"] == []

        final_token = str(uuid4())
        final_claim = await client.call(
            "projector.state.claim.v2",
            {**claim, "claim_token": final_token, "now": retry_at.isoformat()},
        )
        assert final_claim["claimed"][0]["claimed_revision"] == "5"
        final = {
            "projector": projector,
            "session_id": session_id,
            "claim_token": final_token,
            "completed_revision": 5,
            "completed_at": (retry_at + timedelta(seconds=1)).isoformat(),
        }
        done = await client.call("projector.state.complete.v2", final)
        done_replay = await client.call("projector.state.complete.v2", final)
        assert done["state"]["completed_revision"] == "5"
        assert done_replay["exact_replay"] is True
        terminal_claim_replay = await client.call(
            "projector.state.claim.v2",
            {**claim, "claim_token": final_token, "now": retry_at.isoformat()},
        )
        assert terminal_claim_replay["exact_replay"] is True
        assert terminal_claim_replay["claimed"] == []
    finally:
        await client.close()
        await daemon.close()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        lag = await client.call(
            "projector.state.list_lag.v2",
            {"projector": projector, "after_session_id": None, "limit": 100},
        )
        assert lag["states"] == []
    finally:
        await client.close()
        await daemon.close()
