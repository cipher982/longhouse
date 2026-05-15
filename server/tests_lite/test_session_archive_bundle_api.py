from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app


def _fixture_path(provider: str) -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "engine" / "tests" / "fixtures" / "golden" / provider / "basic.jsonl"


def _read_lines_with_offsets(path: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    with path.open("rb") as fh:
        offset = 0
        for raw in fh:
            rows.append((offset, raw.rstrip(b"\r\n").decode("utf-8")))
            offset += len(raw)
    return rows


def _extract_fixture_events(provider: str, line_rows: list[tuple[int, str]], source_path: str) -> list[dict]:
    events: list[dict] = []
    for offset, line in line_rows:
        obj = json.loads(line)
        ts = obj.get("timestamp") or "2026-03-03T00:00:00Z"

        if provider == "claude":
            typ = obj.get("type")
            if typ not in {"user", "assistant"}:
                continue
            message = obj.get("message") or {}
            content = message.get("content")
            content_text = None
            if isinstance(content, str):
                content_text = content
            elif isinstance(content, list):
                texts = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"]
                if texts:
                    content_text = "\n".join(t for t in texts if t)

            events.append(
                {
                    "role": typ,
                    "content_text": content_text,
                    "timestamp": ts,
                    "source_path": source_path,
                    "source_offset": offset,
                    "raw_json": line,
                }
            )
            continue

        if provider == "codex":
            if obj.get("type") != "response_item":
                continue
            payload = obj.get("payload") or {}
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            texts = [item.get("text") for item in payload.get("content") or [] if isinstance(item, dict) and item.get("text")]
            events.append(
                {
                    "role": role,
                    "content_text": "\n".join(texts) if texts else None,
                    "timestamp": ts,
                    "source_path": source_path,
                    "source_offset": offset,
                    "raw_json": line,
                }
            )
    return events


def _pick_started_at(line_rows: list[tuple[int, str]]) -> str:
    for _offset, line in line_rows:
        try:
            ts = json.loads(line).get("timestamp")
        except json.JSONDecodeError:
            ts = None
        if isinstance(ts, str) and ts.strip():
            return ts
    return "2026-03-03T00:00:00Z"


def _make_client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "archive_bundle.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="archive-bundle", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app)


def _ingest_fixture_session(client: TestClient, provider: str, session_id: str) -> str:
    fixture = _fixture_path(provider)
    source_path = str(fixture)
    line_rows = _read_lines_with_offsets(fixture)
    payload = {
        "id": session_id,
        "provider": provider,
        "environment": "test",
        "project": "archive-bundle",
        "device_id": "test-device",
        "cwd": "/tmp/archive-bundle",
        "started_at": _pick_started_at(line_rows),
        "provider_session_id": f"{provider}-fixture",
        "events": _extract_fixture_events(provider, line_rows, source_path),
        "source_lines": [
            {"source_path": source_path, "source_offset": offset, "raw_json": line}
            for offset, line in line_rows
        ],
    }
    ingest = client.post("/agents/ingest", json=payload, headers={"X-Agents-Token": "dev"})
    assert ingest.status_code == 200, ingest.text
    return fixture.read_text(encoding="utf-8")


def _ingest_inline_session(
    client: TestClient,
    *,
    session_id: str,
    started_at: str,
    provider: str = "claude",
    environment: str = "production",
    is_sidechain: bool = False,
) -> None:
    source_path = f"/tmp/{session_id}.jsonl"
    payload = {
        "id": session_id,
        "provider": provider,
        "environment": environment,
        "project": "archive-manifest",
        "device_id": "test-device",
        "cwd": "/tmp/archive-manifest",
        "started_at": started_at,
        "provider_session_id": f"{provider}-{session_id}",
        "events": [
            {
                "role": "user",
                "content_text": "hello",
                "timestamp": started_at,
                "source_path": source_path,
                "source_offset": 0,
                "raw_json": json.dumps({"type": "user", "timestamp": started_at, "text": "hello"}),
            }
        ],
        "source_lines": [
            {
                "source_path": source_path,
                "source_offset": 0,
                "raw_json": json.dumps({"type": "user", "timestamp": started_at, "text": "hello"}),
            }
        ],
        "is_sidechain": is_sidechain,
    }
    ingest = client.post("/agents/ingest", json=payload, headers={"X-Agents-Token": "dev"})
    assert ingest.status_code == 200, ingest.text


def _decode_archive_payload(encoded: str) -> bytes:
    return gzip.decompress(base64.b64decode(encoded.encode("ascii")))


@pytest.mark.parametrize(
    ("provider", "session_id"),
    [
        ("claude", "818a0c1f-fd54-4f02-b4f6-f35c5df3a8a0"),
        ("codex", "a6d42131-c2bb-4a59-89d3-d64a7070b21b"),
    ],
)
def test_archive_bundle_payload_matches_export_jsonl(tmp_path, provider: str, session_id: str):
    client = _make_client(tmp_path)
    try:
        expected = _ingest_fixture_session(client, provider, session_id)

        export_response = client.get(f"/agents/sessions/{session_id}/export", headers={"X-Agents-Token": "dev"})
        assert export_response.status_code == 200, export_response.text

        bundle_response = client.get(f"/agents/sessions/{session_id}/archive-bundle", headers={"X-Agents-Token": "dev"})
        assert bundle_response.status_code == 200, bundle_response.text

        bundle = bundle_response.json()
        decoded_payload = _decode_archive_payload(bundle["archive"]["jsonl_b64_gzip"]).decode("utf-8")

        assert bundle["bundle_version"] == 1
        assert bundle["session"]["id"] == session_id
        assert bundle["session"]["provider"] == provider
        assert bundle["session"]["transcript_revision"] == 1
        assert bundle["archive"]["format"] == "jsonl"
        assert bundle["archive"]["branch_mode"] == "head"
        assert bundle["archive"]["bytes"] == len(export_response.content)
        assert bundle["archive"]["sha256"] == hashlib.sha256(export_response.content).hexdigest()
        assert decoded_payload == export_response.content.decode("utf-8") == expected
    finally:
        api_app.dependency_overrides.clear()


def test_archive_bundle_is_stable_across_repeated_reads(tmp_path):
    client = _make_client(tmp_path)
    try:
        session_id = "095c2c93-8e0f-4ce5-bcb9-a2e80c4f2d95"
        _ingest_fixture_session(client, "claude", session_id)

        first = client.get(f"/agents/sessions/{session_id}/archive-bundle", headers={"X-Agents-Token": "dev"})
        second = client.get(f"/agents/sessions/{session_id}/archive-bundle", headers={"X-Agents-Token": "dev"})
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

        first_bundle = first.json()
        second_bundle = second.json()
        assert first_bundle["archive"] == second_bundle["archive"]
        assert first_bundle["session"] == second_bundle["session"]
    finally:
        api_app.dependency_overrides.clear()


def test_archive_bundle_route_requires_agents_token_dependency():
    route = next(
        candidate
        for candidate in api_app.routes
        if str(getattr(candidate, "path", "") or "").endswith("/agents/sessions/{session_id}/archive-bundle")
    )
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
    assert verify_agents_token in dependency_calls


def test_archive_bundle_rejects_non_head_branch_mode(tmp_path):
    client = _make_client(tmp_path)
    try:
        session_id = "d3977f80-65f9-4f33-9912-653b7923157f"
        _ingest_fixture_session(client, "claude", session_id)

        response = client.get(
            f"/agents/sessions/{session_id}/archive-bundle",
            params={"branch_mode": "all"},
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 400
        assert "branch_mode" in response.text
    finally:
        api_app.dependency_overrides.clear()


def test_archive_manifest_lists_sessions_beyond_90_days(tmp_path):
    client = _make_client(tmp_path)
    try:
        older_session_id = "10000000-0000-4000-8000-000000000001"
        recent_session_id = "10000000-0000-4000-8000-000000000002"
        _ingest_inline_session(client, session_id=older_session_id, started_at="2025-08-01T12:00:00Z")
        _ingest_inline_session(client, session_id=recent_session_id, started_at="2026-04-20T12:00:00Z")

        response = client.get(
            "/agents/sessions/archive-manifest",
            params={"days_back": 3650, "limit": 10, "offset": 0},
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 200, response.text

        payload = response.json()
        assert payload["total"] == 2
        assert [item["id"] for item in payload["sessions"]] == [recent_session_id, older_session_id]
        assert payload["sessions"][1]["transcript_revision"] == 1
    finally:
        api_app.dependency_overrides.clear()


def test_archive_manifest_excludes_test_sessions_by_default(tmp_path):
    client = _make_client(tmp_path)
    try:
        prod_session_id = "20000000-0000-4000-8000-000000000001"
        test_session_id = "20000000-0000-4000-8000-000000000002"
        _ingest_inline_session(client, session_id=prod_session_id, started_at="2026-04-20T12:00:00Z", environment="production")
        _ingest_inline_session(client, session_id=test_session_id, started_at="2026-04-20T13:00:00Z", environment="test")

        default_response = client.get(
            "/agents/sessions/archive-manifest",
            params={"days_back": 3650, "limit": 10, "offset": 0},
            headers={"X-Agents-Token": "dev"},
        )
        assert default_response.status_code == 200, default_response.text
        assert [item["id"] for item in default_response.json()["sessions"]] == [prod_session_id]

        explicit_response = client.get(
            "/agents/sessions/archive-manifest",
            params={"days_back": 3650, "limit": 10, "offset": 0, "include_test": "true"},
            headers={"X-Agents-Token": "dev"},
        )
        assert explicit_response.status_code == 200, explicit_response.text
        assert [item["id"] for item in explicit_response.json()["sessions"]] == [test_session_id, prod_session_id]
    finally:
        api_app.dependency_overrides.clear()


def test_archive_manifest_route_requires_agents_token_dependency():
    route = next(
        candidate
        for candidate in api_app.routes
        if str(getattr(candidate, "path", "") or "").endswith("/agents/sessions/archive-manifest")
    )
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
    assert verify_agents_token in dependency_calls
