"""Per-object replication surface: inventory, fetch, and off-loop encoding.

The archive bundle ships a whole session re-encoded as one JSON artifact. These
routes ship the same immutable objects the storage layer already holds, one at a
time, so a replica never needs the server to assemble or re-encode a session.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import zstandard

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401
from zerg.dependencies.agents_auth import verify_agents_caller  # noqa: E402
from zerg.main import api_app  # noqa: E402
from zerg.storage_v2.raw_objects import decode_raw_object  # noqa: E402

DEVICE_ID = "cinder"


def _golden_transcript() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / "engine" / "tests" / "fixtures" / "golden" / "codex" / "basic.jsonl").read_text(encoding="utf-8")


def _owner_headers(live_catalog) -> tuple[int, dict[str, str]]:
    owner_id = live_catalog.create_user("owner@archive-objects.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)
    return owner_id, {"X-Agents-Token": token}


def _seeded_session(live_catalog):
    owner_id, headers = _owner_headers(live_catalog)
    seeded = live_catalog.commit_session(
        owner_id=owner_id,
        texts=tuple(_golden_transcript().splitlines()),
        project="archive-objects",
    )
    return seeded.session_id, headers


def _reconstruct(session_id, objects: list[dict[str, object]], bodies: dict[str, bytes]) -> bytes:
    """Reassemble JSONL from stored objects exactly as the export route does."""

    payload = bytearray()
    for item in objects:
        spec, _envelope = decode_raw_object(zstandard.ZstdDecompressor().decompress(bodies[str(item["envelope_id"])]))
        assert str(spec.session_id) == str(session_id)
        for record in spec.records:
            payload.extend(record.data)
            if not record.data.endswith(b"\n"):
                payload.extend(b"\n")
    return bytes(payload)


def test_object_manifest_lists_metadata_and_no_payload(live_catalog, live_catalog_client):
    session_id, headers = _seeded_session(live_catalog)

    response = live_catalog_client.get(f"/agents/sessions/{session_id}/objects/manifest", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["v"] == 1
    assert payload["session_id"] == str(session_id)
    assert payload["found"] is True
    assert payload["deleted"] is False
    assert payload["deletion_revision"] is None
    assert payload["objects"], "a shipped session must expose its inventory"
    assert payload["has_more"] is False
    assert payload["next_cursor"] is None
    assert int(payload["transcript_revision"]) >= 1

    for item in payload["objects"]:
        assert len(item["envelope_id"]) == 64
        assert len(item["object_hash"]) == 64
        assert item["uncompressed_size"] > 0
        assert item["compressed_size"] > 0
        assert item["record_count"] > 0
        assert "object_path" not in item


def test_object_fetch_bytes_rebuild_the_export_stream(live_catalog, live_catalog_client):
    """The inventory plus the fetch route is enough to reproduce /export."""

    session_id, headers = _seeded_session(live_catalog)

    manifest = live_catalog_client.get(f"/agents/sessions/{session_id}/objects/manifest", headers=headers).json()
    export = live_catalog_client.get(f"/agents/sessions/{session_id}/export", headers=headers)
    assert export.status_code == 200, export.text

    bodies: dict[str, bytes] = {}
    for item in manifest["objects"]:
        envelope_id = item["envelope_id"]
        fetched = live_catalog_client.get(f"/agents/sessions/{session_id}/objects/{envelope_id}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        assert fetched.headers["content-type"] == "application/zstd"
        assert fetched.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert int(fetched.headers["content-length"]) == item["compressed_size"]
        # The stored bytes are content-addressed: the client can verify them
        # without trusting the transport.
        assert hashlib.sha256(fetched.content).hexdigest() == item["object_hash"]
        bodies[envelope_id] = fetched.content

    assert _reconstruct(session_id, manifest["objects"], bodies) == export.content


def test_object_manifest_cursor_pages_without_repeating_or_dropping(live_catalog, live_catalog_client):
    session_id, headers = _seeded_session(live_catalog)

    full = live_catalog_client.get(f"/agents/sessions/{session_id}/objects/manifest", headers=headers).json()

    collected: list[str] = []
    page_cursor = None
    for _ in range(50):
        params = {"limit": 1}
        if page_cursor is not None:
            params["cursor"] = page_cursor
        page = live_catalog_client.get(
            f"/agents/sessions/{session_id}/objects/manifest",
            params=params,
            headers=headers,
        )
        assert page.status_code == 200, page.text
        body = page.json()
        collected.extend(item["envelope_id"] for item in body["objects"])
        page_cursor = body["next_cursor"]
        if not body["has_more"]:
            break

    assert collected == [item["envelope_id"] for item in full["objects"]]


def test_object_fetch_rejects_unknown_and_malformed_ids(live_catalog, live_catalog_client):
    session_id, headers = _seeded_session(live_catalog)

    unknown = live_catalog_client.get(f"/agents/sessions/{session_id}/objects/{'0' * 64}", headers=headers)
    assert unknown.status_code == 404, unknown.text

    malformed = live_catalog_client.get(f"/agents/sessions/{session_id}/objects/not-a-hash", headers=headers)
    assert malformed.status_code == 422, malformed.text


def test_object_routes_require_agents_token_dependency():
    for suffix in ("/agents/sessions/{session_id}/objects/manifest", "/agents/sessions/{session_id}/objects/{envelope_id}"):
        route = next(candidate for candidate in api_app.routes if str(getattr(candidate, "path", "") or "").endswith(suffix))
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert verify_agents_caller in dependency_calls


def test_archive_bundle_encoding_never_runs_on_the_event_loop(live_catalog, live_catalog_client, monkeypatch):
    """The encode is proportional to session size and must stay off the loop.

    Building a bundle on the event loop thread is what stopped every other route
    on the host for 26 seconds; this pins the property, not the implementation.
    """

    from zerg.services import session_archive

    seen: list[str] = []
    original = session_archive._encode_jsonl_payload

    def record_thread(jsonl_bytes):
        seen.append(threading.current_thread().name)
        return original(jsonl_bytes)

    monkeypatch.setattr(session_archive, "_encode_jsonl_payload", record_thread)

    session_id, headers = _seeded_session(live_catalog)
    response = live_catalog_client.get(f"/agents/sessions/{session_id}/archive-bundle", headers=headers)

    assert response.status_code == 200, response.text
    assert seen, "the bundle path must encode its payload"
    assert threading.main_thread().name not in seen
