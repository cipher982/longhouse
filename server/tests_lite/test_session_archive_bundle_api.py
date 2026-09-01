"""Archive bundle and archive manifest, read back from a real live catalog.

These used to ship their transcripts through v1 ``/agents/ingest``, which parsed
provider JSONL into archive rows and reassembled it on export. A Runtime Host
accepts transcript ingest only through storage-v2, where the raw object holds
the shipped bytes verbatim, so the fixtures below are shipped as real envelopes
and read back through the routes Life Hub actually calls.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401
from zerg.dependencies.agents_auth import verify_agents_caller  # noqa: E402
from zerg.main import api_app  # noqa: E402

DEVICE_ID = "cinder"


def _golden_transcript() -> str:
    """A real shipped transcript: the engine's codex golden fixture."""
    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / "engine" / "tests" / "fixtures" / "golden" / "codex" / "basic.jsonl").read_text(encoding="utf-8")


def _owner_headers(live_catalog) -> tuple[int, dict[str, str]]:
    owner_id = live_catalog.create_user("owner@archive-bundle.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)
    return owner_id, {"X-Agents-Token": token}


def _ship_inline_session(live_catalog, client, headers, *, now, environment: str = "production"):
    """Ship one small transcript, with the session facts the Machine Agent sends."""
    session_id = uuid4()
    body = live_catalog.envelope_body(
        session_id=session_id,
        device_id=DEVICE_ID,
        texts=("hello",),
        project="archive-manifest",
        now=now,
    )
    body["session"]["environment"] = environment
    response = client.post(
        "/agents/storage/v2/envelopes",
        json=body,
        headers={**headers, "X-Longhouse-Storage-Lane": "live"},
    )
    assert response.status_code == 200, response.text
    return session_id


def _decode_archive_payload(encoded: str) -> bytes:
    return gzip.decompress(base64.b64decode(encoded.encode("ascii")))


def test_archive_bundle_payload_matches_export_jsonl(live_catalog, live_catalog_client):
    """The bundle carries the shipped bytes, byte for byte, and so does /export."""
    owner_id, headers = _owner_headers(live_catalog)
    expected = _golden_transcript()
    seeded = live_catalog.commit_session(
        owner_id=owner_id,
        texts=tuple(expected.splitlines()),
        project="archive-bundle",
    )
    session_id = seeded.session_id

    export_response = live_catalog_client.get(f"/agents/sessions/{session_id}/export", headers=headers)
    assert export_response.status_code == 200, export_response.text

    bundle_response = live_catalog_client.get(f"/agents/sessions/{session_id}/archive-bundle", headers=headers)
    assert bundle_response.status_code == 200, bundle_response.text

    bundle = bundle_response.json()
    decoded_payload = _decode_archive_payload(bundle["archive"]["jsonl_b64_gzip"]).decode("utf-8")

    assert bundle["bundle_version"] == 1
    assert bundle["session"]["id"] == str(session_id)
    assert bundle["session"]["provider"] == "codex"
    assert bundle["session"]["transcript_revision"] >= 1
    assert bundle["archive"]["format"] == "jsonl"
    assert bundle["archive"]["branch_mode"] == "head"
    assert bundle["archive"]["bytes"] == len(export_response.content)
    assert bundle["archive"]["sha256"] == hashlib.sha256(export_response.content).hexdigest()
    assert decoded_payload == export_response.content.decode("utf-8") == expected


def test_archive_bundle_is_stable_across_repeated_reads(live_catalog, live_catalog_client):
    owner_id, headers = _owner_headers(live_catalog)
    seeded = live_catalog.commit_session(
        owner_id=owner_id,
        texts=tuple(_golden_transcript().splitlines()),
        project="archive-bundle",
    )

    first = live_catalog_client.get(f"/agents/sessions/{seeded.session_id}/archive-bundle", headers=headers)
    second = live_catalog_client.get(f"/agents/sessions/{seeded.session_id}/archive-bundle", headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    first_bundle = first.json()
    second_bundle = second.json()
    assert first_bundle["archive"] == second_bundle["archive"]
    assert first_bundle["session"] == second_bundle["session"]


def test_archive_bundle_route_requires_agents_token_dependency():
    route = next(
        candidate
        for candidate in api_app.routes
        if str(getattr(candidate, "path", "") or "").endswith("/agents/sessions/{session_id}/archive-bundle")
    )
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
    assert verify_agents_caller in dependency_calls


def test_archive_bundle_rejects_non_head_branch_mode(live_catalog, live_catalog_client):
    owner_id, headers = _owner_headers(live_catalog)
    seeded = live_catalog.commit_session(owner_id=owner_id, project="archive-bundle")

    response = live_catalog_client.get(
        f"/agents/sessions/{seeded.session_id}/archive-bundle",
        params={"branch_mode": "all"},
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert "branch_mode" in response.text


def test_archive_bundle_missing_raw_returns_service_unavailable(live_catalog, live_catalog_client, monkeypatch):
    """Unverifiable raw bytes are an outage, not a 404 that reads as "no session"."""
    from zerg.services.archive_transcript import ArchiveTranscriptUnavailable

    _owner_id, headers = _owner_headers(live_catalog)

    def raise_missing_raw(**_kwargs):
        raise ArchiveTranscriptUnavailable("synthetic missing raw")

    monkeypatch.setattr("zerg.routers.agents_sessions.build_storage_v2_archive_bundle", raise_missing_raw)

    response = live_catalog_client.get(f"/agents/sessions/{uuid4()}/archive-bundle", headers=headers)

    assert response.status_code == 503, response.text
    assert "Transcript raw bytes unavailable" in response.text


def test_archive_manifest_lists_sessions_beyond_90_days(live_catalog, live_catalog_client):
    _owner_id, headers = _owner_headers(live_catalog)
    now = datetime.now(UTC).replace(microsecond=0)
    older_session_id = _ship_inline_session(live_catalog, live_catalog_client, headers, now=now - timedelta(days=300))
    recent_session_id = _ship_inline_session(live_catalog, live_catalog_client, headers, now=now - timedelta(days=1))

    response = live_catalog_client.get(
        "/agents/sessions/archive-manifest",
        params={"days_back": 3650, "limit": 10, "offset": 0},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    payload = response.json()
    assert payload["total"] == 2
    assert [item["id"] for item in payload["sessions"]] == [str(recent_session_id), str(older_session_id)]
    assert payload["sessions"][1]["transcript_revision"] >= 1


def test_archive_manifest_excludes_test_sessions_by_default(live_catalog, live_catalog_client):
    _owner_id, headers = _owner_headers(live_catalog)
    now = datetime.now(UTC).replace(microsecond=0)
    prod_session_id = _ship_inline_session(live_catalog, live_catalog_client, headers, now=now - timedelta(hours=2))
    test_session_id = _ship_inline_session(
        live_catalog,
        live_catalog_client,
        headers,
        now=now - timedelta(hours=1),
        environment="test",
    )

    default_response = live_catalog_client.get(
        "/agents/sessions/archive-manifest",
        params={"days_back": 3650, "limit": 10, "offset": 0},
        headers=headers,
    )
    assert default_response.status_code == 200, default_response.text
    assert [item["id"] for item in default_response.json()["sessions"]] == [str(prod_session_id)]

    explicit_response = live_catalog_client.get(
        "/agents/sessions/archive-manifest",
        params={"days_back": 3650, "limit": 10, "offset": 0, "include_test": "true"},
        headers=headers,
    )
    assert explicit_response.status_code == 200, explicit_response.text
    assert [item["id"] for item in explicit_response.json()["sessions"]] == [
        str(test_session_id),
        str(prod_session_id),
    ]


def test_archive_manifest_route_requires_agents_token_dependency():
    route = next(
        candidate for candidate in api_app.routes if str(getattr(candidate, "path", "") or "").endswith("/agents/sessions/archive-manifest")
    )
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
    assert verify_agents_caller in dependency_calls
