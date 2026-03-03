from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.main import api_app
from zerg.models.agents import AgentsBase


def _fixture_path(provider: str) -> Path:
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "apps" / "engine" / "tests" / "fixtures" / "golden" / provider / "basic.jsonl"


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
            role = typ
            content_text = None
            message = obj.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                content_text = content
            elif isinstance(content, list):
                texts = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"]
                if texts:
                    content_text = "\n".join(t for t in texts if t)

            events.append(
                {
                    "role": role,
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

            texts = []
            for item in payload.get("content") or []:
                if isinstance(item, dict) and item.get("text"):
                    texts.append(item["text"])
            content_text = "\n".join(texts) if texts else None

            events.append(
                {
                    "role": role,
                    "content_text": content_text,
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


def _make_client(tmp_path):
    db_path = tmp_path / "ship_unship_e2e.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    api_app.dependency_overrides[get_db] = override
    return TestClient(api_app)


def _assert_ship_unship_roundtrip(client: TestClient, provider: str, *, include_events: bool, session_id: str) -> None:
    fixture = _fixture_path(provider)
    expected = fixture.read_text(encoding="utf-8")
    source_path = str(fixture)
    line_rows = _read_lines_with_offsets(fixture)
    source_lines = [{"source_path": source_path, "source_offset": offset, "raw_json": line} for offset, line in line_rows]
    events = _extract_fixture_events(provider, line_rows, source_path) if include_events else []

    payload = {
        "id": session_id,
        "provider": provider,
        "environment": "test",
        "project": "ship-unship-e2e",
        "device_id": "test-device",
        "cwd": "/tmp/ship-unship-e2e",
        "started_at": _pick_started_at(line_rows),
        "provider_session_id": f"{provider}-fixture",
        "events": events,
        "source_lines": source_lines,
    }

    ingest = client.post(
        "/agents/ingest",
        json=payload,
        headers={"X-Agents-Token": "dev"},
    )
    assert ingest.status_code == 200, ingest.text

    exported = client.get(
        f"/agents/sessions/{session_id}/export",
        headers={"X-Agents-Token": "dev"},
    )
    assert exported.status_code == 200, exported.text
    assert exported.content.decode("utf-8") == expected


@pytest.mark.parametrize(
    ("provider", "session_id"),
    [
        ("claude", "8cb35416-b2fd-4da5-98ca-9db9c5de8ca1"),
        ("codex", "9de55f12-f7e4-4d4c-b7ef-d8fd7f3777cb"),
    ],
)
def test_ship_unship_roundtrip_api_claude_and_codex(tmp_path, provider: str, session_id: str):
    """Ship fixture logs through API ingest and unship via export with exact byte match."""
    client = _make_client(tmp_path)
    try:
        _assert_ship_unship_roundtrip(client, provider, include_events=True, session_id=session_id)
    finally:
        api_app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("provider", "session_id"),
    [
        ("claude", "caad0bb2-29a4-4e49-899f-8326ea16c1d3"),
        ("codex", "e6e9ecaf-6f48-49e4-bd14-87c2c72a9d42"),
    ],
)
def test_ship_unship_roundtrip_api_source_lines_only_for_schema_drift(tmp_path, provider: str, session_id: str):
    """Even with zero parsed events, source_lines-only ingest must export exact logs."""
    client = _make_client(tmp_path)
    try:
        _assert_ship_unship_roundtrip(client, provider, include_events=False, session_id=session_id)
    finally:
        api_app.dependency_overrides.clear()
