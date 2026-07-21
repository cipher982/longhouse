from __future__ import annotations

import base64
import gzip
import hashlib
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from zerg.services.session_archive import build_storage_v2_archive_bundle
from zerg.services.session_archive import build_storage_v2_archive_manifest


class _Catalog:
    def __init__(self, session_id):
        self.session_id = session_id

    async def call(self, method, params):
        if method == "storage.session.read.v2":
            return {
                "session": {
                    "session_id": str(self.session_id),
                    "owner_id": "1",
                    "provider": "claude",
                    "machine_id": "cinder",
                    "project": "longhouse",
                    "cwd": "/repo",
                    "git_repo": None,
                    "git_branch": "main",
                    "started_at": "2026-07-13T00:00:00Z",
                    "ended_at": None,
                    "last_activity_at": "2026-07-13T00:01:00Z",
                    "summary_title": "Archive test",
                    "transcript_revision": "2",
                }
            }
        if method == "session.read.v2":
            return {
                "observed_at": "2026-07-13T00:02:00Z",
                "facts": {
                    "catalog": {"summary": "summary", "summary_revision": 3, "device_name": "Cinder"},
                    "connections": [{"control_plane": "claude_channel_bridge"}],
                    "provider_alias": "provider-session",
                },
            }
        if method == "storage.session.raw_manifest.v2":
            assert params["after_source_key"] is None
            return {
                "objects": [
                    {
                        "machine_id": "cinder",
                        "provider": "claude",
                        "opaque_source_id": "source",
                        "source_epoch": str(uuid4()),
                        "range_start": 0,
                        "envelope_id": "a" * 64,
                        "object_path": "raw/v2/object.zst",
                        "object_hash": "b" * 64,
                        "tenant_id": "tenant-archive",
                    }
                ],
                "objects_truncated": False,
            }
        raise AssertionError(method)


class _Workers:
    def __init__(self, session_id):
        self.session_id = session_id

    async def read(self, object_path, object_hash, tenant_id):
        assert object_path == "raw/v2/object.zst"
        assert tenant_id == "tenant-archive"
        assert object_hash == "b" * 64
        records = (SimpleNamespace(data=b'{"one":1}'), SimpleNamespace(data=b'{"two":2}\n'))
        return SimpleNamespace(
            envelope_id="a" * 64,
            spec=SimpleNamespace(session_id=self.session_id, records=records),
        )


@pytest.mark.asyncio
async def test_storage_v2_archive_bundle_reads_catalog_and_exact_raw_objects(monkeypatch):
    session_id = uuid4()
    projected = SimpleNamespace(
        thread_root_session_id=str(session_id),
        continued_from_session_id=None,
        continuation_kind=None,
        origin_label="On this Mac",
        session_state=SimpleNamespace(mode="helm"),
        is_sidechain=False,
    )
    monkeypatch.setattr("zerg.services.session_archive.get_catalogd_client", lambda: _Catalog(session_id))
    monkeypatch.setattr("zerg.services.session_archive.get_raw_object_worker_pool", lambda: _Workers(session_id))
    monkeypatch.setattr("zerg.services.session_archive.project_catalog_session_facts", lambda facts, observed_at: projected)

    bundle = await build_storage_v2_archive_bundle(session_id=session_id, owner_id=1)

    assert bundle is not None
    raw = gzip.decompress(base64.b64decode(bundle.archive.jsonl_b64_gzip))
    assert raw == b'{"one":1}\n{"two":2}\n'
    assert bundle.archive.sha256 == hashlib.sha256(raw).hexdigest()
    assert bundle.session.transcript_revision == 2
    assert bundle.session.provider_session_id == "provider-session"
    assert bundle.session.managed_transport == "claude_channel_bridge"


def test_storage_v2_archive_manifest_preserves_revision_and_sidechain(monkeypatch):
    session_id = str(uuid4())
    projected = SimpleNamespace(
        id=session_id,
        started_at=datetime(2026, 7, 13, tzinfo=UTC),
        last_activity_at=datetime(2026, 7, 13, 0, 1, tzinfo=UTC),
        provider="codex",
        project="longhouse",
        is_sidechain=True,
    )
    monkeypatch.setattr("zerg.services.session_archive.project_catalog_session_facts", lambda facts, observed_at: projected)
    snapshot = {
        "observed_at": "2026-07-13T00:02:00Z",
        "total": 1,
        "rows": [{"facts": {"catalog": {"transcript_revision": 42}}}],
    }

    manifest = build_storage_v2_archive_manifest(snapshot)

    assert manifest.total == 1
    assert manifest.sessions[0].id == session_id
    assert manifest.sessions[0].transcript_revision == 42
    assert manifest.sessions[0].is_sidechain is True
